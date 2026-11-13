"""Published evidence must match checkpoints and exclude unfinished/live runs."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from export_validation_evidence import export


class EvidenceTests(unittest.TestCase):
    def test_round_trip_hash_and_incomplete_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';output=root/'output'
            finished=source/'qwen08-calibration/result.json';finished.parent.mkdir(parents=True)
            raw=json.dumps({'status':'completed','signature':{},'shadow':True,'rules_changed':0}).encode()
            finished.write_bytes(raw)
            unfinished=source/'qwen-calibration/result.json';unfinished.parent.mkdir(parents=True)
            unfinished.write_text(json.dumps({'status':'running'}))
            result=export(source,output)
            self.assertEqual(set(result['files']),{'qwen08-calibration/result.json'})
            row=result['files']['qwen08-calibration/result.json']
            self.assertEqual(gzip.decompress((output/row['archive']).read_bytes()),raw)
            self.assertEqual(row['json_sha256'],hashlib.sha256(raw).hexdigest())

    def test_run_with_user_rule_writes_cannot_be_archived(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source'
            path=source/'qwen08-calibration/result.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'status':'completed','signature':{},'shadow':True,'rules_changed':1}))
            with self.assertRaisesRegex(ValueError,'only_shadow_experiments'):
                export(source,root/'output')

    def test_interrupted_truncation_diagnostic_keeps_its_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source'
            path=source/'context-controls/qwen08-native-unbounded/result.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'status':'interrupted','signature':{},'shadow':True,'rules_changed':0}))
            result=export(source,root/'output')
            self.assertEqual(result['files'][path.relative_to(source).as_posix()]['status'],'interrupted')


if __name__=='__main__':unittest.main()
