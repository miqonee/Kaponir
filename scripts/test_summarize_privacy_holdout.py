import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from evaluate_privacy_expansion import main as prepare
from privacy_policy_holdout import load_holdout, score_holdout
from summarize_privacy_holdout import AXES, build_fresh_report, load_fresh_run, render_fresh
from summarize_privacy_policy import compare_quality, digest


class FreshPrivacySummaryTests(unittest.TestCase):
    def fixture(self, directory, model='gemma4-e4b-qat'):
        """Synthetic parser records, not measurements; always in a temporary dir."""
        with redirect_stdout(StringIO()):
            prepare(['--prepare-only', '--model', model, '--output', str(directory)])
        path = directory / 'result.json'
        plan = json.loads(path.with_name('plan.json').read_text())
        plan.update(model_sha256_verified=True, token_budget_validation='verified_runtime',
            runtime_sha256=plan['signature']['settings']['expected_runtime_sha256'],
            adjacent_runtime_libraries_sha256={}, hardware={'fixture': 'parser test'}, native_template_probe={})
        for row in plan['requests']:
            row.update(rendered_prompt_sha256='a' * 64, input_tokens=100)
        holdout = load_holdout()
        state = {'version': 1, 'status': 'running', 'shadow': True, 'rules_changed': 0,
            'signature': {'plan_sha256': plan['plan_sha256'], 'model_sha256': plan['signature']['model_artifact'][2],
                'runtime_sha256': plan['runtime_sha256'], 'adjacent_runtime_libraries_sha256': {},
                'hardware_sha256': digest(plan['hardware']), 'template_probe_sha256': digest({}),
                'rendered_requests_sha256': digest([[r['variant'], r['batch_index'], r['rendered_prompt_sha256'],
                                                      r['input_tokens']] for r in plan['requests']])},
            'responses': {}, 'calls': [], 'stages': {}, 'metrics': {}}
        for row in plan['requests']:
            if row['variant'] != 'D':
                continue
            decisions = [{'id': ident, 'verdict': holdout.expected_by_id[ident],
                          'reason': 'Synthetic parser fixture.'} for ident in row['case_ids']]
            raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'decisions': decisions})}}]}
            entry = {key: row[key] for key in ('variant', 'batch_index', 'case_ids', 'request', 'request_sha256')}
            entry.update(status='received', raw_transport={'status': 200,
                'body_base64': base64.b64encode(json.dumps(raw).encode()).decode()})
            key = digest([entry['variant'], entry['batch_index'], entry['request']])
            state['calls'].append(key)
            state['responses'][key] = entry
            state['stages'].setdefault('D', {'decisions': {}})['decisions'].update({
                decision['id']: {**decision, 'valid': True,
                                'review_origin': 'explicit' if decision['verdict'] == 'review' else None}
                for decision in decisions})
        state['metrics']['D'] = score_holdout(holdout.cases, state['stages']['D']['decisions'], holdout.expected_by_id)
        self.save(path, state, plan)
        return path, state, plan

    def save(self, path, state, plan):
        path.write_text(json.dumps(state))
        path.with_name('plan.json').write_text(json.dumps(plan))

    def test_three_way_review_is_correct_and_partial_variants_are_not_winners(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _, _ = self.fixture(Path(temporary))
            run = load_fresh_run(path)
            q = run['rows'][3]['quality']
            self.assertEqual((q['correct_actions'], q['correct_automatic_actions']), (32, 28))
            self.assertEqual((q['reference_review_correct'], q['unnecessary_explicit_review']), (4, 0))
            self.assertTrue(run['rows'][3]['outcome_evidence']['all_generation_outcomes_observed'])
            report = build_fresh_report([path])
            self.assertFalse(report['rejected_inputs'])
            self.assertIsNone(next(iter(report['cohort_comparisons'].values()))['winner'])
            self.assertIn('14 mask, 14 keep, 4 review', render_fresh(report))
            self.assertEqual(report['new_inference_calls'], 0)
            coverage = build_fresh_report([path], ['gemma4-e4b-qat', 'qwen3.5-2b'])
            self.assertEqual(coverage['model_coverage']['missing_models'], ['qwen3.5-2b'])
            self.assertEqual(next(iter(coverage['cohort_comparisons'].values()))['reason'], 'expected_models_missing')

    def test_new_pinned_model_is_allowed_and_hash_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary), 'spark-x2.6-4b')
            self.assertEqual(load_fresh_run(path)['model'], 'spark-x2.6-4b')
            state['signature']['model_sha256'] = '0' * 64
            self.save(path, state, plan)
            self.assertEqual(build_fresh_report([path])['rejected_inputs'][0]['error'], 'model_hash_mismatch')

    def test_stored_metric_tampering_and_unsupported_verdict_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, plan = self.fixture(Path(temporary))
            state['metrics']['D']['correct_actions'] = 999
            self.save(path, state, plan)
            with self.assertRaisesRegex(ValueError, 'stored_holdout_metrics_mismatch'):
                load_fresh_run(path)
            state['metrics'].clear()
            decision = next(iter(state['stages']['D']['decisions'].values()))
            decision['verdict'] = 'keep' if decision['verdict'] != 'keep' else 'mask'
            self.save(path, state, plan)
            with self.assertRaisesRegex(ValueError, 'decision_not_supported_by_response'):
                load_fresh_run(path)

    def test_fresh_quality_axes_do_not_penalize_required_review(self):
        def row(name, unnecessary=0, overconfident=0):
            return {'row_id': name, 'protocol_complete': True, 'all_review': False,
                'quality': dict(private_keep=0, public_mask=0, unnecessary_explicit_review=unnecessary,
                                overconfident_on_reference_review=overconfident, technical_review=0)}
        result = compare_quality([row('correct'), row('needless', unnecessary=1), row('guess', overconfident=1)], AXES)
        self.assertEqual(result['winner'], 'correct')
        self.assertFalse(result['performance_used'])


if __name__ == '__main__':
    unittest.main()
