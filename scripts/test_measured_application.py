import tempfile
from pathlib import Path
import unittest

from apply_measured_privacy import coverage, run, span_coverage
from apply_scheme_masks import run_scheme, select_masks


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

    def test_span_coverage_distinguishes_partial_from_complete_using_utf8_offsets(self):
        result = span_coverage('АБ В q7', ['АБ', 'АБ В', 'q7'],
                               [{'start': 0, 'end': 4, 'original': 'АБ', 'token': 'ANON_1'}], 'ANON_1 В q7')
        self.assertEqual(result, {'private_occurrence_spans': 3,
            'private_spans_fully_covered': 1, 'private_spans_partly_covered': 1,
            'private_spans_untouched': 1})

    def test_span_coverage_rejects_character_offsets_presented_as_byte_offsets(self):
        with self.assertRaisesRegex(ValueError, 'application_masked_text_not_reconstructed'):
            span_coverage('АБ', ['АБ'], [{'start': 0, 'end': 2, 'original': 'АБ', 'token': 'X'}], 'X')

    def test_span_coverage_rejects_split_utf8_characters(self):
        with self.assertRaisesRegex(ValueError, 'application_replacement_not_grounded'):
            span_coverage('АБ', ['АБ'], [{'start': 0, 'end': 1, 'token': 'X'}], 'XБ')

    def test_span_coverage_accepts_lemma_metadata_for_inflected_names(self):
        result = span_coverage('Межсетевой', ['Межсетевой'],
            [{'start': 0, 'end': 16, 'original': 'Межсетева', 'token': 'ANON_LNAME_4'}], 'ANON_LNAME_4ой')
        self.assertEqual(result['private_spans_partly_covered'], 1)

    def test_span_coverage_rejects_overlapping_replacements(self):
        with self.assertRaisesRegex(ValueError, 'application_replacements_overlap'):
            span_coverage('one', ['one'], [{'start': 0, 'end': 3, 'original': 'one', 'token': 'X'},
                                         {'start': 1, 'end': 3, 'original': 'ne', 'token': 'Y'}], 'X')

    def test_scheme_selection_preserves_guards_without_accepting_invalid_masks(self):
        source = 'secret rejected review kept guarded secret'
        definitions = [('s', 'secret', False, {'valid': True, 'verdict': 'mask'}),
            ('bad', 'rejected', False, {'valid': False, 'verdict': 'mask'}),
            ('r', 'review', False, {'valid': True, 'verdict': 'review'}),
            ('k', 'kept', False, {'valid': True, 'verdict': 'keep'}),
            ('g', 'guarded', True, {'valid': True, 'verdict': 'keep'}),
            ('again', 'secret', False, {'valid': True, 'verdict': 'mask'})]
        candidates = [{'id': ident, 'file': 'doc', 'value': value, 'start': source.index(value),
                       'end': source.index(value) + len(value), 'protected': protected}
                      for ident, value, protected, _ in definitions]
        stage = {'candidates': candidates, 'decisions': {ident: row for ident, _, _, row in definitions}}
        selected, protected = select_masks(stage, {'doc': source})
        self.assertEqual(selected, {'doc': [{'value': 'secret', 'kind': 'CUSTOM'},
                                           {'value': 'guarded', 'kind': 'CUSTOM'}]})
        self.assertEqual(protected, 1)

    def test_scheme_selection_rejects_stale_source_even_for_keep(self):
        stage = {'candidates': [{'id': 'x', 'file': 'doc', 'value': 'private',
                                'start': 0, 'end': 7}],
                 'decisions': {'x': {'valid': True, 'verdict': 'keep'}}}
        with self.assertRaisesRegex(ValueError, 'application_target_not_grounded'):
            select_masks(stage, {'doc': 'changed'})

    def test_scheme_application_does_not_overwrite_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'evidence'
            output.mkdir()
            with self.assertRaisesRegex(ValueError, 'application_output_exists_no_overwrite'):
                run_scheme('missing', 'missing', output, 'point')


if __name__ == '__main__':
    unittest.main()
