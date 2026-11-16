from pathlib import Path
import shutil
import tempfile
import unittest

from check_docs import check, targets
from release_check import qualify_source_links


class ReleaseDocumentationTests(unittest.TestCase):
    def test_local_docs_stay_local_and_omitted_sources_are_bound_to_the_release_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source';package = Path(temporary) / 'package'
            (root / 'docs').mkdir(parents=True);(root / 'scripts').mkdir()
            (root / 'scripts/tool.py').write_text('# synthetic source\n')
            (root / 'docs/other.md').write_text('# Kept heading\n')
            original = ('# Guide\n\n`../scripts/tool.py` [source]( <../scripts/tool.py> "code")\n'
                        '[local](other.md#kept-heading) [web](https://example.org/)\n'
                        '```md\n[example](../scripts/tool.py)\n```\n')
            (root / 'docs/guide.md').write_text(original)
            shutil.copytree(root / 'docs', package / 'docs')
            self.assertFalse(check(root))
            self.assertEqual(len(check(package)), 1)
            revision = 'a' * 40
            self.assertEqual(qualify_source_links(root, package, revision), 1)
            text = (package / 'docs/guide.md').read_text()
            self.assertIn('`../scripts/tool.py` [source]( <https://github.com/miqonee/Kaponir/blob/'
                          + revision + '/scripts/tool.py> "code")', text)
            self.assertIn('[local](other.md#kept-heading)', text)
            self.assertIn('```md\n[example](../scripts/tool.py)', text)
            self.assertEqual((root / 'docs/guide.md').read_text(), original)
            self.assertFalse(check(package))

    def test_missing_original_files_remain_errors_and_href_spans_do_not_rewrite_plain_text(self):
        line = '`one (file).py` [target]( <one (file).py> "title")\n'
        raw, start, end = next(targets(line, with_spans=True))
        self.assertEqual(raw, 'one (file).py')
        self.assertEqual(line[start:end], raw)
        self.assertGreater(start, line.index('[target]'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source';package = Path(temporary) / 'package'
            root.mkdir();package.mkdir()
            text = '# Guide\n[missing](missing.md)\n'
            (root / 'guide.md').write_text(text);(package / 'guide.md').write_text(text)
            self.assertEqual(qualify_source_links(root, package, 'b' * 40), 0)
            self.assertTrue(check(package))


if __name__ == '__main__':
    unittest.main()
