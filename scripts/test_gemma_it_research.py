from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluate_privacy_expansion import main as prepare_privacy
from evaluate_model_schemes import main as prepare_schemes
from evaluate_policy_clarification import main as prepare_policy
from kaponir_worker import FILES
from privacy_expansion_profiles import (GEMMA_IT_CATALOG, GEMMA_IT_MODELS,
    expansion_profile, model_ids, pinned_models, research_registration)
from privacy_policy_clarification import verify_pair_plans
from report_complete_research import write_reports
from run_gemma_it_research import ALL_MODELS, build_jobs, ready


class GemmaItResearchTests(unittest.TestCase):
    def test_extension_is_explicit_and_does_not_expand_original_thirteen_jobs(self):
        self.assertEqual(len(model_ids()), 13)
        self.assertEqual(len(set(ALL_MODELS)), 15)
        selected = model_ids(GEMMA_IT_CATALOG)
        self.assertEqual(len(selected), 9)
        self.assertEqual(set(selected[-2:]), set(GEMMA_IT_MODELS))
        data, source = pinned_models(GEMMA_IT_CATALOG)
        self.assertEqual(source['path'], 'docs/experiments/gemma-it-artifacts-v1.json')
        self.assertTrue(all(row['instruction_tuned'] for row in data['models']))

    def test_gemma_native_and_common_parameters_remain_distinct(self):
        for model in GEMMA_IT_MODELS:
            native = expansion_profile(model, 'native', GEMMA_IT_CATALOG)
            common = expansion_profile(model, 'common', GEMMA_IT_CATALOG)
            self.assertEqual((native['context_tokens'], native['temperature'], native['top_k']),
                             (16384, 1.0, 64))
            self.assertEqual((common['context_tokens'], common['temperature'], common['top_k']),
                             (8192, 0.0, 40))
            self.assertEqual(native['quantization'], 'Q4_K_M')
            self.assertEqual(native['profile_source']['pinned_catalog']['sha256'],
                             common['profile_source']['pinned_catalog']['sha256'])

    def test_extension_registration_is_removed_after_failure(self):
        before = dict(FILES)
        with self.assertRaisesRegex(RuntimeError, 'test_exit'):
            with research_registration(GEMMA_IT_MODELS[0], GEMMA_IT_CATALOG):
                self.assertIn(GEMMA_IT_MODELS[0], FILES)
                raise RuntimeError('test_exit')
        self.assertEqual(FILES, before)

    def test_new_gemma_prepares_policy_pair_without_calling_a_model(self):
        before = dict(FILES)
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            base = Path(temporary)
            common = ['--prepare-only', '--model', GEMMA_IT_MODELS[0],
                      '--new-catalog', str(GEMMA_IT_CATALOG), '--timeout', '3600']
            with patch('evaluate_privacy_expansion.execute') as v1, \
                    patch('evaluate_policy_clarification.execute') as v2:
                prepare_privacy(common + ['--output', str(base / 'v1')])
                prepare_policy(common + ['--output', str(base / 'v2')])
            v1.assert_not_called();v2.assert_not_called()
            old = json.loads((base / 'v1/plan.json').read_text())
            new = json.loads((base / 'v2/plan.json').read_text())
            verified = verify_pair_plans(old, new)
            self.assertTrue(verified['policy_only_request_difference_verified'])
            self.assertFalse(verified['native_pair_verified'])
            self.assertEqual(verified['paired_requests'], 4)
        self.assertEqual(FILES, before)

    def test_six_schemes_use_the_same_explicit_gemma_artifact_without_inference(self):
        before = dict(FILES)
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            with patch('evaluate_model_schemes.execute') as inference:
                prepare_schemes(['--prepare-only', '--model', GEMMA_IT_MODELS[1],
                    '--new-catalog', str(GEMMA_IT_CATALOG), '--profile', 'native', '--output', temporary])
            inference.assert_not_called()
            plan = json.loads((Path(temporary) / 'scheme-plan.json').read_text())
            artifact = pinned_models(GEMMA_IT_CATALOG)[0]['models'][1]['artifact']
            self.assertEqual(plan['model_artifact'][2], artifact['sha256'])
            self.assertEqual(plan['profile']['temperature'], 1.0)
            self.assertEqual(plan['profile']['max_candidates'], 128)
        self.assertEqual(FILES, before)

    def test_twelve_jobs_bind_each_policy_pair_to_its_own_native_reference(self):
        jobs = build_jobs({'runner_root': '/producer', 'output': '/study',
                           'model_dir': '/weights', 'runtime': '/runtime'})
        self.assertEqual((len(jobs), len({row['id'] for row in jobs})), (12, 12))
        for model in GEMMA_IT_MODELS:
            selected = [row for row in jobs if row['model'] == model]
            self.assertEqual(len(selected), 6)
            policy = next(row for row in selected if row['kind'] == 'policy_clarification')
            reference = next(row for row in selected if row['kind'] == 'privacy'
                             and row['dataset'] == 'holdout' and row['profile'] == 'native')
            self.assertEqual(policy['reference_result'], reference['output'] + '/result.json')
            self.assertTrue(all('--new-catalog' in row['command'] for row in selected))

    def test_waits_for_parent_exit_verified_downloads_and_free_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / 'parent.json';artifacts = root / 'artifacts.json'
            config = {'wait_policy_status': str(parent), 'artifacts_status': str(artifacts),
                      'runtime': '/runtime'}
            parent.write_text(json.dumps({'status': 'measurements_finished',
                'controller': {'pid': 123, 'identity': {'start_ticks': 9}}}))
            artifacts.write_text(json.dumps({'status': 'completed'}))
            with patch('run_gemma_it_research.process_identity', return_value=None), \
                    patch('run_gemma_it_research.runtime_processes', return_value=[]):
                self.assertTrue(ready(config))
            with patch('run_gemma_it_research.process_identity', return_value={'start_ticks': 9, 'state': 'S'}):
                self.assertFalse(ready(config))
            with patch('run_gemma_it_research.process_identity', return_value=None), \
                    patch('run_gemma_it_research.runtime_processes', return_value=[456]):
                self.assertFalse(ready(config))
            artifacts.write_text(json.dumps({'status': 'running'}))
            with patch('run_gemma_it_research.process_identity', return_value=None):
                self.assertFalse(ready(config))

    def test_pending_fifteen_model_report_cannot_declare_quality_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_reports({'output': temporary, 'expected_models': list(ALL_MODELS)},
                          {'status': 'waiting', 'jobs': {}}, [])
            progress = json.loads((Path(temporary) / 'reports/progress.json').read_text())
            fresh = json.loads((Path(temporary) / 'reports/fresh-native.json').read_text())
            self.assertEqual(len(progress['models']), 15)
            self.assertEqual(set(fresh['model_coverage']['missing_models']), set(ALL_MODELS))
            self.assertFalse(progress['quality_approval'])
            self.assertIn('15 моделей', (Path(temporary) / 'reports/REPORT.md').read_text())


if __name__ == '__main__':
    unittest.main()
