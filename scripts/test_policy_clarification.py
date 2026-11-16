import copy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluate_policy_clarification import main as prepare_v2
from evaluate_privacy_expansion import main as prepare_v1
from privacy_expansion_profiles import model_ids
from privacy_policy_clarification import load_clarification_run, verify_pair_plans
from report_policy_clarification import read_pair
import run_policy_clarification as queue
from summarize_privacy_holdout import load_fresh_run
from summarize_privacy_policy import REQUEST_FIELDS, digest


class PolicyClarificationTests(unittest.TestCase):
    def plans(self, root, model='gemma4-e4b-qat'):
        with redirect_stdout(StringIO()):
            prepare_v1(['--prepare-only', '--model', model, '--timeout', '3600', '--output', str(root / 'v1')])
            prepare_v2(['--prepare-only', '--model', model, '--output', str(root / 'v2')])
        return tuple(json.loads((root / name / 'plan.json').read_text()) for name in ('v1', 'v2'))

    def test_all_thirteen_plans_differ_only_by_the_registered_trusted_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            for model in model_ids():
                before, after = self.plans(Path(temporary) / model, model)
                verified = verify_pair_plans(before, after)
                self.assertTrue(verified['policy_only_request_difference_verified'])
                self.assertFalse(verified['native_pair_verified'])
                self.assertEqual((verified['paired_requests'], len(verified['case_ids'])), (4, 32))
                self.assertEqual(after['signature']['settings']['variants'], ['D'])
                self.assertEqual(after['inference_calls'], 0)

    def test_changed_user_data_packets_or_sampling_cannot_be_called_policy_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            before, after = self.plans(Path(temporary))
        for field in ('temperature', 'top_k', 'reasoning_budget'):
            changed = copy.deepcopy(after)
            changed['requests'][0]['request'][field] = 'changed'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'policy_pair_changed_request_parameters'):
                verify_pair_plans(before, changed)
        changed = copy.deepcopy(after)
        changed['requests'][0]['request']['messages'][1]['content'] += ' changed source'
        with self.assertRaisesRegex(ValueError, 'policy_pair_changed_nonpolicy_input'):
            verify_pair_plans(before, changed)
        changed = copy.deepcopy(after)
        changed['batches'][0] = list(reversed(changed['batches'][0]))
        with self.assertRaisesRegex(ValueError, 'policy_pair_changed_candidates_or_batches'):
            verify_pair_plans(before, changed)

    def measured_fixture(self, root):
        from test_summarize_privacy_holdout import FreshPrivacySummaryTests
        helper = FreshPrivacySummaryTests()
        baseline_dir = root / 'v1'
        baseline_dir.mkdir(parents=True)
        reference, state, baseline = helper.fixture(baseline_dir, extra_args=['--timeout', '3600'])
        state['status'] = 'completed'
        helper.save(reference, state, baseline)
        with redirect_stdout(StringIO()):
            prepare_v2(['--prepare-only', '--model', 'gemma4-e4b-qat', '--output', str(root / 'v2')])
        path = root / 'v2/result.json'
        treatment = json.loads(path.with_name('plan.json').read_text())
        for key in ('model_sha256_verified', 'token_budget_validation', 'runtime_sha256',
                    'adjacent_runtime_libraries_sha256', 'hardware', 'native_template_probe'):
            treatment[key] = baseline[key]
        for row in treatment['requests']:
            row.update(rendered_prompt_sha256='b' * 64, input_tokens=200)
        new = copy.deepcopy(state)
        new['responses'], new['calls'] = {}, []
        old_entries = {entry['batch_index']: entry for entry in state['responses'].values()}
        for row in treatment['requests']:
            entry = {**copy.deepcopy(old_entries[row['batch_index']]),
                **{key: row[key] for key in ('request', 'request_sha256', 'variant', 'batch_index', 'case_ids')}}
            key = digest(['D', row['batch_index'], row['request']])
            new['responses'][key] = entry;new['calls'].append(key)
        new['signature']['plan_sha256'] = treatment['plan_sha256']
        new['signature']['rendered_requests_sha256'] = digest([[r['variant'], r['batch_index'],
            r['rendered_prompt_sha256'], r['input_tokens']] for r in treatment['requests']])
        helper.save(path, new, treatment)
        from run_privacy_policy_suite import file_sha
        binding = {'reference_result_sha256': file_sha(reference),
            'reference_plan_sha256': baseline['plan_sha256'], 'treatment_result_sha256': file_sha(path),
            'postcondition': verify_pair_plans(baseline, treatment)}
        path.with_name('policy-pair.json').write_text(json.dumps(binding))
        return reference, path, new, treatment

    def test_policy_v2_is_explicit_and_cannot_be_silently_read_as_v1(self):
        with tempfile.TemporaryDirectory() as temporary:
            reference, path, _, _ = self.measured_fixture(Path(temporary))
            self.assertEqual(load_clarification_run(path)['rows'][0]['quality']['correct_actions'], 32)
            with self.assertRaisesRegex(ValueError, 'not_frozen_three_way_holdout'):
                load_fresh_run(path)
            pair = read_pair(reference, path)
            self.assertEqual([row['policy'] for row in pair['rows']], ['v1', 'v2'])
            self.assertTrue(pair['verification']['native_pair_verified'])
            self.assertEqual(pair['comparison']['reason'], 'equivalent_quality')
            self.assertTrue(all(not row['changed'] for row in pair['transitions']))

    def test_mutated_completion_or_policy_pin_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference, path, state, treatment = self.measured_fixture(root)
            binding = path.with_name('policy-pair.json')
            original_binding = binding.read_text()
            binding.write_text(json.dumps({'reference_result_sha256': 'changed', 'reference_plan_sha256': 'changed'}))
            with self.assertRaisesRegex(ValueError, 'policy_pair_reference_binding_mismatch'):
                read_pair(reference, path)
            binding.write_text(original_binding)
            treatment['signature']['policy_sha256'] = '0' * 64
            treatment['plan_sha256'] = digest({'signature': treatment['signature'], 'batches': treatment['batches'],
                'requests': [{k: row[k] for k in REQUEST_FIELDS} for row in treatment['requests']]})
            state['signature']['plan_sha256'] = treatment['plan_sha256']
            from test_summarize_privacy_holdout import FreshPrivacySummaryTests
            FreshPrivacySummaryTests().save(path, state, treatment)
            with self.assertRaisesRegex(ValueError, 'unexpected_fresh_policy'):
                load_clarification_run(path)

    def config(self, root):
        return {'runner_root': str(root), 'output': str(root / 'policy-v2'), 'complete_output': str(root / 'complete'),
            'old_model_dir': '/old', 'new_model_dir': '/new', 'runtime': '/runtime',
            'producer_inputs_sha256': {}}

    def test_queue_contains_all_thirteen_and_never_replays_an_uncertain_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = queue.Controller(self.config(Path(temporary)))
            self.assertEqual(len(controller.jobs), 13)
            self.assertEqual(set(job['model'] for job in controller.jobs), set(model_ids()))
            self.assertTrue(all(job['variants'] == ['D'] for job in controller.jobs))
            job = controller.jobs[0]
            controller.state['jobs'][job['id']] = {**job, 'status': 'launching'}
            with patch.object(controller, 'check_source'), patch('run_complete_research.subprocess.Popen') as spawned:
                with self.assertRaisesRegex(ValueError, 'launch_outcome_unknown_no_automatic_replay'):
                    controller.model(job)
            spawned.assert_not_called()


if __name__ == '__main__':
    unittest.main()
