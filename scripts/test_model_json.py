import json
import unittest

from kaponir_inference import strict_json


class ModelJsonTests(unittest.TestCase):
    def test_duplicates_cannot_override_decisions_at_any_depth(self):
        for value in ['{"decisions":[{"verdict":"mask","verdict":"keep"}]}',
                      '{"decisions":[],"decisions":[{}]}', '{"id":"one","\\u0069d":"two"}']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'duplicate_json_key'):
                strict_json(value)

    def test_non_finite_numbers_and_unframed_text_are_rejected(self):
        for value in ['{"confidence":NaN}', '{"confidence":Infinity}', '{"confidence":1e9999}',
                      '{} trailing', '```json\n{}\n```']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json(value)

    def test_valid_unicode_arrays_and_numbers_keep_their_meaning(self):
        value = '{"values":[null,true,-4,0.75,"цена","NaN"],"other":{"id":"two"}}'
        self.assertEqual(strict_json(value), json.loads(value))


if __name__ == '__main__':
    unittest.main()
