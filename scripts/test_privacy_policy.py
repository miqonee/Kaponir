import argparse
import base64
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS, ROOT
from evaluate_context_controls import REVIEW_PROMPT, digest, prepare_cases
from evaluate_privacy_policy import (execute, file_sha, make_plan, parse_response,
                                     perform_request, performance_summary, observe_runtime_starts,
                                     request_for, request_raw, save_plan)
from experiment_inference import prompts_and_kinds, sampling_settings
from privacy_policy_experiment import (VARIANTS, common_batches, metrics, payload,
    prompts, strict_json, technical_review, transitions, validate)
from validation_pipeline import Candidate, opaque_id


class PrivacyPolicyTests(unittest.TestCase):
    def args(self, output):
        return argparse.Namespace(model='gemma4-e4b-qat', batch_size=8,
            context_tokens=16384, output_tokens=4096, retry_output_tokens=8192,
            reserve_tokens=64, reasoning_budget=2048, response_format='grammar',
            temperature=1, top_p=.95, top_k=64, min_p=0, seed=42, timeout=1200,
            expected_runtime_sha256='a' * 64,
            policy=ROOT / 'docs/experiments/privacy-policy-v1.txt', output=output)

    def case(self, text, value, start=None, name='doc'):
        start = text.index(value) if start is None else start
        return Candidate(opaque_id(name, value), name, value, start, start + len(value))

    def test_strict_json_rejects_duplicate_nested_keys_and_non_json(self):
        for value in ['{"decisions":[{"verdict":"mask","verdict":"keep"}]}',
                      '{"decisions":[],"decisions":[]}', '{"score":NaN}',
                      '```json\n{"decisions":[]}\n```', '{"decisions":[]} trailing']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json(value)

    def test_marking_preserves_document_and_marks_only_selected_unicode_occurrence(self):
        text = '# Документ\n| цена | 10 ₽ | 10 ₽ |\n'
        value = '10 ₽'
        case = self.case(text, value, text.rindex(value))
        marked, contexts = payload('C', [case], {'doc': text})
        item = marked['items'][0]
        self.assertEqual(marked['document'], text)
        self.assertEqual(item['record'], '| цена | 10 ₽ | 10 ₽ |\n')
        self.assertEqual(item['marked_record'],
            f'| цена | 10 ₽ | [[TARGET:{case.id}]]10 ₽[[/TARGET:{case.id}]] |\n')
        self.assertEqual(contexts[case.id]['context'], text)
        self.assertEqual(set(item), {'id', 'value', 'record', 'marked_record'})
        self.assertNotIn('marked_record', payload('B', [case], {'doc': text})[0]['items'][0])
        self.assertEqual(payload('D', [case], {'doc': text})[0], marked)

    def test_wrong_offsets_and_marker_collisions_are_rejected(self):
        text = 'price 10 USD\n'
        case = self.case(text, '10 USD')
        with self.assertRaises(ValueError):
            payload('C', [case], {'doc': text.replace('10 USD', '20 USD')})
        malicious = '[[TARGET:untrusted]] price 10 USD\n'
        with self.assertRaisesRegex(ValueError, 'target_marker_collision'):
            payload('C', [self.case(malicious, '10 USD')], {'doc': malicious})

    def test_all_variants_share_a_plan_when_only_one_needs_smaller_batches(self):
        text = 'a b c d'
        cases = [self.case(text, value) for value in 'abcd']
        planned = common_batches(cases, ['doc'], 4,
            lambda variant, batch: variant != 'C' or len(batch) <= 2)
        self.assertEqual([[case.value for case in batch] for batch in planned], [['a', 'b'], ['c', 'd']])
        with self.assertRaisesRegex(ValueError, 'single_case_context_budget_exceeded'):
            common_batches(cases, ['doc'], 4, lambda variant, batch: variant != 'D')

    def test_d_accepts_privacy_actions_without_type_scope_or_evidence(self):
        case = self.case('private account', 'account')
        reply = {'decisions': [{'id': case.id, 'verdict': 'mask', 'reason': 'Private account.'}]}
        decisions = validate(reply, [case], {}, [], 'D')
        self.assertTrue(decisions[case.id]['valid'])
        expected = {('doc', 'account'): ('USER', 'personal')}
        result = metrics([case], decisions, expected)
        self.assertEqual((result['private_mask'], result['correct_actions']), (1, 1))
        self.assertIsNone(result['typing'])
        self.assertFalse(validate(reply, [case], {}, ['USER'], 'A')[case.id]['valid'])

    def test_duplicate_unknown_missing_ids_and_extra_keys_fail_closed(self):
        cases = [self.case('first second', value) for value in ['first', 'second']]
        good = {'decisions': [{'id': case.id, 'verdict': 'keep', 'reason': 'Public.'}
                              for case in cases]}
        variants = []
        duplicate = copy.deepcopy(good)
        duplicate['decisions'][1]['id'] = cases[0].id
        variants.append(duplicate)
        unknown = copy.deepcopy(good)
        unknown['decisions'][0]['id'] = 'unknown'
        variants.append(unknown)
        variants.append({'decisions': good['decisions'][:1]})
        variants.append({**good, 'unexpected': 'field'})
        for reply in variants:
            with self.subTest(reply=reply):
                result = validate(reply, cases, {}, [], 'D')
                self.assertTrue(all(not row['valid'] and row['review_origin'] == 'technical'
                                    for row in result.values()))
        extra = copy.deepcopy(good)
        extra['decisions'][0]['kind'] = 'USER'
        result = validate(extra, cases, {}, [], 'D')
        self.assertFalse(result[cases[0].id]['valid'])
        self.assertTrue(result[cases[1].id]['valid'])
        for reason in ['', ' ', 'a' * 121]:
            extra = copy.deepcopy(good)
            extra['decisions'][0]['reason'] = reason
            self.assertFalse(validate(extra, cases, {}, [], 'D')[cases[0].id]['valid'])

    def test_scoring_separates_semantic_technical_explicit_and_unattempted(self):
        text = 'private1 private2 public1 public2 private3'
        cases = [self.case(text, value) for value in text.split()]
        expected = {('doc', case.value): ('CUSTOM', 'personal')
                    for case in cases if case.value.startswith('private')}
        decisions = {
            cases[0].id: {'verdict': 'keep', 'valid': True},
            cases[1].id: {'verdict': 'review', 'valid': True, 'review_origin': 'explicit'},
            cases[2].id: {'verdict': 'mask', 'valid': True},
            cases[3].id: technical_review(cases[3], 'timeout')}
        result = metrics(cases, decisions, expected)
        self.assertEqual((result['private_keep'], result['public_mask']), (1, 1))
        self.assertEqual((result['explicit_review'], result['technical_review']), (1, 1))
        self.assertEqual((result['attempted_cases'], result['unattempted_cases']), (4, 1))
        self.assertEqual(result['technical_error_counts'], {'timeout': 1})
        self.assertEqual(result['automated_decisions'], 2)
        rows = transitions(cases, {'A': {'decisions': decisions}, 'B': {'decisions': {
            cases[0].id: {'verdict': 'mask', 'valid': True}}}}, expected)
        self.assertTrue(rows[0]['changes']['A_to_B'])

    def test_frozen_plan_contains_same_cases_and_no_labels_in_model_requests(self):
        _, expected, public, sources, _ = load_corpus()
        sources = {name: sources[name] for name in DOCUMENTS}
        cases, case_data = prepare_cases(ROOT / 'tests/fixtures/context-control-candidates.json',
                                        sources, expected, public, 32)
        instructions = prompts('General trusted policy.\n')
        _, _, kinds = prompts_and_kinds()
        with tempfile.TemporaryDirectory() as temporary:
            args = self.args(Path(temporary))
            batches = common_batches(cases, DOCUMENTS, 8)
            plan = make_plan(args, cases, case_data, sources, instructions, kinds, batches)
            self.assertEqual((len(batches), len(plan['requests'])), (5, 20))
            self.assertEqual(instructions['A'], REVIEW_PROMPT)
            self.assertEqual(plan['token_budget_validation'], 'pending_runtime')
            by_variant = {variant: [row for row in plan['requests'] if row['variant'] == variant]
                          for variant in VARIANTS}
            for rows in by_variant.values():
                self.assertEqual([row['case_ids'] for row in rows], plan['batches'])
                self.assertEqual(sum(len(row['case_ids']) for row in rows), 32)
            for left, right in zip(by_variant['A'], by_variant['B']):
                self.assertEqual(left['input_sha256'], right['input_sha256'])
                self.assertEqual(left['schema_sha256'], right['schema_sha256'])
                self.assertNotEqual(left['prompt_sha256'], right['prompt_sha256'])
            for left, right in zip(by_variant['C'], by_variant['D']):
                self.assertEqual(left['input_sha256'], right['input_sha256'])
                self.assertNotEqual(left['schema_sha256'], right['schema_sha256'])
            forbidden = {'kind', 'scope', 'private', 'reference_private', 'file', 'origin', 'protected'}
            for row in plan['requests']:
                value = strict_json(row['request']['messages'][1]['content'])
                self.assertEqual(set(value), {'document', 'items'})
                for item in value['items']:
                    self.assertFalse(set(item) & forbidden)
                    self.assertRegex(item['id'], r'^[a-f0-9]{24}$')
            destination = Path(temporary) / 'plan.json'
            save_plan(destination, plan)
            changed = copy.deepcopy(plan)
            changed['signature']['settings']['seed'] = 99
            with self.assertRaisesRegex(ValueError, 'prepared_profile_changed'):
                save_plan(destination, changed)

    def test_new_a_preserves_archived_joint_contract_request_identity(self):
        archive = ROOT / 'benchmark/data/validation/context-controls--gemma-card-document--result.json.gz'
        with gzip.open(archive, 'rt') as handle:
            historical = json.load(handle)
        _, expected, public, sources, _ = load_corpus()
        sources = {name: sources[name] for name in DOCUMENTS}
        cases, _ = prepare_cases(ROOT / 'tests/fixtures/context-control-candidates.json',
                                 sources, expected, public, 32)
        _, _, kinds = prompts_and_kinds()
        settings = historical['signature']['settings']
        sampling = sampling_settings({key: settings[key]
            for key in ['temperature', 'top_p', 'top_k', 'min_p', 'seed']})
        for batch in common_batches(cases, DOCUMENTS, 8):
            value = payload('A', batch, sources)[0]
            from privacy_policy_experiment import schema
            key = digest(['review-document-bounded', prompts('policy')['A'], value,
                          schema('A', batch, kinds), True, 4096, 'grammar', sampling])
            self.assertIn(digest([key, 2048]), historical['responses'])

    def test_response_parsing_never_accepts_truncation_or_duplicate_fields(self):
        entry = {'raw_reply': {'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"decisions":[],"decisions":[]}'}}]}}
        self.assertEqual(parse_response(entry), (None, 'duplicate_json_key'))
        entry['raw_reply']['choices'][0].update(finish_reason='length', message={'content': '{"decisions":[]}'})
        self.assertEqual(parse_response(entry), (None, 'model_response_truncated'))

    def test_request_and_full_raw_reply_are_checkpointed_before_scoring(self):
        state = {'responses': {}, 'calls': []}
        snapshots = []
        args = self.args(Path('/tmp/unused'))
        worker = argparse.Namespace(ocr_base='http://fixture', resources=lambda: {}, count_text_tokens=len,
                                    process=argparse.Namespace(poll=lambda: None))
        planned = {'variant': 'D', 'batch_index': 0, 'case_ids': ['opaque'],
                   'request': {'messages': [], 'max_tokens': 4096}}
        raw = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"decisions":[]}', 'reasoning_content': 'Full raw reasoning.'}}]}

        def save():
            snapshots.append(copy.deepcopy(state))

        def request(*_, **__):
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(next(iter(snapshots[0]['responses'].values()))['status'], 'pending')
            return {'status': 200, 'body_base64': base64.b64encode(json.dumps(raw).encode()).decode()}

        with patch('evaluate_privacy_policy.request_raw', side_effect=request):
            entry = perform_request(state, save, worker, args, planned, 4096, 100)
        self.assertIn('raw_transport', next(iter(snapshots[1]['responses'].values())))
        self.assertNotIn('raw_reply', next(iter(snapshots[1]['responses'].values())))
        self.assertEqual(entry['raw_reply']['choices'][0]['message']['reasoning_content'], 'Full raw reasoning.')
        self.assertNotIn('reply', entry)
        self.assertIn('sampled_resources', entry['record'])
        self.assertIn('roles', entry['record']['sampled_resources'])
        self.assertGreaterEqual(entry['record']['http_wall_seconds'], 0)
        self.assertFalse(entry['record']['throughput']['ttft_available'])
        with patch('evaluate_privacy_policy.request_raw') as request:
            self.assertIs(perform_request(state, save, worker, args, planned, 4096, 100), entry)
            request.assert_not_called()

    def test_raw_checkpoint_failure_propagates_and_is_not_a_model_error(self):
        state = {'responses': {}, 'calls': []}
        args = self.args(Path('/tmp/unused'))
        worker = argparse.Namespace(ocr_base='http://fixture')
        planned = {'variant': 'A', 'batch_index': 0, 'case_ids': [], 'request': {}}
        save = Mock(side_effect=[None, OSError('disk full')])
        with patch('evaluate_privacy_policy.request_raw', return_value={'status': 200, 'body_base64': ''}):
            with self.assertRaises(OSError):
                perform_request(state, save, worker, args, planned, 4096, 100)
        entry = next(iter(state['responses'].values()))
        self.assertNotIn('error', entry)
        self.assertIn('raw_transport', entry)

    def test_malformed_transport_is_durable_and_interrupted_request_is_not_replayed(self):
        state = {'responses': {}, 'calls': []}
        args = self.args(Path('/tmp/unused'))
        worker = argparse.Namespace(ocr_base='http://fixture', resources=lambda: {}, process=None)
        planned = {'variant': 'D', 'batch_index': 0, 'case_ids': [], 'request': {}}
        malformed = {'status': 502, 'body_base64': base64.b64encode(b'upstream failed\xff').decode()}
        snapshots = []
        with patch('evaluate_privacy_policy.request_raw', return_value=malformed):
            entry = perform_request(state, lambda: snapshots.append(copy.deepcopy(state)),
                                    worker, args, planned, 4096, 100)
        self.assertEqual(entry['raw_transport'], malformed)
        self.assertEqual(entry['error'], 'invalid_runtime_json')
        self.assertNotIn('error', next(iter(snapshots[1]['responses'].values())))
        state = {'responses': {}, 'calls': []}
        key = digest(['D', 0, {'max_tokens': 4096}])
        state['responses'][key] = {'status': 'pending'}
        with patch('evaluate_privacy_policy.request_raw') as send:
            entry = perform_request(state, lambda: None, worker, args, planned, 4096, 100)
        send.assert_not_called()
        self.assertEqual(entry['status'], 'interrupted')
        self.assertEqual(entry['error'], 'interrupted_before_response_checkpoint')

    def test_transport_retains_http_error_body_bytes(self):
        from io import BytesIO
        from urllib.error import HTTPError
        error = HTTPError('http://fixture', 500, 'server error',
                          {'Content-Type': 'text/plain'}, BytesIO(b'bad JSON\xff'))
        with patch('evaluate_privacy_policy.urllib.request.urlopen', side_effect=error):
            result = request_raw('http://fixture', {'request': 'value'}, 1)
        self.assertEqual(result['status'], 500)
        self.assertEqual(base64.b64decode(result['body_base64']), b'bad JSON\xff')

    def test_resource_aggregation_counts_retries_and_uses_weighted_native_rates(self):
        entries = []
        for index, (seconds, tokens, cpu, peak) in enumerate([
                (2.0, 20, 3.0, 400), (8.0, 40, 7.0, 600)]):
            entries.append({'variant': 'A', 'batch_index': 0, 'case_ids': ['x', 'y'],
                'status': 'received', 'raw_transport': {}, 'record': {
                    'http_wall_seconds': seconds,
                    'throughput': {'native_prediction': {'tokens': tokens, 'seconds': seconds}},
                    'sampled_resources': {'roles': {'model': {
                        'cpu_seconds': cpu, 'observed_peak_rss_bytes': peak}}}}})
        summary = performance_summary({'responses': dict(enumerate(entries))})['A']
        self.assertEqual(summary['recorded_request_attempts'], 2)
        self.assertEqual(summary['retried_batches'], 1)
        self.assertEqual(summary['http_wall_seconds_including_retries'], 10)
        self.assertEqual(summary['amortized_http_seconds_per_case_including_retries'], 5)
        self.assertEqual(summary['native_throughput']['native_prediction']['tokens_per_second'], 6)
        self.assertEqual(summary['model_cpu_seconds_including_retries'], 10)
        self.assertEqual(summary['model_observed_peak_rss_bytes'], 600)
        entries.append({'variant': 'A', 'batch_index': 1, 'status': 'interrupted'})
        summary = performance_summary({'responses': dict(enumerate(entries))})['A']
        self.assertIsNone(summary['http_wall_seconds_including_retries'])
        self.assertEqual(summary['http_wall_seconds_observed_including_retries'], 10)
        self.assertEqual(summary['unknown_attempt_timings'], 1)
        self.assertFalse(summary['model_cpu_measurement_complete'])

    def test_runtime_start_and_reuse_have_separate_readiness_events(self):
        worker = argparse.Namespace(process=None)
        def start(*args, **kwargs):
            worker.process = argparse.Namespace(pid=1234)
        worker.start_runtime = start
        events = []
        observe_runtime_starts(worker, events)
        worker.start_runtime('job', 'model', context_tokens=16384, reasoning_budget=2048)
        worker.start_runtime('job', 'model', context_tokens=16384, reasoning_budget=2048)
        self.assertTrue(events[0]['process_started'])
        self.assertTrue(events[1]['process_reused'])
        self.assertEqual(events[0]['reasoning_budget'], 2048)
        self.assertEqual(events[0]['filesystem_cache_state'], 'uncontrolled')
        self.assertGreaterEqual(events[0]['seconds'], 0)

    def test_request_failure_keeps_elapsed_time_and_sampled_resource_evidence(self):
        state = {'responses': {}, 'calls': []}
        worker = argparse.Namespace(ocr_base='http://fixture', resources=lambda: {}, process=None)
        planned = {'variant': 'D', 'batch_index': 0, 'case_ids': ['x'], 'request': {}}
        with patch('evaluate_privacy_policy.request_raw', side_effect=TimeoutError()):
            entry = perform_request(state, lambda: None, worker, self.args(Path('/tmp/unused')),
                                    planned, 4096, 100)
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(entry['error'], 'TimeoutError')
        self.assertIn('sampled_resources', entry['record'])
        self.assertGreaterEqual(entry['record']['http_wall_seconds'], 0)

    def test_native_preflight_freezes_all_profiles_without_control_generation(self):
        _, expected, public, sources, _ = load_corpus()
        sources = {name: sources[name] for name in DOCUMENTS}
        cases, case_data = prepare_cases(ROOT / 'tests/fixtures/context-control-candidates.json',
                                        sources, expected, public, 32)
        _, _, kinds = prompts_and_kinds()
        instructions = prompts('General privacy policy.')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            model = directory / 'model.gguf'
            runtime = directory / 'llama-server'
            model.write_bytes(b'm')
            runtime.write_bytes(b'runtime')
            args = self.args(directory)
            args.model = 'fixture'
            args.model_dir = directory
            args.runtime = runtime
            args.preflight_only = True
            args.expected_runtime_sha256 = file_sha(runtime)
            artifact = ('model.gguf', 1, file_sha(model), 'https://example.org/model.gguf')
            worker = argparse.Namespace(detect_json_schema=Mock(), process_supports_json_schema=True,
                ocr_base='http://fixture', count_text_tokens=lambda text: len(text) // 4,
                resources=lambda: {}, stop=Mock(), stop_runtime=Mock(), process=None, ocr_process=None)

            def client_factory(worker, _):
                worker.detect_json_schema()
                return argparse.Namespace(
                    configure_budget=lambda _: worker.detect_json_schema(),
                    tokens=lambda prompt, value, contract, thinking:
                        len(prompt) // 50 + len(value['document']) // 50 + len(value['items']) * 10)

            def runtime_query(url, body):
                self.assertTrue(url.endswith('/apply-template'))
                return {'prompt': json.dumps(body, sort_keys=True)}

            with patch('evaluate_privacy_policy.FILES', {'fixture': artifact}), \
                    patch('evaluate_privacy_policy.Worker', return_value=worker), \
                    patch('evaluate_privacy_policy.ControlledClient', side_effect=client_factory), \
                    patch('evaluate_privacy_policy.http_json', side_effect=runtime_query), \
                    patch('evaluate_privacy_policy.request_raw') as inference, redirect_stdout(StringIO()):
                plan = make_plan(args, cases, case_data, sources, instructions, kinds,
                                 common_batches(cases, DOCUMENTS, 8))
                execute(args, plan, cases, sources, expected, instructions, kinds)
            inference.assert_not_called()
            preflight = json.loads((directory / 'preflight.json').read_text())
            self.assertEqual(preflight['measurement_inference_calls'], 0)
            self.assertEqual(preflight['planned_measurement_requests'], 20)
            self.assertEqual(len(preflight['probes']), 2)
            native = json.loads((directory / 'plan.json').read_text())
            self.assertEqual(native['token_budget_validation'], 'verified_runtime')
            self.assertTrue(all('rendered_prompt_sha256' in row and 'input_tokens' in row
                                for row in native['requests']))
            self.assertFalse((directory / 'result.json').exists())
            session = preflight['execution_session']
            self.assertEqual(session['mode'], 'preflight')
            self.assertIn('hardware', session)
            self.assertIn('sampled_resources', session)
            self.assertIn('runtime_cleanup', session['phases_seconds'])
            self.assertGreaterEqual(session['execution_wall_seconds'], 0)
            self.assertEqual(len(list(directory.glob('session-*.json'))), 1)
            worker.stop_runtime.assert_called_once()

            # Exercise the measured path with a mock runtime, including a truncated
            # physical response and its retry. This is test data, not a model run.
            args.preflight_only = False
            attempts = []
            def measured_request(url, request, timeout):
                attempts.append(request)
                items = json.loads(request['messages'][1]['content'])['items']
                contract = request['response_format']['json_schema']['schema']
                fields = contract['properties']['decisions']['items']['properties']
                decisions = []
                for item in items:
                    row = {'id': item['id'], 'verdict': 'review', 'reason': 'Needs review.'}
                    if 'kind' in fields:
                        row.update(kind='CUSTOM', scope='personal', evidence='')
                    decisions.append(row)
                reply = {'choices': [{'finish_reason': 'length' if len(attempts) == 1 else 'stop',
                          'message': {'content': json.dumps({'decisions': decisions})}}],
                         'usage': {'completion_tokens': 2},
                         'timings': {'prompt_n': 10, 'prompt_ms': 100,
                                     'predicted_n': 2, 'predicted_ms': 100}}
                return {'status': 200, 'body_base64': base64.b64encode(json.dumps(reply).encode()).decode()}
            with patch('evaluate_privacy_policy.FILES', {'fixture': artifact}), \
                    patch('evaluate_privacy_policy.Worker', return_value=worker), \
                    patch('evaluate_privacy_policy.ControlledClient', side_effect=client_factory), \
                    patch('evaluate_privacy_policy.http_json', side_effect=runtime_query), \
                    patch('evaluate_privacy_policy.request_raw', side_effect=measured_request), \
                    redirect_stdout(StringIO()):
                execute(args, plan, cases, sources, expected, instructions, kinds)
            result = json.loads((directory / 'result.json').read_text())
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(len(attempts), 21)
            self.assertEqual(result['performance']['A']['retried_batches'], 1)
            self.assertEqual(result['performance']['A']['recorded_request_attempts'], 6)
            self.assertEqual(result['performance']['A']['native_throughput']['native_prediction']['tokens'], 12)
            self.assertEqual(len(result['variant_execution_intervals']), 4)
            self.assertTrue(all(row['active_wall_seconds_including_scoring'] >= 0
                                for row in result['performance'].values()))
            self.assertEqual(result['execution_sessions'][0]['new_request_slots_recorded'], 21)
            self.assertTrue(all(value['explicit_review'] == 32 for value in result['metrics'].values()))


if __name__ == '__main__':
    unittest.main()
