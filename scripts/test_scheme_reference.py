from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import evaluate_model_schemes as schemes
from privacy_expansion_profiles import GEMMA_IT_MODELS
from run_legacy_new_models import MODELS, build_jobs
from scheme_reference import (REQUESTED_REFERENCE, matching_requested_references,
                              reference, verify_reference_inputs)


class SchemeReferenceTests(unittest.TestCase):
    def prepared(self, output, model, reference_model='nanbeige42-3b'):
        args = ['--model', model, '--profile', 'common', '--legacy-reference', reference_model,
                '--output', str(output), '--prepare-only']
        if model in GEMMA_IT_MODELS:
            args += ['--new-catalog', str(schemes.ROOT / 'docs/experiments/gemma-it-artifacts-v1.json')]
        with redirect_stdout(StringIO()), patch.object(schemes, 'execute') as generation:
            schemes.main(args)
        generation.assert_not_called()
        return json.loads((output / 'scheme-plan.json').read_text())

    def test_all_eight_new_models_replay_existing_inputs_and_limits_without_old_inference(self):
        old, binding = reference('nanbeige42-3b')
        archive = schemes.ROOT / binding['path'];before = archive.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            config = {'runner_root': str(schemes.ROOT), 'output': temporary,
                      'reference_model': 'nanbeige42-3b', 'runtime': '/unused',
                      'model_dirs': {model: '/unused' for model in MODELS}}
            jobs = build_jobs(config)
            self.assertEqual({job['model'] for job in jobs}, set(MODELS))
            self.assertEqual(len(jobs), 8)
            for model in MODELS:
                with self.subTest(model=model):
                    plan = self.prepared(Path(temporary) / model, model)
                    self.assertEqual(plan['legacy_reference'], binding)
                    self.assertEqual(verify_reference_inputs(plan, 'nanbeige42-3b'), binding)
                    p = plan['profile']
                    self.assertEqual((p['context_tokens'], p['output_tokens'], p['retry_output_tokens']), (4096, 1600, 1600))
                    self.assertEqual((p['transport_timeout_seconds'], p['worker_configuration_timeout_seconds']), (1200, 1200))
                    self.assertEqual(p['response_format'], 'auto')
                    self.assertEqual(plan['legacy_reference_mode'], REQUESTED_REFERENCE)
                    self.assertEqual(set(matching_requested_references(plan)),
                                     {'gemma4-e4b-qat', 'granite42-3b', 'nanbeige42-3b'})
                    self.assertEqual(p['template_kwargs'], {'enable_thinking': False})
                    self.assertEqual(p['extra_sampling'], {})
                    self.assertEqual(plan['source_sha256'], old['signature']['source_sha256'])
                    self.assertEqual(plan['prompt_sha256'], old['signature']['prompts'])
        self.assertEqual(archive.read_bytes(), before)

    def test_reference_mode_rejects_old_model_calls_or_per_model_format_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'requires_new_model'):
                self.prepared(Path(temporary) / 'old', 'qwen3.5-2b')
            with redirect_stdout(StringIO()), self.assertRaisesRegex(ValueError, 'format_is_fixed'):
                schemes.main(['--model', MODELS[0], '--profile', 'common', '--legacy-reference', 'nanbeige42-3b',
                    '--response-format', 'grammar', '--output', str(Path(temporary) / 'format'), '--prepare-only'])

    def test_profile_and_prompt_drift_are_detected_instead_of_reusing_old_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = self.prepared(Path(temporary), MODELS[0])
            for key, value in [('output_tokens', 1536), ('temperature', .6),
                               ('transport_timeout_seconds', 3600), ('response_format', 'grammar')]:
                modified = json.loads(json.dumps(plan));modified['profile'][key] = value
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'legacy_reference'):
                    verify_reference_inputs(modified, 'nanbeige42-3b')
            plan['prompt_sha256']['primary'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'input_differs:prompt_sha256'):
                verify_reference_inputs(plan, 'nanbeige42-3b')
            with self.assertRaisesRegex(ValueError, 'missing_explicit_sampling_or_format'):
                reference('minicpm5-2b')

    def test_slow_gguf_verification_does_not_spend_or_extend_measured_http_budget(self):
        clock = [0.0];worker = Mock()
        worker.status.side_effect = lambda: {'models': {'test': {'downloaded': clock[0] >= 45}}}
        worker.submit.return_value = {'id': 'test-job'}
        worker.process_supports_json_schema = True;worker.ocr_base = 'http://unused'
        worker.count_text_tokens.return_value = 5
        profile = {'reasoning': 'off', 'reasoning_format': 'auto', 'reasoning_budget': -1,
                   'template_kwargs': {'enable_thinking': False}, 'transport_timeout_seconds': 1200}
        def advance(seconds):
            clock[0] += seconds
        with patch('recover_privacy_transport.time.monotonic', side_effect=lambda: clock[0]), \
             patch('recover_privacy_transport.time.sleep', side_effect=advance), \
             patch.object(schemes, 'http_json', return_value={'prompt': 'Public reference'}), \
             patch.object(schemes, 'request_raw') as measured_http:
            client = schemes.MeasuredClient(worker, 'test', 4096, 1600, 'prompt', {}, profile, Path('/unused'), [])
        self.assertGreaterEqual(clock[0], 45)
        worker.start_runtime.assert_called_once()
        measured_http.assert_not_called()
        self.assertEqual(client.profile['transport_timeout_seconds'], 1200)

    def test_archived_forced_effective_v1_remains_readable_but_is_not_requested_auto(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = self.prepared(Path(temporary), MODELS[0])
            old = json.loads(json.dumps(plan));old.pop('legacy_reference_mode')
            old['profile']['response_format'] = 'prompt'
            self.assertEqual(verify_reference_inputs(old, 'nanbeige42-3b'), plan['legacy_reference'])
            with self.assertRaisesRegex(ValueError, 'requested_reference_plan_required'):
                matching_requested_references(old)
            with self.assertRaisesRegex(ValueError, 'transport_or_format_differs'):
                verify_reference_inputs(plan | {'profile': old['profile']}, 'nanbeige42-3b')


if __name__ == '__main__':
    unittest.main()
