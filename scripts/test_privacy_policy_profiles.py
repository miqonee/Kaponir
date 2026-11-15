import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import privacy_policy_profiles as profiles


ORDER = ['gemma4-e4b-qat', 'qwen3.5-2b', 'qwen3.5-0.8b',
         'huihui-qwen3.5-2b-abliterated', 'minicpm5-2b', 'granite42-3b', 'nanbeige42-3b']


class PrivacyPolicyProfilesTests(unittest.TestCase):
    def source(self):
        return json.loads(profiles.PROFILE_PATH.read_text())

    def test_profile_order_and_source_hash_bind_all_settings(self):
        rows = list(profiles.iter_profiles())
        self.assertEqual([row['model'] for row in rows], ORDER)
        expected_hash = hashlib.sha256(profiles.PROFILE_PATH.read_bytes()).hexdigest()
        for row in rows:
            with self.subTest(model=row['model']):
                self.assertEqual(row['profile_source']['sha256'], expected_hash)
                self.assertEqual(row['profile_source']['path'], 'docs/experiments/phase2-model-profiles.json')
                self.assertTrue(row['fresh_A_required'])
                self.assertEqual((row['context_tokens'], row['output_tokens'], row['retry_output_tokens']),
                                 (16384, 4096, 8192))
                self.assertEqual((row['reasoning_budget'], row['batch_size'], row['threads']), (2048, 8, 2))

    def test_all_references_identify_their_own_model_and_pinned_archive(self):
        expected = {
            'gemma4-e4b-qat': (7, 9, 16, 0),
            'qwen3.5-2b': (0, 10, 7, 15),
            'qwen3.5-0.8b': (0, 5, 3, 24),
            'minicpm5-2b': (2, 12, 13, 4),
            'granite42-3b': (0, 3, 8, 21),
            'nanbeige42-3b': (0, 0, 4, 28)}
        for model, counts in expected.items():
            with self.subTest(model=model):
                reference = profiles.historical_reference(model)
                self.assertEqual(reference['model'], model)
                self.assertEqual(reference['settings']['model'], model)
                path = profiles.ROOT / reference['path']
                compressed = path.read_bytes()
                self.assertEqual(reference['archive_sha256'], hashlib.sha256(compressed).hexdigest())
                self.assertEqual(reference['json_sha256'], hashlib.sha256(gzip.decompress(compressed)).hexdigest())
                self.assertEqual(reference['method'], 'review-document-bounded')
                actual = reference['action_counts']
                self.assertEqual(tuple(actual[key] for key in
                    ['private_mask', 'private_keep', 'public_keep', 'review']), counts)
                self.assertEqual(actual['private_cases'] + actual['public_cases'], 32)

    def test_huihui_has_no_invented_full_document_baseline(self):
        self.assertIsNone(profiles.historical_reference('huihui-qwen3.5-2b-abliterated'))
        row = profiles.profile('huihui-qwen3.5-2b-abliterated')
        self.assertIsNone(row['baseline_archive'])
        self.assertFalse(row['historical_full_baseline_available'])
        self.assertIn('no_historical_baseline', row['profile_origin'])
        self.assertEqual((row['temperature'], row['timeout']), (0, 600))

    def test_native_modes_and_sampling_match_historical_profiles(self):
        for row in profiles.iter_profiles():
            if row['historical_reference'] is None:
                continue
            reference = row['historical_reference']
            self.assertEqual(row['response_format'], reference['effective_response_format'])
            for key in ['temperature', 'top_k', 'top_p', 'min_p', 'seed', 'timeout',
                        'context_tokens', 'output_tokens', 'retry_output_tokens', 'reserve_tokens', 'batch_size']:
                self.assertEqual(row[key], reference['settings'][key], (row['model'], key))
        self.assertEqual(profiles.profile('nanbeige42-3b')['response_format'], 'prompt')
        for model in ['qwen3.5-2b', 'qwen3.5-0.8b', 'minicpm5-2b']:
            self.assertEqual(profiles.profile(model)['temperature'], 0)

    def test_wrong_model_archive_wiring_is_rejected(self):
        source = self.source()
        source['models'][1]['baseline_archive'] = source['models'][0]['baseline_archive']
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'profiles.json'
            path.write_text(json.dumps(source))
            with patch.object(profiles, 'PROFILE_PATH', path):
                with self.assertRaisesRegex(ValueError, 'historical_model_mismatch'):
                    profiles.historical_reference('qwen3.5-2b')

    def test_model_artifact_identity_is_verified(self):
        path = profiles.VALIDATION_ROOT / self.source()['models'][0]['baseline_archive']
        state = json.loads(gzip.decompress(path.read_bytes()))
        state['artifact'][2] = '0' * 64
        with patch('privacy_policy_profiles.gzip.decompress', return_value=json.dumps(state).encode()):
            with self.assertRaisesRegex(ValueError, 'historical_model_artifact_mismatch'):
                profiles.historical_reference('gemma4-e4b-qat')

    def test_archive_checksums_are_verified_against_frozen_manifest(self):
        archive = self.source()['models'][0]['baseline_archive']
        compressed = (profiles.VALIDATION_ROOT / archive).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / archive).write_bytes(gzip.compress(gzip.decompress(compressed), mtime=1))
            (directory / 'manifest.json').write_bytes((profiles.VALIDATION_ROOT / 'manifest.json').read_bytes())
            with patch.object(profiles, 'VALIDATION_ROOT', directory):
                with self.assertRaisesRegex(ValueError, 'historical_archive_checksum_mismatch'):
                    profiles.historical_reference('gemma4-e4b-qat')

    def test_unknown_models_and_duplicate_profile_entries_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unknown_phase2_model'):
            profiles.profile('unknown-model')
        source = self.source()
        source['models'].append(copy.deepcopy(source['models'][0]))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'profiles.json'
            path.write_text(json.dumps(source))
            with patch.object(profiles, 'PROFILE_PATH', path):
                with self.assertRaisesRegex(ValueError, 'invalid_phase2_model_profiles'):
                    list(profiles.iter_profiles())


if __name__ == '__main__':
    unittest.main()
