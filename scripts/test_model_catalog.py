"""Hugging Face registration and artifact integrity without external requests or model weights."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
from kaponir_worker import Worker, configuration, hf_reference, resolve_hf


REVISION = 'a'*40
PAYLOAD = b'GGUF'+b'fixture-weights-0123456789'


def metadata(payload=PAYLOAD, revision=REVISION):
    def file(name):
        return {'rfilename':name,'size':len(payload),'lfs':{'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}}
    return {'sha':revision,'siblings':[file('Example-Q8_0.gguf'),file('quants/Example-Q4_K_M.gguf'),
            file('mmproj-F16.gguf'),file('Example-00001-of-00002.gguf'),{'rfilename':'model.safetensors'}]}


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        self.worker=Worker(self.home,configuration(self.home),'unused')
        self.workers=[self.worker]

    def tearDown(self):
        for worker in self.workers:
            worker.stop.set();worker.stop_runtime()
            if worker.verification_thread:worker.verification_thread.join(timeout=5)
        self.temp.cleanup()

    def response(self, value):
        return patch('kaponir_worker.urllib.request.urlopen',side_effect=lambda *args,**kwargs:io.BytesIO(json.dumps(value).encode()))

    def settled(self, model, worker=None):
        worker=worker or self.worker;deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=worker.status()['models'][model]
            if state['state']!='checking':return state
            time.sleep(.01)
        self.fail('verification did not settle')

    def test_public_repository_and_file_references(self):
        self.assertEqual(hf_reference('https://hf.co/acme/model-GGUF'),('acme/model-GGUF','main',None))
        self.assertEqual(hf_reference('https://huggingface.co/acme/model/blob/'+REVISION+'/quants/model.gguf?download=true'),
                         ('acme/model',REVISION,'quants/model.gguf'))
        self.assertEqual(hf_reference('https://huggingface.co/acme/model/tree/main'),('acme/model','main',None))

    def test_untrusted_urls_cannot_change_host_or_traverse_paths(self):
        for url in ['http://huggingface.co/a/b','https://127.0.0.1/weights.gguf','https://huggingface.co.evil/a/b',
                    'https://token@huggingface.co/a/b','https://huggingface.co:443/a/b','https://huggingface.co/a/b?token=secret',
                    'https://huggingface.co/datasets/a/b','https://huggingface.co/a/b/resolve/main/%2e%2e/weights.gguf',
                    'https://huggingface.co/a/b/resolve/main/a%5cb.gguf','https://huggingface.co/a/b#secret']:
            with self.subTest(url=url),patch('kaponir_worker.urllib.request.urlopen',side_effect=AssertionError('must reject before network')):
                with self.assertRaisesRegex(ValueError,'hf_invalid_url'):resolve_hf(url)

    def test_resolution_prefers_q4_and_pins_artifact_revision(self):
        with self.response(metadata()):
            result=resolve_hf('https://huggingface.co/acme/model')
        self.assertEqual(result['selected'],'quants/Example-Q4_K_M.gguf')
        self.assertEqual(len(result['files']),2)
        self.assertIn('/resolve/'+REVISION+'/',result['files'][0]['url'])
        self.assertEqual(result['files'][0]['sha256'],hashlib.sha256(PAYLOAD).hexdigest())
        with self.response(metadata()):
            explicit=resolve_hf('https://huggingface.co/acme/model/resolve/main/Example-Q8_0.gguf')
        self.assertEqual([item['file'] for item in explicit['files']],['Example-Q8_0.gguf'])

    def test_missing_gguf_and_hash_are_actionable_errors(self):
        for value in [{'sha':REVISION,'siblings':[{'rfilename':'model.safetensors'}]},
                      {'sha':REVISION,'siblings':[{'rfilename':'weights.gguf','size':10}]}]:
            with self.response(value),self.assertRaisesRegex(ValueError,'hf_no_gguf'):resolve_hf('https://huggingface.co/acme/model')
        with self.response({'sha':REVISION,'siblings':'invalid'}),self.assertRaisesRegex(ValueError,'hf_invalid_metadata'):
            resolve_hf('https://huggingface.co/acme/model')

    def test_metadata_failures_do_not_include_provider_bodies(self):
        for code,expected in [(401,'hf_access_denied'),(404,'hf_not_found'),(503,'hf_unavailable')]:
            error=urllib.error.HTTPError('https://RAW_SECRET',code,'RAW_SECRET',{},None)
            with patch('kaponir_worker.urllib.request.urlopen',side_effect=error),self.assertRaisesRegex(ValueError,'^'+expected+'$'):
                resolve_hf('https://huggingface.co/acme/model')

    def test_pinned_revision_must_match_the_metadata(self):
        with self.response(metadata(revision='b'*40)),self.assertRaisesRegex(ValueError,'hf_invalid_metadata'):
            resolve_hf('https://huggingface.co/acme/model/resolve/'+REVISION+'/Example-Q8_0.gguf')

    def test_registration_is_durable_deduplicated_and_works_offline(self):
        with self.response(metadata()):
            model=self.worker.register_model('https://huggingface.co/acme/model')['model']
            self.assertEqual(self.worker.register_model('https://huggingface.co/acme/model')['model'],model)
        self.assertEqual(len(self.worker.custom_models),1)
        self.assertEqual(self.settled(model)['state'],'missing')
        self.worker.runtime.parent.mkdir(parents=True);self.worker.runtime.write_text('fixture');self.worker.runtime.chmod(0o755)
        job=self.worker.submit('download',model=model)
        with patch('kaponir_worker.urllib.request.urlopen',side_effect=lambda *args,**kwargs:io.BytesIO(PAYLOAD)):
            self.worker.install(job['id'],model)
        self.assertTrue(self.settled(model)['downloaded'])
        recovered=Worker(self.home,configuration(self.home),'unused');self.workers.append(recovered)
        with patch('kaponir_worker.urllib.request.urlopen',side_effect=AssertionError('offline')):
            state=self.settled(model,recovered)
            self.assertTrue(state['downloaded']);self.assertIn(model,[item['id'] for item in recovered.status()['catalog']])
            self.assertTrue(Path(state['path']).is_relative_to(self.worker.models))
            self.assertEqual(recovered.submit('evaluate',model=model)['model'],model)

    def test_changed_revision_gets_separate_id_and_local_file(self):
        with self.response(metadata()):first=self.worker.register_model('https://huggingface.co/acme/model')['model']
        with self.response(metadata(revision='b'*40)):second=self.worker.register_model('https://huggingface.co/acme/model')['model']
        self.assertNotEqual(first,second)
        self.assertNotEqual(self.worker.model_files()[first][0],self.worker.model_files()[second][0])

    def test_disguised_file_cannot_become_ready(self):
        data=b'not-GGUF-but-valid-checksum'
        with self.response(metadata(data)):model=self.worker.register_model('https://huggingface.co/acme/model')['model']
        job=self.worker.submit('download',model=model)
        with patch('kaponir_worker.urllib.request.urlopen',side_effect=lambda *args,**kwargs:io.BytesIO(data)):
            with self.assertRaisesRegex(ValueError,'invalid_gguf'):self.worker.install(job['id'],model)
        self.assertFalse((self.worker.models/self.worker.model_files()[model][0]).exists())
        self.assertFalse(list(self.worker.models.glob('*.part')))
        # A matching checksum on a manually copied file still requires the GGUF header.
        path=self.worker.models/self.worker.model_files()[model][0];path.write_bytes(data)
        with patch('kaponir_worker.urllib.request.urlopen',side_effect=AssertionError('offline')):
            with self.assertRaisesRegex(ValueError,'invalid_gguf'):self.worker.install(job['id'],model)
        self.assertEqual(self.settled(model)['state'],'invalid')


if __name__=='__main__':unittest.main()
