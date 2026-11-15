import base64
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from summarize_privacy_policy import (VARIANTS, build_report, catalog_models,
    compare_quality, digest, load_run, main, render_markdown)


class PrivacySummaryTests(unittest.TestCase):
    def fixture(self, directory):
        """Structurally valid synthetic records for parser tests, never model results."""
        artifact = catalog_models()['gemma4-e4b-qat']['artifact']
        ids = [format(index, '024x') for index in range(32)]
        cases = {'candidates': [{'id': ident, 'file': 'doc', 'value': 'value-' + str(index),
                               'start': index, 'end': index + 1} for index, ident in enumerate(ids)],
                 'reference_private': 16, 'reference_public': 16}
        batches = [ids[index:index + 8] for index in range(0, 32, 8)]
        settings = {'model': 'gemma4-e4b-qat', 'context_tokens': 16384,
                    'reserve_tokens': 64, 'retry_output_tokens': 8192,
                    'expected_runtime_sha256': 'a' * 64, 'threads': 2}
        signature = {'settings': settings, 'case_data_sha256': digest(cases),
            'source_sha256': {'doc': 'b' * 64}, 'annotations_sha256': 'c' * 64,
            'policy_sha256': 'd' * 64,
            'model_artifact': [artifact['filename'], artifact['bytes'], artifact['sha256'], artifact['url']]}
        requests = []
        for variant in VARIANTS:
            for index, batch in enumerate(batches):
                request = {'model': settings['model'], 'max_tokens': 4096,
                           'messages': [{'role': 'user', 'content': json.dumps(batch)}]}
                requests.append({'variant': variant, 'batch_index': index, 'case_ids': batch,
                    'request': request, 'request_sha256': digest(request),
                    'prompt_sha256': 'e' * 64, 'input_sha256': 'f' * 64, 'schema_sha256': '1' * 64})
        plan = {'version': 1, 'signature': signature, 'variant_order': list(VARIANTS),
            'control_cases': cases, 'batches': batches, 'requests': requests,
            'protected_bypass': False, 'model_sha256_verified': True,
            'token_budget_validation': 'verified_runtime', 'runtime_sha256': 'a' * 64,
            'adjacent_runtime_libraries_sha256': {'libllama.so': '4' * 64},
            'hardware': {'cpu_models': ['Synthetic parser-test hardware']},
            'native_template_probe': {'on': {'formatted_sha256': '2' * 64, 'tokens': 5}}}
        plan['plan_sha256'] = digest({'signature': signature, 'batches': batches, 'requests': requests})
        for row in requests:
            row.update(input_tokens=1000, rendered_prompt_sha256='3' * 64)
        state = {'version': 1, 'status': 'running', 'shadow': True, 'rules_changed': 0,
            'signature': {'plan_sha256': plan['plan_sha256'], 'model_sha256': artifact['sha256'],
                'runtime_sha256': 'a' * 64, 'hardware_sha256': digest(plan['hardware']),
                'adjacent_runtime_libraries_sha256': {'libllama.so': '4' * 64},
                'template_probe_sha256': digest(plan['native_template_probe']),
                'rendered_requests_sha256': digest([[row['variant'], row['batch_index'],
                    row['rendered_prompt_sha256'], row['input_tokens']] for row in requests])},
            'stages': {}, 'metrics': {}, 'responses': {}, 'calls': [],
            'historical_reference': {'private_keep': 999, 'private_mask': 999},
            'case_transitions': [{'id': ident, 'reference_private': index < 16, 'variants': {}}
                                 for index, ident in enumerate(ids)]}
        return directory / 'result.json', state, plan

    def save(self, path, state, plan):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
        path.with_name('plan.json').write_text(json.dumps(plan))

    def add_batch(self, state, plan, variant='A', index=0, status='received', scored=True):
        row = next(row for row in plan['requests'] if row['variant'] == variant and row['batch_index'] == index)
        request = copy.deepcopy(row['request'])
        entry = {'variant': variant, 'batch_index': index, 'case_ids': row['case_ids'],
            'request': request, 'request_sha256': digest(request), 'status': status}
        decisions = []
        labels = {row['id']: row['reference_private'] for row in state['case_transitions']}
        for ident in row['case_ids']:
            verdict = 'mask' if labels[ident] else 'keep'
            decisions.append({'id': ident, 'verdict': verdict, 'reason': 'Synthetic parser fixture.'})
        if status == 'received':
            reply = {'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'decisions': decisions})}}]}
            entry['raw_transport'] = {'status': 200,
                'body_base64': base64.b64encode(json.dumps(reply).encode()).decode()}
            entry['record'] = {'http_wall_seconds': 10.0}
        key = digest([variant, index, request])
        state['calls'].append(key)
        state['responses'][key] = entry
        if scored:
            state['stages'].setdefault(variant, {'decisions': {}})['decisions'].update({
                row['id']: {**row, 'valid': status == 'received',
                    'review_origin': None if status == 'received' else 'technical'} for row in decisions})
        return entry

    def quality_row(self, name, keep=0, mask=0, review=0, technical=0, complete=True, speed=1):
        return {'row_id': name, 'protocol_complete': complete,
            'quality': {'private_keep': keep, 'public_mask': mask, 'explicit_review': review,
                        'technical_review': technical},
            'all_review': review + technical == 32,
            'performance': {'seconds': speed, 'rss': speed * 1000}}

    def test_partial_snapshot_keeps_denominators_and_pending_is_not_an_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary))
            self.add_batch(state, plan)
            self.add_batch(state, plan, index=1, status='pending', scored=False)
            self.save(path, state, plan)
            run = load_run(path)
            a, b = run['rows'][:2]
            self.assertEqual(a['status'], 'partial')
            self.assertEqual(a['quality']['cases'], 32)
            self.assertEqual(a['quality']['private_cases'], 16)
            self.assertEqual(a['quality']['public_cases'], 16)
            self.assertEqual(a['quality']['attempted_cases'], 8)
            self.assertEqual(a['quality']['private_attempted_cases'], 8)
            self.assertEqual(a['quality']['public_attempted_cases'], 0)
            self.assertEqual(a['quality']['unattempted_cases'], 24)
            self.assertEqual(a['quality']['applicable_decisions'], 8)
            self.assertEqual(a['registered_case_slots'], 16)
            self.assertEqual(a['pending_request_slots'], 1)
            self.assertEqual(a['quality']['technical_review'], 0)
            self.assertEqual(b['status'], 'not_started')
            self.assertIsNone(a['performance']['http_wall_seconds_including_retries'])
            self.assertEqual(a['performance']['known_http_wall_seconds_including_retries'], 10)
            self.assertIsNone(compare_quality(run['rows'])['winner'])
            self.assertNotEqual(a['quality']['private_keep'], 999)

    def test_received_unscored_raw_reply_remains_unscored(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary))
            self.add_batch(state, plan, scored=False)
            self.save(path, state, plan)
            run = load_run(path)
            self.assertEqual(run['rows'][0]['quality']['attempted_cases'], 0)
            self.assertEqual(run['rows'][0]['quality']['applicable_decisions'], 0)
            self.assertTrue(all(not row['variants'] for row in run['case_transitions']))

    def test_unscored_initial_snapshot_has_no_inferred_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary))
            state.pop('case_transitions')
            self.save(path, state, plan)
            run = load_run(path)
            self.assertTrue(all(row['reference_private'] is None for row in run['case_transitions']))
            self.assertEqual(run['rows'][0]['quality']['unattempted_cases'], 32)

    def test_quality_frontier_preserves_all_review_leakage_tradeoff(self):
        rows = [self.quality_row('A', keep=1), self.quality_row('B', review=32),
                self.quality_row('C', keep=3, mask=1), self.quality_row('D', keep=2, mask=1, review=3)]
        result = compare_quality(rows)
        self.assertEqual(set(result['frontier']), {'A', 'B'})
        self.assertIsNone(result['winner'])
        self.assertEqual(result['reason'], 'unresolved_quality_tradeoffs')

    def test_all_review_ties_and_resource_differences_never_create_winner(self):
        rows = [self.quality_row(v, review=32, speed=index + 1) for index, v in enumerate(VARIANTS)]
        result = compare_quality(rows)
        self.assertIsNone(result['winner'])
        self.assertEqual(result['reason'], 'all_review_is_not_automation')
        equal = [self.quality_row('A', speed=1), self.quality_row('B', speed=100),
                 self.quality_row('C', keep=1), self.quality_row('D', mask=1)]
        first = compare_quality(equal)
        self.assertEqual(first['reason'], 'equivalent_quality')
        self.assertIsNone(first['winner'])
        equal[0]['performance'] = {'seconds': 100000, 'rss': 2 ** 50}
        self.assertEqual(compare_quality(equal), first)
        self.assertFalse(first['performance_used'])
        self.assertIsNone(first['numeric_weights'])

    def test_unique_domination_requires_complete_applicable_comparison(self):
        rows = [self.quality_row('A', keep=2, mask=2), self.quality_row('B', keep=1),
                self.quality_row('C'), self.quality_row('D', mask=1)]
        result = compare_quality(rows)
        self.assertEqual(result['winner'], 'C')
        self.assertFalse(result['production_approval'])
        rows[0]['protocol_complete'] = False
        partial = compare_quality(rows)
        self.assertIsNone(partial['winner'])
        self.assertEqual(partial['reason'], 'comparison_incomplete')
        rows[0]['protocol_complete'] = True
        rows[0]['quality']['technical_review'] = 1
        technical = compare_quality(rows)
        self.assertIsNone(technical['winner'])
        self.assertEqual(technical['reason'], 'technical_incompleteness')

    def test_observed_inapplicable_output_is_a_measured_quality_axis(self):
        rows = [self.quality_row('A', keep=2), self.quality_row('B', technical=3),
                self.quality_row('C'), self.quality_row('D', mask=1)]
        for row in rows:
            row['outcome_evidence'] = {'all_generation_outcomes_observed': True}
        result = compare_quality(rows)
        self.assertEqual(result['winner'], 'C')
        self.assertTrue(result['all_generation_outcomes_observed'])
        rows[1]['outcome_evidence']['all_generation_outcomes_observed'] = False
        self.assertIsNone(compare_quality(rows)['winner'])
        self.assertEqual(compare_quality(rows)['reason'], 'technical_incompleteness')

    def test_delivered_invalid_final_and_transport_failure_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary))
            invalid = self.add_batch(state, plan)
            reply = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{bad final'}}]}
            invalid['raw_transport']['body_base64'] = base64.b64encode(json.dumps(reply).encode()).decode()
            for decision in state['stages']['A']['decisions'].values():
                decision.update(valid=False, review_origin='technical')
            self.add_batch(state, plan, index=1, status='failed')
            self.save(path, state, plan)
            row = load_run(path)['rows'][0]
            self.assertEqual(row['quality']['technical_review'], 16)
            self.assertEqual(row['quality']['correct_actions'], 0)
            self.assertEqual(row['outcome_evidence']['delivered_inapplicable_cases'], 8)
            self.assertEqual(row['outcome_evidence']['unobserved_scored_cases'], 8)
            self.assertFalse(row['outcome_evidence']['all_generation_outcomes_observed'])

    def test_native_http_error_is_not_observed_model_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary))
            entry = self.add_batch(state, plan)
            entry['raw_transport']['status'] = 500
            for decision in state['stages']['A']['decisions'].values():
                decision.update(valid=False, review_origin='technical')
            self.save(path, state, plan)
            row = load_run(path)['rows'][0]
            self.assertEqual(row['outcome_evidence']['unobserved_scored_cases'], 8)
            self.assertEqual(row['outcome_evidence']['delivered_inapplicable_cases'], 0)

    def test_source_provenance_rejects_hash_tampering_and_mock_models(self):
        mutations = [
            ('plan_hash_mismatch', lambda state, plan: plan['signature']['settings'].update(threads=9)),
            ('model_hash_mismatch', lambda state, plan: state['signature'].update(model_sha256='0' * 64)),
            ('template_hash_mismatch', lambda state, plan: plan['native_template_probe'].update(extra='tampered')),
            ('rendered_request_hash_mismatch', lambda state, plan: plan['requests'][0].update(input_tokens=1001)),
            ('runtime_library_hash_mismatch', lambda state, plan: plan['adjacent_runtime_libraries_sha256'].update(other='5' * 64)),
            ('unknown_or_test_model', lambda state, plan: plan['signature']['settings'].update(model='fixture')),
            ('mock_result_not_measurement', lambda state, plan: state.update(test_only=True)),
            ('not_phase2_result', lambda state, plan: state.pop('version'))]
        for expected, change in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                path, state, plan = self.fixture(Path(temporary))
                change(state, plan)
                self.save(path, state, plan)
                with self.assertRaisesRegex(ValueError, expected):
                    load_run(path)

    def test_missing_duplicate_unknown_labels_and_fabricated_answers_fail(self):
        mutations = [
            lambda state, plan: state['case_transitions'].pop(),
            lambda state, plan: state['case_transitions'][1].update(id=state['case_transitions'][0]['id']),
            lambda state, plan: state['case_transitions'][0].update(id='unknown'),
            lambda state, plan: state['case_transitions'][0].update(reference_private='yes'),
            lambda state, plan: state['case_transitions'][0].update(reference_private=False),
            lambda state, plan: state['stages']['A']['decisions'][plan['batches'][0][0]].update(verdict='keep')]
        for change in mutations:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                path, state, plan = self.fixture(Path(temporary))
                self.add_batch(state, plan)
                change(state, plan)
                self.save(path, state, plan)
                with self.assertRaises(ValueError):
                    load_run(path)

    def test_cli_only_writes_separate_report_outputs_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path, state, plan = self.fixture(directory / 'run')
            self.add_batch(state, plan)
            self.save(path, state, plan)
            before = {p: p.read_bytes() for p in [path, path.with_name('plan.json')]}
            output = directory / 'report'
            with patch('sys.argv', ['summary', '--results', str(path), '--output-dir', str(output)]), redirect_stdout(StringIO()):
                main()
            self.assertEqual({p.name for p in output.iterdir()}, {'report.md', 'report.json'})
            self.assertEqual({p: p.read_bytes() for p in before}, before)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['new_inference_calls'], 0)
            self.assertTrue(report['runs'][0]['historical_reference_excluded'])
            self.assertIsNone(report['within_run_comparisons'][report['runs'][0]['run_id']]['winner'])
            markdown = (output / 'report.md').read_text()
            self.assertIn('Время и ресурсы', markdown)
            self.assertIn('partial', markdown)
            self.assertIn(plan['batches'][0][0], markdown)
            with patch('sys.argv', ['summary', '--results', str(path), '--output-dir', str(path.parent)]), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main()
            self.assertFalse((path.parent / 'report.json').exists())

    def test_explicit_inputs_reject_preflight_without_scanning_other_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary) / 'run')
            self.save(path, state, plan)
            preflight = path.with_name('preflight.json')
            preflight.write_text(json.dumps({'status': 'preflight_completed'}))
            report = build_report([preflight])
            self.assertEqual(report['runs'], [])
            self.assertEqual(report['rejected_inputs'][0]['error'], 'explicit_phase2_result_required')
            self.assertNotIn('999', render_markdown(report))

    def test_malformed_input_does_not_discard_an_independent_valid_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path, state, plan = self.fixture(directory / 'good')
            self.save(path, state, plan)
            bad = directory / 'bad' / 'result.json'
            self.save(bad, state, [])
            report = build_report([path, bad])
            self.assertEqual(len(report['runs']), 1)
            self.assertEqual(report['rejected_inputs'][0]['error'], 'not_phase2_plan')

    def test_valid_decision_cannot_borrow_evidence_from_invalid_raw_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary))
            entry = self.add_batch(state, plan)
            raw = json.loads(base64.b64decode(entry['raw_transport']['body_base64']))
            rows = json.loads(raw['choices'][0]['message']['content'])
            rows['decisions'][1]['id'] = rows['decisions'][0]['id']
            raw['choices'][0]['message']['content'] = json.dumps(rows)
            entry['raw_transport']['body_base64'] = base64.b64encode(json.dumps(raw).encode()).decode()
            self.save(path, state, plan)
            with self.assertRaisesRegex(ValueError, 'applicable_decision_not_supported_by_response'):
                load_run(path)


if __name__ == '__main__':
    unittest.main()
