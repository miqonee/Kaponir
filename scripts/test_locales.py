"""Translation contract: keys, interpolation and explicitly translated HTML attributes."""
from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

ASSETS = Path(__file__).resolve().parent.parent / 'crates/kaponir-proxy/assets'

class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.keys, self.ids = [], []
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == 'id': self.ids.append(value)
            if key.startswith('data-i18n'): self.keys.append(value)

class LocaleTests(unittest.TestCase):
    def test_dictionary_keys_and_placeholders_match(self):
        ru = json.loads((ASSETS/'locales/ru.json').read_text())
        en = json.loads((ASSETS/'locales/en.json').read_text())
        self.assertEqual(set(ru), set(en))
        for key in ru:
            self.assertTrue(en[key], key)
            self.assertEqual(sorted(re.findall(r'\{\{\d+\}\}',ru[key])),
                             sorted(re.findall(r'\{\{\d+\}\}',en[key])), key)
    def test_static_labels_are_translated_and_ids_are_unique(self):
        markup=Markup();markup.feed((ASSETS/'index.html').read_text())
        en=json.loads((ASSETS/'locales/en.json').read_text())
        self.assertFalse(set(markup.keys)-set(en))
        self.assertEqual(len(markup.ids),len(set(markup.ids)),Counter(markup.ids))
