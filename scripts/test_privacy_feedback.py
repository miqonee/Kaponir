"""Occurrence labels used by the optional epoch-2 feedback editor."""
import unittest
from kaponir_worker import ground_privacy_spans


class PrivacyFeedbackTests(unittest.TestCase):
    def test_second_literal_occurrence_uses_utf8_byte_offsets(self):
        text = 'пример 🙂 alpha: alpha'
        span = {'value':'alpha', 'kind':'USER', 'action':'mask', 'occurrence':2}
        result, = ground_privacy_spans(text, {'spans':[span]})
        self.assertEqual(result['start_byte'], text.encode().rindex(b'alpha'))
        self.assertEqual(result['end_byte'], len(text.encode()))

    def test_invalid_copy_is_not_moved_to_another_occurrence(self):
        span = {'value':'a', 'kind':'USER', 'action':'keep', 'occurrence':2}
        with self.assertRaisesRegex(ValueError, 'ungrounded'):
            ground_privacy_spans('a here', {'spans':[span]})
        with self.assertRaisesRegex(ValueError, 'invalid_model_response'):
            ground_privacy_spans('aa', {'spans':[{**span, 'occurrence':True}]})
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            ground_privacy_spans('aa', {'spans':[span,span]})
