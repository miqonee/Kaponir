import argparse
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from compare_local_models import ROOT
from evaluate_privacy_expansion import main
from evaluate_privacy_policy import request_for
from kaponir_worker import FILES
from privacy_expansion_profiles import (NEW_CATALOG, ProfiledClient, expansion_profile,
                                        model_ids, pinned_models, research_registration)
from privacy_policy_experiment import prompts
from validation_pipeline import Candidate


class PrivacyExpansionTests(unittest.TestCase):
    def test_thirteen_models_and_explicit_native_capability_limits(self):
        ids = model_ids()
        self.assertEqual((len(ids), len(set(ids))), (13, 13))
        aya = expansion_profile('tiny-aya-global')
        self.assertEqual((aya['context_tokens'], aya['reasoning_budget']), (8192, -1))
        self.assertEqual(aya['template_kwargs'], {'enable_thinking': False})
        self.assertEqual(aya['native_capability']['status'], 'not_yet_probed')
        lfm = expansion_profile('lfm2.5-2.6b')
        self.assertEqual(lfm['reasoning_budget'], 2048)
        self.assertEqual(lfm['extra_sampling'], {'repeat_penalty': 1.1})
        self.assertIsNone(lfm['historical_reference'])
        spark = expansion_profile('spark-x2.6-4b')
        self.assertEqual(spark['template_kwargs']['reasoning_effort'], 'high')

    def test_common_profile_is_distinct_and_does_not_claim_native_equivalence(self):
        for model in model_ids():
            with self.subTest(model=model):
                profile = expansion_profile(model, 'common')
                self.assertEqual(profile['context_tokens'], 8192)
                self.assertEqual(profile['temperature'], 0)
                self.assertEqual(profile['extra_sampling'], {})
                self.assertIn('requested', profile['thinking_mode'])
                self.assertEqual(profile['profile_source']['mode'], 'common')

    def test_research_registration_is_temporary_even_on_failure(self):
        model = 'lfm2.5-1.2b-instruct'
        before = dict(FILES)
        with self.assertRaisesRegex(RuntimeError, 'test_exit'):
            with research_registration(model):
                self.assertIn(model, FILES)
                self.assertTrue(FILES[model][3].startswith('https://huggingface.co/'))
                raise RuntimeError('test_exit')
        self.assertEqual(FILES, before)

    def test_changed_or_unpinned_metadata_is_rejected(self):
        original = json.loads(NEW_CATALOG.read_text())
        for field, value in [('sha256', 'bad'), ('revision', 'main'), ('filename', '../bad.gguf')]:
            data = copy.deepcopy(original)
            data['models'][0]['artifact'][field] = value
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'catalog.json'
                path.write_text(json.dumps(data))
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'unpinned_expansion_artifact'):
                    pinned_models(path)

    def test_penalties_and_native_kwargs_do_not_replace_the_document_payload(self):
        case = Candidate('opaque', 'doc', 'value', 0, 5)
        args = argparse.Namespace(model='fixture', response_format='grammar',
            output_tokens=4096, temperature=0.1, top_p=.95, top_k=50, min_p=0.0, seed=42,
            extra_sampling={'repeat_penalty': 1.1, 'presence_penalty': 1.5},
            template_kwargs={'enable_thinking': True, 'reasoning_effort': 'low'})
        request = request_for(args, 'D', [case], {'doc': 'value'}, prompts('General policy.'), [])
        value = json.loads(request['messages'][1]['content'])
        self.assertEqual(value['document'], 'value')
        self.assertEqual(value['items'][0]['value'], 'value')
        self.assertEqual(request['repeat_penalty'], 1.1)
        self.assertEqual(request['presence_penalty'], 1.5)
        self.assertEqual(request['chat_template_kwargs'], args.template_kwargs)
        args.extra_sampling = {'messages': 1}
        with self.assertRaisesRegex(ValueError, 'invalid_extra_sampling'):
            request_for(args, 'D', [case], {'doc': 'value'}, prompts('policy'), [])

    def test_native_token_count_uses_exact_requested_template_kwargs(self):
        client = ProfiledClient.__new__(ProfiledClient)
        client.mode = 'grammar'
        client.args = argparse.Namespace(template_kwargs={'enable_thinking': True, 'reasoning_effort': 'low'})
        client.worker = argparse.Namespace(prompt_token_count=Mock(return_value=42))
        self.assertEqual(client.tokens('prompt', {'document': 'source'}, {}, True), 42)
        self.assertEqual(client.worker.prompt_token_count.call_args.args[2], client.args.template_kwargs)

    def test_prepare_new_model_binds_artifact_and_three_way_oracle_without_inference(self):
        before = dict(FILES)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch('evaluate_privacy_expansion.execute') as inference, redirect_stdout(StringIO()):
                main(['--prepare-only', '--model', 'lfm2.5-2.6b', '--variants', 'A', 'D',
                      '--output', str(output)])
            inference.assert_not_called()
            plan = json.loads((output / 'plan.json').read_text())
            self.assertEqual((len(plan['requests']), plan['variant_order']), (8, ['A', 'D']))
            self.assertEqual(plan['signature']['quantization'], 'Q4_K_M')
            self.assertEqual(plan['signature']['settings']['extra_sampling'], {'repeat_penalty': 1.1})
            self.assertEqual(plan['signature']['quality_contract']['reference_class_counts'],
                             {'mask': 14, 'keep': 14, 'review': 4})
            self.assertEqual(plan['signature']['annotations_sha256'], hashlib.sha256(
                (ROOT / 'tests/fixtures/privacy-policy-holdout-v1/cases.json').read_bytes()).hexdigest())
            for row in plan['requests']:
                value = json.loads(row['request']['messages'][1]['content'])
                for item in value['items']:
                    self.assertFalse(set(item) & {'expected_verdict', 'kind', 'scope', 'label', 'file'})
            self.assertIsNone(plan['historical_reference'])
        self.assertEqual(FILES, before)

    def test_native_execution_is_blocked_while_another_model_is_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('evaluate_privacy_expansion.runtime_processes', return_value=[123]), \
                    patch('evaluate_privacy_expansion.execute') as inference:
                with self.assertRaisesRegex(ValueError, 'another_benchmark_runtime_is_active'):
                    main(['--preflight-only', '--model', 'lfm2.5-1.2b-instruct',
                          '--model-dir', temporary, '--runtime', str(Path(temporary) / 'llama-server'),
                          '--output', str(Path(temporary) / 'result')])
            inference.assert_not_called()

    def test_long_http_wait_is_distinct_from_worker_configuration_limit(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            main(['--prepare-only', '--model', 'gemma4-e4b-qat', '--profile', 'common',
                  '--variants', 'D', '--output', temporary])
            plan = json.loads((Path(temporary) / 'plan.json').read_text())
        settings = plan['signature']['settings']
        self.assertEqual(settings['timeout'], 3600)
        self.assertEqual(settings['worker_configuration_timeout_seconds'], 1800)
        self.assertTrue(all('timeout' not in row['request'] for row in plan['requests']))

    def test_new_model_calibration_uses_original_binary_controls_without_gold_input(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            main(['--prepare-only', '--dataset-kind', 'calibration', '--model', 'lfm2.5-1.2b-instruct',
                  '--output', temporary])
            plan = json.loads((Path(temporary) / 'plan.json').read_text())
        self.assertEqual((plan['control_cases']['reference_private'], plan['control_cases']['reference_public']), (16, 16))
        self.assertEqual(plan['signature']['quality_contract']['dataset'], 'calibration-controls-v1')
        self.assertEqual(len(plan['requests']), 20)
        self.assertIsNone(plan['historical_reference'])
        for request in plan['requests']:
            value = json.loads(request['request']['messages'][1]['content'])
            for item in value['items']:
                self.assertFalse(set(item) & {'expected_verdict', 'reference_private', 'kind', 'scope'})

    def test_tiny_calibration_preparation_does_not_claim_the_long_document_fits(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            main(['--prepare-only', '--dataset-kind', 'calibration', '--model', 'tiny-aya-global',
                  '--output', temporary])
            plan = json.loads((Path(temporary) / 'plan.json').read_text())
        self.assertEqual(plan['signature']['settings']['context_tokens'], 8192)
        self.assertEqual(plan['token_budget_validation'], 'pending_runtime')
        self.assertFalse(plan['model_sha256_verified'])


if __name__ == '__main__':
    unittest.main()
