import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import evaluate_privacy_pilot as pilot
from evaluate_privacy_policy import parse_response
from privacy_expansion_profiles import all_pinned_model_rows
from privacy_policy_experiment import metrics, payload, validate
from summarize_privacy_policy import catalog_models, digest, load_run


class PrivacyPilotTests(unittest.TestCase):
    def prepared(self, root, model='lfm2.5-1.2b-instruct'):
        with redirect_stdout(StringIO()):
            pilot.main(['--prepare-only', '--model', model, '--output', str(root)])
        return json.loads((root / 'plan.json').read_text())

    def test_all_eight_models_prepare_same_balanced_targets_without_inference(self):
        before = (pilot.ROOT / 'tests/fixtures/context-control-candidates.json').read_bytes()
        targets = None
        for model in pilot.manifest()['models']:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temporary:
                with patch.object(pilot, 'execute') as inference:
                    plan = self.prepared(Path(temporary), model)
                inference.assert_not_called()
                ids = [row['id'] for row in plan['control_cases']['candidates']]
                if targets is None:
                    targets = ids
                self.assertEqual(ids, targets)
                self.assertEqual(len(ids), 8)
                self.assertEqual((plan['control_cases']['reference_private'], plan['control_cases']['reference_public']), (4, 4))
                self.assertEqual((plan['variant_order'], len(plan['requests'])), (['B', 'D'], 4))
                for request in plan['requests']:
                    data = json.loads(request['request']['messages'][1]['content'])
                    for item in data['items']:
                        self.assertFalse(set(item) & {'reference_private', 'expected_verdict', 'kind', 'scope'})
                budget = plan['signature']['settings']['reasoning_budget']
                self.assertEqual(budget, -1 if model in ('lfm2.5-1.2b-instruct', 'tiny-aya-global') else 512)
        self.assertEqual((pilot.ROOT / 'tests/fixtures/context-control-candidates.json').read_bytes(), before)

    def test_research_reader_uses_exact_pins_instead_of_old_planning_placeholders(self):
        catalog = catalog_models()
        for model in all_pinned_model_rows():
            self.assertEqual(catalog[model['id']]['artifact']['sha256'], model['artifact']['sha256'])
            self.assertEqual(catalog[model['id']]['artifact']['bytes'], model['artifact']['bytes'])

    def test_full_reader_does_not_admit_eight_control_pilot_as_full_calibration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, state = self.observed(root)
            (root / 'result.json').write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, 'not_phase2_plan'):
                load_run(root / 'result.json')

    def observed(self, root):
        plan = self.prepared(root)
        spec = pilot.manifest();dataset = pilot.selected_data(spec)
        _, _, kinds = pilot.prompts_and_kinds()
        plan.update(model_sha256_verified=True, token_budget_validation='verified_runtime',
            runtime_sha256=plan['signature']['settings']['expected_runtime_sha256'],
            adjacent_runtime_libraries_sha256={'libllama.so': 'a'*64}, hardware={'test_hardware': 'parser only'},
            native_template_probe={})
        for row in plan['requests']:
            row.update(input_tokens=100, rendered_prompt_sha256='b'*64)
        state = {'version': 1, 'shadow': True, 'rules_changed': 0, 'status': 'completed',
            'signature': {'plan_sha256': plan['plan_sha256'], 'model_sha256': plan['signature']['model_artifact'][2],
                'runtime_sha256': plan['runtime_sha256'], 'hardware_sha256': digest(plan['hardware']),
                'adjacent_runtime_libraries_sha256': plan['adjacent_runtime_libraries_sha256'],
                'template_probe_sha256': digest({}), 'rendered_requests_sha256': digest([
                    [row['variant'], row['batch_index'], row['rendered_prompt_sha256'], row['input_tokens']]
                    for row in plan['requests']])},
            'stages': {}, 'metrics': {}, 'calls': [], 'responses': {}, 'execution_sessions': [
                {'finished_at_utc': '2026-01-01T00:00:00Z', 'phases_seconds': {'runtime_cleanup': .1}}]}
        by_id = {case.id: case for case in dataset.cases}
        for row in plan['requests']:
            cases = [by_id[ident] for ident in row['case_ids']]
            decisions = []
            for case in cases:
                decision = {'id': case.id, 'verdict': 'mask' if (case.file, case.value) in dataset.expected else 'keep',
                            'reason': 'Synthetic parser test.'}
                if row['variant'] == 'B':
                    decision.update(kind='CUSTOM', scope='personal', evidence=case.value)
                decisions.append(decision)
            reply = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'decisions': decisions})}}]}
            entry = {**{key: row[key] for key in ('variant', 'batch_index', 'case_ids', 'request', 'request_sha256')},
                'status': 'received', 'raw_reply': reply, 'raw_transport': {'status': 200,
                    'body_base64': base64.b64encode(json.dumps(reply).encode()).decode()},
                'record': {'http_wall_seconds': 1.0}}
            key = digest([row['variant'], row['batch_index'], row['request']])
            state['calls'].append(key);state['responses'][key] = entry
            checked = validate(parse_response(entry)[0], cases, payload(row['variant'], cases, dataset.sources)[1], kinds, row['variant'])
            group = state['stages'].setdefault(row['variant'], {'decisions': {}})['decisions'];group.update(checked)
            state['metrics'][row['variant']] = metrics(dataset.cases, group, dataset.expected, row['variant'] != 'D')
        (root / 'plan.json').write_text(json.dumps(plan))
        (root / 'result.json').write_text(json.dumps(state))
        return plan, state

    def test_pilot_rescores_raw_bytes_and_refuses_unmeasured_or_rewritten_results(self):
        for corruption in (None, 'cached_decision', 'missing_call', 'different_target', 'budget'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary);plan, state = self.observed(root)
                if corruption == 'cached_decision':
                    next(iter(state['stages']['D']['decisions'].values()))['verdict'] = 'review'
                elif corruption == 'missing_call':
                    key = state['calls'].pop();state['responses'].pop(key)
                elif corruption in ('different_target', 'budget'):
                    if corruption == 'different_target':
                        plan['control_cases']['candidates'][0]['value'] += 'changed'
                        plan['signature']['case_data_sha256'] = digest(plan['control_cases'])
                    else:
                        plan['signature']['settings']['timeout'] += 1
                    plan['plan_sha256'] = digest({'signature': plan['signature'], 'batches': plan['batches'],
                        'requests': [{key: value for key, value in row.items() if key not in ('input_tokens', 'rendered_prompt_sha256')}
                                     for row in plan['requests']]})
                    state['signature']['plan_sha256'] = plan['plan_sha256']
                (root / 'plan.json').write_text(json.dumps(plan));(root / 'result.json').write_text(json.dumps(state))
                if corruption:
                    with self.assertRaises(ValueError):
                        pilot.load_pilot_run(root / 'result.json')
                else:
                    run = pilot.load_pilot_run(root / 'result.json')
                    self.assertFalse(run['final_model_selection_allowed'])
                    self.assertEqual(run['models_excluded_from_full_study'], [])
                    self.assertEqual([row['quality']['correct_actions'] for row in run['rows']], [8, 8])

    def test_pilot_refuses_new_measurements_while_runtime_is_busy(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(pilot, 'runtime_processes', return_value=[123]), \
                patch.object(pilot, 'execute') as inference:
            with self.assertRaisesRegex(ValueError, 'runtime_busy_no_parallel_inference'):
                pilot.main(['--model', 'lfm2.5-1.2b-instruct', '--output', temporary,
                            '--model-dir', temporary, '--runtime', str(Path(temporary) / 'llama-server')])
            inference.assert_not_called()


if __name__ == '__main__':
    unittest.main()
