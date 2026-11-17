import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apply_measured_privacy as application
import test_summarize_privacy_policy as original_tests
from summarize_privacy_policy import REQUEST_FIELDS, digest, load_run, load_selected_run


class SelectedPrivacyTests(unittest.TestCase):
    def rebind(self, state, plan):
        plan['plan_sha256'] = digest({'signature': plan['signature'], 'batches': plan['batches'],
            'requests': [{key: row[key] for key in REQUEST_FIELDS} for row in plan['requests']]})
        state['signature']['plan_sha256'] = plan['plan_sha256']
        state['signature']['rendered_requests_sha256'] = digest([
            [row['variant'], row['batch_index'], row['rendered_prompt_sha256'], row['input_tokens']]
            for row in plan['requests']])

    def fixture(self, folder, selected=True):
        # Parser fixtures only; these synthetic records are never benchmark data.
        helper = original_tests.PrivacySummaryTests()
        path, state, plan = helper.fixture(folder)
        for index in range(4):
            helper.add_batch(state, plan, variant='D', index=index)
        state['status'] = 'completed'
        if selected:
            plan['variant_order'] = ['D']
            plan['requests'] = [row for row in plan['requests'] if row['variant'] == 'D']
            self.rebind(state, plan)
        helper.save(path, state, plan)
        return helper, path, state, plan

    def test_explicit_D_preserves_old_D_metrics_without_inventing_A_to_C(self):
        with tempfile.TemporaryDirectory() as temporary:
            helper, path, state, plan = self.fixture(Path(temporary), selected=False)
            reference = load_run(path)['rows'][-1]['quality']
            plan['variant_order'] = ['D']
            plan['requests'] = [row for row in plan['requests'] if row['variant'] == 'D']
            self.rebind(state, plan)
            helper.save(path, state, plan)
            original = path.read_bytes(), path.with_name('plan.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'not_phase2_plan'):
                load_run(path)
            selected = load_selected_run(path, variants=['D'])
            self.assertEqual([row['variant'] for row in selected['rows']], ['D'])
            self.assertEqual(selected['rows'][0]['quality'], reference)
            self.assertEqual(selected['rows'][0]['quality']['cases'], 32)
            self.assertEqual(selected['rows'][0]['quality']['private_mask'], 16)
            self.assertTrue(selected['rows'][0]['protocol_complete'])
            self.assertFalse(selected['abcd_comparison'])
            self.assertTrue(all(set(case['variants']) == {'D'} for case in selected['case_transitions']))
            self.assertEqual((path.read_bytes(), path.with_name('plan.json').read_bytes()), original)

    def test_rejects_invalid_selection_and_unplanned_stage(self):
        for variants in (None, [], ['D', 'D'], ['unknown'], 'D'):
            with self.subTest(variants=variants), self.assertRaisesRegex(ValueError, 'invalid_selected_variants'):
                load_selected_run('does-not-exist/result.json', variants=variants)
        with tempfile.TemporaryDirectory() as temporary:
            helper, path, state, plan = self.fixture(Path(temporary))
            state['stages']['A'] = {'decisions': {}}
            helper.save(path, state, plan)
            with self.assertRaisesRegex(ValueError, 'unplanned_variant_stages'):
                load_selected_run(path, variants=['D'])

    def test_selected_reader_still_requires_raw_support_for_stored_verdict(self):
        with tempfile.TemporaryDirectory() as temporary:
            helper, path, state, plan = self.fixture(Path(temporary))
            next(iter(state['stages']['D']['decisions'].values()))['verdict'] = 'keep'
            helper.save(path, state, plan)
            with self.assertRaisesRegex(ValueError, 'applicable_decision_not_supported_by_response'):
                load_selected_run(path, variants=['D'])

    def test_application_requires_explicit_selection_and_only_adds_saved_masks(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            helper, path, state, plan = self.fixture(folder)
            text = ' '.join('value-' + str(i) for i in range(32))
            for case in plan['control_cases']['candidates']:
                case['start'] = text.index(case['value'])
                case['end'] = case['start'] + len(case['value'])
            plan['signature']['case_data_sha256'] = digest(plan['control_cases'])
            plan['signature']['source_sha256'] = {'doc': hashlib.sha256(text.encode()).hexdigest()}
            self.rebind(state, plan)
            helper.save(path, state, plan)
            reference = ({'doc': text}, {'doc': ['value-' + str(i) for i in range(16)]},
                         {'doc': ['value-' + str(i) for i in range(16, 32)]}, 'synthetic parser fixture')
            with patch.object(application, 'reference_data', return_value=reference), \
                    patch.object(application, 'apply_documents') as apply:
                with self.assertRaisesRegex(ValueError, 'not_phase2_plan'):
                    application.run(path, folder / 'binary', folder / 'default')
                apply.assert_not_called()
                application.run(path, folder / 'binary', folder / 'explicit', selected_variants=['D'])
                selected, metadata = apply.call_args.args[6:8]
                self.assertEqual(selected, {'doc': [
                    {'value': 'value-' + str(i), 'kind': 'CUSTOM'} for i in range(16)]})
                self.assertEqual(metadata['decision_view'], 'selected_variants')
                self.assertEqual(metadata['selected_variants'], ['D'])
                self.assertFalse(metadata['abcd_comparison'])
                with self.assertRaisesRegex(ValueError, 'application_variant_not_selected'):
                    application.run(path, folder / 'binary', folder / 'wrong', variant='A', selected_variants=['D'])
                self.assertEqual(apply.call_count, 1)

    def test_selected_application_does_not_bypass_fresh_or_transport_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            helper, path, state, plan = self.fixture(folder)
            with patch.object(application, 'apply_documents') as apply:
                with self.assertRaisesRegex(ValueError, 'selected_variants_require_calibration'):
                    application.run(path, folder / 'binary', folder / 'supplement',
                                    selected_variants=['D'], transport_supplement=folder / 'supplement.json')
                plan['signature']['quality_contract'] = {'phase': 'fresh_holdout'}
                self.rebind(state, plan)
                helper.save(path, state, plan)
                with self.assertRaisesRegex(ValueError, 'selected_reader_requires_calibration'):
                    load_selected_run(path, variants=['D'])
                with self.assertRaisesRegex(ValueError, 'selected_variants_require_calibration'):
                    application.run(path, folder / 'binary', folder / 'fresh', selected_variants=['D'])
                apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
