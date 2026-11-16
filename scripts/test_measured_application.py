import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import apply_measured_privacy as application
from apply_measured_privacy import coverage, run, span_coverage
from apply_scheme_masks import run_scheme, select_masks


class MeasuredApplicationTest(unittest.TestCase):
    def transport_fixture(self, root):
        from test_summarize_privacy_recovery import RecoverySummaryTests
        helper = RecoverySummaryTests()
        original, supplement, state, plan, corpus = helper.fixture(
            root, missing_batch=('D', 0), recovered_verdict='mask')
        state.update(finished_at_utc='2026-11-16T00:00:00Z', runtime_cleanup_seconds=0.1)
        helper.save(supplement, state, plan)
        reference = (corpus[3], {'doc': ['value-' + str(i) for i in range(16)]},
                     {'doc': ['value-' + str(i) for i in range(16, 32)]}, 'fixture')
        return helper, original, supplement, state, plan, corpus, reference

    def test_transport_application_selects_verified_new_masks_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, original, supplement, _, _, corpus, reference = self.transport_fixture(root)
            before = original.read_bytes(), supplement.read_bytes()
            with patch('summarize_privacy_recovery.load_corpus', return_value=corpus), \
                    patch.object(application, 'reference_data', return_value=reference), \
                    patch.object(application, 'apply_documents') as apply:
                application.run(original, root / 'binary', root / 'output', 'D',
                                transport_supplement=supplement)
            selected, metadata = apply.call_args.args[6:8]
            self.assertEqual({item['value'] for item in selected['doc']},
                             {'value-' + str(i) for i in range(16)})
            self.assertEqual(metadata['original_measured_decision_quality']['private_mask'], 8)
            self.assertEqual(metadata['measured_decision_quality']['private_mask'], 16)
            self.assertEqual(metadata['decision_view'], 'original_plus_transport_supplement')
            self.assertEqual(metadata['reinterpreted_received_failures'], 0)
            self.assertEqual((original.read_bytes(), supplement.read_bytes()), before)

    def test_transport_application_requires_cleanup_and_rejects_cached_decision_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper, original, supplement, state, plan, corpus, reference = self.transport_fixture(root)
            state.pop('runtime_cleanup_seconds')
            helper.save(supplement, state, plan)
            with patch('summarize_privacy_recovery.load_corpus', return_value=corpus), \
                    patch.object(application, 'reference_data', return_value=reference), \
                    patch.object(application, 'apply_documents') as apply:
                with self.assertRaisesRegex(ValueError, 'transport_application_requires_durable_cleanup'):
                    application.run(original, root / 'binary', root / 'output', 'D',
                                    transport_supplement=supplement)
                state['runtime_cleanup_seconds'] = 0.1
                next(iter(state['batches'][0]['decisions'].values()))['verdict'] = 'keep'
                helper.save(supplement, state, plan)
                with self.assertRaisesRegex(ValueError, 'supplement_stored_decisions_or_metrics_mismatch'):
                    application.run(original, root / 'binary', root / 'output', 'D',
                                    transport_supplement=supplement)
                apply.assert_not_called()

    def test_source_changed_during_validation_is_rejected_before_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, original, _, _, _, _, _ = self.transport_fixture(root)
            verified = application.load_run(original)
            def change_source(_):
                original.write_bytes(original.read_bytes() + b'\n')
                return verified
            with patch.object(application, 'load_run', side_effect=change_source), \
                    patch.object(application, 'apply_documents') as apply, \
                    self.assertRaisesRegex(ValueError, 'application_source_result_changed'):
                application.run(original, root / 'binary', root / 'output')
            apply.assert_not_called()

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
