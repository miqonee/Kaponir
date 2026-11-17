import gzip
from io import BytesIO
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from export_privacy_snapshot import ROOT, STUDY, contained, decompress, export, records, safe_relative, verify


class PrivacySnapshotTests(unittest.TestCase):
    def test_explicit_resumed_queue_preserves_the_original_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'original/result.json'
            config = root / 'complete-v1';config.mkdir()
            (config / 'config.json').write_text(json.dumps({'reference_results': {'gemma4-e4b-qat': str(original)}}))
            (config / 'status.json').write_text(json.dumps({'jobs': {}}))
            for directory in ('recovery-v1', 'resumed'):
                path = root / directory;path.mkdir()
                (path / 'queue.json').write_text(json.dumps({'runs': [{'result': str(original),
                    'producer_root': str(root / 'test-producer'), 'output': str(path / 'gemma')}]}))
            old = (root / 'recovery-v1/queue.json').read_bytes()
            with patch('export_privacy_snapshot.build_jobs', return_value=[]):
                rows = records(root, root / 'resumed/queue.json')
            self.assertEqual(rows[-1]['path'], root / 'resumed/gemma/recovery-result.json')
            self.assertEqual((root / 'recovery-v1/queue.json').read_bytes(), old)

    def test_continuation_archive_can_be_rescored_after_original_paths_are_removed(self):
        from test_summarize_privacy_recovery import RecoverySummaryTests
        import summarize_privacy_recovery as recovery
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);study = root / 'study';study.mkdir()
            path, _, _, corpus = RecoverySummaryTests().continuation_fixture(study)
            original = study / 'original/result.json'
            producer = study / 'test-producer';(producer / 'scripts').mkdir(parents=True)
            for name in ('privacy_policy_experiment.py', 'validation_pipeline.py'):
                shutil.copyfile(ROOT / 'scripts' / name, producer / 'scripts' / name)
            rows = [{'id': 'original/gemma', 'model': 'gemma4-e4b-qat', 'kind': 'calibration',
                     'path': original, 'producer': producer},
                    {'id': 'recovery/gemma', 'model': 'gemma4-e4b-qat', 'kind': 'recovery',
                     'path': path, 'producer': producer, 'reference': str(original)}]
            stream = BytesIO()
            with tarfile.open(fileobj=stream, mode='w'):
                pass
            output = root / 'snapshot'
            with patch('export_privacy_snapshot.STUDY', study), \
                    patch('export_privacy_snapshot.records', return_value=rows), \
                    patch('export_privacy_snapshot.producer_bundle', return_value=(gzip.compress(stream.getvalue()), {})), \
                    patch.object(recovery, 'load_corpus', return_value=corpus):
                manifest = export(study, output)
                shutil.rmtree(study)
                checked = verify(output, rescore=True)
            self.assertEqual(checked['runs'], 2)
            self.assertIn('continuation-source/recovery-result.json', manifest['runs'][1]['files'])
            self.assertIn('continuation-source/recovery-plan.json', manifest['runs'][1]['files'])

    def test_archive_paths_and_sources_cannot_escape_their_declared_root(self):
        for value in ('../private', '/private', 'run/../../private', 'run\\..\\private', './run', ''):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'unsafe_snapshot_relative_path'):
                safe_relative(value)
        self.assertEqual(safe_relative('fresh-native-old/gemma4-e4b-qat'), 'fresh-native-old/gemma4-e4b-qat')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);allowed = root / 'allowed';allowed.mkdir()
            foreign = root / 'private.txt';foreign.write_text('not an eligible source')
            link = allowed / 'link';link.symlink_to(foreign)
            for path in (foreign, link):
                with self.assertRaisesRegex(ValueError, 'snapshot_source_outside_allowed_tree'):
                    contained(path, allowed)

    def test_gzip_keeps_exact_invalid_json_bytes_and_foreign_studies_are_refused(self):
        raw = b'{"action":"mask","action":"keep"}\n'
        self.assertEqual(decompress(gzip.compress(raw, mtime=0)), raw)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'only_explicit_synthetic_phase2_study_allowed'):
                export(root, root / 'export')
            self.assertFalse((root / 'export').exists())

    def test_existing_snapshots_and_the_original_benchmark_are_never_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            original = destination / 'evidence.txt';original.write_text('existing bytes')
            with self.assertRaisesRegex(ValueError, 'snapshot_output_exists_no_overwrite'):
                export(STUDY, destination)
            self.assertEqual(original.read_text(), 'existing bytes')
            forbidden = ROOT / 'benchmark' / ('forbidden-' + destination.name)
            with patch('export_privacy_snapshot.records') as read_sources:
                with self.assertRaisesRegex(ValueError, 'old_benchmark_must_stay_immutable'):
                    export(STUDY, forbidden)
                read_sources.assert_not_called()
            self.assertFalse(forbidden.exists())


if __name__ == '__main__':
    unittest.main()
