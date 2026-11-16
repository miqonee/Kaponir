import tempfile
from pathlib import Path
import unittest

from apply_measured_privacy import coverage, run


class MeasuredApplicationTest(unittest.TestCase):
    def test_coverage_counts_whole_values_and_public_losses(self):
        values = coverage('q7 q73 q7_1 q7 TLS TLS', 'ANON_1 q73 q7_1 q7 TLS ANON_2',
                          ['q7'], ['TLS'])
        self.assertEqual(values, {'private_values_annotated': 1,
            'private_occurrences_before': 2, 'private_occurrences_visible': 1,
            'public_occurrences_before': 2, 'public_occurrences_lost': 1})

    def test_existing_evidence_is_rejected_before_reading_or_starting_product(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'evidence'
            output.mkdir()
            sentinel = output / 'result.json'
            sentinel.write_bytes(b'original evidence')
            with self.assertRaisesRegex(ValueError, 'application_output_exists_no_overwrite'):
                run(Path(temporary) / 'missing-result.json', Path(temporary) / 'missing-binary', output)
            self.assertEqual(sentinel.read_bytes(), b'original evidence')


if __name__ == '__main__':
    unittest.main()
