"""Exercise directory approval, proxy masking, session review and raw document analysis."""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright
from validate_console import free_port, wait_for
from validate_review_dataset import seed_path_group


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply({'online': True, 'configured': True, 'enabled': True, 'state': 'ready',
                    'runtime_ready': True, 'models': {'qwen3-0.6b': {'downloaded': True}}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        local = self.path == '/evaluate'
        if not local and self.path != '/detect':
            self.server.sent.append(body['messages'][-1]['content'])
            self.reply({'choices': [{'message': {'role': 'assistant', 'content': 'Готово.'}}]})
            return
        if local:
            packet, schema = body['input'], body['schema']
        else:
            packet = json.loads(body['messages'][1]['content'])
            schema = body['response_format']['json_schema']['schema']
        self.server.calls.append((self.path, packet, schema))
        if 'items' in packet:  # Scenario D.
            result = {'decisions': [{'id': row['id'], 'verdict': 'mask', 'reason': 'Внутренний проект'}
                                    for row in packet['items']]}
        elif 'candidates' in packet:  # Standard categorization.
            result = {'decisions': [{'kind': 'PROJ', 'scope': 'team', 'action': 'mask', 'confidence': .95}
                                    for _ in packet['candidates']]}
        else:
            properties = schema['properties']['candidates']['items']['properties']
            allowed = properties['value'].get('enum')
            paths = re.findall(r'[^\s",]+/notes/[^\s",]+?\.txt', packet['source'])
            if allowed is None:
                values = paths  # Reproduce a model that proposes every complete path.
            else:
                private = re.findall(r'внутреннего проекта\S* [^\s]+/notes/([\w-]+)\.txt', packet['source'])
                values = sorted(set(private) & set(allowed))
            result = {'candidates': [
                {'value': v, 'confidence': .95, **({'path': next(p for p in paths if p.endswith('/'+v+'.txt'))}
                                                  if 'path' in properties else {}),
                 **({'kind': 'PATH' if allowed is None else 'PROJ'}
                                                  if 'kind' in properties else {})} for v in values]}
            if allowed is not None and self.server.invalid_refinement:
                result = {'candidates': [{'value': 'unrelated value', 'confidence': .99}]}
        self.reply({'result': result} if local else
                   {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]})


def exercise(args, model, home):
    root_url = f'http://127.0.0.1:{free_port()}'
    upstream = f'http://127.0.0.1:{model.server_port}'
    env = {k: v for k, v in os.environ.items() if not k.startswith('KAPONIR_')}
    env['KAPONIR_WORKER_URL'] = upstream

    def api(path, body=None, method=None, headers=None):
        request = Request(root_url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json', **(headers or {})})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    @contextmanager
    def serve(binary):
        with (args.output/'server.log').open('a') as log:
            process = subprocess.Popen([binary, '--home', home, 'serve', '--listen', root_url[7:],
                                        '--openai-upstream', upstream], env=env, stdout=log, stderr=log)
            try:
                wait_for(lambda: api('/api/v1/ui/info'))
                yield
            finally:
                process.terminate()
                process.wait(timeout=20)

    def configure(role, **changes):
        endpoint = '/api/v1/ui/' + role
        settings = api(endpoint)['settings']
        api(endpoint, {'settings': {**settings, **changes}}, 'PUT')

    def run(path, body, expected_state="succeeded"):
        queued = api('/api/v1/ui/analysis/' + path, body)
        job_id, = queued['ids']
        def finished():
            state = api('/api/v1/ui/learning')
            job = next(job for job in state['jobs'] if job['id'] == job_id)
            if job['state'] in ('succeeded', 'failed'):
                return job, [e for e in state['entries'] if e['job_id'] == job_id]
        job, entries = wait_for(finished)
        assert job['state'] == expected_state, job
        if expected_state == 'succeeded':
            assert api('/api/v1/ui/analysis/' + path, body)['existing_ids'] == [job_id]
        else:
            assert job['error'] == 'invalid_candidate_response' and job['retryable'], job
        return job_id, entries

    def screenshot(job_id, side, value=None):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 960}, locale='ru-RU', color_scheme='light')
            page.goto(root_url+'/ui/#jobs/learning/'+job_id)
            expect(page.locator('#jobs-content .job-state')).to_be_visible()
            page.screenshot(path=args.output/f'{side}-job.png', animations='disabled')
            if value:
                page.get_by_role('button', name='Открыть находки этого запуска').click()
                page.locator('#review-list').get_by_role('button', name=value, exact=True).click()
                mark = page.locator('#finding-detail mark')
                expect(mark).to_have_text(value)
                expect(page.locator('#finding-detail figcaption')).to_have_text(
                    f'Начало пути уже скрыто. Решение касается только «{value}».')
                assert '…/notes/' in mark.evaluate('el => el.previousSibling.textContent')
                page.screenshot(path=args.output/'after-finding.png', animations='disabled')
            browser.close()

    first = args.baseline or args.binary
    subprocess.run([first, '--home', home, 'init'], env=env, capture_output=True, check=True)
    with serve(first):
        root, _ = seed_path_group(api)
        review = '/api/v1/ui/review'
        group = next(r for r in api(review+'?status=pending&kind=PATH')['items'] if r.get('path_group'))
        api(review+'/path-prefix', {'prefix': root, 'keys': group['occurrence_keys'], 'scope': group['scope']})
        api('/api/v1/connections', {'kind': 'openai', 'name': 'Fixture', 'base_url': upstream, 'api_key': 'fixture',
                                    'pool': True, 'models': ['chat'], 'auto_models': False})
        cat = api('/api/v1/ui/categorization')['settings']
        configure('categorization', provider='api', http={**cat['http'], 'url': upstream+'/detect',
                                                          'protocol': 'openai', 'model': 'fixture'})
        configure('analysis', provider='api', model='fixture', url=upstream+'/detect', enabled=True,
                  background=False, auto_apply=False)
        sensitive = root+'notes/Orion.txt'
        message = f'Orion — созвездие.\nФайл внутреннего проекта: {sensitive}\nКопия: {sensitive}\nОбычный файл: {root}notes/report.txt'
        api('/v1/chat/completions', {'model': 'chat', 'messages': [{'role': 'user', 'content': message}]},
            headers={'X-Kaponir-Session': 'path-review-demo'})
        sent = model.sent[-1]
        assert re.search(r'ANON_PATH_\w+/notes/Orion\.txt', sent) and root not in sent, sent
        event = api('/api/v1/metrics')['traffic']['events'][0]
        session = {'target': {'session': event['session_id']}, 'revision': api('/api/v1/ui/analysis')['revision']}
        if args.baseline:
            job, entries = run('session', session)
            assert not entries, entries
            screenshot(job, 'before')

    with serve(args.binary):
        model.calls.clear()
        job, entries = run('session', session)
        assert [e['value'] for e in entries] == ['Orion'], entries
        entry = entries[0]
        start, end = entry['context_span']
        quote = entry['quote'].encode()
        assert quote[start:end] == b'Orion' and quote[:start].decode().endswith('…/notes/')
        assert len([p for _, p, _ in model.calls if 'source' in p]) == 2
        assert not any(raw in json.dumps(p, ensure_ascii=False) for _, p, _ in model.calls
                       for raw in (root, 'ANON_PATH_'))
        screenshot(job, 'after', 'Orion')

        # The same analysis path for a raw document, values-only local discovery and marked review.
        settings = api('/api/v1/ui/analysis')['settings']
        configure('analysis', provider='local', model='qwen3-0.6b',
                  chunking={**settings['chunking'], 'discovery_mode': 'values'})
        configure('categorization', provider='local', model='qwen3-0.6b', scenario='D')
        model.calls.clear()
        raw = f'В справочнике упомянута Вега.\nФайл внутреннего проекта: {root}notes/Вега.txt\nКопия: {root}notes/Вега.txt'
        _, entries = run('documents', {'documents': [{'name': 'Проект.txt', 'text': raw}]})
        assert [e['value'] for e in entries] == ['Вега'], entries
        marked = next(p for _, p, _ in model.calls if 'items' in p)
        assert '…/notes/[[TARGET:t0]]Вега[[/TARGET:t0]].txt' in marked['items'][0]['marked_record']
        assert all(route == '/evaluate' and root not in json.dumps(p, ensure_ascii=False) for route, p, _ in model.calls)
        _, benign = run('documents', {'documents': [{'name': 'Обычный файл.txt', 'text': root+'notes/report.txt'}]})
        assert not benign, benign

        retry = {'documents': [{'name': 'retry.txt', 'text': 'Файл внутреннего проекта: '+root+'notes/Atlas.txt'}]}
        model.invalid_refinement = True
        failed, entries = run('documents', retry, expected_state='failed')
        assert not entries
        model.invalid_refinement = False
        retried, entries = run('documents', retry)
        assert retried == failed and [e['value'] for e in entries] == ['Atlas']

        configure('analysis', background=True)
        model.calls.clear()
        api('/v1/chat/completions', {'model': 'chat', 'messages': [
            {'role': 'user', 'content': 'Файл внутреннего проекта: '+root+'notes/LyraDemo.txt'}]},
            headers={'X-Kaponir-Session': 'path-background'})
        wait_for(lambda: any(e['value'] == 'LyraDemo' and e['path_suffix']
                             for e in api('/api/v1/ui/learning')['entries']))
        assert all(s not in json.dumps(p, ensure_ascii=False) for _, p, _ in model.calls
                   for s in (root, 'ANON_PATH_'))

        masked = api('/api/v1/anonymize', {'text': sensitive, 'session': 'roundtrip'})['text']
        assert masked.endswith('/notes/Orion.txt')
        assert api('/api/v1/deanonymize', {'text': masked, 'session': 'roundtrip'})['text'] == sensitive

        item, = [row for row in api(review+'?status=pending&q=Orion')['items'] if row['value'] == 'Orion']
        api(f"{review}/{item['origin']}/{item['id']}/mask", {'kind': 'PROJ', 'scope': 'team'})
        twice = api('/api/v1/anonymize', {'text': sensitive, 'session': 'both-rules'})['text']
        assert re.fullmatch(r'ANON_PATH_\w+/notes/ANON_PROJ_\w+\.txt', twice), twice
        assert api('/api/v1/deanonymize', {'text': twice, 'session': 'both-rules'})['text'] == sensitive
        assert not any(row['value'] == 'Orion' for row in api(review+'?status=pending&q=Orion')['items'])



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--baseline', type=Path, help='Also exercise upgrade from the previous release.')
    parser.add_argument('--output', type=Path, default=Path('artifacts/path-suffix'))
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    if args.baseline:
        args.baseline = args.baseline.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(('127.0.0.1', 0), Model) as model, tempfile.TemporaryDirectory() as home:
        model.calls, model.sent, model.invalid_refinement = [], [], False
        threading.Thread(target=model.serve_forever, daemon=True).start()
        try:
            exercise(args, model, home)
        finally:
            model.shutdown()
    print('Verified session masking, review spans, grouped refinement, ordinary paths and restoration.')


if __name__ == '__main__':
    main()
