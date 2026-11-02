#!/usr/bin/env python3
"""Interactive live audit. Credentials stay in memory; only synthetic bodies are saved.

Run from the project root; enter keys at the hidden prompts, then use the printed
commands. A loopback relay records bodies (never headers) before forwarding them
to the explicitly supplied upstream. All vault files live in TemporaryDirectory.
"""
import argparse
import concurrent.futures
import getpass
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(url, body=None, method=None, headers=None, timeout=90):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class Audit:
    def __init__(self, args, llm_key, s1_key):
        self.args, self.keys = args, {'llm': llm_key, 's1': s1_key}
        self.out = Path(args.output).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.binary = str(Path(args.binary).resolve())
        self.temp = tempfile.TemporaryDirectory(prefix='kaponir-live-audit-')
        self.home = Path(self.temp.name) / 'original'
        self.proc = None
        self.log = None
        self.lock = threading.Lock()
        self.records = []
        self.results = {}
        self.fault = False
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        audit = self

        class Relay(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                path = self.path
                if path.split('?')[0] not in ('/v1/models', '/v1/chat/completions', '/v1/systemone'):
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                record = {'path': path, 'request': json.loads(body) if body else None}
                start = time.perf_counter()
                with audit.lock:
                    index = len(audit.records)
                    audit.records.append(record)
                try:
                    if path == '/v1/systemone' and audit.fault:
                        self.send_response(503)
                        self.end_headers()
                        self.wfile.write(b'{"error":"synthetic outage"}')
                        record.update(status=503, synthetic_outage=True)
                        return
                    token = audit.keys['s1' if path == '/v1/systemone' else 'llm']
                    req = urllib.request.Request(audit.args.upstream.rstrip('/') + path.removeprefix('/v1'),
                        data=body if self.command == 'POST' else None,
                        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
                    try:
                        response = urllib.request.urlopen(req, timeout=90)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        record['status'] = response.status
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        self.end_headers()
                        chunks = []
                        timings = []
                        while True:
                            chunk = response.read1(4096)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            timings.append(round((time.perf_counter() - start) * 1000, 2))
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        record['response'] = b''.join(chunks).decode(errors='replace')
                        record['chunk_ms'] = timings
                except Exception as exc:
                    record['error_type'] = type(exc).__name__
                finally:
                    record['elapsed_ms'] = round((time.perf_counter() - start) * 1000, 2)
                    audit.save(f'wire/{index:03}.json', record)

        self.relay = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Relay)
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        self.relay_base = f'http://127.0.0.1:{self.relay.server_port}'
        subprocess.run([self.binary, '--home', str(self.home), 'init', '--corp-domain', 'aurora.internal'],
                       check=True, capture_output=True)
        (self.home / 'config.toml').write_text(f'''owner = "live-audit"
[serve]
listen = "127.0.0.1:{self.port}"
system_hint = false
''')
        self.start()
        self.save('runtime.json', {'base': self.base, 'relay': self.relay_base,
                                   'upstream': args.upstream, 'temporary_home': str(self.home)})

    def save(self, name, value):
        path = self.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, ensure_ascii=False, indent=2)
        for key in self.keys.values():
            if key:
                raw = raw.replace(key, '[REDACTED]')
        path.write_text(raw)

    def api(self, path, body=None, method=None):
        return request(self.base + path, body, method)

    def start(self):
        self.log = (self.out / 'server.log').open('ab')
        self.proc = subprocess.Popen([self.binary, '--home', str(self.home), 'serve'],
            env={**os.environ, 'KAPONIR_S1_HTTP_TOKEN': 'local-relay-only'},
            stdout=self.log, stderr=self.log)
        for _ in range(150):
            try:
                self.api('/api/v1/ui/info')
                return
            except (OSError, ValueError):
                if self.proc.poll() is not None:
                    raise RuntimeError('Server exited; inspect server.log')
                time.sleep(.1)
        raise RuntimeError('Server readiness timeout')

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        if self.log:
            self.log.close()

    def configure(self, mode='off', level='l2'):
        settings = {'provider': 'none' if mode == 'off' else 'http',
                    'shadow': mode == 'shadow', 'context_bytes': 160,
                    'http': {'url': self.relay_base + '/v1/systemone', 'protocol': 'systemone',
                             'model': 'oc/jev-1.13-free', 'token_env': 'KAPONIR_S1_HTTP_TOKEN',
                             'timeout_ms': 20000, 'disclosure': level}}
        return self.api('/api/v1/ui/s1', {'settings': settings}, 'PUT')

    def smoke(self):
        connection = self.api('/api/v1/connections', {'kind': 'openai',
            'name': 'Gemini · реальный API через проверочный relay',
            'base_url': self.relay_base + '/v1', 'api_key': 'local-relay-only',
            'enabled': True, 'pool': True, 'auto_models': True})
        self.save('connection.json', connection)
        catalog = self.api('/v1/models')
        self.save('models.json', catalog)
        models = [item['id'] for item in catalog['data']]
        self.model = next((m for m in models if 'gemini' in m.lower() and 'flash' in m.lower()), None)
        if not self.model:
            raise RuntimeError('No Gemini Flash model discovered; choose a model explicitly')
        connections = self.api('/api/v1/connections')
        self.save('connections.json', connections)
        items = connections['connections']
        self.connection_id = items[0]['id']
        start = time.perf_counter()
        result = request(self.args.upstream.rstrip('/') + '/systemone', {
            'model': 'oc/jev-1.13-free',
            'state': 'My payments have failed for three days and I am losing sales. Please help now.',
            'questions': {'is_urgent': {'type': 'noul', 'instructions': 'Does this request require urgent attention?'}}},
            headers={'Authorization': 'Bearer ' + self.keys['s1']})
        self.save('s1-smoke.json', {'response': result, 'elapsed_ms': (time.perf_counter()-start)*1000})
        self.results['smoke'] = {'models': models, 'selected_model': self.model, 's1_response': result}
        print(json.dumps(self.results['smoke'], ensure_ascii=False), flush=True)

    def s1(self):
        cases = [
            ('private', 'Закрытый репозиторий клиента /workspace/auroraledger/k8s. Не раскрывать имя репозитория.\npassword: Synthetic_Audit_Q7!', 'auroraledger', True),
            ('public', 'Публичный пример сборки open-source: /workspace/otelcollector/k8s. Имя не конфиденциально.\npassword: Synthetic_Audit_Q7!', 'otelcollector', False),
            ('business', 'Проект: Северный Вектор. Сумма закрытого предложения клиенту 1801234.37 RUB. Коммерческая тайна.\npassword: Synthetic_Audit_Q7!', '1801234.37', True),
        ]
        rows, baseline = [], {}
        for mode, level in [('off', 'l0'), ('shadow', 'l0'), ('shadow', 'l2'), ('active', 'l2')]:
            self.configure(mode, level)
            for name, original, target, sensitive in cases:
                start = time.perf_counter()
                first = len(self.records)
                masked = self.api('/api/v1/ui/mask', {'text': original, 'commit': True})
                restored = self.api('/api/v1/ui/restore', {'text': masked['text']})
                wire = [r for r in self.records[first:] if r['path'] == '/v1/systemone']
                scores = [e for c in masked['explain'] for e in c['evidence'] if e['source'].startswith('s1')]
                row = {'case': name, 'mode': mode, 'level': level, 'original': original,
                       'masked': masked, 's1_scores': scores, 's1_calls': len(wire),
                       'sensitive_target': sensitive, 'target_hidden': target not in masked['text'],
                       'secret_hidden': 'Synthetic_Audit_Q7!' not in masked['text'],
                       's1_secret_hidden': all('Synthetic_Audit_Q7!' not in json.dumps(r['request']) for r in wire),
                       'restore_exact': restored['text'] == original,
                       'elapsed_ms': round((time.perf_counter()-start)*1000, 2)}
                if mode == 'off':
                    baseline[name] = masked['text']
                row['same_as_off'] = masked['text'] == baseline[name]
                rows.append(row)
                self.save('s1-integration.json', rows)
                print(json.dumps({k: v for k, v in row.items() if k not in ('original', 'masked', 's1_scores')}, ensure_ascii=False), flush=True)
        self.fault = True
        try:
            masked = self.api('/api/v1/ui/mask', {'text': cases[0][1], 'commit': True})
            restored = self.api('/api/v1/ui/restore', {'text': masked['text']})
            fallback = {'same_as_off': masked['text'] == baseline['private'],
                        'restore_exact': restored['text'] == cases[0][1]}
            self.save('s1-outage.json', fallback)
        finally:
            self.fault = False
        self.results['s1'] = {'cases': len(rows), 'restored': sum(r['restore_exact'] for r in rows),
            'secret_hidden_everywhere': all(r['secret_hidden'] and r['s1_secret_hidden'] for r in rows),
            'shadow_unchanged': all(r['same_as_off'] for r in rows if r['mode'] == 'shadow'),
            'fallback': fallback}
        self.configure('shadow')

    def completion(self, name, streaming=False, tool=False, history=None):
        original = 'password: Synthetic_Audit_Q7!\nHost: db.aurora.internal\nPath: /home/tester/auroraledger/config.yaml'
        prompt = 'Echo the following block exactly, preserving every character. No markdown or explanations.\n' + original
        messages = history or [{'role': 'user', 'content': prompt}]
        body = {'model': self.model, 'messages': messages, 'temperature': 0, 'max_tokens': 700, 'stream': streaming}
        if tool:
            body['messages'] = [{'role': 'user', 'content': 'Call record_text with this exact text:\n' + original}]
            body['tools'] = [{'type': 'function', 'function': {'name': 'record_text', 'description': 'Record exact text',
                'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}}]
            body['tool_choice'] = {'type': 'function', 'function': {'name': 'record_text'}}
        path = f'/p/{self.connection_id}/v1/chat/completions' if streaming else '/v1/chat/completions'
        first = len(self.records)
        start = time.perf_counter()
        chunks, events = [], []
        if streaming:
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=90) as response:
                for line in response:
                    if not line.startswith(b'data:'):
                        continue
                    data = line[5:].strip()
                    events.append({'ms': round((time.perf_counter()-start)*1000, 2), 'data': data.decode()})
                    if data == b'[DONE]':
                        continue
                    event = json.loads(data)
                    for choice in event.get('choices', []):
                        content = choice.get('delta', {}).get('content') or ''
                        chunks.append(content)
                        if content:
                            print('STREAM ' + repr(content), flush=True)
            returned = ''.join(chunks)
            response_body = events
        else:
            response_body = self.api(path, body)
            message = response_body['choices'][0]['message']
            returned = json.loads(message['tool_calls'][0]['function']['arguments'])['text'] if tool else message.get('content', '')
        # Relay persists its record immediately after flushing upstream bytes.
        for _ in range(50):
            wire = [r for r in self.records[first:] if r['path'] == '/v1/chat/completions']
            if wire and 'elapsed_ms' in wire[-1]:
                break
            time.sleep(.02)
        secrets = ['Synthetic_Audit_Q7!', 'aurora.internal', 'auroraledger']
        sent = json.dumps(wire[-1]['request'], ensure_ascii=False) if wire else ''
        row = {'scenario': name, 'streaming': streaming, 'tool': tool, 'original': original,
               'request': body, 'response': response_body, 'returned': returned,
               'wire_requests': len(wire), 'no_originals_sent': bool(wire) and all(s not in sent for s in secrets),
               'all_originals_restored': all(s in returned for s in secrets),
               'exact_echo': returned == original, 'sse_done': any(e['data'] == '[DONE]' for e in events),
               'content_chunks': sum(bool(c) for c in chunks),
               'elapsed_ms': round((time.perf_counter()-start)*1000, 2)}
        self.save(f'real-{name}.json', row)
        summary = {k: v for k, v in row.items() if k not in ('request', 'response', 'original', 'returned')}
        self.results.setdefault('real', []).append(summary)
        print(json.dumps(summary), flush=True)
        return original, returned

    def real(self):
        self.configure('shadow')
        original, answer = self.completion('common')
        self.completion('stream-direct', streaming=True)
        self.completion('tool-arguments', tool=True)
        self.completion('multi-turn', history=[{'role': 'user', 'content': 'Echo exactly:\n' + original},
            {'role': 'assistant', 'content': answer},
            {'role': 'user', 'content': 'Repeat your preceding answer exactly. No commentary.'}])
        self.save('metrics-after-real.json', self.api('/api/v1/metrics'))

    def load(self):
        def run_job(spec):
            i, size = spec
            text = f'password: Load_Only_{i}_Q9!\nHost: load{i}.aurora.internal\n'
            text += 'Public nginx documentation.\n' * max(1, size // 28)
            session = f'load-{i}-{size}'
            start = time.perf_counter()
            masked = self.api('/api/v1/anonymize', {'text': text, 'session': session})
            mask_ms = (time.perf_counter()-start)*1000
            restore_start = time.perf_counter()
            restored = self.api('/api/v1/deanonymize', {'text': masked['text'], 'session': session})
            return {'bytes': len(text.encode()), 'mask_ms': round(mask_ms, 2),
                    'restore_ms': round((time.perf_counter()-restore_start)*1000, 2),
                    'restore_exact': restored['text'] == text,
                    'secret_hidden': f'Load_Only_{i}_Q9!' not in masked['text']}
        self.configure('off')
        sizes = [1000]*8 + [10000]*8 + [100000]*4
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(run_job, enumerate(sizes)))
        rows.append(run_job((99, 1000000)))
        self.save('sqlite-load.json', {'workers': 4, 'rows': rows, 'elapsed_s': time.perf_counter()-start})
        self.configure('shadow')
        def s1_job(i):
            text = f'Закрытый репозиторий /workspace/aurorapay{i}/k8s. Не раскрывать.\npassword: Parallel_Only_Q9!'
            start = time.perf_counter()
            masked = self.api('/api/v1/ui/mask', {'text': text, 'commit': True})
            restored = self.api('/api/v1/ui/restore', {'text': masked['text']})
            return {'i': i, 'elapsed_ms': (time.perf_counter()-start)*1000,
                    'restore_exact': restored['text'] == text,
                    'secret_hidden': 'Parallel_Only_Q9!' not in masked['text'],
                    's1_scores': sum(e['source'].startswith('s1') for c in masked['explain'] for e in c['evidence'])}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            s1_rows = list(pool.map(s1_job, range(4)))
        self.save('sqlite-s1-concurrency.json', s1_rows)
        self.results['load'] = {'sqlite_cases': len(rows), 'exact': sum(r['restore_exact'] for r in rows),
                                's1_concurrency': s1_rows}
        print(json.dumps(self.results['load']), flush=True)

    def backup(self):
        original = 'password: Backup_Only_Z8!\nHost: backup.aurora.internal'
        masked = self.api('/api/v1/ui/mask', {'text': original, 'commit': True})
        connections_before = self.api('/api/v1/connections')
        self.stop()
        backup = Path(self.temp.name) / 'cold-backup'
        shutil.copytree(self.home, backup)
        restored_home = Path(self.temp.name) / 'restored'
        shutil.copytree(backup, restored_home)
        self.home = restored_home
        self.start()
        restored = self.api('/api/v1/ui/restore', {'text': masked['text']})
        connections_after = self.api('/api/v1/connections')
        row = {'cold_backup': True, 'separate_directory': True,
               'restore_exact': restored['text'] == original,
               'connections_preserved': connections_before == connections_after}
        self.save('backup-restore.json', row)
        self.results['backup'] = row
        print(json.dumps(row), flush=True)

    def close(self):
        self.stop()
        self.relay.shutdown()
        self.relay.server_close()
        self.temp.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', default='target/release/kaponir')
    parser.add_argument('--output', default='artifacts/live-validation-2026-11-01')
    parser.add_argument('--upstream', default='https://route.fn.x3dev.ru/v1')
    args = parser.parse_args()
    llm_key = getpass.getpass('LLM key (hidden): ')
    s1_key = getpass.getpass('S1 key (hidden): ')
    audit = Audit(args, llm_key, s1_key)
    del llm_key, s1_key
    print('READY ' + audit.base + '/ui/', flush=True)
    print('Commands: smoke, s1, real, load, backup, metrics, finish', flush=True)
    try:
        while True:
            command = input('audit> ').strip()
            if command == 'finish':
                audit.save('final-metrics.json', audit.api('/api/v1/metrics'))
                break
            try:
                if command == 'metrics':
                    audit.save('metrics-snapshot.json', audit.api('/api/v1/metrics'))
                elif command in ('smoke', 's1', 'real', 'load', 'backup'):
                    getattr(audit, command)()
                else:
                    print('Unknown command', flush=True)
                    continue
                audit.save('summary.json', audit.results)
                print('DONE ' + command, flush=True)
            except Exception as exc:
                # Exception messages can include arbitrary response text; persist only type/status.
                failure = {'command': command, 'type': type(exc).__name__, 'status': getattr(exc, 'code', None)}
                audit.results.setdefault('failures', []).append(failure)
                audit.save('summary.json', audit.results)
                print('FAILED ' + json.dumps(failure), flush=True)
    finally:
        audit.close()
        audit.save('cleanup.json', {'temporary_directory_removed': not Path(audit.temp.name).exists(),
                                   'server_stopped': audit.proc.poll() is not None})


if __name__ == '__main__':
    main()
