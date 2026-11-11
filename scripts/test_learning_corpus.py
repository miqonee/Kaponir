"""Regression checks for benchmark scoring and integrity, without running a model."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from check_learning_corpus import compare, load_corpus, occurrences, read_predictions, validate_estimate


class LearningCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus, cls.expected, cls.public, cls.sources, cls.stats = load_corpus()

    def test_exact_predictions_have_full_discovery_and_categorization(self):
        report = compare(self.expected, self.expected, self.public, self.sources)
        self.assertEqual(report['discovery_recall'], 1)
        self.assertEqual(report['prediction_precision'], 1)
        self.assertEqual(report['categorization']['joint_accuracy'], 1)
        self.assertFalse(report['missing'])
        self.assertFalse(report['extra'])
        control = next(r for r in report['by_document'] if r['file'] == '08-public-reference.md')
        self.assertEqual(control['predicted'], 0)
        self.assertIsNone(control['recall'])

    def test_discovery_errors_are_separate_from_category_errors(self):
        actual = dict(self.expected)
        missing = next(iter(actual)); del actual[missing]
        wrong_kind = next(iter(actual)); actual[wrong_kind] = ('SECRET', actual[wrong_kind][1])
        wrong_scope = next(k for k in actual if k != wrong_kind)
        actual[wrong_scope] = (actual[wrong_scope][0], 'global' if actual[wrong_scope][1] != 'global' else 'personal')
        actual[('08-public-reference.md', 'Python')] = ('PROJ', 'team')
        actual[('08-public-reference.md', 'InventedInternalProject')] = ('PROJ', 'team')
        report = compare(self.expected, actual, self.public, self.sources)
        self.assertEqual(len(report['missing']), 1)
        self.assertEqual(len(report['extra']), 2)
        self.assertEqual(len(report['public_false_positives']), 1)
        self.assertEqual(len(report['not_in_source']), 1)
        self.assertEqual(len(report['wrong_category']), 2)

    def test_detection_only_does_not_claim_category_accuracy(self):
        actual = {key: (None, None) for key in self.expected}
        report = compare(self.expected, actual, self.public, self.sources)
        self.assertEqual(report['discovery_recall'], 1)
        self.assertEqual(report['categorization']['missing'], len(actual))
        self.assertEqual(report['categorization']['scored'], 0)
        self.assertIsNone(report['categorization']['kind_accuracy'])

    def test_empty_results_and_boundary_errors_are_visible(self):
        empty = compare(self.expected, {}, self.public, self.sources)
        self.assertEqual(empty['discovery_recall'], 0)
        self.assertIsNone(empty['prediction_precision'])
        actual = {('01-cost-estimate.md', 'Кедровый'): ('PROJ', 'team')}
        report = compare(self.expected, actual, self.public, self.sources)
        self.assertIn('Кедровый Атлас', report['boundary_mismatches'][0]['related_expected'])
        self.assertFalse(report['not_in_source'])
        self.assertEqual(len(occurrences('s1 s10 xs1 7 500,00 ₽ 17 500,00 ₽', 's1')), 1)
        self.assertEqual(len(occurrences('7 500,00 ₽ 17 500,00 ₽', '7 500,00 ₽')), 1)

    def test_api_results_duplicates_and_unknown_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            row = {'source': '02-application.log', 'value': 'Reed!62Cove#918', 'kind': 'SECRET', 'scope': 'personal'}
            path.write_text(json.dumps({'entries': [row, row]}))
            self.assertEqual(len(read_predictions(path, self.sources)), 1)
            path.write_text(json.dumps({'entries': [row, {**row, 'kind': 'PROJ'}]}))
            with self.assertRaisesRegex(ValueError, 'Conflicting categories'):
                read_predictions(path, self.sources)
            path.write_text(json.dumps([{'file': 'other.md', 'value': 'x'}]))
            with self.assertRaisesRegex(ValueError, 'Unknown document'):
                read_predictions(path, self.sources)

    def test_modified_source_is_rejected_even_when_labels_still_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'input').mkdir()
            manifest = deepcopy(self.corpus)
            manifest['documents'] = [manifest['documents'][0]]
            name = manifest['documents'][0]['file']
            (root / 'expected.json').write_text(json.dumps(manifest))
            (root / 'input' / name).write_text(self.sources[name] + '\nAdditional prose.\n')
            with self.assertRaisesRegex(ValueError, 'source digest changed'):
                load_corpus(root)

    def test_estimate_arithmetic_catches_an_incorrect_line_total(self):
        doc = self.corpus['documents'][0]
        validate_estimate(self.sources[doc['file']], doc['financial_check'])
        changed = self.sources[doc['file']].replace('48 600,00 ₽ |', '48 601,00 ₽ |', 1)
        with self.assertRaisesRegex(ValueError, 'price × quantity'):
            validate_estimate(changed, doc['financial_check'])


if __name__ == '__main__':
    unittest.main()
