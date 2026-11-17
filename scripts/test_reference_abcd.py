import copy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from compare_local_models import ROOT
import evaluate_privacy_expansion as expansion
from privacy_abcd_reference import bind, contract, reference
from run_legacy_new_models import MODELS
from run_reference_abcd import DEFAULT_ORDER, build_jobs, dependency_ready, terminal_result
from run_privacy_policy_suite import atomic_json


class ReferenceABCDTests(unittest.TestCase):
    def prepared(self, root, model=MODELS[0]):
        config = {'runner_root': str(ROOT), 'output': str(root), 'runtime': '/unused',
                  'model_dirs': {name: '/unused' for name in MODELS}}
        job = next(row for row in build_jobs(config) if row['model'] == model)
        with patch.object(expansion, 'execute') as inference, redirect_stdout(StringIO()):
            expansion.main(job['command'][3:] + ['--prepare-only'])
        inference.assert_not_called()
        return json.loads((Path(job['output']) / 'plan.json').read_text())

    def test_all_eight_have_own_ABCD_and_same_old_requests_without_inference(self):
        old = reference()
        with tempfile.TemporaryDirectory() as temporary:
            config = {'runner_root': str(ROOT), 'output': temporary, 'runtime': '/unused',
                      'model_dirs': {model: '/unused' for model in MODELS}}
            jobs = build_jobs(config)
            self.assertEqual(tuple(job['model'] for job in jobs), DEFAULT_ORDER)
            self.assertEqual(jobs[-1]['model'], 'lfm2.5-2.6b')
            self.assertEqual({job['model'] for job in jobs}, set(MODELS))
            for model in MODELS:
                with self.subTest(model=model):
                    plan = self.prepared(Path(temporary), model)
                    self.assertEqual(plan['signature']['settings']['model'], model)
                    self.assertEqual(plan['variant_order'], ['A', 'B', 'C', 'D'])
                    self.assertEqual(len(plan['requests']), 20)
                    self.assertEqual(contract(plan), contract(old))
                    self.assertEqual(plan['signature']['paired_reference'], bind(plan))

    def test_order_can_change_without_dropping_or_duplicating_a_model(self):
        config = {'runner_root': str(ROOT), 'output': '/unused', 'runtime': '/unused',
                  'model_dirs': {model: '/unused' for model in MODELS}, 'model_order': list(reversed(MODELS))}
        self.assertEqual([row['model'] for row in build_jobs(config)], list(reversed(MODELS)))
        for order in [list(MODELS[:-1]), [MODELS[0]] * len(MODELS), [*MODELS[:-1], 'old-model']]:
            with self.subTest(order=order), self.assertRaisesRegex(ValueError, 'exactly_once'):
                build_jobs(config | {'model_order': order})

    def test_D_only_prompt_sampling_and_batch_drift_are_not_same_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = self.prepared(Path(temporary))
        for change, message in [('D', 'requires_all_ABCD'), ('prompt', 'contract_differs'),
                                ('sampling', 'setting_differs'), ('batch', 'batch_plan_differs')]:
            changed = copy.deepcopy(plan)
            if change == 'D': changed['variant_order'] = ['D']
            if change == 'prompt': changed['requests'][0]['request']['messages'][0]['content'] += ' changed'
            if change == 'sampling': changed['signature']['settings']['temperature'] = 0
            if change == 'batch': changed['batches'][0] = changed['batches'][0][:-1]
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                bind(changed)

    def test_dependency_waits_for_all_schemes_and_all_application_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);source, app = root/'schemes.json', root/'app.json'
            cfg = {'wait_schemes': str(source), 'wait_scheme_applications': str(app),
                   'scheme_config_digest': 'fixture', 'runtime': '/unused'}
            state = {'status': 'running', 'config_sha256': 'fixture'}
            atomic_json(source, state)
            self.assertFalse(dependency_ready(cfg))
            state.update(status='measurements_finished_with_limitations',
                jobs={name: {'status': 'completed'} for name in MODELS},
                controller={'pid': 123, 'identity': {'start_ticks': 1}})
            atomic_json(source, state);atomic_json(app, {'status': 'waiting_sources', 'terminal_jobs': 47})
            with patch('run_reference_abcd.process_identity', return_value=None), \
                    patch('run_reference_abcd.runtime_processes', return_value=[]):
                self.assertFalse(dependency_ready(cfg))
                atomic_json(app, {'status': 'applications_finished_with_source_limitations', 'terminal_jobs': 48})
                self.assertTrue(dependency_ready(cfg))

    def test_zero_call_incompatibility_requires_durable_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_json(root/'native-budget-observations.json',
                {'error': 'paired_reference_batch_plan_differs', 'measurement_request_slots_recorded': 0})
            job = {'output': str(root)}
            with self.assertRaisesRegex(ValueError, 'cleanup_missing'): terminal_result(job)
            atomic_json(root/'session-1.json', {'finished_at_utc': 'fixture',
                                              'phases_seconds': {'runtime_cleanup': .1}})
            self.assertEqual(terminal_result(job)['status'], 'unavailable_context')


if __name__ == '__main__':
    unittest.main()
