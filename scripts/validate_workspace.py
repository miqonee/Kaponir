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
from validate_console import choose_preference, free_port, wait_for


def word(number):
    suffix = ''
    while True:
        suffix = chr(97 + number % 26) + suffix
        number = number // 26 - 1
        if number < 0:
            return 'zorgworkspace' + suffix


def verify_repeated_findings(page, api, output):
    value = 'zorgdedupaccount'
    api('/api/v1/ui/mask', {'text': f'/srv/{value}/config.toml', 'commit': True})
    api('/api/v1/ui/mask', {'text': '/srv/zorgdedupneighbor/config.toml', 'commit': True})
    for index in range(3):
        text = f'ticket {index}\nПроект: {value}\naccount: {value}'
        api('/api/v1/ui/discover', {'remember': True, 'documents': [{'name': f'origin-{index}.txt', 'text': text}]})

    findings = '/api/v1/ui/review?q=' + value
    raw = api(findings + '&status=all')['items']
    assert len(raw) == 4
    assert {(item['origin'], item['kind']) for item in raw} == {('detector', 'PATH'), ('learning', 'PROJ')}
    assert api(findings)['total'] == 1
    assert api('/api/v1/ui/info')['review_pending'] == api('/api/v1/ui/review')['total']
    snapshot = api('/api/v1/ui/review?q=zorgdedup&stable=true&limit=1')['snapshot']
    frozen = api('/api/v1/ui/review?snapshot=' + snapshot)
    assert frozen['total'] == 2 and sum(len(i.get('occurrence_keys', [i['key']])) for i in frozen['items']) == 5
    group = next(item for item in frozen['items'] if item['value'] == value)
    only_neighbor = api('/api/v1/ui/dataset/collect', {'snapshot': snapshot, 'excluded': [group['key']],
                                                        'request_id': 'exclude-whole-value'})
    assert only_neighbor['added'] == only_neighbor['new_occurrences'] == 1

    page.locator('[data-tab=review]').click()
    section = page.locator('#tab-review')
    section.locator('[data-review-view=pending]').click()
    section.locator('#review-query').fill(value)
    expect(section.get_by_role('button', name='В датасет все 1', exact=True)).to_be_visible()
    section.locator('.finding-value').click()
    occurrences = section.get_by_role('button', name='Показать вхождения · 4')
    expect(occurrences).to_be_visible()
    page.screenshot(path=output/'findings-one-value.png')
    occurrences.click()
    expect(section.locator('.finding-value')).to_have_count(4)
    section.get_by_role('button', name='Все находки', exact=True).click()
    section.locator('#review-query').fill(value)
    section.get_by_role('button', name='В датасет все 1', exact=True).click()
    expect(section.locator('#review-result')).to_contain_text('Добавлено 1 · уже были 0')
    section.get_by_role('button', name='Открыть добавленные примеры').click()
    with page.expect_download() as download:
        page.get_by_role('button', name='Выгрузить черновики').click()
    example = json.loads(Path(download.value.path()).read_text())
    assert example['value'] == value
    assert {entry['finding'] for entry in example['occurrences']} == {item['key'] for item in raw}
    for entry in example['occurrences']:
        mark, = entry['spans']
        assert entry['text'].encode()[mark['start_byte']:mark['end_byte']].decode() == value
    page.locator('#vault-dataset .finding-value').click()
    page.get_by_role('button', name='Показать вхождения · 4').click()
    dialog = page.get_by_role('dialog', name='Вхождения значения')
    expect(dialog.locator('li')).to_have_count(4)
    expect(dialog.locator('mark')).to_have_count(4)
    dialog.get_by_role('button', name='Закрыть').click()

    page.locator('[data-tab=review]').click()
    section.locator('#review-query').fill(value)
    section.locator('.finding-value').click()
    section.get_by_role('button', name='Решить вручную').click()
    # Another operator finishes while the decision dialog is open.
    detector = next(item for item in raw if item['origin'] == 'detector')
    route = '/api/v1/ui/review/detector/' + detector['id']
    api(route + '/mask', {'kind': 'USER', 'scope': 'team'})
    rules_before = api('/api/v1/rules')
    page.locator('#review-confirm-apply').click()
    expect(section.locator('#review-result')).to_contain_text('Уже учтено правилами · 1')
    expect(section.locator('.finding-value')).to_have_count(0)
    assert api(findings)['total'] == 0
    assert api(findings + '&status=proposals')['total'] == 0
    assert {item['state'] for item in api(findings + '&status=history')['items']} == {'masked', 'covered'}
    for item in raw:
        path = f"/api/v1/ui/review/{item['origin']}/{item['id']}/mask"
        assert api(path, {})['already_covered']
    assert api('/api/v1/rules') == rules_before
    assert api('/api/v1/ui/info')['review_pending'] == api('/api/v1/ui/review')['total']
    page.screenshot(path=output/'findings-after-mask.png')
    section.get_by_role('button', name='Искать в истории').click()
    expect(section.locator('.finding-value')).to_have_count(4)
    expect(section.locator('#review-query')).to_have_value(value)
    api(route + '/rollback', {})
    reopened = api(findings)
    assert reopened['total'] == 1 and len(reopened['items'][0]['occurrence_keys']) == 4
    api(route + '/mask', {'kind': 'USER', 'scope': 'team'})
    api(route + '/rollback', {})
    assert api(findings)['total'] == 1


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
                    context=browser.new_context(locale='ru-RU',viewport={'width':1440,'height':940})
                    page=context.new_page()
                    errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.set_default_timeout(6500)
                    page.goto(base+'/ui/#review')
                    section=page.locator('#tab-review')
                    expect(section.locator('#review-model-source')).to_have_count(0)
                    expect(section.locator('.finding-filters')).not_to_have_attribute('open', '')
                    expect(section.locator('#review-query')).to_be_visible()
                    section.locator('#review-query').fill('zorgworkspace')
                    expect(section.locator('#review-list')).to_have_attribute('aria-busy','false')
                    expect(section.get_by_role('button',name='В датасет все 137',exact=True)).to_be_visible()
                    expect(section.locator('#review-match-count')).to_contain_text('137')
                    first_check=section.locator('.findings-table input[type=checkbox]').first
                    expect(section.locator('#review-selection')).not_to_be_visible()
                    first_check.focus()
                    page.keyboard.press('Space')
                    expect(section.locator('#review-select-page')).to_have_js_property('indeterminate',True)
                    expect(section.get_by_role('button', name='В датасет · 1',exact=True)).to_be_visible()
                    page.keyboard.press('Space')
                    section.get_by_role('button',name='В датасет все 137',exact=True).click()
                    expect(section.locator('#review-result')).to_contain_text('Добавлено 137')
                    section.get_by_role('button',name='Открыть добавленные примеры').click()
                    page.locator('#vault-dataset .finding-value').first.click()
                    previous_value = page.locator('.dataset-source h2').inner_text()
                    page.locator('.dataset-marks .work-row button').first.click()
                    page.locator('#vault-dataset').get_by_role('combobox',name='Действие',exact=True).select_option('mask')
                    page.get_by_role('button',name='Сохранить и продолжить проверку').click()
                    wait_for(lambda:api('/api/v1/ui/dataset?status=checked')['total']==1)
                    expect(page.locator('.dataset-source h2')).not_to_have_text(previous_value)
                    expect(page.locator('.dataset-editor-nav')).to_contain_text('136')
                    page.get_by_role('button',name='К примерам',exact=True).click()
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
                    expect(section.locator('#review-result')).to_contain_text('уже были 137')
                    example_page = context.new_page()
                    writes = []
                    example_page.on('request',lambda request: writes.append(request.url) if request.method == 'POST' and request.url.endswith('/collect') else None)
                    example_page.goto(base + '/ui/#review')
                    first_value = example_page.locator('#review-list .finding-value').first
                    first_value.wait_for()
                    value = first_value.inner_text()
                    first_value.click()
                    example_page.get_by_role('button',name='Открыть в датасете').click()
                    expect(example_page.locator('.dataset-source h2')).to_have_text(value)
                    assert not writes
                    example_page.close()
                    section.locator('#review-size').select_option('50')
                    expect(section.locator('.finding-value')).to_have_count(50)
                    section.locator('.finding-value').last.click()
                    inspector = section.locator('#finding-detail')
                    expect(inspector.locator('.finding-pager')).to_contain_text('50 из 137')
                    inspector.get_by_role('button',name='Следующая находка').click()
                    expect(inspector.locator('.finding-pager')).to_contain_text('51 из 137')
                    inspector.get_by_role('button',name='Предыдущая находка').click()
                    expect(inspector.locator('.finding-pager')).to_contain_text('50 из 137')
                    inspector.get_by_role('button',name='Следующая находка').click()
                    expect(inspector.locator('.finding-pager')).to_contain_text('51 из 137')
                    inspector.get_by_role('button',name='Назад к списку').click()
                    expect(section.locator('#review-pagination')).to_contain_text('51–100')
                    section.get_by_role('button',name='Выбрать все 137 по фильтру').click()
                    expect(section.locator('#review-selection')).to_contain_text('Выбрано: 137')
                    dock=section.locator('#review-selection')
                    page.evaluate('window.scrollBy(0, 1000)')
                    assert dock.evaluate('el => { const r=el.getBoundingClientRect(); return r.bottom <= innerHeight && r.top > innerHeight / 2 }')
                    dock.screenshot(path=str(output/'findings-selection.png'))
                    page.set_viewport_size({'width':390,'height':844})
                    assert dock.bounding_box()['height'] < 112
                    page.set_viewport_size({'width':1440,'height':940})
                    section.locator('.findings-table tbody tr').first.locator('td').nth(3).click()
                    expect(section.locator('.finding-detail')).to_be_visible()
                    expect(section.locator('.finding-detail mark').first).to_be_visible()
                    page.screenshot(path=str(output/'finding-context.png'))
                    section.get_by_role('button',name='Назад к списку').click()
                    with lock:
                        allowed_packets = 2
                    release.clear()
                    section.get_by_role('button',name='Проверить моделью · 137').click()
                    page.wait_for_url(re.compile(r'.*#jobs/review/[a-f0-9]{64}'))
                    url=page.url
                    jid=url.rsplit('/',1)[-1]
                    expect(page.locator('#jobs-content')).to_contain_text('Принято 137')
                    page.locator('[data-tab=overview]').click()
                    expect(page.locator('#job-indicator')).to_contain_text('В работе')
                    page.locator('#job-indicator').click()
                    preview=page.locator('#job-preview')
                    expect(preview).to_be_visible()
                    expect(preview).to_contain_text('В работе')
                    first_job=preview.locator('.job-preview-row').first
                    expect(first_job).to_contain_text('workspace-fixture')
                    expect(first_job).to_contain_text(re.compile(r'Обработано [1-9]\d* из 137'))
                    expect(first_job.get_by_role('progressbar')).to_have_attribute('max','137')
                    page.screenshot(path=str(output/'jobs-running.png'))
                    preview.locator('.job-preview-row').first.click()
                    page.wait_for_url(url)
                    observer = page.context.new_page()
                    observer.goto(base + '/ui/#review')
                    expect(observer.locator('#review-list').get_by_text('Идёт проверка ИИ', exact=True).first).to_be_visible()
                    focused=observer.locator('.findings-table input[type=checkbox]').first
                    focused.focus()
                    observer.evaluate('loadReview({background:true})')
                    expect(focused).to_be_focused()
                    observer.close()
                    release.set()
                    expect(page.locator('#jobs-content')).to_contain_text('Обработано 137 из 137',timeout=40000)
                    page.screenshot(path=str(output/'jobs-completed.png'))
                    assert len(set(seen))==137 and 1 < peak <= 4,(len(seen),peak)
                    page.reload()
                    expect(page.locator('#jobs-content')).to_contain_text('Обработано 137 из 137')
                    page.get_by_role('button',name='Вернуться к выборке').click()
                    expect(section.locator('#review-pagination')).to_contain_text('51–100')
                    expect(section.locator('#review-selection')).to_contain_text('Выбрано: 137')
                    for lang in ('ru','en'):
                        choose_preference(page, 'language', lang)
                        for theme in ('dark','light'):
                            choose_preference(page, 'theme', theme)
                            for width in (390,768,1440):
                                page.set_viewport_size({'width':width,'height':940})
                                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,lang,theme)
                    page.screenshot(path=str(output/'workspace-findings.png'))
                    choose_preference(page, 'language', 'ru')
                    api('/api/v1/ui/mask', {'text':'Путь /srv/zorgfinaldraft/config.toml', 'commit':True})
                    page.goto(base+'/ui/#review')
                    section.locator('#review-query').fill('zorgfinaldraft')
                    section.get_by_role('button',name='В датасет все 1',exact=True).click()
                    section.get_by_role('button',name='Открыть добавленные примеры').click()
                    page.get_by_role('button',name='Начать проверку · 1',exact=True).click()
                    expect(page.locator('.dataset-source h2')).to_have_text('zorgfinaldraft')
                    dataset_api = '**/api/v1/ui/dataset?*'
                    page.route(dataset_api, lambda route: route.fulfill(status=503, json={}))
                    try:
                        page.get_by_role('button',name='Сохранить и продолжить проверку').click()
                        expect(page.locator('#vault-dataset').get_by_role('button',name='Повторить')).to_be_visible()
                    finally:
                        page.unroute(dataset_api)
                    page.locator('#vault-dataset').get_by_role('button',name='Повторить').click()
                    expect(page.locator('#dataset-feedback')).to_contain_text('Все черновики этой выборки проверены')
                    expect(page.get_by_role('button',name='Выгрузить для обучения')).to_be_enabled()
                    verify_repeated_findings(page, api, output)
                    assert not errors,errors
                    browser.close()
                proc.terminate();proc.wait();proc=start()
                assert api('/api/v1/ui/review/jobs/'+jid)['completed']==137
                assert api('/api/v1/ui/dataset?status=checked')['total']==2
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
                    assert api('/api/v1/ui/dataset?collection='+receipts[0]['id'])['total']==10000
                    assert api('/api/v1/ui/dataset/collections/'+receipts[0]['id']+'/undo',{})['removed']==9863
                print(f'Workspace flows passed. 137 model results, peak concurrency {peak}, persistent history and labels.'+
                      (' Verified a stable selection and idempotent collection of 10,000 findings.' if args.large else ''))
            finally:
                release.set();proc.terminate();proc.wait();model.shutdown();model.server_close()


if __name__=='__main__':
    main()
