import copy
from contextlib import redirect_stdout
import gzip
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS, ROOT
from evaluate_validation_schemes import detector_pool_from_export, frozen_detector_pool, main
from experiment_inference import sampling_settings
from kaponir_worker import FILES


EXPORT = ROOT / 'benchmark/evidence/artifacts/validation-schemes/gemma-calibration/detectors.json.gz'


class FrozenDetectorPoolTests(unittest.TestCase):
    def sources(self):
        _, _, _, sources, _ = load_corpus()
        return {name: sources[name] for name in DOCUMENTS}

    def test_frozen_export_reconstructs_exact_original_pool_without_execution(self):
        expected = json.loads(gzip.decompress((ROOT / 'benchmark/data/validation/minicpm-calibration--result.json.gz').read_bytes()))
        with patch('evaluate_validation_schemes.subprocess.run') as process:
            pool, data = frozen_detector_pool(EXPORT, self.sources())
        process.assert_not_called()
        self.assertEqual([row.json() for row in pool], expected['candidate_pool'])
        self.assertEqual(len(pool), 94)
        self.assertIs(data['s1'], False)
        self.assertIs(data['rules'], False)

    def test_wrong_document_pool_or_changed_values_are_rejected(self):
        data = json.loads(gzip.decompress(EXPORT.read_bytes()))
        wrong = copy.deepcopy(data)
        wrong['documents'].pop()
        with self.assertRaisesRegex(ValueError, 'detector_export_document_mismatch'):
            detector_pool_from_export(wrong, self.sources())
        wrong = copy.deepcopy(data)
        wrong['documents'][0]['candidates'][0]['value'] = 'not the source value'
        with self.assertRaisesRegex(ValueError, 'detector_offset_mismatch'):
            detector_pool_from_export(wrong, self.sources())
        for flag in ('s1', 'rules'):
            wrong = copy.deepcopy(data)
            wrong[flag] = True
            with self.assertRaisesRegex(ValueError, 'detector_export_must_be_local_and_rule_free'):
                detector_pool_from_export(wrong, self.sources())

    def test_invalid_boolean_flags_are_not_silently_accepted(self):
        data = json.loads(gzip.decompress(EXPORT.read_bytes()))
        data['documents'][0]['candidates'][0]['protected'] = 'false'
        with self.assertRaisesRegex(ValueError, 'invalid_detector_candidate'):
            detector_pool_from_export(data, self.sources())

    def test_legacy_entrypoint_identifies_frozen_origin_and_honors_hooks(self):
        class FakeWorker:
            def __init__(self, *_):
                self.stop = threading.Event()
                self.process_supports_json_schema = True
            def model_files(self):
                return FILES
            def canonical_model(self, model):
                return model
            def stop_runtime(self):
                return None
        client = SimpleNamespace(sampling=sampling_settings(), output_mode='grammar', records=[],
            input_tokens=lambda *_: 1, token_count=len, infer=lambda *_: {'decisions': []})
        hooks, checkpoints = [], []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'runtime'
            runtime.write_bytes(b'synthetic parser-test runtime; never executed')
            with patch('evaluate_validation_schemes.Worker', FakeWorker), redirect_stdout(StringIO()):
                main(['--model', 'gemma4-e4b-qat', '--model-dir', str(root), '--runtime', str(runtime),
                      '--frozen-detectors', str(EXPORT), '--output', str(root / 'output'), '--schemes', 'point'],
                     client_factory=lambda *_: client, worker_hook=lambda worker: hooks.append(worker),
                     checkpoint_hook=lambda state: checkpoints.append(state['status']),
                     experiment_lineage={'role': 'synthetic parser test'})
            state = json.loads((root / 'output/result.json').read_text())
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['signature']['detector_pool_source']['mode'], 'frozen_export_not_new_detector_execution')
        self.assertIsNone(state['detector_binary_sha256'])
        self.assertEqual(state['signature']['experiment_lineage']['role'], 'synthetic parser test')
        self.assertEqual(len(hooks), 1)
        self.assertIn('completed', checkpoints)


if __name__ == '__main__':
    unittest.main()
