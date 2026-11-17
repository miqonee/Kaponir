import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import summarize_privacy_recovery as report
from recover_privacy_transport import selected_timeouts
from summarize_privacy_policy import digest
from validation_pipeline import Candidate


class RecoverySummaryTests(unittest.TestCase):
    def test_supplement_startup_memory_reaches_hardware_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,path,state,plan,corpus=self.fixture(Path(temporary))
            state['sampled_resources']={'roles':{'model':{'observed_peak_rss_bytes':8*2**30}}}
            next(iter(state['responses'].values()))['record']['sampled_resources']={
                'roles':{'model':{'observed_peak_rss_bytes':3*2**30}}}
            self.save(path,state,plan)
            with patch.object(report,'load_corpus',return_value=corpus):
                run=report.load_supplement(path)
            performance=run['effective_rows'][3]['performance']
            self.assertEqual(performance['model_observed_peak_rss_bytes'],3*2**30)
            self.assertEqual(performance['shared_session_model_observed_peak_rss_bytes'],8*2**30)
            self.assertEqual(performance['hardware_planning_model_observed_peak_rss_bytes'],8*2**30)

    def response(self, rows):
        return {'status': 'received', 'raw_transport': {'status': 200, 'body_base64':
            base64.b64encode(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'decisions': rows})}}]}).encode()).decode()}}

    def test_received_incompatible_type_stays_a_technical_failure(self):
        text = 'Private estimate: 740,00 ₽\n'
        case = Candidate(id='a' * 24, file='doc', value='740,00 ₽', start=18, end=26)
        case.start = text.index(case.value)
        case.end = case.start + len(case.value)
        entry = self.response([{'id': case.id, 'verdict': 'mask', 'kind': 'PERSON', 'scope': 'global',
                               'evidence': text.strip(), 'reason': 'A private price.'}])
        selected = {'variant': 'B', 'case_ids': [case.id]}
        decisions, metrics = report.rescore_entry(entry, selected, [case], {'doc': text},
            {('doc', case.value): ('AMOUNT', 'global')}, ['AMOUNT', 'PERSON'])
        self.assertFalse(decisions[case.id]['valid'])
        self.assertEqual((metrics['private_mask'], metrics['technical_review']), (0, 1))
        entry['raw_reply'] = {'a': 'cached JSON cannot override bytes'}
        with self.assertRaisesRegex(ValueError, 'cached_supplement_reply_mismatch'):
            report.rescore_entry(entry, selected, [case], {'doc': text}, {}, ['AMOUNT', 'PERSON'])

    def fixture(self, root, missing_batch=('D', 2), recovered_verdict='keep'):
        from test_summarize_privacy_policy import PrivacySummaryTests
        helper = PrivacySummaryTests()
        original, original_state, original_plan = helper.fixture(root / 'original')
        source = '\n'.join('row ' + str(i) + ': value-' + str(i) for i in range(32))
        cases = original_plan['control_cases']['candidates']
        for case in cases:
            case['start'] = source.index(': ' + case['value']) + 2
            case['end'] = case['start'] + len(case['value'])
        signature = original_plan['signature']
        signature['settings']['timeout'] = 1200
        signature['source_sha256'] = {'doc': hashlib.sha256(source.encode()).hexdigest()}
        signature['case_data_sha256'] = digest(original_plan['control_cases'])
        signature['code_sha256'] = {name: report.sha(report.ROOT / 'scripts' / name)
            for name in ('privacy_policy_experiment.py', 'validation_pipeline.py')}
        signature['kind_source_sha256'] = report.sha(report.ROOT / 'crates/kaponir-core/src/kinds.rs')
        keys = ('variant', 'batch_index', 'case_ids', 'request', 'request_sha256',
                'prompt_sha256', 'input_sha256', 'schema_sha256')
        original_plan['plan_sha256'] = digest({'signature': signature, 'batches': original_plan['batches'],
            'requests': [{k: row[k] for k in keys} for row in original_plan['requests']]})
        original_state['signature']['plan_sha256'] = original_plan['plan_sha256']
        original_state['status'] = 'completed'
        for variant in ('A', 'B', 'C', 'D'):
            for index in range(4):
                entry = helper.add_batch(original_state, original_plan, variant, index,
                    status='failed' if (variant, index) == missing_batch else 'received')
                if entry['status'] == 'failed':
                    entry.update(error='TimeoutError', record={'http_wall_seconds': 1200.1})
        helper.save(original, original_state, original_plan)
        requests = selected_timeouts(original_state, original_plan)
        plan = {'version': 1, 'experiment_kind': 'transport-recovery-v1',
            'original_result_path': str(original), 'original_result_sha256': report.sha(original),
            'original_plan_file_sha256': report.sha(original.with_name('plan.json')),
            'original_plan_sha256': original_plan['plan_sha256'], 'original_signature': signature,
            'original_control_cases': original_plan['control_cases'],
            'original_template_probe': original_plan['native_template_probe'],
            'original_runtime_libraries': original_plan['adjacent_runtime_libraries_sha256'],
            'client_timeout_seconds': 3600, 'worker_configuration_timeout_seconds': 1200,
            'inference_request_parameters_changed': False, 'requests': requests,
            'shadow': True, 'rules_changed': 0}
        plan['recovery_plan_sha256'] = digest(plan)
        selected = requests[0]
        rows = [{'id': ident, 'verdict': recovered_verdict, 'reason': 'Observed transport supplement.'}
                for ident in selected['case_ids']]
        entry = {**self.response(rows), **{key: selected[key] for key in
            ('variant', 'batch_index', 'case_ids', 'request', 'request_sha256')},
            'record': {'http_wall_seconds': 42.0}}
        key = digest([selected['variant'], selected['batch_index'], selected['request']])
        expected = {('doc', row['value']): ('CUSTOM', 'personal') for row in cases[:16]}
        decisions, metrics = report.rescore_entry(entry, selected, [Candidate(**row) for row in cases],
            {'doc': source}, expected, [])
        state = {'version': 1, 'experiment_kind': 'transport-recovery-v1',
            'status': 'completed', 'shadow': True, 'rules_changed': 0, 'original_files_unchanged': True,
            'recovery_plan_sha256': plan['recovery_plan_sha256'], 'calls': [key], 'responses': {key: entry},
            'batches': [{'variant': selected['variant'], 'batch_index': selected['batch_index'],
                'original_response_key': selected['original_response_key'],
                'request_sha256': selected['request_sha256'], 'decisions': decisions,
                'metrics_selected_cases_only': metrics}]}
        path = root / 'supplement/recovery-result.json'
        path.parent.mkdir()
        self.save(path, state, plan)
        corpus = ([], expected, {}, {'doc': source}, {})
        return original, path, state, plan, corpus

    def save(self, path, state, plan):
        path.write_text(json.dumps(state))
        path.with_name('recovery-plan.json').write_text(json.dumps(plan))

    def test_effective_view_keeps_original_failure_and_cost_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            original, path, _, _, corpus = self.fixture(Path(temporary))
            before = original.read_bytes()
            with patch.object(report, 'load_corpus', return_value=corpus):
                run = report.load_supplement(path)
            self.assertEqual(original.read_bytes(), before)
            old = next(row for row in run['original_rows'] if row['variant'] == 'D')
            effective = next(row for row in run['effective_rows'] if row['variant'] == 'D')
            self.assertEqual((old['quality']['technical_review'], effective['quality']['technical_review']), (8, 0))
            self.assertEqual(effective['quality']['correct_actions'], 32)
            self.assertAlmostEqual(effective['performance']['http_wall_seconds_including_retries'], 1272.1)
            self.assertEqual(run['reinterpreted_received_failures'], 0)
            self.assertFalse(run['automatic_production_approval'])

    def test_received_packets_cannot_be_added_to_timeout_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, path, state, plan, corpus = self.fixture(Path(temporary))
            extra = copy.deepcopy(plan['requests'][0]);extra.update(variant='B', batch_index=0)
            plan['requests'].append(extra)
            plan['recovery_plan_sha256'] = digest({k: v for k, v in plan.items() if k != 'recovery_plan_sha256'})
            state['recovery_plan_sha256'] = plan['recovery_plan_sha256']
            self.save(path, state, plan)
            with patch.object(report, 'load_corpus', return_value=corpus), self.assertRaisesRegex(
                    ValueError, 'supplement_selection_not_definite_timeouts'):
                report.load_supplement(path)

    def test_stored_decisions_cannot_override_a_returned_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, path, state, plan, corpus = self.fixture(Path(temporary))
            row = next(iter(state['batches'][0]['decisions'].values()))
            row['verdict'] = 'mask'
            self.save(path, state, plan)
            with patch.object(report, 'load_corpus', return_value=corpus), self.assertRaisesRegex(
                    ValueError, 'supplement_stored_decisions_or_metrics_mismatch'):
                report.load_supplement(path)


if __name__ == '__main__':
    unittest.main()
