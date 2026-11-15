from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from evaluate_privacy_holdout import configure, main, parser, prepare_plan
from privacy_policy_holdout import (CASES_SHA256, DEFAULT_DATASET, MANIFEST_SHA256,
    holdout_transitions, load_holdout, score_holdout, scoring_callbacks)


class PrivacyPolicyHoldoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holdout = load_holdout()

    def decisions(self, verdict=None):
        return {case.id: {'id': case.id, 'verdict': verdict or self.holdout.expected_by_id[case.id],
                         'valid': True, 'review_origin': 'explicit' if (
                             verdict or self.holdout.expected_by_id[case.id]) == 'review' else None}
                for case in self.holdout.cases}

    def args(self, directory, *options):
        command = parser()
        return configure(command.parse_args(['--prepare-only', '--output', str(directory), *options]), command)

    def test_frozen_loader_has_32_grounded_label_free_candidates_and_three_classes(self):
        holdout = self.holdout
        self.assertEqual((len(holdout.cases), len(holdout.sources)), (32, 4))
        self.assertEqual(holdout.quality_contract['reference_class_counts'], {'mask': 14, 'keep': 14, 'review': 4})
        self.assertEqual(holdout.quality_contract['manifest_sha256'], MANIFEST_SHA256)
        self.assertEqual(holdout.quality_contract['annotations_sha256'], CASES_SHA256)
        for case, row in zip(holdout.cases, holdout.case_data['candidates']):
            self.assertEqual(set(row), {'id', 'file', 'value', 'start', 'end'})
            self.assertFalse(case.protected)
            self.assertEqual((case.kind, case.scope), ('CUSTOM', 'personal'))
            self.assertEqual(holdout.sources[case.file][case.start:case.end], case.value)
            self.assertRegex(case.id, r'^[a-f0-9]{24}$')

    def test_tampered_source_or_manifest_is_rejected_without_changing_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / 'dataset'
            shutil.copytree(DEFAULT_DATASET, copied)
            source = copied / self.holdout.documents[0]
            source.write_bytes(source.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'holdout_file_checksum_mismatch'):
                load_holdout(copied)
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / 'dataset'
            shutil.copytree(DEFAULT_DATASET, copied)
            source = copied / 'manifest.json'
            source.write_bytes(source.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'holdout_manifest_checksum_mismatch'):
                load_holdout(copied)

    def test_three_way_errors_never_treat_reference_review_as_public(self):
        holdout = self.holdout
        groups = {label: [case.id for case in holdout.cases if holdout.expected_by_id[case.id] == label]
                  for label in ('mask', 'keep', 'review')}
        decisions = self.decisions()
        decisions[groups['mask'][0]]['verdict'] = 'keep'
        decisions[groups['keep'][0]]['verdict'] = 'mask'
        decisions[groups['review'][0]]['verdict'] = 'mask'
        decisions[groups['review'][1]]['verdict'] = 'keep'
        decisions[groups['review'][2]].update(valid=False, error='timeout', review_origin='technical')
        del decisions[groups['review'][3]]
        result = score_holdout(holdout.cases, decisions, holdout.expected_by_id)
        self.assertEqual((result['private_cases'], result['public_cases'], result['reference_review_cases']), (14, 14, 4))
        self.assertEqual((result['private_keep'], result['public_mask']), (1, 1))
        self.assertEqual(result['overconfident_on_reference_review'], 2)
        self.assertEqual((result['reference_review_mask'], result['reference_review_keep']), (1, 1))
        self.assertEqual((result['technical_review'], result['unattempted_cases']), (1, 1))
        self.assertEqual((result['applicable_decisions'], result['correct_actions']), (30, 26))
        self.assertEqual(sum(sum(row.values()) for row in result['confusion_matrix'].values()), 30)
        self.assertEqual(result['per_reference_class']['review']['technical_review'], 1)
        self.assertEqual(result['per_reference_class']['review']['unattempted_cases'], 1)

    def test_all_review_has_only_four_correct_uncertainty_decisions_and_zero_automation(self):
        result = score_holdout(self.holdout.cases, self.decisions('review'), self.holdout.expected_by_id)
        self.assertEqual((result['correct_actions'], result['reference_review_correct']), (4, 4))
        self.assertEqual((result['correct_automatic_actions'], result['automated_decisions']), (0, 0))
        self.assertEqual((result['explicit_review'], result['unnecessary_explicit_review']), (32, 28))
        self.assertEqual((result['private_keep'], result['public_mask']), (0, 0))
        self.assertIsNone(result['typing'])

    def test_invalid_null_and_mismatched_id_rows_are_technical_not_correct_review(self):
        ids = [case.id for case in self.holdout.cases if self.holdout.expected_by_id[case.id] == 'review']
        decisions = {ids[0]: None,
            ids[1]: {'id': ids[1], 'valid': False, 'verdict': 'review', 'error': 'bad_json'},
            ids[2]: {'id': 'wrong-id', 'valid': True, 'verdict': 'review'},
            ids[3]: {'id': ids[3], 'valid': True, 'verdict': 'review', 'review_origin': 'technical'}}
        result = score_holdout(self.holdout.cases, decisions, self.holdout.expected_by_id)
        self.assertEqual((result['attempted_cases'], result['technical_review'], result['unattempted_cases']), (4, 4, 28))
        self.assertEqual(result['reference_review_correct'], 0)
        self.assertEqual(result['technical_error_counts']['invalid_decision_id'], 1)
        with self.assertRaisesRegex(ValueError, 'unknown_holdout_decision_ids'):
            score_holdout(self.holdout.cases, {'unknown': {}}, self.holdout.expected_by_id)

    def test_callbacks_ignore_binary_oracle_and_never_claim_type_scope_accuracy(self):
        metric, transition = scoring_callbacks(self.holdout.expected_by_id, ('A', 'D'))
        result = metric(self.holdout.cases, self.decisions(), {'pretend_everything_private': 'wrong'}, True)
        self.assertEqual(result['correct_actions'], 32)
        self.assertIsNone(result['typing'])
        stages = {'A': {'decisions': self.decisions('review')}, 'D': {'decisions': self.decisions()}}
        rows = transition(self.holdout.cases, stages, {})
        self.assertTrue(all(set(row['variants']) == {'A', 'D'} for row in rows))
        self.assertTrue(all(set(row['changes']) == {'A_to_D'} for row in rows))
        self.assertEqual({row['expected_verdict'] for row in rows}, {'mask', 'keep', 'review'})
        self.assertTrue(all('reference_private' not in row for row in rows))

    def test_transition_distinguishes_explicit_technical_and_unattempted_review(self):
        first = self.holdout.cases[0]
        stages = {'A': {'decisions': {first.id: {'id': first.id, 'valid': True, 'verdict': 'review'}}},
                  'D': {'decisions': {first.id: {'id': first.id, 'valid': False, 'verdict': 'review', 'error': 'timeout'}}}}
        rows = holdout_transitions(self.holdout.cases, stages, self.holdout.expected_by_id, ('A', 'D'))
        self.assertEqual(rows[0]['variants']['A']['state'], 'explicit_review')
        self.assertEqual(rows[0]['variants']['D']['state'], 'technical_review')
        self.assertTrue(rows[0]['changes']['A_to_D'])
        self.assertEqual(rows[1]['variants']['A']['state'], 'unattempted')
        self.assertIsNone(rows[1]['changes']['A_to_D'])

    def test_plan_binds_holdout_producers_oracle_and_phase_but_requests_are_label_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.args(Path(temporary), '--variants', 'A', 'D')
            holdout, _, _, plan = prepare_plan(args)
        self.assertEqual(plan['variant_order'], ['A', 'D'])
        self.assertEqual(len(plan['requests']), 8)
        self.assertEqual(plan['signature']['annotations_sha256'], CASES_SHA256)
        self.assertEqual(plan['signature']['quality_contract']['phase'], 'fresh_holdout')
        self.assertIn('privacy_policy_holdout.py', plan['signature']['code_sha256'])
        self.assertIn('evaluate_privacy_holdout.py', plan['signature']['code_sha256'])
        self.assertIsNone(plan['historical_reference'])
        for request in plan['requests']:
            value = json.loads(request['request']['messages'][1]['content'])
            self.assertEqual(set(value), {'document', 'items'})
            for item in value['items']:
                allowed = {'id', 'value', 'record', 'marked_record'}
                self.assertTrue(set(item).issubset(allowed))
                self.assertNotIn('expected_verdict', item)
                self.assertNotIn('rationale', item)
                self.assertNotIn('kind', item)
                self.assertNotIn('scope', item)
            self.assertIn(value['document'], holdout.sources.values())

    def test_prepare_only_never_dispatches_inference_and_freezes_variant_selection(self):
        with tempfile.TemporaryDirectory() as temporary, patch('evaluate_privacy_holdout.execute') as execute:
            options = ['--prepare-only', '--output', temporary, '--variants', 'D', '--model', 'qwen3.5-2b']
            with redirect_stdout(StringIO()):
                main(options)
            execute.assert_not_called()
            plan = json.loads((Path(temporary) / 'plan.json').read_text())
            self.assertEqual((len(plan['requests']), plan['signature']['settings']['temperature']), (4, 0.0))
            self.assertFalse((Path(temporary) / 'result.json').exists())
            with self.assertRaisesRegex(ValueError, 'prepared_profile_changed'):
                main(['--prepare-only', '--output', temporary, '--variants', 'A'])

    def test_preflight_dispatch_uses_three_way_callbacks_and_only_holdout_documents(self):
        captured = {}
        def execute(args, plan, cases, sources, binary_expected, instructions, kinds, **kwargs):
            captured.update(kwargs)
            self.assertEqual(binary_expected, {})
            self.assertTrue(args.preflight_only)
            result = kwargs['metric_function'](cases, self.decisions('review'), {}, True)
            self.assertEqual((result['reference_review_correct'], result['public_cases']), (4, 14))
            self.assertEqual(plan['variant_order'], ['D'])
        with tempfile.TemporaryDirectory() as temporary, patch('evaluate_privacy_holdout.execute', side_effect=execute):
            main(['--preflight-only', '--output', temporary, '--variants', 'D',
                  '--model-dir', '/tmp/unused-models', '--runtime', '/tmp/unused-runtime'])
        self.assertEqual(captured['documents'], self.holdout.documents)
        self.assertTrue(callable(captured['transition_function']))


if __name__ == '__main__':
    unittest.main()
