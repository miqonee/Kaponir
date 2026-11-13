"""Failure-path tests: transport and budget failures must not erase candidates."""
import argparse
import json
from pathlib import Path
from kaponir_worker import FILES, MODEL_ALIASES
import unittest
from unittest.mock import patch

from evaluate_validation_schemes import Experiment
from experiment_inference import LocalClient, sampling_settings
from validation_pipeline import Candidate


class SamplingTests(unittest.TestCase):
    def test_invalid_sampling_settings_are_rejected(self):
        for value in [{'temperature':float('nan')},{'temperature':True},{'top_p':0},
                      {'top_k':1.5},{'seed':-1},{'messages':[]}]:
            with self.assertRaises(ValueError):sampling_settings(value)

    def test_requested_sampling_is_sent_without_changing_context_or_schema(self):
        client=LocalClient.__new__(LocalClient)
        client.worker=argparse.Namespace(prompt_token_count=lambda *a:10,
            cfg={'ocr_timeout_seconds':1},ocr_base='http://fixture',resources=lambda:{})
        client.output_mode='grammar';client.planning_schema={};client.records=[]
        client.sampling=sampling_settings({'temperature':1,'top_p':0.95,'top_k':64,'seed':42})
        reply={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        with patch('experiment_inference.http_json',return_value=reply) as request:
            client.infer('fixture',{'source':'quoted'}, {'type':'object'},1600)
        body=request.call_args.args[1]
        self.assertEqual(body['temperature'],1)
        self.assertEqual(body['seed'],42)
        self.assertEqual(body['top_k'],64)
        self.assertEqual(body['max_tokens'],1600)
        self.assertEqual(body['response_format']['json_schema']['schema'],{'type':'object'})


class ModelCatalogTests(unittest.TestCase):
    def test_download_artifacts_match_frozen_comparison_manifest(self):
        path = Path(__file__).resolve().parent.parent/'tests/fixtures/validation-models.json'
        for model in json.loads(path.read_text())['models']:
            if model.get('builtin'):
                self.assertIn(model['id'], FILES)
            artifact = FILES[model['id']]
            self.assertEqual(artifact[:3], (model['file'], model['bytes'], model['sha']))
            self.assertEqual(artifact[3], model['url'].replace('/blob/', '/resolve/'))
            self.assertEqual(MODEL_ALIASES[model['id']], model['id'])


class RegionFailureTests(unittest.TestCase):
    def experiment(self):
        args = argparse.Namespace(max_candidates=2, hint_tokens=0, context_policy='prefix',
                                  max_fragments=4, output_tokens=100,
                                  reserve_tokens=10, context_tokens=1000)
        client = argparse.Namespace(token_count=len, input_tokens=lambda *a: 1)
        e = Experiment(client, args, {'doc': 'private-value\n'}, ['CUSTOM'], {}, lambda: None)
        return e, Candidate('id', 'doc', 'private-value', 0, 13)

    def test_planning_failure_preserves_original_as_pending(self):
        e, c = self.experiment()
        with patch('evaluate_validation_schemes.plan_text', side_effect=ValueError('document_fragment_limit')):
            values, regions = e.extract_regions([c])
        self.assertEqual(values[0].value, c.value)
        self.assertEqual(values[0].origin, 'region_pending')
        self.assertEqual(regions[0]['error'], 'document_fragment_limit')

    def test_transport_failure_preserves_original_as_pending(self):
        e, c = self.experiment()
        e.infer = lambda *a: {'error': 'TimeoutError'}
        values, regions = e.extract_regions([c])
        self.assertEqual(values[0].origin, 'region_pending')
        self.assertEqual(regions[0]['error'], 'TimeoutError')

    def test_empty_success_is_distinct_from_failed_extraction(self):
        e, c = self.experiment()
        e.infer = lambda *a: {'reply': {'candidates': []}}
        values, regions = e.extract_regions([c])
        self.assertEqual(values, [])
        self.assertIsNone(regions[0]['error'])

    def test_protected_candidate_survives_empty_extraction(self):
        e, c = self.experiment()
        c.protected = True
        e.infer = lambda *a: {'reply': {'candidates': []}}
        values, _ = e.extract_regions([c])
        self.assertEqual(values, [c])


if __name__ == '__main__':
    unittest.main()
