"""Exercise the operator workspace against a fresh encrypted vault and a local model fixture."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright
from validate_console import free_port, wait_for


def word(number):
    suffix = ''
    while True:
        suffix = chr(97 + number % 26) + suffix
        number = number // 26 - 1
        if number < 0:
            return 'zorgworkspace' + suffix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--large', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    seen, active, peak = [], 0, 0
    lock, release = threading.Lock(), threading.Event()
    release.set()
    allowed_packets = None

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            nonlocal active, peak, allowed_packets
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            items = json.loads(data['messages'][-1]['content'])['items']
            with lock:
                active += 1
                peak = max(peak, active)
                seen.extend(item['value'] for item in items)
                may_finish = allowed_packets is not None and allowed_packets > 0
                if may_finish:
                    allowed_packets -= 1
            if not may_finish:
                release.wait(30)
            time.sleep(.025)
            with lock:
                active -= 1
            result = [{'id':row['id'],'kind':'PROJ','scope':'team','action':'mask',
                       'confidence':.9,'reason':'Private project fixture'} for row in reversed(items)]
            encoded = json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'decisions':result})}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (ConnectionResetError, BrokenPipeError):
                pass

    model = ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=model.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='kaponir-workspace-') as tmp:
        home = Path(tmp)
        output = args.output or home
        output.mkdir(parents=True,exist_ok=True)
        env = {k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')}
        subprocess.run([binary,'--home',home,'init'],env=env,check=True,capture_output=True)
        base = 'http://127.0.0.1:'+str(free_port())

        def api(path,body=None,method=None):
            req = Request(base+path,data=None if body is None else json.dumps(body).encode(),method=method,
                          headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=90) as r:
                return json.load(r)

        def start():
            process = subprocess.Popen([binary,'--home',home,'serve','--listen',base.split('//')[1]],
                                       stdout=log,stderr=log,env=env)
            try:
                wait_for(lambda:api('/api/v1/ui/info'))
            except Exception:
                process.terminate()
                process.wait()
                raise
            return process

        def seed(start_at,count):
            for first in range(start_at,start_at+count,100):
                text='\n'.join('/srv/'+word(i)+'/config.toml' for i in range(first,min(first+100,start_at+count)))
                api('/api/v1/ui/mask',{'text':text,'commit':True})

        with (output/'workspace-server.log').open('w') as log:
            proc=start()
            try:
                settings=api('/api/v1/ui/analysis')['settings']
                settings.update(provider='api',url=f'http://127.0.0.1:{model.server_port}/v1/chat/completions',
                                model='workspace-fixture',parallelism=4,background=False,auto_apply=False,enabled=True)
                settings['inference']['batch_size']=8
                api('/api/v1/ui/analysis',{'settings':settings},'PUT')
                cat=api('/api/v1/ui/categorization')['settings'];cat['provider']='none'
                api('/api/v1/ui/categorization',{'settings':cat},'PUT')
                seed(0,137)
                assert api('/api/v1/ui/review?q=zorgworkspace')['total']==137
                shared=api('/api/v1/ui/workspace/model',{'use_analysis_api':True},'PUT')
                assert shared['analysis']['connection_id']==shared['categorization']['connection_id']
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path=shutil.which('chromium'),args=['--disable-background-networking'])
                    page=browser.new_page(locale='ru-RU',viewport={'width':1440,'height':940})
                    errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.set_default_timeout(6500)
                    page.goto(base+'/ui/#review')
                    section=page.locator('#tab-review')
                    expect(section.locator('#review-model-source')).to_have_count(0)
                    section.locator('.finding-filters summary').click()
                    section.locator('#review-query').fill('zorgworkspace')
                    expect(section.locator('#review-list')).to_have_attribute('aria-busy','false')
                    expect(section.get_by_role('button',name='В датасет все 137',exact=True)).to_be_visible()
                    section.locator('.finding-filters summary').click()
                    first_check=section.locator('.findings-table input[type=checkbox]').first
                    first_check.focus()
                    page.keyboard.press('Space')
                    expect(section.locator('#review-select-page')).to_have_js_property('indeterminate',True)
                    expect(section.get_by_role('button', name='В датасет · 1',exact=True)).to_be_visible()
                    page.keyboard.press('Space')
                    section.get_by_role('button',name='В датасет все 137',exact=True).click()
                    expect(section.get_by_role('status')).to_contain_text('Добавлено 137')
                    section.get_by_role('button',name='Открыть добавленные примеры').click()
                    page.locator('#vault-dataset .finding-value').first.click()
                    page.locator('.dataset-marks .work-row button').first.click()
                    page.locator('#vault-dataset').get_by_role('combobox',name='Действие',exact=True).select_option('mask')
                    page.get_by_role('button',name='Сохранить проверенным').click()
                    wait_for(lambda:api('/api/v1/ui/dataset?status=checked')['total']==1)
                    with page.expect_download() as training_event:
                        page.get_by_role('button', name='Выгрузить для обучения').click()
                    training=json.loads(Path(training_event.value.path()).read_text())
                    assert set(training)=={'id','group','source','language','format','text','spans'}
                    assert training['spans'][0]['action']=='mask'
                    with page.expect_download() as drafts_event:
                        page.get_by_role('button', name='Выгрузить черновики').click()
                    drafts=Path(drafts_event.value.path()).read_text().splitlines()
                    assert len(drafts)==136
                    correction=json.loads(drafts[0])
                    correction['spans']=[]
                    page.locator('#vault-dataset details summary').click()
                    page.locator('#vault-dataset input[type=file]').set_input_files({
                        'name':'corrections.jsonl','mimeType':'application/x-ndjson','buffer':json.dumps(correction).encode()})
                    wait_for(lambda:any(row['id']==correction['id'] and row['revision']==2 and not row['spans']
                                        for row in api('/api/v1/ui/dataset?status=draft&limit=250')['items']))

                    page.locator('[data-tab=review]').click()
                    section.get_by_role('button',name='В датасет все 137',exact=True).click()
                    expect(section.get_by_role('status')).to_contain_text('уже были 137')
                    section.locator('#review-size').select_option('50')
                    section.get_by_role('button',name='Далее',exact=True).click()
                    expect(section.locator('#review-pagination')).to_contain_text('51–100')
                    section.get_by_role('button',name='Выбрать все 137 по фильтру').click()
                    expect(section.locator('#review-selection')).to_contain_text('Выбрано: 137')
                    section.locator('.finding-value').first.click()
                    expect(section.locator('.finding-detail')).to_be_visible()
                    section.get_by_role('button',name='Назад к списку').click()
                    release.clear()
                    section.get_by_role('button',name='Проверить моделью · 137').click()
                    page.wait_for_url(re.compile(r'.*#jobs/review/[a-f0-9]{64}'))
                    url=page.url
                    jid=url.rsplit('/',1)[-1]
                    expect(page.locator('#jobs-content')).to_contain_text('Принято 137')
                    page.locator('[data-tab=overview]').click()
                    expect(page.locator('#job-indicator')).to_contain_text('workspace-fixture')
                    page.locator('#job-indicator').click()
                    page.wait_for_url(url)
                    release.set()
                    expect(page.locator('#jobs-content')).to_contain_text('Обработано 137 из 137',timeout=40000)
                    assert len(set(seen))==137 and 1 < peak <= 4,(len(seen),peak)
                    page.reload()
                    expect(page.locator('#jobs-content')).to_contain_text('Обработано 137 из 137')
                    page.get_by_role('button',name='Вернуться к выборке').click()
                    expect(section.locator('#review-pagination')).to_contain_text('51–100')
                    expect(section.locator('#review-selection')).to_contain_text('Выбрано: 137')
                    for lang in ('ru','en'):
                        page.locator(f'#language-switch label:has(input[value={lang}])').click()
                        for theme in ('dark','light'):
                            page.locator(f'#theme-switch label:has(input[value={theme}])').click()
                            for width in (390,768,1440):
                                page.set_viewport_size({'width':width,'height':940})
                                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,lang,theme)
                    page.screenshot(path=str(output/'workspace-findings.png'))
                    assert not errors,errors
                    browser.close()
                proc.terminate();proc.wait();proc=start()
                assert api('/api/v1/ui/review/jobs/'+jid)['completed']==137
                assert api('/api/v1/ui/dataset?status=checked')['total']==1
                doc=api('/api/v1/ui/discover',{'remember':True,'documents':[{'name':'contract.md','text':'Цена: 250 USD'}]})
                assert doc['finding_keys']
                empty=api('/api/v1/ui/discover/sessions',{'remember':True})
                assert not empty['sessions_analyzed'] and not empty['report']['suggestions']
                saved=api('/api/v1/ui/discover/profile')['report']
                assert set(doc['finding_keys']).issubset(saved['finding_keys'])
                applied=api('/api/v1/ui/discover/apply',{'saved':True,'scope':'team','ids':[doc['suggestions'][0]['id']]})
                assert applied['applied']==1
                source=api('/api/v1/ui/review?status=all&q=contract.md')['items'][0]
                assert source['state']=='covered' and source['document_rule']['scope']=='team'
                related=api('/api/v1/ui/review/selection',{'status':'all','rule_id':source['document_rule']['id'],'rule_scope':'team'})
                assert related['total']>0

                assert any(rule['id']==source['document_rule']['id'] for scope in api('/api/v1/rules')
                           for rule in scope['file'].get('rule',[]))

                values=api('/api/v1/ui/review?q=zorgworkspace&limit=32')['items']
                body={'keys':[item['key'] for item in values],'request_id':'workspace-interrupted-replay'}
                with lock:
                    allowed_packets=1
                release.clear()
                interrupted=api('/api/v1/ui/review/jobs',body)['id']
                same=api('/api/v1/ui/review/jobs',body)
                assert same['id']==interrupted
                job_url='/api/v1/ui/review/jobs/'+interrupted
                wait_for(lambda:0<api(job_url+'?brief=true')['completed']<len(values))
                proc.kill()
                proc.wait()
                release.set()
                proc=start()
                old=api(job_url)
                assert old['state']=='failed' and old['error']=='interrupted_review'
                assert api('/api/v1/ui/review/jobs',body)['id']==interrupted
                old_results=set(old['results'])
                again=api(job_url+'/retry',{'request_id':'only-unfinished-retry'})
                retried=wait_for(lambda:(j if (j:=api('/api/v1/ui/review/jobs/'+again['id']))['state']=='succeeded' else None))
                assert set(retried['results'])==set(body['keys'])-old_results
                if args.large:
                    seed(137,10000-137)
                    snapshot=api('/api/v1/ui/review/selection',{'q':'zorgworkspace'})
                    assert snapshot['total']==10000,snapshot
                    page=api('/api/v1/ui/review?snapshot='+snapshot['snapshot']+'&offset=9750&limit=250')
                    assert len(page['items'])==250 and page['total']==10000
                    payload={'snapshot':snapshot['snapshot'],'request_id':'workspace-large-collection'}
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        receipts=list(pool.map(lambda _:api('/api/v1/ui/dataset/collect',payload),range(2)))
                    assert receipts[0]==receipts[1]
                    assert api('/api/v1/ui/dataset')['total']==10000
                    assert api('/api/v1/ui/dataset/collections/'+receipts[0]['id']+'/undo',{})['removed']==9863
                print(f'Workspace flows passed. 137 model results, peak concurrency {peak}, persistent history and labels.'+
                      (' Verified a stable selection and idempotent collection of 10,000 findings.' if args.large else ''))
            finally:
                release.set();proc.terminate();proc.wait();model.shutdown();model.server_close()


if __name__=='__main__':
    main()
