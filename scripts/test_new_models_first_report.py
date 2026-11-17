import copy
import json
from pathlib import Path
import tempfile
import unittest

from apply_measured_privacy import coverage, span_coverage
from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS
from report_new_models_first import application_summary, render
from run_privacy_policy_suite import file_sha


class AppliedComparisonTests(unittest.TestCase):
    def fixture(self, folder):
        source = folder / 'source.json'
        source.write_text('{"synthetic_fixture":true}\n')
        measured = {'model': 'fixture', 'result_sha256': file_sha(source),
                    'rows': [{'method': 'point', 'quality': {'confirmed_matches': 0}}]}
        _, expected, public, sources, _ = load_corpus()
        docs, evidence = [], {}
        for index, name in enumerate(DOCUMENTS):
            original = sources[name]
            private = [value for file, value in expected if file == name]
            for mode in ('baseline', 'model_masks_added'):
                prefix = mode + '/' + str(index + 1).zfill(2) + '-' + Path(name).stem
                base = folder / prefix
                base.mkdir(parents=True)
                mask = {'text': original, 'replacements': []}
                for suffix, value in [('mask.json', mask), ('restore.json', {'text': original})]:
                    (base / suffix).write_text(json.dumps(value))
                (base / 'masked.txt').write_text(original)
                for path in base.iterdir():
                    evidence[path.relative_to(folder).as_posix()] = file_sha(path)
                docs.append({'file': name, 'mode': mode, 'restore_exact': True,
                    'persistent_rules_unchanged': True, 'replacements': 0,
                    **coverage(original, original, private, public.get(name, [])),
                    **span_coverage(original, private, [], original)})
        data = {'status': 'completed', 'source_phase': 'six_scheme_calibration',
                'model': 'fixture', 'variant': 'point', 'source_result_sha256': file_sha(source),
                'source_file_sha256': file_sha(source), 'binary_sha256': 'fixture-binary',
                'new_inference_calls': 0, 'user_rules_changed': 0,
                'model_predicted_type_or_scope_applied': False, 'exact_roundtrips': 6,
                'measured_decision_quality': measured['rows'][0]['quality'], 'documents': docs}
        path = folder / 'result.json'
        path.write_text(json.dumps(data))
        receipt = {'output': str(folder), 'result_sha256': file_sha(path),
                   'source_file_sha256': file_sha(source), 'exact_roundtrips': 6,
                   'evidence_files_sha256': evidence}
        job = {'source': str(source), 'archived': False, 'model': 'fixture', 'method': 'point'}
        return job, receipt, measured, data

    def test_recomputes_actual_occurrences_without_model_or_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.fixture(Path(temporary))
            result = application_summary(*args[:3], 'fixture-binary')
        self.assertEqual(result['private_spans_fully_covered'], 0)
        self.assertEqual(result['private_spans_untouched'], 193)
        self.assertEqual(result['public_occurrences_lost'], 0)
        self.assertEqual(result['exact_roundtrips'], 6)

    def test_refuses_cross_model_source_or_product_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, receipt, measured, _ = self.fixture(Path(temporary))
            with self.assertRaisesRegex(ValueError, 'different_binary'):
                application_summary(job, receipt, measured, 'other-binary')
            other = copy.deepcopy(measured)
            other['result_sha256'] = 'other-source'
            with self.assertRaisesRegex(ValueError, 'wrong_source'):
                application_summary(job, receipt, other, 'fixture-binary')
            other['model'] = 'another-model'
            with self.assertRaisesRegex(ValueError, 'wrong_identity'):
                application_summary(job, receipt, other, 'fixture-binary')

    def test_rejects_changed_coverage_even_with_updated_result_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            job, receipt, measured, data = self.fixture(folder)
            data['documents'][0]['private_spans_fully_covered'] = 1
            (folder / 'result.json').write_text(json.dumps(data))
            receipt['result_sha256'] = file_sha(folder / 'result.json')
            with self.assertRaisesRegex(ValueError, 'coverage_mismatch'):
                application_summary(job, receipt, measured, 'fixture-binary')

    def test_partial_mask_of_public_value_counts_as_public_loss(self):
        # Real corpus example: the selected value "1024" differs from the
        # public annotation "1024 байта". Exact candidate equality misses it.
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            job, receipt, measured, data = self.fixture(folder)
            name = '08-public-reference.md'
            _, expected, public, sources, _ = load_corpus()
            original = sources[name]
            start = original.index('1024 байта')
            token = 'ANON_CUSTOM_1'
            mask = {'text': original[:start] + token + original[start + 4:],
                    'replacements': [{'start': len(original[:start].encode()),
                                      'end': len(original[:start + 4].encode()), 'token': token}]}
            base = folder / 'model_masks_added/03-08-public-reference'
            (base / 'mask.json').write_text(json.dumps(mask))
            (base / 'masked.txt').write_text(mask['text'])
            for suffix in ('mask.json', 'masked.txt'):
                receipt['evidence_files_sha256'][(base / suffix).relative_to(folder).as_posix()] = file_sha(base / suffix)
            private = [value for file, value in expected if file == name]
            doc = next(doc for doc in data['documents'] if doc['file'] == name and doc['mode'] == 'model_masks_added')
            doc.update(coverage(original, mask['text'], private, public[name]))
            doc.update(span_coverage(original, private, mask['replacements'], mask['text']))
            doc['replacements'] = 1
            (folder / 'result.json').write_text(json.dumps(data))
            receipt['result_sha256'] = file_sha(folder / 'result.json')
            result = application_summary(job, receipt, measured, 'fixture-binary')
        self.assertEqual(result['public_occurrences_lost'], 1)
        self.assertEqual(result['public_reference_replacements'], 1)

    def test_pending_is_missing_and_technical_review_remains_separate(self):
        report = {'new_models_complete': 0, 'new_models_total': 8, 'application_jobs_complete': 0,
                  'exact_roundtrips': 0, 'rejected_inputs': [], 'runs': [], 'rows': [
                    {'model': 'pending', 'group': 'new_matched_reference', 'method': 'point',
                     'measurement': None, 'application': None},
                    {'model': 'measured', 'group': 'archived_other_profile', 'method': 'point',
                     'measurement': {'quality': {'confirmed_matches': 7, 'correct_values_filtered': 0},
                                     'technical_review_values': 72, 'protected_values': 10}, 'application': None}]}
        text = render(report)
        self.assertIn('### pending\n\nПолный результат ещё не получен; оценок качества нет.', text)
        self.assertIn('| Проверка списка | 7 | 0 | 72 | 10 | — | — | — | — | — |', text)
        self.assertIn('Остальные исторические профили', text)


if __name__ == '__main__':
    unittest.main()
