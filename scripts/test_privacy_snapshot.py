import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from export_privacy_snapshot import ROOT, STUDY, contained, decompress, export, safe_relative


class PrivacySnapshotTests(unittest.TestCase):
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
