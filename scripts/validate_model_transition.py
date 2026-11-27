"""Exercise API profiles, streamed jobs, model switches and review navigation in a real vault and browser."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright
from validate_console import free_port, open_review_actions, wait_for


def check_model_selection(page, base, upstream, api, catalog_reads, slow_started, release_slow):
    saved_analysis = api('/api/v1/ui/analysis')['settings']
    api('/api/v1/ui/analysis', {'settings': {**saved_analysis, 'provider': 'none', 'enabled': False}}, 'PUT')
    private = upstream + '/private/v1'
    catalog = lambda **fields: api('/api/v1/ui/model-catalog', {'role':'categorization', 'url':private, **fields})
    settings = api('/api/v1/ui/categorization')['settings']
    settings['provider'] = 's1'  # Previously saved chat configuration.
    settings['scenario'] = 'B'
    settings['http'].update(url=private, protocol='openai', model='old')
    saved = api('/api/v1/ui/categorization', {'settings':settings, 'key':'fixture-key'}, 'PUT')['settings']
    assert saved['provider'] == 'api' and saved['http']['protocol'] == 'openai' and saved['scenario'] == 'D'
    assert catalog()['status'] == 'available'
    assert catalog(clear_key=True)['status'] == 'unauthorized'
    assert catalog(url=upstream+'/other/v1')['status'] == 'unauthorized'
    assert catalog_reads[-1] == ('/other/v1/models', None)
    assert catalog(url=private+'/models?revision=2', key='fixture-key')['status'] == 'available'
    assert catalog_reads[-1] == ('/private/v1/models?revision=2', 'Bearer fixture-key')

    page.goto(base+'/ui/#categorization')
    expect(page.locator('#session-dialog')).to_be_hidden()
    method = page.locator('#categorization-provider')
    expect(method).to_have_value('api')
    expect(page.locator('#categorization-scenario-field')).to_be_hidden()
    assert method.locator('option:not([hidden])').all_text_contents() == ['Выключено','Локальная модель','Модель по API','S1 Choice']
    picker = page.locator('#categorization-api-catalog')
    manual = page.locator('#categorization-api-model')
    url = page.locator('#categorization-url')
    status = page.locator('#categorization-catalog-status')
    expect(picker).to_be_visible()
    assert picker.locator('option').all_text_contents() == ['Выберите модель','new','old']
    with page.expect_response(lambda r:r.url.endswith('/model-catalog')):
        page.locator('#categorization-catalog-refresh').click()
    picker.select_option('new')
    expect(page.locator('#categorization-api-profile-note')).to_contain_text('32 768')
    page.get_by_role('button',name='Ввести ID вручную').click()
    manual.fill('qwen3-0.6b')  # Also a local catalog ID, but this assignment is remote.
    url.fill(upstream+'/slow/v1')
    assert slow_started.wait(4)
    url.fill(upstream+'/manual/v1')
    expect(status).to_contain_text('не предоставляет каталог')
    release_slow.set()
    with page.expect_response(lambda r:r.url.endswith('/model-catalog')):
        page.locator('#categorization-catalog-refresh').click()
    expect(manual).to_be_visible()
    expect(manual).to_have_value('qwen3-0.6b')
    assert catalog_reads[-1] == ('/manual/v1/models', None)
    # Separate, unsaved drafts for the Choice protocol and chat API.
    method.select_option('s1')
    expect(url).to_have_value('')
    expect(page.locator('#categorization-catalog-actions')).to_be_hidden()
    expect(page.locator('#categorization-scenario-field')).to_be_hidden()
    url.fill(upstream+'/choice')
    manual.fill('choice-fixture')
    method.select_option('api')
    expect(url).to_have_value(upstream+'/manual/v1')
    expect(manual).to_have_value('qwen3-0.6b')
    with page.expect_response(lambda r:r.url.endswith('/ui/categorization') and r.request.method=='PUT') as response:
        page.locator('#categorization-save').click()
    assert response.value.ok
    page.goto(base+'/ui/#settings')
    section = page.locator('#workspace-model-choice')
    expect(section.locator('dd strong').last).to_have_text('qwen3-0.6b')
    expect(section.locator('dd small').last).to_contain_text('Модель по API')
    expect(section.locator('#workspace-local-setup')).to_be_hidden()
    section.get_by_role('button',name='Выбрать локальную модель').click()
    expect(section.locator('#workspace-local-setup')).to_be_visible()

    page.goto(base+'/ui/#categorization')
    method.select_option('s1')
    url.fill(upstream+'/choice')
    manual.fill('choice-fixture')
    with page.expect_response(lambda r:r.url.endswith('/categorization/test')) as probe:
        page.locator('#categorization-test').click()
    assert probe.value.json()['decisions'][0]['scope'] == 'team'
    with page.expect_response(lambda r:r.url.endswith('/ui/categorization') and r.request.method=='PUT') as response:
        page.locator('#categorization-save').click()
    choice = response.value.json()['settings']
    assert choice['provider']=='s1' and choice['http']['protocol']=='systemone'
    method.select_option('none')
    expect(page.locator('#categorization-result')).to_contain_text('несохранённые изменения')
    api('/api/v1/ui/analysis', {'settings': saved_analysis}, 'PUT')
    page.goto(base + '/ui/#analysis')
    expect(page.locator('#analysis-provider')).to_have_value('api')
    expect(page.locator('#analysis-share-api')).to_have_count(0)
    page.goto(base+'/ui/#worker')
    if page.locator('#worker-settings').get_attribute('open') is None:
        page.locator('#worker-settings > summary').click()
    mode = page.locator('#worker-mode')
    mode.select_option('configured')
    expect(page.locator('#worker-assignment')).to_contain_text('S1 Choice · choice-fixture')
    expect(page.locator('#worker-scenario-field')).to_be_hidden()
    expect(page.locator('#worker-job-form .setting-help-button:visible')).to_have_count(0)
    mode.select_option('qwen')
    expect(page.locator('#worker-job-model')).to_be_visible()
    page.locator('#worker-job-model').select_option('qwen3-0.6b-q8')
    expect(page.locator('#worker-model')).to_have_value('qwen3-0.6b-q8')
    mode.select_option('local')
    expect(page.locator('#worker-job-model')).to_have_count(0)
    expect(page.locator('#worker-mode-help')).to_contain_text('без языковой модели')

    page.goto(base + '/ui/#categorization')
    expect(method).to_have_value('s1')
    assert method.locator('option:not([hidden])').all_text_contents() == ['Модель обнаружения', 'S1 Choice']
    assert api('/api/v1/ui/categorization')['settings']['http']['model'] == 'choice-fixture'
    method.select_option('analysis')
    expect(page.locator('#categorization-api-fields')).to_be_hidden()
    with page.expect_response(lambda r: r.url.endswith('/ui/categorization') and r.request.method == 'PUT') as saved:
        page.locator('#categorization-save').click()
    assert saved.value.ok and saved.value.json()['uses_analysis_api']
    category = saved.value.json()['settings']
    assert category['provider'] == 'api' and category['http']['url'] == saved_analysis['url']
    assert category['http']['model'] == saved_analysis['model'] and category['inference'] == saved_analysis['inference']
    assert category['scenario'] == 'D'
    expect(page.locator('#categorization-analysis-note')).to_contain_text(saved_analysis['model'])
    page.goto(base + '/ui/#worker')
    if page.locator('#worker-settings').get_attribute('open') is None:
        page.locator('#worker-settings > summary').click()
    page.locator('#worker-mode').select_option('configured')
    expect(page.locator('#worker-scenario-field')).to_be_hidden()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    binary = args.binary.resolve()
    gates = {name: threading.Event() for name in ('old', 'new')}
    gates['new'].set()
    lock = threading.Lock()
    seen = []
    old_review_calls = 0
    catalog_reads = []
    slow_started, release_slow = threading.Event(), threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, body, status=200):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if '/models' in self.path:
                token = self.headers.get('Authorization')
                catalog_reads.append((self.path, token))
                if self.path.startswith(('/private/','/other/')) and token != 'Bearer fixture-key':
                    return self.reply({}, 401)
                if self.path.startswith('/manual/'):
                    return self.reply({}, 404)
                if self.path.startswith('/slow/'):
                    slow_started.set()
                    release_slow.wait(4)
                    return self.reply({'data':[{'id':'stale-model'}]})
            if self.path == '/status':
                self.reply({'enabled':True,'online':True,'runtime_ready':True,'configured':True,'can_manage':True,'models':{}})
            elif self.path == '/workspace/jobs':
                self.reply({'online':True,'jobs':[]})
            else:
                self.reply({'data':[{'id':'old'}, {'id':'old'}, {'id':'new','context_length':65536,
                                      'top_provider':{'context_length':32768,'max_completion_tokens':4096}}]})

        def do_POST(self):
            nonlocal old_review_calls
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            if self.path == '/choice':
                assert data['model'] == 'choice-fixture' and 'messages' not in data
                return self.reply({'answers':{k:{'type':'choice','choice':'mask:team:PROJ'} for k in data['questions']}})
            model = data.get('model')
            if model not in gates:
                self.reply({'choices':[{'index':0,'message':{'role':'assistant','content':'Принято'},'finish_reason':'stop'}]})
                return
            payload = json.loads(data['messages'][-1]['content'])
            if 'strict-gateway' in payload.get('source',''):
                for parameter, message in [('max_tokens','Use max_completion_tokens instead of max_tokens'),
                                           ('temperature','Only the default temperature is supported'),
                                           ('response_format','JSON schema is not supported')]:
                    if parameter in data:
                        self.reply({'error':{'param':parameter,'code':'unsupported_parameter','message':message}},400)
                        return
                assert data['max_completion_tokens']>0
                self.reply({'choices':[{'message':{'content':'{"candidates":[]}'},'finish_reason':'stop'}]})
                return
            if 'legacy-api' in payload.get('source',''):
                if data.get('stream'):
                    self.reply({'error':{'param':'stream_options','message':'Streaming unsupported'}},422)
                else:
                    response={'choices':[{'message':{'content':'{"candidates":[]}'},'finish_reason':'stop'}]}
                    if 'usage' in payload['source']:
                        response['usage']={'completion_tokens':11}
                    self.reply(response)
                return
            with lock:
                seen.append((model,payload))
                if 'items' in payload and model == 'old':
                    old_review_calls += 1
                may_finish = model == 'old' and 'items' in payload and old_review_calls == 1
            if 'items' in payload:
                assert all('marked_record' in item for item in payload['items'])
                reply = {'decisions':[{'id':i['id'],'verdict':'mask','reason':'Internal project'}
                                      for i in payload['items']]}
            else:
                reply = {'candidates':[]}
            text = json.dumps(reply)
            try:
                if data.get('stream'):
                    self.send_response(200)
                    self.send_header('Content-Type','text/event-stream')
                    self.end_headers()
                    def chunk(value):
                        self.wfile.write(('data: '+json.dumps(value)+'\n\n').encode())
                        self.wfile.flush()
                    chunk({'choices':[{'delta':{'content':text[:3]}}],'usage':{'completion_tokens':3},
                           'timings':{'predicted_n':3,'predicted_per_second':12.5}})
                    if not may_finish:
                        gates[model].wait(75)
                    chunk({'choices':[{'delta':{'content':text[3:]},'finish_reason':'stop'}],
                           'usage':{'completion_tokens':16,'prompt_tokens':12,'total_tokens':28}})
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    if not may_finish:
                        gates[model].wait(75)
                    self.reply({'choices':[{'message':{'content':text},'finish_reason':'stop'}]})
            except (ConnectionResetError,BrokenPipeError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1',0), Provider)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    upstream = f'http://127.0.0.1:{server.server_port}'
    endpoint = upstream+'/v1/chat/completions'
    env = {k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')}
    env['KAPONIR_WORKER_URL'] = upstream
    with tempfile.TemporaryDirectory(prefix='kaponir-transition-') as home:
        subprocess.run([binary,'--home',home,'init'],env=env,capture_output=True,check=True)
        base = f'http://127.0.0.1:{free_port()}'
        def api(path, data=None, method=None, headers=None):
            req = Request(base+path, data=None if data is None else json.dumps(data).encode(),
                          headers={'Content-Type':'application/json',**(headers or {})}, method=method)
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        def documents(names):
            return api('/api/v1/ui/analysis/documents', {'documents':[{'name':s,'text':'Internal instruction '+s} for s in names]})['ids']
        def learning():
            return {j['id']:j for j in api('/api/v1/ui/learning')['jobs']}
        def pending_counts():
            return api('/api/v1/ui/model-queue')
        def switch(name, action=None):
            settings = api('/api/v1/ui/analysis')['settings']
            settings['model'] = name
            body={'settings':settings}
            if action:
                body['queue_change']={'action':action,'revision':pending_counts()['revision']}
            return api('/api/v1/ui/analysis',body,'PUT')
        with (args.output/'proxy.log').open('w') as log:
            proc = subprocess.Popen([binary,'--home',home,'serve','--listen',base.split('//')[1],
                                     '--openai-upstream',upstream],env=env,stdout=log,stderr=log)
            try:
                wait_for(lambda:api('/api/v1/ui/info'))
                settings = api('/api/v1/ui/categorization')['settings']
                settings['provider']='none'
                api('/api/v1/ui/categorization',{'settings':settings},'PUT')
                settings = api('/api/v1/ui/analysis')['settings']
                settings.update(model='old',url=endpoint,enabled=True,provider='api',parallelism=1,background=False,auto_apply=False)
                settings['inference']['batch_size']=1
                api('/api/v1/ui/analysis',{'settings':settings},'PUT')
                category = api('/api/v1/ui/categorization')
                assert category['uses_analysis_api'] and category['settings']['http']['model'] == 'old'
                profile = lambda model: api('/api/v1/ui/model-profile',{'role':'analysis','url':endpoint,'model':model})
                assert profile('new')['inference']['context_tokens']==32768
                assert profile('missing')['reported_context'] is None and profile('old')['inference']['context_tokens']==8192
                # Persist one recommendation and hold the next packet inside generation.
                for name in ['zorgflownorth','zorgflowsouth','zorgfloweast']:
                    api('/api/v1/ui/mask',{'text':'Open /srv/'+name+'/config.toml','commit':True})
                keys = [i['key'] for i in api('/api/v1/ui/review?q=zorgflow')['items']]
                assert len(keys)==3, keys
                job = api('/api/v1/ui/review/jobs',{'keys':keys,'model_source':'categorization'})['id']
                detail = '/api/v1/ui/review/jobs/'+job
                wait_for(lambda:api(detail)['completed']==1)
                # The shared API slot is busy, with both running and queued discovery work.
                ids = documents(['north.txt','south.txt'])
                wait_for(lambda:pending_counts()['learning']==2)
                try:
                    switch('new')
                except HTTPError as e:
                    assert e.code==409 and b'queue_choice_required' in e.read()
                else:
                    raise AssertionError('A live queue was rebound without a choice')
                assert api('/api/v1/ui/analysis')['settings']['model']=='old'
                wait_for(lambda:any(c.get('tokens_per_second')==12.5 for c in api('/api/v1/ui/jobs')['model_activity'].get(job,{}).get('calls',[])))
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1440,'height':960},locale='ru-RU',color_scheme='light')
                    page.goto(base+'/ui/#jobs/review/'+job)
                    expect(page.locator('#jobs-content .job-meter')).to_contain_text('ток/с')
                    page.locator('#jobs-content').screenshot(path=str(args.output/'generation.png'))
                    page.goto(base+'/ui/#analysis')
                    field=page.locator('#analysis-api-catalog')
                    expect(field).to_be_visible()
                    expect(field).to_have_value('old')
                    field.select_option('new')
                    note=page.locator('#analysis-api-profile-note')
                    expect(note).to_contain_text('Контекст по каталогу API')
                    assert page.locator('#analysis-output-tokens').input_value()=='4096'
                    assert page.locator('#analysis-batch-size').input_value()=='20'
                    page.locator('#analysis-form details.advanced-settings').evaluate('e=>e.open=true')
                    page.locator('#analysis-context-tokens').fill('4096')
                    assert page.locator('#analysis-output-tokens').input_value()=='1024'
                    assert page.locator('#analysis-batch-size').input_value()=='5'
                    page.locator('#analysis-api-fields').screenshot(path=str(args.output/'api-profile.png'))
                    save=page.locator('#analysis-save')
                    save.click()
                    dialog=page.get_by_role('dialog',name='Что сделать с текущей очередью?')
                    expect(dialog.get_by_role('button',name='Отмена')).to_be_focused()
                    dialog.screenshot(path=str(args.output/'model-switch.png'))
                    dialog.get_by_role('button',name='Отмена').click()
                    assert api('/api/v1/ui/analysis')['settings']['model']=='old'
                    save.click()
                    dialog.get_by_role('button',name='Перенести незавершённые').click()
                    wait_for(lambda:api(detail)['state']=='succeeded')
                    final=api(detail)
                    assert sorted(r['model'] for r in final['recommendations'].values())==['new','new','old'], final
                    assert len(final['results'])==final['completed']==3
                    gates['old'].set()
                    wait_for(lambda:all(learning()[i]['state']=='succeeded' for i in ids))
                    assert all(learning()[i]['model']=='new' for i in ids)
                    assert documents(['north.txt','south.txt'])==[]
                    saved=api('/api/v1/ui/analysis')['settings']
                    assert 'context-tokens' in saved['api_manual'] and saved['inference']['max_output_tokens']==1024
                    category = api('/api/v1/ui/categorization')
                    assert category['uses_analysis_api'] and category['settings']['http']['model'] == 'new'
                    # Act on successive proposals in place, including keyboard focus and the final empty state.
                    page.goto(base+'/ui/#review')
                    page.locator('label[for="review-advice-yes"]').click()
                    buttons=page.locator('#review-list .finding-value')
                    wait_for(lambda:api('/api/v1/ui/review?status=proposals')['total']==3)
                    expect(buttons).to_have_count(3)
                    open_review_actions(page)
                    page.locator('#review-capture').check()
                    wait_for(lambda:api('/api/v1/ui/dataset')['capture_review'])
                    labels=buttons.all_text_contents()
                    expect(page.locator('#review-list tbody tr').first.locator('td').nth(3)).to_contain_text('/ Команда')
                    buttons.first.click()
                    card=page.locator('#finding-detail')
                    card.locator('[data-accept-proposal]').click()
                    expect(card.locator('h2')).to_contain_text(labels[1])
                    expect(card.locator('h2')).to_be_focused()
                    card.get_by_role('button',name='Отклонить предложение',exact=True).click()
                    expect(card.locator('h2')).to_contain_text(labels[2])
                    declined = next(i for i in api('/api/v1/ui/review')['items'] if i['value'] == labels[1])
                    assert declined['proposal']['declined'] and not declined['in_dataset']
                    card.screenshot(path=str(args.output/'next-finding.png'))
                    card.locator('[data-accept-proposal]').click()
                    expect(buttons).to_have_count(0)
                    expect(page.locator('#review-result')).to_be_focused()
                    saved['parallelism']=3
                    api('/api/v1/ui/analysis',{'settings':saved},'PUT')
                    gates['new'].clear()
                    abandoned=documents(['drop-one.txt','drop-two.txt','drop-three.txt'])
                    def all_generating():
                        live=api('/api/v1/ui/jobs')['model_activity']
                        return all(any(c.get('tokens_per_second')==12.5 for c in live.get(i,{}).get('calls',[])) for i in abandoned)
                    wait_for(all_generating)
                    page.goto(base+'/ui/#jobs')
                    running=page.locator('#jobs-content [data-group=running]')
                    expect(running.locator('.job-meter').filter(has_text='12,5 ток/с')).to_have_count(3)
                    running.screenshot(path=str(args.output/'three-running.png'))
                    browser.close()
                # Discard also retires a queued review and an active HTTP call. Late replies stay retired.
                pending=next(i for i in api('/api/v1/ui/review?q=zorgflow')['items'] if i['key'] in keys)
                dropped=api('/api/v1/ui/review/jobs',{'keys':[pending['key']],'model_source':'analysis'})['id']
                wait_for(lambda:pending_counts()['review']==1 and pending_counts()['learning']==3)
                switch('old','discard')
                gates['new'].set()
                wait_for(lambda:all(learning()[i]['state']=='cancelled' for i in abandoned))
                assert api('/api/v1/ui/review/jobs/'+dropped)['state']=='cancelled'
                assert pending_counts()['learning']==pending_counts()['review']==0
                assert api(detail)['completed']==3
                legacy=documents(['legacy-api-usage.txt','legacy-api-silent.txt'])
                wait_for(lambda:all(learning()[i]['state']=='succeeded' for i in legacy))
                counted, silent=[learning()[i]['last_call'] for i in legacy]
                assert counted['completion_tokens']==11 and counted['average_tokens_per_second']>0
                assert 'tokens_per_second' not in silent and 'completion_tokens' not in silent, silent
                strict=documents(['strict-gateway.txt'])[0]
                wait_for(lambda:learning()[strict]['state']=='succeeded')
                # Session analysis uses only the selected, retained, masked wire conversation once.
                api('/api/v1/connections',{'kind':'openai','name':'Client chat','base_url':upstream,'api_key':'fixture',
                                           'pool':True,'models':['chat'],'auto_models':False})
                first={'role':'user','content':'Check password: DemoFixture_47!Secret'}
                answer={'role':'assistant','content':'Принято'}
                def chat(session,messages):
                    api('/v1/chat/completions',{'model':'chat','messages':messages},headers={'X-Kaponir-Session':session})
                chat('flow-session',[{'role':'system','content':'SYSTEM_ONLY_FIXTURE'},first])
                chat('flow-session',[first,answer,{'role':'user','content':'Check the April ledger'}])
                session=api('/api/v1/metrics')['traffic']['events'][0]['session_id']
                chat('other-session',[{'role':'user','content':'UNRELATED_FIXTURE'}])
                body={'target':{'session':session},'revision':pending_counts()['revision']}
                session_run=api('/api/v1/ui/analysis/session',body)
                assert session_run['used']==session_run['total']==2 and not session_run['partial'], session_run
                jid=session_run['ids'][0]
                wait_for(lambda:learning()[jid]['state']=='succeeded')
                assert api('/api/v1/ui/analysis/session',body)['existing_ids']==[jid]
                with lock:
                    captured=' '.join(p.get('source','') for _,p in seen if 'April ledger' in p.get('source',''))
                assert captured.count('April ledger')==1 and captured.count('[user]')==2, captured
                assert all(s not in captured for s in ['DemoFixture_47','SYSTEM_ONLY_FIXTURE','UNRELATED_FIXTURE']), captured
                chat('marked-session',[{'role':'user','content':'Открой /srv/zorgauditrecord/config.toml.\npassword: ExampleFixture_73!Secret'}])
                events=api('/api/v1/metrics')['traffic']['events']
                marked=max(events,key=lambda e:e['id'])
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1440,'height':960},locale='ru-RU')
                    page.goto(base+'/ui/#events')
                    page.locator('#events-table [data-session-open="'+marked['session_id']+'"]').click()
                    dialog=page.locator('#session-dialog')
                    expect(dialog.locator('mark.masked').first).to_be_visible()
                    expect(dialog.locator('mark.uncertain').first).to_be_visible()
                    dialog.screenshot(path=str(args.output/'conversation-marks.png'))
                    check_model_selection(page, base, upstream, api, catalog_reads, slow_started, release_slow)
                    browser.close()
                time.sleep(.1)
                assert all(learning()[i]['state']=='cancelled' for i in abandoned)
                print('Verified API catalog selection, manual IDs, credential isolation, S1 Choice, worker modes, streaming, queue transfer/discard and review navigation.')
            finally:
                for gate in gates.values():
                    gate.set()
                proc.terminate()
                proc.wait()
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    main()
