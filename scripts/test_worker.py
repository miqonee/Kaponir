"""Queue lifecycle, file boundary, download integrity and HTTP auth without models."""
import hashlib
import errno
import json
from pathlib import Path
import tempfile
import threading
import socket
import ssl
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from kaponir_worker import Worker, configuration, handler, Interrupted, grounded_candidates, text_pages, text_chunks, installation_error


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        self.root=self.home/'inbox';self.root.mkdir()
        self.worker=Worker(self.home,configuration(self.home),'unused-binary')
    def tearDown(self):
        self.worker.stop.set();self.worker.stop_runtime()
        if self.worker.verification_thread:
            self.worker.verification_thread.join(timeout=5)
        self.temp.cleanup()

    def settled_model(self, worker=None):
        worker = worker or self.worker
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            model = worker.status()['models']['qwen3-0.6b']
            if model['state'] != 'checking': return model
            time.sleep(.01)
        self.fail('model verification did not finish')

    def test_offline_verification_while_disabled_detects_same_size_corruption_and_restart(self):
        data = b'fixture-model'
        digest = hashlib.sha256(data).hexdigest()
        item = ('fixture.gguf', len(data), digest, 'https://example.test/model')
        self.worker.activate(False)
        with patch.dict('kaponir_worker.FILES', {'q4': item}), patch('kaponir_worker.urllib.request.urlopen', side_effect=AssertionError('offline')):
            self.assertEqual(self.settled_model()['state'], 'missing')
            path = self.worker.models / item[0]
            path.write_bytes(data)
            self.assertTrue(self.settled_model()['downloaded'])
            self.assertFalse(self.worker.enabled)
            with patch('kaponir_worker.hashlib.file_digest', side_effect=AssertionError('cached')):
                self.assertTrue(self.worker.status()['models']['q4']['downloaded'])
            path.write_bytes(b'x' * len(data))
            invalid = self.settled_model()
            self.assertEqual(invalid['state'], 'invalid')
            self.assertEqual(invalid['error'], 'checksum_mismatch')
            self.assertFalse(invalid['downloaded'])
            path.write_bytes(data[:-1])
            self.assertEqual(self.settled_model()['error'], 'size_mismatch')
            path.write_bytes(data)
            self.worker.verify_models('qwen3-0.6b')
            self.assertTrue(self.settled_model()['downloaded'])
            recovered = Worker(self.home, configuration(self.home), 'unused')
            self.assertTrue(self.settled_model(recovered)['downloaded'])
            self.assertFalse(recovered.enabled)
            status = self.worker.status()
            self.assertEqual(status['models']['q4'], status['models']['qwen3-0.6b'])
            self.assertEqual(status['models']['q4']['path'], str(path))
            self.assertEqual(status['models']['q4']['sha256'], digest)
            self.assertEqual(status['runtime']['path'], str(self.worker.runtime))
            self.assertTrue(status['model_verification'])

    def test_download_deduplicates_canonical_model_including_paused_jobs(self):
        first = self.worker.submit('download', model='q4')
        duplicate = self.worker.submit('download', model='qwen3-0.6b')
        self.assertEqual(first['id'], duplicate['id'])
        self.worker.action(first['id'], 'pause')
        self.assertEqual(self.worker.submit('download', model='q4')['id'], first['id'])
        self.worker.action(first['id'], 'cancel')
        self.assertNotEqual(self.worker.submit('download', model='q4')['id'], first['id'])

    def test_unreadable_files_do_not_break_status_and_removed_files_become_missing(self):
        path = self.worker.models / 'fixture.gguf'
        path.write_bytes(b'fixture')
        item = ('fixture.gguf', 7, hashlib.sha256(b'fixture').hexdigest(), 'https://example.test/model')
        with patch.dict('kaponir_worker.FILES', {'q4': item}):
            with patch.object(self.worker, 'file_signature', side_effect=PermissionError(errno.EACCES, 'private path')):
                model = self.worker.status()['models']['q4']
                self.assertEqual(model['state'], 'unreadable')
                self.assertFalse(model['downloaded'])
            path.unlink()
            self.assertEqual(self.worker.status()['models']['q4']['state'], 'missing')
        with patch.object(Path, 'is_file', side_effect=PermissionError(errno.EACCES, 'private path')):
            status = self.worker.status()
            self.assertEqual(status['runtime']['state'], 'unreadable')
            self.assertFalse(status['runtime_ready'])

    def test_runtime_failure_retains_verified_weights_without_network(self):
        data = b'fixture-model'
        item = ('fixture.gguf', len(data), hashlib.sha256(data).hexdigest(), 'https://example.test/model')
        (self.worker.models / item[0]).write_bytes(data)
        self.worker.cfg['runtime'] = '/missing/llama-server'
        with patch.dict('kaponir_worker.FILES', {'q4': item}), patch('kaponir_worker.urllib.request.urlopen', side_effect=AssertionError('offline')):
            job = self.worker.submit('download')
            self.worker.process_one(job['id'])
            result = self.worker.get(job['id'])
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['errors'][0]['code'], 'runtime_missing')
            self.assertEqual(result['errors'][0]['stage'], 'runtime_install')
            self.assertTrue(self.settled_model()['downloaded'])
            self.assertFalse(self.worker.status()['runtime_ready'])
            self.worker.runtime.parent.mkdir(parents=True, exist_ok=True)
            self.worker.runtime.write_text('fixture')
            self.assertFalse(self.worker.status()['runtime_ready'])
            self.worker.runtime.chmod(0o755)
            retry = self.worker.submit('download')
            self.worker.process_one(retry['id'])
            self.assertEqual(self.worker.get(retry['id'])['state'], 'succeeded')

    def test_installation_errors_are_actionable_without_arbitrary_exception_text(self):
        cases = [
            (urllib.error.URLError(socket.gaierror(-2, 'RAW_SECRET')), 'dns_error'),
            (urllib.error.URLError(ssl.SSLError('RAW_SECRET')), 'tls_error'),
            (urllib.error.URLError(TimeoutError('RAW_SECRET')), 'download_timeout'),
            (urllib.error.URLError('proxy RAW_SECRET'), 'proxy_error'),
            (urllib.error.URLError('RAW_SECRET'), 'connection_failed'),
            (urllib.error.HTTPError('https://RAW_SECRET', 404, 'RAW_SECRET', {}, None), 'http_error'),
            (urllib.error.HTTPError('https://RAW_SECRET', 407, 'RAW_SECRET', {}, None), 'proxy_error'),
            (OSError(errno.ENOSPC, 'RAW_SECRET'), 'disk_full'),
            (PermissionError(errno.EACCES, 'RAW_SECRET'), 'permission_denied'),
            (RuntimeError('RAW_SECRET'), 'installation_failed'),
        ]
        for exception, code in cases:
            with self.subTest(code=code):
                result = installation_error(exception, 'model_download', 'huggingface.co')
                self.assertEqual(result['code'], code)
                self.assertNotIn('RAW_SECRET', json.dumps(result))
        job = self.worker.submit('download')
        with patch('kaponir_worker.urllib.request.urlopen', side_effect=urllib.error.URLError('RAW_SECRET')):
            self.worker.process_one(job['id'])
        error = self.worker.get(job['id'])['errors'][0]
        self.assertEqual(error['code'], 'connection_failed')
        self.assertEqual(error['stage'], 'model_download')
        self.assertEqual(error['host'], 'huggingface.co')
        self.assertNotIn('RAW_SECRET', json.dumps(error))
    def test_activation_persists_without_downloading_or_losing_jobs(self):
        (self.root/'a.txt').write_text('project: Amber Pine')
        job = self.worker.submit('scan')
        self.worker.activate(False)
        self.assertFalse(self.worker.status()['enabled'])
        with self.assertRaises(Interrupted): self.worker.check(job['id'])
        recovered = Worker(self.home, configuration(self.home), 'unused-binary')
        self.assertFalse(recovered.enabled)
        self.assertEqual(recovered.get(job['id'])['state'], 'queued')
        self.assertFalse(any(m['downloaded'] for m in recovered.status()['models'].values()))
        recovered.activate(True)
        self.assertTrue(Worker(self.home, configuration(self.home), 'unused-binary').enabled)

    def test_disable_checkpoints_active_job_and_resumes_only_activation_pause(self):
        job = self.worker.submit('scan')
        manual = self.worker.submit('scan')
        self.worker.action(manual['id'], 'pause')
        self.worker.active = job['id']
        self.worker.update(job['id'], state='running')
        self.worker.activate(False)
        self.assertEqual(self.worker.get(job['id'])['control'], 'paused')
        self.worker.update(job['id'], state='paused')
        self.worker.active = None
        self.worker.activate(True)
        self.assertEqual(self.worker.get(job['id'])['state'], 'queued')
        self.assertEqual(self.worker.get(manual['id'])['state'], 'paused')

    def test_session_scan_uses_model_pipeline_and_requires_proxy(self):
        job = self.worker.submit('scan', '@sessions', mode='qwen')
        with self.assertRaises(ValueError): self.worker.process_sessions(job)
        self.worker.cfg['api_url'] = 'http://127.0.0.1:1'
        with patch('kaponir_worker.http_json', return_value={'documents':[{'name':'session-a','text':'project: Amber Pine'}]}) as request:
            with patch.object(self.worker, 'import_text') as process:
                self.worker.process_sessions(job)
                process.assert_called_once_with(job['id'], 'session-a', 'project: Amber Pine', False)
            self.assertTrue(request.call_args.args[0].endswith('/api/v1/ui/sessions/export'))
        self.assertEqual(self.worker.get(job['id'])['state'], 'succeeded')

    def test_parallel_evaluations_are_bounded_and_support_ten_slots(self):
        import time
        active = 0
        peak = 0
        lock = threading.Lock()
        def response(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.05)
            with lock: active -= 1
            return {'choices':[{'finish_reason':'stop','message':{'content':'{"candidates":[]}'}}]}
        self.worker.ocr_base='http://fixture'
        results=[]
        body={'model':'qwen3-0.6b','parallelism':3,'prompt':'fixture','schema':{},'input':{}}
        with patch.object(self.worker,'start_runtime') as runtime, patch('kaponir_worker.http_json',side_effect=response):
            threads=[threading.Thread(target=lambda:results.append(self.worker.evaluate(body))) for _ in range(7)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=5)
            self.assertEqual(len(results),7)
            self.assertGreater(peak,1)
            self.assertLessEqual(peak,3)
            self.worker.evaluate({**body,'parallelism':10})
            self.assertEqual(runtime.call_args.args[2],10)
        self.assertFalse(self.worker.evaluations)
        for invalid in [0,11,True]:
            with self.assertRaises(ValueError): self.worker.evaluate({**body,'parallelism':invalid})

    def test_configured_document_analysis_uses_background_queue(self):
        self.worker.cfg['api_url']='http://fixture'
        job=self.worker.submit('scan',mode='configured')
        with patch('kaponir_worker.http_json',return_value={'queued':1}) as request:
            self.worker.import_text(job['id'],'fixture.txt','private text',False)
            self.assertTrue(request.call_args.args[0].endswith('/api/v1/ui/analysis/documents'))
        memory=self.worker.resources()
        if memory['worker_rss_bytes'] is not None: self.assertGreater(memory['worker_rss_bytes'],0)

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
            self.worker.activate(False)
            verify = urllib.request.Request(url+'/models/verify', data=b'{"model":"qwen3-0.6b"}', headers={'Content-Type':'application/json','Authorization':'Bearer fixture-token'})
            with urllib.request.urlopen(verify) as response:
                self.assertEqual(json.load(response)['state'], 'missing')
            self.assertFalse(self.worker.enabled)
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
            self.assertIn('/api/v1/ui/sessions/export', req.call_args.args[0])
        self.assertEqual(self.worker.get(j_sess['id'])['state'], 'succeeded')

if __name__=='__main__':unittest.main()
