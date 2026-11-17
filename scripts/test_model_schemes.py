import argparse
import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evaluate_model_schemes import MeasuredClient, execute, main, scheme_profile
from experiment_inference import LocalClient, discover, sampling_settings
from kaponir_worker import FILES


class ModelSchemesTests(unittest.TestCase):
    def client(self, directory, mode='common'):
        value = MeasuredClient.__new__(MeasuredClient)
        value.worker = SimpleNamespace(ocr_base='http://synthetic', process=None, ocr_process=None,
            prompt_token_count=Mock(return_value=1), count_text_tokens=Mock(return_value=1), resources=lambda: {})
        value.model = 'lfm2.5-1.2b-instruct'
        value.profile = scheme_profile(value.model, mode)
        value.context_tokens = value.profile['context_tokens']
        value.output_tokens = value.profile['output_tokens']
        value.output, value.history, value.records, value.planning_schema = directory, [], [], {}
        value.output_mode, value.template_probe = 'grammar', {}
        value.sampling = sampling_settings({key: value.profile[key] for key in
            ('temperature', 'top_p', 'top_k', 'min_p', 'seed')})
        return value

    def transport(self, final='{}', finish='stop'):
        body = {'choices': [{'finish_reason': finish, 'message': {'content': final}}]}
        return {'status': 200, 'body_base64': base64.b64encode(json.dumps(body).encode()).decode()}

    def patches(self):
        class Sampler:
            def __init__(self, *_):
                pass
            def start(self):
                return self
            def stop(self):
                return {'sample_count': 1}
        return patch('evaluate_model_schemes.ProcessSampler', Sampler)

    def test_common_normal_request_matches_historical_client_exactly(self):
        old = LocalClient.__new__(LocalClient)
        old.worker = SimpleNamespace(ocr_base='http://synthetic', prompt_token_count=lambda *_: 1,
            cfg={'ocr_timeout_seconds': 300}, resources=lambda: {})
        old.output_mode, old.planning_schema, old.records = 'grammar', {}, []
        old.sampling = sampling_settings()
        reply = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}
        with patch('experiment_inference.http_json', return_value=reply) as old_request:
            old.infer('exact prompt', {'source': 'synthetic source'}, {'type': 'object'}, 1600)
        with tempfile.TemporaryDirectory() as temporary:
            client = self.client(Path(temporary))
            with self.patches(), patch('evaluate_model_schemes.http_json', return_value={'prompt': 'native'}), \
                    patch('evaluate_model_schemes.request_raw', return_value=self.transport()) as sent:
                self.assertEqual(client.infer('exact prompt', {'source': 'synthetic source'}, {'type': 'object'}, 1600), {})
            self.assertEqual(sent.call_args.args[1], old_request.call_args.args[1])
            self.assertEqual(json.dumps(sent.call_args.args[1], sort_keys=True),
                             json.dumps(old_request.call_args.args[1], sort_keys=True))
            self.assertEqual(sent.call_args.args[2], 3600)
            self.assertEqual(client.profile['worker_configuration_timeout_seconds'], 300)

    def test_auto_format_matches_old_native_capability_selection(self):
        for supported, expected in ((True, 'grammar'), (False, 'prompt')):
            with self.subTest(supported=supported), tempfile.TemporaryDirectory() as temporary:
                worker = Mock()
                worker.status.return_value = {'models': {'test': {'downloaded': True}}}
                worker.submit.return_value = {'id': 'job'}
                worker.process_supports_json_schema = supported
                worker.ocr_base = 'http://unused'
                worker.count_text_tokens.return_value = 5
                old = LocalClient(worker, 'test', 4096, 1600, 'auto')
                profile = scheme_profile('lfm2.5-1.2b-instruct', 'common')
                with patch('evaluate_model_schemes.http_json', return_value={'prompt': 'Public reference'}), \
                        patch('evaluate_model_schemes.request_raw') as inference:
                    new = MeasuredClient(worker, 'test', 4096, 1600, 'auto', {}, profile, Path(temporary), [])
                self.assertEqual(new.output_mode, old.output_mode)
                self.assertEqual(new.output_mode, expected)
                inference.assert_not_called()

    def test_prompt_discovery_reserves_the_actual_schema_before_splitting(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = self.client(Path(temporary))
            client.output_mode = 'prompt'
            def tokens(prompt, value, *_):
                return 20 + len(value['source']) + (200 if '"candidates"' in prompt else 0)
            client.worker.prompt_token_count.side_effect = tokens
            fragments = []
            def infer(prompt, value, schema, output):
                self.assertLessEqual(client.input_tokens(prompt, value, schema) + output, 300)
                fragments.append(value['source'])
                client.records.append({})
                return {'candidates': []}
            client.infer = infer
            args = argparse.Namespace(candidate_mode='typed', max_candidates=24, hint_tokens=0,
                strategy='auto', output_tokens=50, reserve_tokens=0, context_tokens=300,
                target_input_tokens=0, max_fragments=64)
            result = discover(client, 'x' * 180, 'extract', ['CUSTOM'], args)
            self.assertFalse(result['errors'])
            self.assertGreater(len(fragments), 1)
            self.assertEqual(''.join(fragments), 'x' * 180)

    def test_exact_pending_request_is_saved_before_send_and_malformed_bytes_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = self.client(Path(temporary))
            transport = {'status': 200, 'body_base64': base64.b64encode(b'{invalid envelope').decode()}
            def respond(url, body, timeout):
                saved = json.loads((client.output / 'transports.json').read_text())['requests'][0]
                self.assertEqual(saved['status'], 'pending')
                self.assertEqual(saved['request'], body)
                self.assertNotIn('raw_transport', saved)
                return transport
            with self.patches(), patch('evaluate_model_schemes.http_json', return_value={'prompt': 'native'}), \
                    patch('evaluate_model_schemes.request_raw', side_effect=respond) as sent:
                with self.assertRaises(json.JSONDecodeError):
                    client.infer('prompt', {'source': 'source'}, {}, 1600)
            self.assertEqual(sent.call_count, 1)
            saved = json.loads((client.output / 'transports.json').read_text())['requests'][0]
            self.assertEqual(saved['raw_transport'], transport)
            self.assertEqual(saved['error'], 'JSONDecodeError')

    def test_timeout_never_retries_or_becomes_a_semantic_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = self.client(Path(temporary), 'native')
            with self.patches(), patch('evaluate_model_schemes.http_json', return_value={'prompt': 'native'}), \
                    patch('evaluate_model_schemes.request_raw', side_effect=TimeoutError) as sent:
                with self.assertRaises(TimeoutError):
                    client.infer('prompt', {'source': 'source'}, {}, 4096)
            self.assertEqual(sent.call_count, 1)
            self.assertEqual(client.history[0]['status'], 'failed')
            self.assertNotIn('raw_transport', client.history[0])

    def test_only_length_truncation_gets_the_declared_larger_native_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = self.client(Path(temporary), 'native')
            with self.patches(), patch('evaluate_model_schemes.http_json', return_value={'prompt': 'native'}), \
                    patch('evaluate_model_schemes.request_raw', side_effect=[self.transport('', 'length'), self.transport()]) as sent:
                self.assertEqual(client.infer('prompt', {'source': 'source'}, {}, 4096), {})
            self.assertEqual([call.args[1]['max_tokens'] for call in sent.call_args_list], [4096, 8192])
            self.assertEqual(len(client.history), 2)
            first, second = [row['request'] for row in client.history]
            self.assertEqual({**first, 'max_tokens': second['max_tokens']}, second)
            self.assertEqual(client.history[0]['parse_error'], 'model_response_truncated')

    def test_completed_invalid_final_and_other_finish_reasons_are_not_retried(self):
        for response in (self.transport('{bad'), self.transport('', 'content_filter'), self.transport('', 'error')):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as temporary:
                client = self.client(Path(temporary), 'native')
                with self.patches(), patch('evaluate_model_schemes.http_json', return_value={'prompt': 'native'}), \
                        patch('evaluate_model_schemes.request_raw', return_value=response) as sent:
                    with self.assertRaises(ValueError):
                        client.infer('prompt', {'source': 'source'}, {}, 4096)
                self.assertEqual(sent.call_count, 1)

    def test_common_does_not_silently_raise_the_1600_token_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = self.client(Path(temporary))
            with self.patches(), patch('evaluate_model_schemes.http_json', return_value={'prompt': 'native'}), \
                    patch('evaluate_model_schemes.request_raw', return_value=self.transport('', 'length')) as sent:
                with self.assertRaisesRegex(ValueError, 'model_response_truncated'):
                    client.infer('prompt', {'source': 'source'}, {}, 1600)
            self.assertEqual(sent.call_count, 1)

    def test_native_tokenizer_uses_exact_template_kwargs(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = self.client(Path(temporary))
            client.profile['template_kwargs'] = {'enable_thinking': True, 'reasoning_effort': 'high'}
            client.input_tokens('prompt', {'source': 'source'}, {})
            self.assertEqual(client.worker.prompt_token_count.call_args.args[2], client.profile['template_kwargs'])

    def test_prepare_is_offline_binds_frozen_pool_and_restores_catalog(self):
        before = dict(FILES)
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            with patch('evaluate_model_schemes.execute') as inference:
                main(['--prepare-only', '--model', 'spark-x2.6-4b', '--profile', 'native', '--output', temporary])
            inference.assert_not_called()
            plan = json.loads((Path(temporary) / 'scheme-plan.json').read_text())
        self.assertEqual(FILES, before)
        self.assertEqual(plan['candidate_count'], 94)
        self.assertEqual(len(plan['methods']), 6)
        self.assertFalse(plan['capabilities_verified_during_preparation'])
        self.assertEqual(plan['profile']['template_kwargs']['reasoning_effort'], 'high')

    def test_busy_runtime_is_rejected_before_loading_weights(self):
        with patch('evaluate_model_schemes.runtime_processes', return_value=[123]):
            with self.assertRaisesRegex(ValueError, 'runtime_busy_no_parallel_inference'):
                execute(argparse.Namespace(runtime=Path('/runtime')), {})


if __name__ == '__main__':
    unittest.main()
