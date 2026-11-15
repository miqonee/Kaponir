import copy
import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import recover_privacy_transport as recovery


class TransportRecoveryTests(unittest.TestCase):
    def records(self):
        request = {'model': 'test', 'max_tokens': 4096,
                   'messages': [{'role': 'user', 'content': 'synthetic source'}],
                   'seed': 42, 'temperature': 1.0, 'chat_template_kwargs': {'enable_thinking': True}}
        row = {'variant': 'B', 'batch_index': 2, 'request': request,
               'case_ids': ['a', 'b'], 'input_tokens': 123,
               'rendered_prompt_sha256': 'a' * 64}
        plan = {'requests': [row]}
        entry = {'variant': 'B', 'batch_index': 2, 'request': copy.deepcopy(request),
                 'request_sha256': recovery.digest(request), 'case_ids': ['a', 'b'],
                 'status': 'failed', 'error': 'TimeoutError', 'record': {'http_wall_seconds': 1200.1}}
        state = {'calls': ['first'], 'responses': {'first': entry}}
        return state, plan

    def test_only_last_definite_timeout_is_selected_without_parameter_changes(self):
        state, plan = self.records()
        before = copy.deepcopy((state, plan))
        selected = recovery.selected_timeouts(state, plan)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['request'], plan['requests'][0]['request'])
        self.assertEqual(selected[0]['request_sha256'], recovery.digest(selected[0]['request']))
        self.assertEqual((state, plan), before)
        state['calls'].append('later')
        state['responses']['later'] = {**state['responses']['first'], 'status': 'received',
                                       'error': 'invalid_review_evidence_or_shape', 'raw_transport': {}}
        self.assertEqual(recovery.selected_timeouts(state, plan), [])

    def test_completed_bad_type_truncation_or_uncertain_send_are_not_retried(self):
        for status, error, transport in [('received', 'invalid_review_evidence_or_shape', True),
                ('received', 'model_response_truncated', True), ('interrupted', 'TimeoutError', False),
                ('pending', None, False), ('failed', 'ConnectionResetError', False),
                ('failed', 'TimeoutError', True)]:
            with self.subTest(status=status, error=error):
                state, plan = self.records()
                entry = state['responses']['first']
                entry.update(status=status, error=error)
                if transport:
                    entry['raw_transport'] = {}
                self.assertEqual(recovery.selected_timeouts(state, plan), [])

    def test_request_tampering_is_rejected(self):
        state, plan = self.records()
        state['responses']['first']['request']['temperature'] = 0
        with self.assertRaisesRegex(ValueError, 'original_request_hash_mismatch'):
            recovery.selected_timeouts(state, plan)
        state['responses']['first']['request_sha256'] = recovery.digest(state['responses']['first']['request'])
        with self.assertRaisesRegex(ValueError, 'original_request_changed'):
            recovery.selected_timeouts(state, plan)

    def on_disk(self, root):
        state, plan = self.records()
        state.update(status='completed', execution_sessions=[{'phases_seconds': {'runtime_cleanup': 0.1}}])
        files = {'scripts/core.py': b'frozen producer', 'docs/experiments/privacy-policy-v1.txt': b'policy',
                 'tests/docs/expected.json': b'annotations', 'crates/kaponir-core/src/kinds.rs': b'kinds'}
        producer = root / 'producer'
        for name, body in files.items():
            path = producer / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        plan.update(signature={'settings': {'timeout': 1200},
            'code_sha256': {'core.py': recovery.file_sha(producer / 'scripts/core.py')},
            'policy_sha256': recovery.file_sha(producer / 'docs/experiments/privacy-policy-v1.txt'),
            'annotations_sha256': recovery.file_sha(producer / 'tests/docs/expected.json'),
            'kind_source_sha256': recovery.file_sha(producer / 'crates/kaponir-core/src/kinds.rs')},
            plan_sha256='a' * 64, adjacent_runtime_libraries_sha256={},
            native_template_probe={}, control_cases={})
        result = root / 'original/result.json'
        result.parent.mkdir()
        result.write_text(json.dumps(state))
        result.with_name('plan.json').write_text(json.dumps(plan))
        return result, producer, state, plan

    def test_prepare_preserves_sources_and_binds_changed_client_wait_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, producer, _, _ = self.on_disk(root)
            before = {path: path.read_bytes() for path in result.parent.iterdir()}
            with patch.object(recovery, 'load_run', return_value={'rows': [{'protocol_complete': True}]}):
                plan = recovery.make_recovery_plan(result, producer, 3600)
            self.assertEqual(plan['client_timeout_seconds'], 3600)
            self.assertEqual(plan['original_signature']['settings']['timeout'], 1200)
            self.assertFalse(plan['inference_request_parameters_changed'])
            self.assertEqual(plan['original_result_sha256'], recovery.file_sha(result))
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            unhashed = {key: value for key, value in plan.items() if key != 'recovery_plan_sha256'}
            self.assertEqual(recovery.digest(unhashed), plan['recovery_plan_sha256'])

    def test_incomplete_source_missing_cleanup_or_changed_producer_are_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, producer, state, _ = self.on_disk(root)
            with patch.object(recovery, 'load_run', return_value={'rows': [{'protocol_complete': True}]}):
                state['status'] = 'running'
                result.write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, 'original_run_not_complete'):
                    recovery.make_recovery_plan(result, producer, 3600)
                state.update(status='completed', execution_sessions=[])
                result.write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, 'original_cleanup_not_durable'):
                    recovery.make_recovery_plan(result, producer, 3600)
                state['execution_sessions'] = [{'phases_seconds': {'runtime_cleanup': 0.1}}]
                result.write_text(json.dumps(state))
                (producer / 'scripts/core.py').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'original_producer_changed'):
                    recovery.make_recovery_plan(result, producer, 3600)

    def test_prepare_only_never_imports_or_starts_inference_producer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, producer, _, _ = self.on_disk(root)
            args = SimpleNamespace(result=result, producer_root=producer, timeout=3600,
                output=root / 'supplement', prepare_only=True)
            with patch.object(recovery, 'load_run', return_value={'rows': [{'protocol_complete': True}]}), \
                    patch.object(recovery, 'load_producer') as imported, redirect_stdout(StringIO()):
                recovery.run_one(args)
            imported.assert_not_called()
            self.assertTrue((args.output / 'recovery-plan.json').is_file())
            self.assertFalse((args.output / 'recovery-result.json').exists())

    def test_runtime_guard_precedes_producer_import(self):
        args = SimpleNamespace(runtime=Path('/synthetic/llama-server'))
        with patch.object(recovery, 'runtime_processes', return_value=[123]), \
                patch.object(recovery, 'load_producer') as imported:
            with self.assertRaisesRegex(ValueError, 'runtime_busy_no_parallel_inference'):
                recovery.execute_recovery(args, {})
        imported.assert_not_called()

    def test_execution_keeps_exact_body_raw_evidence_and_never_retries_truncation(self):
        import evaluate_privacy_policy as core
        from validation_pipeline import Candidate
        for finish, technical in [('stop', 0), ('length', 1)]:
            with self.subTest(finish=finish), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime, model = root / 'runtime', root / 'test.gguf'
                runtime.write_bytes(b'runtime')
                model.write_bytes(b'model')
                source_result, source_plan = root / 'source-result.json', root / 'source-plan.json'
                source_result.write_text('original result')
                source_plan.write_text('original plan')
                output = root / 'supplement'
                output.mkdir()
                args = SimpleNamespace(runtime=runtime, model_dir=root, output=output)
                state, original = self.records()
                selected = recovery.selected_timeouts(state, original)[0]
                selected.update(variant='D', case_ids=['a'],
                    rendered_prompt_sha256=__import__('hashlib').sha256(b'rendered').hexdigest())
                plan = {'producer_root': str(root), 'original_signature': {
                    'settings': {'model': 'test', 'timeout': 1200, 'response_format': 'grammar',
                                 'expected_runtime_sha256': recovery.file_sha(runtime), 'reasoning_budget': 2048},
                    'model_artifact': [model.name, model.stat().st_size, recovery.file_sha(model), '']},
                    'client_timeout_seconds': 3600, 'original_runtime_libraries': {}, 'original_control_cases': {},
                    'recovery_plan_sha256': 'p' * 64, 'requests': [selected],
                    'original_result_path': str(source_result), 'original_result_sha256': recovery.file_sha(source_result),
                    'original_plan_path': str(source_plan), 'original_plan_file_sha256': recovery.file_sha(source_plan),
                    'original_template_probe': {mode: {'formatted_sha256': selected['rendered_prompt_sha256'], 'tokens': 123}
                                                for mode in ('off', 'on')}}
                reply = {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps({
                    'decisions': [{'id': 'a', 'verdict': 'keep', 'reason': 'Public synthetic reference.'}]})}}]}
                transport = {'status': 200, 'body_base64': base64.b64encode(json.dumps(reply).encode()).decode()}
                class FakeWorker:
                    def __init__(self, *_):
                        self.stop = threading.Event()
                        self.process, self.ocr_process, self.ocr_base = None, None, 'http://synthetic'
                        self.process_supports_json_schema = True
                    def detect_json_schema(self):
                        return None
                    def count_text_tokens(self, _):
                        return 123
                    def resources(self):
                        return {}
                    def stop_runtime(self):
                        return None
                class FakeSampler:
                    def __init__(self, *_):
                        pass
                    def start(self):
                        return self
                    def stop(self):
                        return {'sample_count': 1}
                client = SimpleNamespace(configure_budget=lambda _: None)
                patches = {'Worker': FakeWorker, 'ProcessSampler': FakeSampler,
                    'ControlledClient': lambda *_: client, 'DOCUMENTS': ['doc'],
                    'load_corpus': lambda: (None, {}, {}, {'doc': 'value'}, None),
                    'prepare_cases': lambda *_: ([Candidate('a', 'doc', 'value', 0, 5)], {}),
                    'prompts_and_kinds': lambda: (None, None, []),
                    'http_json': lambda *_: {'prompt': 'rendered'}}
                with patch.object(recovery, 'runtime_processes', return_value=[]), \
                        patch.object(recovery, 'load_producer', return_value=core), \
                        patch.multiple(core, **patches), patch.object(core, 'request_raw', return_value=transport) as sent:
                    recovery.execute_recovery(args, plan)
                    sent.assert_called_once_with('http://synthetic/v1/chat/completions', selected['request'], timeout=3600)
                    with self.assertRaisesRegex(ValueError, 'recovery_attempt_exists_no_automatic_replay'):
                        recovery.execute_recovery(args, plan)
                    self.assertEqual(sent.call_count, 1)
                result = json.loads((output / 'recovery-result.json').read_text())
                self.assertEqual(result['status'], 'completed')
                self.assertTrue(result['original_files_unchanged'])
                self.assertEqual(result['batches'][0]['metrics_selected_cases_only']['technical_review'], technical)
                saved = next(iter(result['responses'].values()))
                self.assertEqual(saved['request'], selected['request'])
                self.assertEqual(saved['raw_transport'], transport)
                self.assertEqual(saved['request_sha256'], selected['request_sha256'])

    def test_queue_waits_for_terminal_suite_controller_exit_and_idle_runtime(self):
        running = {'status': 'running'}
        completed = {'status': 'completed_with_failures', 'controller': {'pid': 7, 'start_ticks': 80}}
        self.assertFalse(recovery.suite_ready(running, '/runtime'))
        with patch.object(recovery, 'process_identity', return_value={'start_ticks': 80, 'state': 'S'}):
            self.assertFalse(recovery.suite_ready(completed, '/runtime'))
        with patch.object(recovery, 'process_identity', return_value=None), \
                patch.object(recovery, 'runtime_processes', return_value=[9]):
            self.assertFalse(recovery.suite_ready(completed, '/runtime'))
        with patch.object(recovery, 'process_identity', return_value=None), \
                patch.object(recovery, 'runtime_processes', return_value=[]):
            self.assertTrue(recovery.suite_ready(completed, '/runtime'))
        with self.assertRaisesRegex(ValueError, 'original_suite_blocked'):
            recovery.suite_ready({'status': 'blocked'}, '/runtime')


if __name__ == '__main__':
    unittest.main()
