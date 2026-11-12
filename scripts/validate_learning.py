#!/usr/bin/env python3
"""Background learning against a deterministic local model/worker fixture, without downloads."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from validate_console import free_port, wait_for


def check_browser(base, api, output):
    from playwright.sync_api import sync_playwright
    import shutil
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path=shutil.which('chromium') or shutil.which('google-chrome'),headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1440,'height':1000},locale='ru-RU',color_scheme='dark')
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+'/ui/#analysis')
        page.wait_for_function("() => document.querySelector('#analysis-provider')?.value==='api'")
        page.locator('#analysis-parallelism').fill('10')
        page.locator('#analysis-form details.advanced-settings').evaluate('(node)=>node.open=true')
        page.locator('#analysis-context-tokens').fill('8192')
        page.locator('#analysis-output-tokens').fill('2048')
        page.locator('#analysis-target-input').fill('2048')
        page.locator('#analysis-save').click()
        page.wait_for_function("() => document.querySelector('#analysis-result').textContent.includes('Настройки сохранены')")
        page.locator('#analysis-test').click()
        page.wait_for_function("() => document.querySelector('#analysis-result').textContent==='Подключение проверено'")
        assert api('/api/v1/ui/analysis')['settings']['parallelism']==10
        assert api('/api/v1/ui/analysis')['settings']['inference']['max_output_tokens']==2048
        assert api('/api/v1/ui/analysis')['settings']['chunking']['target_input_tokens']==2048
        page.reload();page.wait_for_function("() => document.querySelector('#analysis-parallelism').value==='10'")
        assert page.locator('#analysis-context-tokens').input_value()=='8192'
        page.locator('#analysis-form details.advanced-settings').evaluate('(node)=>node.open=true')
        page.locator('#analysis-context-tokens').locator('xpath=../..').get_by_role('button',name='Пояснение настройки').click()
        assert page.locator('#setting-help-popup').is_visible()
        page.keyboard.press('Escape')
        page.locator('#analysis-navigation [data-go=categorization]').click()
        page.locator('#categorization-test').click()
        page.wait_for_function("() => document.querySelector('#categorization-result').textContent==='Подключение проверено'")
        page.locator('#learning-open-history').click()
        page.wait_for_function("() => location.hash==='#review' && document.querySelector('[data-review-view=history]').getAttribute('aria-pressed')==='true'")
        page.locator('[data-review-view=pending]').click()
        pending=next(e for e in api('/api/v1/ui/learning')['entries'] if e['value']=='CedarCat85')
        selector='[data-learning-id="'+pending['id']+'"]'
        page.locator('#review-list '+selector).get_by_role('button',name='Создать правило маскирования',exact=True).click()
        page.locator('#review-confirm-apply').click()
        wait_for(lambda:any(e['id']==pending['id'] and e['state']=='active' for e in api('/api/v1/ui/learning')['entries']))
        page.locator('[data-review-view=history]').click()
        page.locator('#review-list '+selector).get_by_role('button',name='Отменить правило',exact=True).click()
        page.locator('#review-confirm-apply').click()
        wait_for(lambda:any(e['id']==pending['id'] and e['state']=='rolled_back' for e in api('/api/v1/ui/learning')['entries']))
        page.locator('[data-tab=discover]').click()
        page.locator('#d-mode').select_option('background')
        page.locator('#d-text').fill('Internal label CedarBrowser86')
        page.locator('#d-analyze').click()
        wait_for(lambda:any(e['value']=='CedarBrowser86' and e['state']=='active' for e in api('/api/v1/ui/learning')['entries']))
        page.locator('[data-tab=analysis]').click()
        page.locator('#learning-toggle').click()
        wait_for(lambda:not api('/api/v1/ui/analysis')['settings']['enabled'])
        page.wait_for_function("() => document.querySelector('#learning-state').textContent==='Анализ приостановлен'")
        page.locator('#learning-toggle').click()
        wait_for(lambda:api('/api/v1/ui/analysis')['settings']['enabled'])
        for width in [390,1440]:
            page.set_viewport_size({'width':width,'height':1000})
            for theme in ['dark','light']:
                page.locator('#theme-switch label:has(input[value='+theme+'])').click()
                for tab in ['analysis','categorization','review']:
                    page.evaluate('(tab)=>showTab(tab)',tab);page.wait_for_timeout(200)
                    if tab=='review':
                        page.locator('.inference-details').first.evaluate('(node)=>node.open=true')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,theme,tab)
                    page.screenshot(path=str(output/f'learning-{tab}-{theme}-{width}.png'),full_page=True)
        page.set_viewport_size({'width':1440,'height':1000})
        page.locator('#language-switch label:has(input[value=en])').click()
        page.evaluate("showTab('analysis')")
        page.wait_for_function("() => document.documentElement.lang==='en'")
        page.screenshot(path=str(output/'learning-analysis-en-1440.png'),full_page=True)
        assert not errors,errors
        browser.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path)
    parser.add_argument('--output',type=Path,default=Path('artifacts/learning'))
    args=parser.parse_args();binary=args.binary.resolve();args.output.mkdir(parents=True,exist_ok=True)
    enabled=True; allow=threading.Event();started=threading.Event();lock=threading.Lock()
    model_calls=[];upstream=[];active=0;peak=0
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def reply(self,body,status=200):
            data=json.dumps(body).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if self.path=='/status':self.reply({'online':True,'configured':True,'can_manage':True,'enabled':enabled,'state':'ready' if enabled else 'disabled','models':{},'runtime_ready':True})
            else:self.reply({'data':[{'id':'fixture-model'}]})
        def do_POST(self):
            nonlocal enabled,active,peak
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            if self.path in ['/enable','/disable']:
                enabled=self.path=='/enable';self.reply({'enabled':enabled});return
            if self.path=='/analysis':
                source=json.loads(body['messages'][1]['content'])['source']
                with lock: model_calls.append(source);active+=1;peak=max(peak,active)
                started.set();allow.wait(timeout=15);time.sleep(.15)
                if ('CedarBudgetA' in source and 'CedarBudgetB' in source) or ('CedarPartialGood' in source and 'MODEL_FAIL_MARKER' in source):
                    with lock:active-=1
                    self.reply({'choices':[{'finish_reason':'length','message':{'content':'{"candidates":['}}],
                        'usage':{'prompt_tokens':1000,'completion_tokens':1600,'total_tokens':2600}});return
                if 'MODEL_FAIL_MARKER' in source:
                    with lock:active-=1
                    self.reply({'error':{'code':'service_unavailable'}},503);return
                candidates=[]
                for value in dict.fromkeys(re.findall(r'Cedar[A-Za-z0-9]+',source)):
                    confidence=.84 if 'Low84' in value else .85 if 'Edge85' in value else .99 if 'Cat85' in value else .96
                    candidates.append({'value':value,'kind':'PROJ','confidence':confidence})
                for value in ['questions','gemini-3.8-flash','...']:
                    if value in source:candidates.append({'value':value,'kind':'PROJ','confidence':.99})
                candidates.append({'value':'InventedWithoutEvidence','kind':'PROJ','confidence':1.0})
                if 'CedarSaturate' in source:
                    candidates=candidates[:body['response_format']['json_schema']['schema']['properties']['candidates']['maxItems']]
                if 'CedarRepeatOne' in source:
                    candidates=[{'value':'CedarRepeatOne','kind':'PROJ','confidence':.96}]*24
                with lock:active-=1
                self.reply({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'candidates':candidates})}}]});return
            if self.path=='/categorize':
                payload=json.loads(body['messages'][1]['content'])
                decisions=[{'kind':'PROJ','scope':'team','action':'mask','confidence':.85 if 'Cat85' in c.get('value',c.get('surface','')) else .99} for c in payload['candidates']]
                self.reply({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'decisions':decisions})}}]});return
            upstream.append(body)
            text=body['messages'][-1]['content']
            if body.get('stream'):
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                for part in [text[:len(text)//2],text[len(text)//2:]]:
                    self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':{'content':part}}]})+'\n\n').encode());self.wfile.flush();time.sleep(.03)
                self.wfile.write(b'data: [DONE]\n\n')
            else:self.reply({'choices':[{'message':{'role':'assistant','content':text}}]})
    fixture=ThreadingHTTPServer(('127.0.0.1',0),Fixture);fixture.daemon_threads=True
    threading.Thread(target=fixture.serve_forever,daemon=True).start();model_base=f'http://127.0.0.1:{fixture.server_port}'
    env={k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')};env['KAPONIR_WORKER_URL']=model_base
    with tempfile.TemporaryDirectory(prefix='kaponir-learning-') as directory:
        home=Path(directory);subprocess.run([binary,'--home',home,'init'],env=env,capture_output=True,check=True)
        base=f'http://127.0.0.1:{free_port()}';log=(args.output/'server.log').open('w');process=None
        def api(path,body=None,method=None):
            req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'},method=method)
            with urllib.request.urlopen(req,timeout=30) as response:
                return response.read().decode() if 'text/event-stream' in response.headers.get('Content-Type','') else json.load(response)
        def start():
            nonlocal process
            process=subprocess.Popen([binary,'--home',home,'serve','--listen',base.removeprefix('http://'),'--openai-upstream',model_base],env=env,stdout=log,stderr=log)
            wait_for(lambda:api('/api/v1/ui/info'))
        def stop():
            if process and process.poll() is None:process.terminate();process.wait(timeout=20)
        def configure(role,**changes):
            old=api('/api/v1/ui/'+role)['settings'];old.update(changes)
            return api('/api/v1/ui/'+role,{'settings':old},'PUT')['settings']
        def completed():
            value=api('/api/v1/ui/learning')
            return value if not value['summary']['queued'] and not value['summary']['running'] else None
        try:
            start()
            assert not api('/api/v1/ui/analysis')['settings']['enabled']
            api('/api/v1/connections',{'kind':'openai','name':'Fixture','base_url':model_base,'api_key':'fixture','pool':True,'models':['fixture-model'],'auto_models':False})
            cat=api('/api/v1/ui/categorization')['settings'];cat['provider']='s1';cat['http'].update(url=model_base+'/categorize',protocol='openai',model='fixture',timeout_ms=30000)
            api('/api/v1/ui/categorization',{'settings':cat},'PUT')
            configure('analysis',provider='api',enabled=True,url=model_base+'/analysis',model='fixture',parallelism=1)
            first={'model':'fixture-model','stream':True,'messages':[{'role':'user','content':'Internal label CedarFirst86'}]}
            before=time.monotonic();response=api('/v1/chat/completions',first);elapsed=time.monotonic()-before
            assert 'CedarFirst86' in response and elapsed<2,(elapsed,response)
            assert started.wait(timeout=5),'background analysis did not start'
            assert not allow.is_set()
            assert api('/api/v1/ui/learning')['summary']['running']==1
            # A second client request completes while background inference is blocked.
            second={'model':'fixture-model','messages':first['messages']+[{'role':'assistant','content':'OK'},{'role':'user','content':'continue'}]}
            before=time.monotonic();api('/v1/chat/completions',second);assert time.monotonic()-before<2
            allow.set();result=wait_for(completed)
            first_entry=next(e for e in result['entries'] if e['value']=='CedarFirst86');assert first_entry['state']=='active',first_entry
            assert sum('CedarFirst86' in text for text in model_calls)==1,model_calls
            # Subsequent requests use the newly learned rule; previous token mappings remain reversible.
            masked=api('/api/v1/anonymize',{'text':'CedarFirst86','session':'learned'})
            assert 'CedarFirst86' not in masked['text']
            doc='CedarHigh86 CedarLow84 CedarEdge85 CedarCat85 questions gemini-3.8-flash ...'
            queued=api('/api/v1/ui/analysis/documents',{'documents':[{'name':'thresholds.txt','text':doc}]});assert queued['queued']==1
            result=wait_for(completed);entries={e['value']:e for e in result['entries']}
            assert entries['CedarHigh86']['state']=='active'
            for value in ['CedarLow84','CedarEdge85','CedarCat85']:assert entries[value]['state']=='pending',entries[value]
            assert not {'questions','gemini-3.8-flash','...','InventedWithoutEvidence'}&entries.keys()
            low=entries['CedarLow84'];api('/api/v1/ui/learning/'+low['id']+'/approve',{'scope':'team','kind':'PROJ'})
            api('/api/v1/ui/learning/'+entries['CedarEdge85']['id']+'/reject',{})
            api('/api/v1/ui/learning/'+first_entry['id']+'/rollback',{})
            assert api('/api/v1/anonymize',{'text':'CedarFirst86','session':'learned'})['text']=='CedarFirst86'
            assert api('/api/v1/deanonymize',{'text':masked['text'],'session':'learned'})['text']=='CedarFirst86'
            # Three independent jobs actually call the model in parallel.
            configure('analysis',parallelism=3)
            for i in range(3):api('/api/v1/ui/analysis/documents',{'documents':[{'name':f'parallel-{i}','text':f'New internal label CedarParallel{i}'}]})
            result=wait_for(completed);assert 1<peak<=3,peak
            assert result['summary']['queue_bytes']==0
            # Identical documents are not queued twice, even under a different filename.
            assert api('/api/v1/ui/analysis/documents',{'documents':[{'name':'duplicate.txt','text':doc}]})['queued']==0
            # Rollback blocks automatic relearning from another source.
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'other','text':'Other source repeats CedarFirst86'}]})
            result=wait_for(completed);assert any(e['value']=='CedarFirst86' and e['state']=='rejected' for e in result['entries'])
            before_calls=len(model_calls)
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'known-only','text':'project: CedarLow84'}]})
            result=wait_for(completed);assert len(model_calls)==before_calls and result['summary']['totals']['skipped_known']>0
            # Pause stops collection; existing rules continue to mask.
            configure('analysis',enabled=False)
            api('/v1/chat/completions',{'model':'fixture-model','messages':[{'role':'user','content':'CedarPaused'}]})
            assert api('/api/v1/ui/learning')['summary']['queued']==0
            assert 'CedarLow84' not in api('/api/v1/anonymize',{'text':'CedarLow84'})['text']
            # Durable queue survives restart and is independent of event inspectors.
            configure('analysis',enabled=True);enabled=False
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'restart','text':'CedarRestart86 QueuePrivateFixture_482'}]})
            configure('analysis',enabled=False);assert api('/api/v1/ui/learning')['summary']['queued']==1
            for path in home.glob('**/*'):
                if path.is_file() and ('.sqlite' in path.name or path.suffix=='.db'):
                    assert b'QueuePrivateFixture_482' not in path.read_bytes(),path
            stop();start();assert api('/api/v1/ui/learning')['summary']['queued']==1
            configure('analysis',enabled=True);api('/api/v1/ui/worker/start',{})
            result=wait_for(completed);assert any(e['value']=='CedarRestart86' and e['state']=='active' for e in result['entries'])
            # Truncated output is retried by structural boundary, preserving all child results.
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'retry-split','text':'CedarBudgetA\n\nCedarBudgetB\n\nCedarBudgetC'}]})
            result=wait_for(completed)
            assert {'CedarBudgetA','CedarBudgetB','CedarBudgetC'} <= {e['value'] for e in result['entries']}
            retry_job=next(j for j in result['jobs'] if j['name']=='retry-split')
            assert retry_job['state']=='succeeded' and any(t.get('finish_reason')=='length' and t.get('completion_tokens')==1600 for t in retry_job['inference']),retry_job
            # A full list of distinct quoted values triggers subdivision; duplicates do not.
            chunking=api('/api/v1/ui/analysis')['settings']['chunking'];configure('analysis',chunking={**chunking,'max_candidates':2})
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'saturated','text':'CedarSaturateA\n\nCedarSaturateB\n\nCedarSaturateC\n\nCedarSaturateD'}]})
            result=wait_for(completed)
            assert {'CedarSaturateA','CedarSaturateB','CedarSaturateC','CedarSaturateD'} <= {e['value'] for e in result['entries']}
            configure('analysis',chunking=chunking)
            before_calls=len(model_calls)
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'duplicate-array','text':'Internal project CedarRepeatOne'}]})
            result=wait_for(completed);assert len(model_calls)==before_calls+1
            # Re-uploading with a different inference profile must not reuse a completed search.
            neutral='NEUTRAL_PROFILE_DOCUMENT alpha beta gamma'
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'profile-cache','text':neutral}]});wait_for(completed)
            assert api('/api/v1/ui/analysis/documents',{'documents':[{'name':'profile-cache','text':neutral}]})['queued']==0
            inference=api('/api/v1/ui/analysis')['settings']['inference']
            configure('analysis',inference={**inference,'max_output_tokens':inference['max_output_tokens']+1})
            before_calls=len(model_calls)
            assert api('/api/v1/ui/analysis/documents',{'documents':[{'name':'profile-cache','text':neutral}]})['queued']==1
            wait_for(completed);assert len(model_calls)==before_calls+1
            configure('analysis',inference=inference)
            # One failed fragment must not erase completed fragments or autoactivate partial findings.
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'partial','text':'CedarPartialGood\n\nMODEL_FAIL_MARKER'}]})
            result=wait_for(completed)
            partial=next(e for e in result['entries'] if e['value']=='CedarPartialGood')
            assert partial['state']=='pending' and partial['reason']=='analysis_partial',partial
            partial_job=next(j for j in result['jobs'] if j['name']=='partial')
            assert partial_job['state']=='failed' and partial_job['extracted']==1 and partial_job['retryable'],partial_job
            check_browser(base,api,args.output)
            result=wait_for(completed)
            report={'passed':True,'nonblocking_request_seconds':round(elapsed,3),'model_peak_concurrency':peak,'summary':result['summary'],'checks':['SSE completion','no LLM wait','history deduplication','strict 85%','grounding','manual approval','rollback','revocation','parallelism','pause','encrypted queue','restart','source deletion']}
            (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))
        finally:
            allow.set();stop();log.close();fixture.shutdown();fixture.server_close()


if __name__=='__main__':main()
