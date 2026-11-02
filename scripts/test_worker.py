"""Queue lifecycle, file boundary, download integrity and HTTP auth without models."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from kaponir_worker import Worker, configuration, handler, Interrupted


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
        (self.root/'link.txt').symlink_to(outside)
        with self.assertRaises(ValueError):self.worker.submit('scan','../outside.txt')
        with self.assertRaises(ValueError):self.worker.submit('scan','link.txt')
        with self.assertRaises(ValueError):self.worker.submit('scan',str(outside))
        job=self.worker.submit('scan')
        files,skipped=self.worker.scan(job['id'],'.')
        self.assertEqual([f['path'] for f in files],['a.txt']);self.assertEqual(skipped,1)
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

if __name__=='__main__':unittest.main()
