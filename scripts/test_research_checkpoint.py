import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checkpoint_privacy_research import create, verify, verify_applications
from run_privacy_policy_suite import atomic_json, file_sha
import test_new_models_first_report as application_fixtures


class ResearchCheckpointTests(unittest.TestCase):
    def minimal(self, root):
        raw = b'{"status":"running","calls":[]}\n'
        archive = root/'evidence/active.json.gz';archive.parent.mkdir()
        archive.write_bytes(gzip.compress(raw,mtime=0))
        atomic_json(root/'legacy/manifest.json', {'runs': []})
        manifest = {'version': 1, 'new_inference_calls': 0, 'user_rules_changed': 0,
            'closed_schemes': [], 'captured': [{'source': 'active/result.json',
                'archive': 'evidence/active.json.gz', 'raw_bytes': len(raw),
                'raw_sha256': hashlib.sha256(raw).hexdigest()}],
            'files': {p.relative_to(root).as_posix(): {'sha256': file_sha(p), 'bytes': p.stat().st_size}
                      for p in root.rglob('*') if p.is_file()}}
        atomic_json(root/'manifest.json',manifest)
        return archive

    def test_partial_payload_is_preserved_without_becoming_a_completed_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.minimal(root)
            with patch('checkpoint_privacy_research.verify_legacy', return_value={'runs':0}):
                result=verify(root,rescore=True)
            self.assertEqual(result['closed_new_scheme_models'],0)
            self.assertEqual(result['new_inference_calls'],0)

    def test_changed_artifact_and_existing_checkpoint_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);archive=self.minimal(root);archive.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'checkpoint_file_changed'):verify(root)
            with self.assertRaisesRegex(ValueError,'exists_no_overwrite'):create(root)

    def test_saved_application_is_rescored_with_source_and_restore_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);_,_,measured,data=application_fixtures.AppliedComparisonTests().fixture(root)
            self.assertEqual(verify_applications(root,{measured['result_sha256']}),(1,6))
            with self.assertRaisesRegex(ValueError,'source_missing'):verify_applications(root,set())
            data['documents'][0]['private_spans_fully_covered']=1
            atomic_json(root/'result.json',data)
            with self.assertRaisesRegex(ValueError,'metrics_changed'):
                verify_applications(root,{measured['result_sha256']})


if __name__ == '__main__':unittest.main()
