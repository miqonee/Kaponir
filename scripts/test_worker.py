"""Queue lifecycle, file boundary, download integrity and HTTP auth without models."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from kaponir_worker import Worker, configuration, handler, Interrupted, grounded_candidates, text_pages, text_chunks


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        self.root=self.home/'inbox';self.root.mkdir()
        self.worker=Worker(self.home,configuration(self.home),'unused-binary')
    def tearDown(self):
        self.worker.stop.set();self.worker.stop_runtime();self.temp.cleanup()
    def test_source_boundary_and_symlinks(self):
        (self.root/'a.txt').write_text('hello')
        outside=self.home/'outside.txt';outside.write_text('outside')
        has_symlink = True
        try:
            (self.root/'link.txt').symlink_to(outside)
        except OSError:
            has_symlink = False
        with self.assertRaises(ValueError):self.worker.submit('scan','../outside.txt')
        if has_symlink:
            with self.assertRaises(ValueError):self.worker.submit('scan','link.txt')
        with self.assertRaises(ValueError):self.worker.submit('scan',str(outside))
        job=self.worker.submit('scan')
        files,skipped=self.worker.scan(job['id'],'.')
        self.assertEqual([f['path'] for f in files],['a.txt'])
        if has_symlink:
            self.assertEqual(skipped,1)
    def test_pause_resume_and_restart_preserve_checkpoint(self):
        for name in ['a','b']:(self.root/(name+'.txt')).write_text(name)
        job=self.worker.submit('scan');seen=[]
        def receive(ident,name,text,use_s1):
            if text=='b' and len(seen)==1:
                self.worker.action(ident,'pause');raise Interrupted()
            seen.append(text)
        self.worker.import_text=receive;self.worker.process_one(job['id'])
        paused=self.worker.get(job['id']);self.assertEqual(paused['state'],'paused');self.assertEqual(paused['completed'],1)
        other=Worker(self.home,configuration(self.home),'unused');other.import_text=lambda *args:seen.append(args[2])
        other.action(job['id'],'resume');other.update(job['id'],state='running');other.recover()
        self.assertEqual(other.get(job['id'])['state'],'queued')
        other.process_one(job['id']);self.assertEqual(seen,['a','b']);self.assertEqual(other.get(job['id'])['state'],'succeeded')
    def test_cancel_prevents_processing(self):
        (self.root/'a.txt').write_text('a');job=self.worker.submit('scan')
        self.worker.action(job['id'],'cancel');self.worker.process_one(job['id'])
        self.assertEqual(self.worker.get(job['id'])['state'],'cancelled')
    def test_changed_file_is_reported(self):
        p=self.root/'a.txt';p.write_text('a');job=self.worker.submit('scan');files,skip=self.worker.scan(job['id'],'.')
        self.worker.update(job['id'],files=files,total=1);p.write_text('different')
        self.worker.process_one(job['id']);job=self.worker.get(job['id'])
        self.assertEqual(job['state'],'failed');self.assertIn('source changed',job['errors'][0]['message'])
    def test_download_verifies_and_replaces_corruption(self):
        data=b'fixture-model';digest=hashlib.sha256(data).hexdigest()
        class H(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            job=self.worker.submit('download');url=f'http://127.0.0.1:{server.server_port}/model'
            path=self.worker.download_file(job['id'],'fixture.gguf',url,digest,len(data));self.assertEqual(path.read_bytes(),data)
            path.write_bytes(b'X'*len(data));self.worker.download_file(job['id'],'fixture.gguf',url,digest,len(data));self.assertEqual(path.read_bytes(),data)
            with self.assertRaises(ValueError):self.worker.download_file(job['id'],'bad.gguf',url,'0'*64,len(data))
            self.assertFalse((self.worker.models/'bad.gguf').exists());self.assertFalse((self.worker.models/'bad.gguf.part').exists())
        finally:server.shutdown();server.server_close()
    def test_http_auth_and_origin(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.worker,'fixture-token'));threading.Thread(target=server.serve_forever,daemon=True).start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(urllib.error.HTTPError) as err:urllib.request.urlopen(url+'/status')
            self.assertEqual(err.exception.code,401)
            req=urllib.request.Request(url+'/jobs',data=b'{"source":"."}',headers={'Content-Type':'application/json','Authorization':'Bearer fixture-token'})
            with urllib.request.urlopen(req) as r:self.assertEqual(json.load(r)['state'],'queued')
            req.add_header('Origin','https://other.example')
            with self.assertRaises(urllib.error.HTTPError) as err:urllib.request.urlopen(req)
            self.assertEqual(err.exception.code,400)
        finally:server.shutdown();server.server_close()
    def test_history_pruning_keeps_unfinished_jobs_and_requires_boolean_consent(self):
        with self.assertRaises(ValueError):self.worker.submit('scan',use_s1='false')
        pending=self.worker.submit('scan')
        with self.worker.connection() as db:
            for n in range(999):
                ident='finished-'+str(n);job={**pending,'id':ident,'state':'succeeded','created':n}
                db.execute('INSERT INTO jobs VALUES (?,?,?,?)',(ident,n,n,json.dumps(job)))
        self.worker.submit('scan')
        self.assertEqual(self.worker.get(pending['id'])['state'],'queued')
        with self.worker.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1000)

    def test_model_and_input_limits(self):
        with self.assertRaises(ValueError):self.worker.submit('download',model='arbitrary-url')
        self.worker.cfg['max_files']=1
        for name in ['a','b']:(self.root/(name+'.txt')).write_text('a')
        job=self.worker.submit('scan')
        self.worker.process_one(job['id']);self.assertEqual(self.worker.get(job['id'])['state'],'failed')


    def test_grounding_rejects_hallucinations_labels_and_preserves_credentials(self):
        text = 'Наш движок называется Лунная Слива. Фраза доступа: туман-482-ирис.'
        proposals, rejected = grounded_candidates({
            'candidates': ['Лунная Слива', 'Лунная Слива', 'Туман-482-ирис', 'Выдумка', 'Фраза доступа'],
            'credentials': ['туман-482-ирис'],
        }, text)
        self.assertEqual(rejected, 3)
        self.assertEqual({p['value'] for p in proposals}, {'Лунная Слива', 'туман-482-ирис'})
        self.assertEqual(proposals[0]['kind'], 'SECRET')
        self.assertTrue(all(p['quote'] in text and p['value'] in p['quote'] for p in proposals))
        for bad in [{}, {'candidates': [None], 'credentials': []}, {'candidates': [], 'credentials': 'key'}]:
            with self.assertRaises(ValueError): grounded_candidates(bad, text)

    def test_model_mode_roundtrip_and_old_jobs_default_local(self):
        for mode in ['local', 'xiaomi']:
            job = self.worker.submit('scan', mode=mode)
            self.assertEqual(self.worker.get(job['id'])['mode'], mode)
        for mode in ['other', None, [], 1]:
            with self.assertRaises(ValueError): self.worker.submit('scan', mode=mode)
        job = self.worker.submit('scan')
        with patch('kaponir_worker.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b'{}'
            with patch.object(self.worker, 'model_candidates', side_effect=AssertionError('model not requested')):
                self.worker.import_text(job['id'], 'test', 'Aster Birch', False)

    def test_model_failure_preserves_local_import_and_reports_failure_without_raw_text(self):
        (self.root/'a.txt').write_text('project: Amber Pine')
        job = self.worker.submit('scan', mode='xiaomi')
        with patch.object(self.worker, 'model_candidates', side_effect=RuntimeError('RAW_SECRET')):
            with patch('kaponir_worker.subprocess.run') as run:
                run.return_value.returncode = 0; run.return_value.stdout = b'{}'
                self.worker.process_one(job['id'])
                self.assertEqual(run.call_count, 1)
        result = self.worker.get(job['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertIn('local candidates saved', result['errors'][0]['message'])
        self.assertNotIn('RAW_SECRET', json.dumps(result))

    def test_model_import_passes_evidence_to_remote_discover_and_s1(self):
        job = self.worker.submit('scan', mode='xiaomi', use_s1=True)
        self.worker.cfg['api_url'] = 'http://localhost:1234'
        proposals = [{'value': 'Amber Pine', 'kind': 'CUSTOM', 'quote': 'Internal Amber Pine'}]
        with patch.object(self.worker, 'model_candidates', return_value=proposals):
            with patch('kaponir_worker.http_json', return_value={'s1_status':'scored'}) as request:
                self.worker.import_text(job['id'], 'doc', 'Internal Amber Pine', True)
        body = request.call_args.args[1]
        self.assertTrue(body['remember']); self.assertTrue(body['use_s1'])
        self.assertEqual(body['documents'][0]['model_candidates'], proposals)
        self.assertNotIn('Amber Pine', json.dumps(self.worker.get(job['id'])))

    def test_pause_during_model_analysis_does_not_import_incomplete_result(self):
        job = self.worker.submit('scan', mode='xiaomi')
        with patch.object(self.worker, 'model_candidates', side_effect=Interrupted()):
            with patch('kaponir_worker.subprocess.run') as run:
                with self.assertRaises(Interrupted): self.worker.import_text(job['id'], 'doc', 'Amber Pine', False)
                run.assert_not_called()

    def test_rendering_keeps_original_slices_and_enforces_page_limit(self):
        try:
            import PIL
        except ImportError:
            self.skipTest('Pillow required for document rendering')
        text = '\n'.join(f'Строка {n}: внутреннее название Лунная Слива {n}.' for n in range(120))
        pages = list(text_pages(text, 20))
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(source in text and image.size == (1024,1024) for source,image in pages))
        self.assertTrue(text.startswith(pages[0][0])); self.assertTrue(text.endswith(pages[-1][0]))
        covered = [False] * len(text)
        cursor = 0
        for source, _ in pages:
            start = text.find(source, cursor)
            self.assertGreaterEqual(start, 0)
            covered[start:start+len(source)] = [True]*len(source)
            cursor = start+1
        self.assertTrue(all(covered))
        with self.assertRaises(ValueError): list(text_pages(text, 1))
        with self.assertRaises(ValueError): list(text_pages('hello', 1, edge=256))

    def test_text_chunks_and_qwen_aliases_and_sessions(self):
        text = '\n'.join(f'Строка {n}: внутренний проект Омега-Квант {n}.' for n in range(120))
        chunks = list(text_chunks(text, 20, chunk_chars=600))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c in text for c in chunks))
        j_qwen = self.worker.submit('scan', mode='qwen', model='qwen3-0.6b')
        self.assertEqual(j_qwen['mode'], 'qwen')
        self.assertEqual(j_qwen['model'], 'q4')
        j_sess = self.worker.submit('scan', source='@sessions', mode='local', use_s1=True)
        self.worker.cfg['api_url'] = 'http://localhost:1234'
        with patch('kaponir_worker.http_json', return_value={'documents_analyzed': 2}) as req:
            self.worker.process_one(j_sess['id'])
            self.assertEqual(req.call_count, 1)
            self.assertIn('/api/v1/ui/discover/sessions', req.call_args.args[0])
        self.assertEqual(self.worker.get(j_sess['id'])['state'], 'succeeded')

if __name__=='__main__':unittest.main()
