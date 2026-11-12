#!/usr/bin/env python3
"""Exercise real worker/proxy/UI installation flows with tiny fixtures, without internet or LLM weights."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from kaponir_worker import FILES, Worker, configuration, handler
from validate_console import free_port, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--output', type=Path, default=Path('artifacts/models'))
    args = parser.parse_args()
    binary = args.binary.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    downloads, fail_download, release = [], threading.Event(), threading.Event()
    fail_download.set()
    fixture_data = {variant: ('fixture weights ' + variant).encode() for variant in FILES}

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def do_GET(self):
            downloads.append(self.path)
            if fail_download.is_set():
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'RAW_PROVIDER_BODY_MUST_NOT_LEAK')
                return
            release.wait(timeout=10)
            data = fixture_data[self.path.lstrip('/')]
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    catalog = {variant: (item[0], len(fixture_data[variant]), hashlib.sha256(fixture_data[variant]).hexdigest(),
                         f'http://127.0.0.1:{origin.server_port}/{variant}') for variant, item in FILES.items()}
    with tempfile.TemporaryDirectory(prefix='kaponir-models-') as directory, patch.dict(FILES, catalog):
        home = Path(directory)
        worker_home = home / 'worker-home'
        cfg = configuration(worker_home)
        cfg['idle'] = True
        cfg['input_root'].mkdir(parents=True)
        worker = Worker(worker_home, cfg, str(binary))
        worker_server = ThreadingHTTPServer(('127.0.0.1', 0), handler(worker, 'fixture-worker-token'))
        threading.Thread(target=worker_server.serve_forever, daemon=True).start()
        processing = threading.Thread(target=worker.loop, daemon=True)
        processing.start()
        env = {key: value for key, value in os.environ.items() if not key.startswith('KAPONIR_')}
        env.update(KAPONIR_WORKER_URL=f'http://127.0.0.1:{worker_server.server_port}',
                   KAPONIR_WORKER_TOKEN='fixture-worker-token')
        proxy_home = home / 'proxy-home'
        subprocess.run([binary, '--home', proxy_home, 'init'], check=True, capture_output=True, env=env)
        port = free_port()
        base = f'http://127.0.0.1:{port}'
        worker.cfg['api_url'] = base

        def api(path, body=None, method=None):
            request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                             headers={'Content-Type': 'application/json'}, method=method)
            with urllib.request.urlopen(request, timeout=20) as response: return json.load(response)

        def model_ready(model):
            return api('/api/v1/ui/worker')['models'][model]['downloaded']

        log = (args.output / 'server.log').open('w')
        process = subprocess.Popen([binary, '--home', proxy_home, 'serve', '--listen', f'127.0.0.1:{port}'],
                                   stdout=log, stderr=log, env=env)
        try:
            wait_for(lambda: api('/api/v1/ui/info'))
            legacy_settings = api('/api/v1/ui/analysis')['settings']
            legacy_settings.update(provider='local', model='q4')
            api('/api/v1/ui/analysis', {'settings': legacy_settings}, method='PUT')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=shutil.which('chromium') or shutil.which('google-chrome'),
                                                       headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, locale='ru-RU', color_scheme='dark')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/ui/#analysis')
                page.locator('#analysis-provider').select_option('local')
                assert page.locator('#analysis-local-model').input_value() == 'qwen3-0.6b'
                page.locator('#analysis-model-install .model-state').filter(has_text='не установлена').wait_for()
                assert page.locator('#analysis-download').is_disabled()
                page.locator('#analysis-verify').click()
                wait_for(lambda: not page.locator('#analysis-verify').is_disabled())
                assert not worker.enabled and not downloads
                page.locator('#analysis-model-install .model-offline summary').click()
                offline = page.locator('#analysis-model-install .model-offline')
                assert str(worker.models / FILES['q4'][0]) in offline.inner_text()
                assert FILES['q4'][2] in offline.inner_text()
                assert str(worker.runtime) in offline.inner_text()
                assert 'llama-b12038-bin-ubuntu-x64.tar.gz' in offline.inner_text()

                # Saving an enabled configuration must wait without downloading anything.
                page.locator('#analysis-enabled').check()
                page.locator('#analysis-save').click()
                try:
                    wait_for(lambda: worker.enabled)
                except AssertionError:
                    page.screenshot(path=str(args.output / 'failure.png'), full_page=True)
                    print(json.dumps({'errors': errors, 'result': page.locator('#analysis-result').inner_text(),
                        'invalid': page.locator('#analysis-form').evaluate("form=>[...form.elements].filter(x=>!x.checkValidity()).map(x=>({id:x.id,value:x.value,message:x.validationMessage}))")}, ensure_ascii=False))
                    raise
                page.locator('#learning-state').filter(has_text='ожидает подготовки модели обнаружения').wait_for()
                assert api('/api/v1/ui/analysis')['settings']['enabled']
                assert not downloads

                page.locator('#analysis-download').click()
                wait_for(lambda: any(job['kind'] == 'download' and job['state'] == 'failed' for job in worker.jobs()))
                page.locator('#analysis-model-install .installation-error').filter(has_text='ошибку HTTP').wait_for()
                assert 'RAW_PROVIDER_BODY_MUST_NOT_LEAK' not in json.dumps(worker.jobs())
                page.locator('#analysis-model-install .installation-error summary').click()
                assert '503' in page.locator('#analysis-model-install .installation-error').inner_text()

                # Retrying uses a single job even when two clients request the same alias.
                worker.runtime.parent.mkdir(parents=True, exist_ok=True)
                worker.runtime.write_text('#!/bin/sh\nexit 0\n')
                worker.runtime.chmod(0o755)
                fail_download.clear()
                page.locator('#analysis-download').click()
                wait_for(lambda: len(downloads) == 2)
                first = api('/api/v1/ui/worker/models/download', {'model': 'q4'})
                second = api('/api/v1/ui/worker/models/download', {'model': 'qwen3-0.6b'})
                assert first['id'] == second['id']
                release.set()
                wait_for(lambda: model_ready('qwen3-0.6b'))
                page.locator('#analysis-model-install .model-state').filter(has_text='проверены и готовы').wait_for()
                wait_for(lambda: not page.locator('#analysis-test').is_disabled())
                assert len(downloads) == 2

                # A different selected model has its own presence/integrity state.
                page.locator('#analysis-local-model').select_option('qwen3.5-0.8b')
                page.locator('#analysis-save').click()
                page.locator('#learning-state').filter(has_text='ожидает подготовки модели обнаружения').wait_for()
                assert api('/api/v1/ui/worker')['models']['qwen3-0.6b']['downloaded']
                path = worker.models / FILES['qwen3.5-0.8b'][0]
                path.write_bytes(b'x' * len(fixture_data['qwen3.5-0.8b']))
                page.locator('#analysis-verify').click()
                page.locator('#analysis-model-install .installation-error').filter(has_text='Контрольная сумма').wait_for()
                assert page.locator('#analysis-test').is_disabled()
                path.write_bytes(fixture_data['qwen3.5-0.8b'])
                page.locator('#analysis-verify').click()
                wait_for(lambda: model_ready('qwen3.5-0.8b'))
                page.locator('#analysis-model-install .model-state').filter(has_text='проверены и готовы').wait_for()
                wait_for(lambda: 'ожидает подготовки' not in page.locator('#learning-state').inner_text())

                page.locator('#analysis-navigation [data-go=categorization]').click()
                page.locator('#categorization-local-model').select_option('qwen3-0.6b-q8')
                page.locator('#categorization-save').click()
                page.locator('#learning-state').filter(has_text='ожидает подготовки модели категоризации').wait_for()
                (worker.models / FILES['q5'][0]).write_bytes(fixture_data['q5'])
                page.locator('#categorization-verify').click()
                wait_for(lambda: model_ready('qwen3-0.6b-q8'))
                wait_for(lambda: 'ожидает подготовки' not in page.locator('#learning-state').inner_text())
                assert len(downloads) == 2

                # Status polling and language changes preserve unsaved form values and keyboard focus.
                page.locator('#analysis-navigation [data-go=analysis]').click()
                page.locator('#analysis-form .advanced-settings summary').click()
                page.locator('#analysis-timeout').fill('77')
                page.locator('#analysis-verify').focus()
                page.evaluate('loadWorkerSummary()')
                wait_for(lambda: page.locator('#analysis-timeout').input_value() == '77')
                assert page.evaluate("document.activeElement.id === 'analysis-verify'")
                assert page.locator('#analysis-model-install .model-offline').evaluate('(node)=>node.open')

                page.locator('#analysis-navigation [data-go=worker]').click()
                assert not page.locator('#worker-s1').is_visible()
                page.locator('#worker-mode').select_option('local')
                assert page.locator('#worker-s1').is_visible()
                page.locator('#worker-scan-sessions').click()
                page.locator('#worker-jobs').filter(has_text='Нет данных для анализа').wait_for()
                legacy = worker.submit('download', model='q4')
                worker.update(legacy['id'], state='failed', errors=[{'type': 'URLError', 'message': 'worker operation failed'}])
                page.evaluate('loadWorkerSummary()')
                page.locator('#worker-jobs').filter(has_text='Причина старого сбоя не сохранена').wait_for()

                page.locator('#worker-toggle').click()
                wait_for(lambda: not worker.enabled)
                page.locator('#analysis-navigation [data-go=analysis]').click()
                page.locator('#analysis-verify').click()
                wait_for(lambda: not page.locator('#analysis-verify').is_disabled())
                assert not worker.enabled and len(downloads) == 2

                # A successful settings PUT followed by a failed start must report the partial outcome.
                page.route('**/api/v1/ui/worker/start', lambda route: route.fulfill(status=503, json={'error': {'message': 'fixture service unavailable'}}))
                page.locator('#analysis-save').click()
                page.locator('#analysis-result.err').filter(has_text='Настройки сохранены, но служба').wait_for()
                assert api('/api/v1/ui/analysis')['settings']['enabled']
                assert not worker.enabled
                page.unroute('**/api/v1/ui/worker/start')

                # An unknown saved/preset ID is a catalog problem, not a missing weight file.
                unknown=worker.status();unknown['models'].pop('minicpm5-2b');unknown['catalog']=[item for item in unknown['catalog'] if item['id']!='minicpm5-2b']
                page.locator('#analysis-local-model').select_option('minicpm5-2b')
                page.route('**/api/v1/ui/worker',lambda route:route.fulfill(json=unknown))
                page.evaluate('loadWorkerSummary()')
                page.locator('#analysis-model-install .model-state').filter(has_text='нет в каталоге').wait_for()
                assert page.locator('#analysis-download').is_disabled() and page.locator('#analysis-verify').is_disabled()
                page.unroute('**/api/v1/ui/worker');page.locator('#analysis-local-model').select_option('qwen3.5-0.8b');page.evaluate('loadWorkerSummary()')

                # An older worker and an unreachable worker must never look like a missing model.
                old = worker.status()
                old.pop('model_verification')
                old.pop('runtime')
                for model in old['models'].values():
                    for key in ['state', 'path', 'sha256', 'url']: model.pop(key, None)
                page.route('**/api/v1/ui/worker', lambda route: route.fulfill(json=old))
                page.evaluate('loadWorkerSummary()')
                page.locator('#analysis-model-install .model-state').filter(has_text='Обновите воркер').wait_for()
                assert page.locator('#analysis-verify').is_disabled()
                page.unroute('**/api/v1/ui/worker')
                page.route('**/api/v1/ui/worker', lambda route: route.fulfill(json={'online': False, 'configured': True, 'enabled': False, 'state': 'error', 'can_manage': True}))
                page.evaluate('loadWorkerSummary()')
                page.locator('#analysis-model-install .model-state').filter(has_text='Наличие файлов пока не проверено').wait_for()
                page.unroute('**/api/v1/ui/worker')
                page.evaluate('loadWorkerSummary()')
                page.locator('#analysis-model-install .model-state').filter(has_text='проверены и готовы').wait_for()

                for language in ['en', 'ru']:
                    page.locator(f'#language-switch label:has(input[value={language}])').click()
                    wait_for(lambda: page.locator('html').get_attribute('lang') == language)
                    assert page.locator('#analysis-timeout').input_value() == '77'
                    if language == 'en':
                        assert not re.search('[А-Яа-яЁё]', page.locator('#analysis-model-install').inner_text())
                    for theme in ['light', 'dark']:
                        page.locator(f'#theme-switch label:has(input[value={theme}])').click()
                        for width in [390, 1440]:
                            page.set_viewport_size({'width': width, 'height': 1000})
                            for tab in ['analysis', 'categorization', 'worker']:
                                page.evaluate('(tab)=>showTab(tab)', tab)
                                page.wait_for_timeout(120)
                                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (language, theme, width, tab)
                            page.evaluate("showTab('analysis')")
                            page.screenshot(path=str(args.output / f'models-{language}-{theme}-{width}.png'), full_page=True)
                # Real worker/proxy/UI HF flow, with only metadata and tiny GGUF bytes mocked.
                for role in ['analysis','categorization','worker']:
                    select='#worker-model' if role=='worker' else f'#{role}-local-model'
                    for model in ['minicpm5-2b','qwen3.5-2b','huihui-qwen3.5-2b-abliterated']:
                        assert page.locator(select+f' option[value="{model}"]').count()==1
                hf_bytes=b'GGUFfixture-custom-model';hf_sha=hashlib.sha256(hf_bytes).hexdigest();revision='a'*40
                original_open=urllib.request.urlopen;metadata_requests=[]
                def hf_open(request,*params,**kwargs):
                    url=request.full_url if isinstance(request,urllib.request.Request) else str(request)
                    if url.startswith('https://huggingface.co/api/models/'):
                        metadata_requests.append(url)
                        files=[{'rfilename':'Custom-Q8_0.gguf','lfs':{'size':len(hf_bytes),'sha256':hf_sha}},
                               {'rfilename':'Custom-Q4_K_M.gguf','lfs':{'size':len(hf_bytes),'sha256':hf_sha}}]
                        if '/acme/safetensors/' in url:files=[{'rfilename':'model.safetensors'}]
                        return io.BytesIO(json.dumps({'sha':revision,'siblings':files}).encode())
                    if url.startswith('https://huggingface.co/acme/custom/resolve/'):
                        downloads.append('custom-hf');return io.BytesIO(hf_bytes)
                    return original_open(request,*params,**kwargs)
                with patch('kaponir_worker.urllib.request.urlopen',side_effect=hf_open):
                    page.set_viewport_size({'width':1440,'height':1000});page.evaluate("showTab('analysis')")
                    page.locator('[data-add-hf=analysis]').click();page.locator('#hf-model-url').fill('https://example.com/weights.gguf')
                    page.locator('#hf-model-resolve').click();page.locator('#hf-model-result .err').filter(has_text='HTTPS-ссылку').wait_for()
                    assert not metadata_requests
                    page.locator('#hf-model-url').fill('https://huggingface.co/acme/custom')
                    page.wait_for_timeout(2700);assert page.locator('#hf-model-url').input_value()=='https://huggingface.co/acme/custom'
                    page.locator('#hf-model-resolve').click();page.locator('#hf-model-file').wait_for()
                    assert page.locator('#hf-model-file').input_value()=='Custom-Q4_K_M.gguf'
                    assert page.locator('#hf-model-file option').count()==2
                    assert revision in page.locator('#hf-model-result').inner_text() and hf_sha in page.locator('#hf-model-result').inner_text()
                    for width in [390,1440]:
                        page.set_viewport_size({'width':width,'height':1000});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.screenshot(path=str(args.output/f'hf-dialog-ru-dark-{width}.png'),full_page=True)
                    saved_before=api('/api/v1/ui/analysis')['settings'];downloads_before=len(downloads)
                    page.locator('#hf-model-add').click();wait_for(lambda:not page.locator('#hf-model-dialog').evaluate('(dialog)=>dialog.open'))
                    custom_id=page.locator('#analysis-local-model').input_value();assert re.fullmatch('hf-[a-f0-9]{24}',custom_id)
                    assert api('/api/v1/ui/analysis')['settings']==saved_before
                    assert len(downloads)==downloads_before
                    assert all(page.locator(selector+f' option[value="{custom_id}"]').count()==1 for selector in ['#analysis-local-model','#categorization-local-model','#worker-model'])
                    if not worker.enabled:api('/api/v1/ui/worker/start',{})
                    page.evaluate('loadWorkerSummary()');wait_for(lambda:page.locator('#analysis-download').is_enabled())
                    page.locator('#analysis-download').click();wait_for(lambda:model_ready(custom_id))
                    page.locator('#analysis-model-install .model-state').filter(has_text='проверены и готовы').wait_for()
                    custom_state=api('/api/v1/ui/worker')['models'][custom_id]
                    assert Path(custom_state['path']).read_bytes()==hf_bytes and custom_state['sha256']==hf_sha
                    page.locator('#analysis-save').click();wait_for(lambda:api('/api/v1/ui/analysis')['settings']['model']==custom_id)
                    page.reload();wait_for(lambda:page.locator('#analysis-local-model').input_value()==custom_id)
                    page.locator('#analysis-model-install .model-offline summary').click()
                    assert custom_state['path'] in page.locator('#analysis-model-install .model-offline').inner_text()
                    page.locator('[data-add-hf=analysis]').click();page.locator('#hf-model-url').fill('https://huggingface.co/acme/safetensors')
                    page.locator('#hf-model-resolve').click();page.locator('#hf-model-result .err').filter(has_text='нет подходящего GGUF').wait_for()
                    assert page.locator('#hf-model-add').is_disabled();page.keyboard.press('Escape')
                    assert page.locator('#analysis-local-model').input_value()==custom_id
                assert not errors, errors
                browser.close()
            report = {'passed': True, 'downloads': len(downloads), 'cases': ['offline verification', 'network error and retry',
                      'duplicate installation', 'same-size corruption', 'independent model stages', 'waiting and readiness',
                      'unsaved form and focus', 'empty sessions', 'legacy errors', 'partial save', 'old and offline worker',
                      'languages, themes and viewport sizes','unknown catalog ID','three additional presets','HF repository and file selection',
                      'invalid HF URL and unsupported format','HF import without hidden download or settings changes',
                      'custom GGUF integrity, selection and reload','custom offline instructions']}
            (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report))
        finally:
            release.set()
            process.terminate()
            process.wait(timeout=20)
            log.close()
            worker.stop.set()
            worker.stop_runtime()
            processing.join(timeout=10)
            if worker.verification_thread: worker.verification_thread.join(timeout=10)
            worker_server.shutdown()
            worker_server.server_close()
            origin.shutdown()
            origin.server_close()


if __name__ == '__main__': main()
