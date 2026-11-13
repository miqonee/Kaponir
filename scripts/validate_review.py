#!/usr/bin/env python3
"""Browser/API regression for explicit review actions and asynchronous LLM recommendations. No model downloads."""
import argparse,json,os,re,shutil,subprocess,tempfile,threading,time,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright
from validate_console import free_port,wait_for

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary',type=Path);parser.add_argument('--output',type=Path,default=Path('artifacts/review'));args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fail=threading.Event();release=threading.Event();release.set();seen=[];seen_formats=[]
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def respond(self,data,status=200):
            raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            self.respond({'configured':True,'online':True,'enabled':True,'state':'ready','can_manage':False,
                'models':{'qwen3-0.6b':{'downloaded':True,'state':'ready'}},'runtime_ready':True} if self.path=='/status' else {'data':[{'id':'fixture-review'}]})
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
            if self.path in ['/enable','/disable']:self.respond({'enabled':True});return
            if self.path=='/evaluate':
                self.respond({'decisions':[{'kind':'AMOUNT','scope':'personal','confidence':.99} for _ in body['input']['candidates']]});return
            if body.get('response_format') or any(isinstance(m.get('content'),str) and 'JSON Schema:' in m['content'] for m in body.get('messages',[])):
                seen_formats.append('response_format' in body)
                content=json.loads(body['messages'][-1]['content']);seen.append(content)
                if 'items' in content:
                    release.wait(timeout=10)
                    if fail.is_set():self.respond({'error':'RAW_BODY_MUST_NOT_LEAK'},503);return
                    result={'decisions':[{'id':item['id'],'action':'review' if 'uncertain' in item['value'] else 'keep' if 'publictool' in item['value'] else 'mask','kind':'PROJ','scope':'team','confidence':.97,'reason':'Not enough context to establish confidentiality.' if 'uncertain' in item['value'] else 'Confirmed private project in source context.'} for item in content['items']]}
                else:result={'candidates':[{'value':'40%','kind':'PERSON','confidence':.90},{'value':'1 320,00 ₽','kind':'SVC','confidence':.78}]}
                self.respond({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}]});return
            self.respond({'choices':[{'message':{'role':'assistant','content':'ready'}}]})
    provider=ThreadingHTTPServer(('127.0.0.1',0),Provider);threading.Thread(target=provider.serve_forever,daemon=True).start();upstream=f'http://127.0.0.1:{provider.server_port}'
    with tempfile.TemporaryDirectory(prefix='kaponir-review-ui-') as directory:
        binary=args.binary.resolve();home=Path(directory);env={k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')};env['KAPONIR_WORKER_URL']=upstream
        subprocess.run([binary,'--home',home,'init'],check=True,capture_output=True,env=env)
        base=f'http://127.0.0.1:{free_port()}';log=(args.output/'server.log').open('w');process=subprocess.Popen([binary,'--home',home,'serve','--listen',base.split('://')[1],'--openai-upstream',upstream],env=env,stdout=log,stderr=log)
        def api(path,body=None,method=None):
            request=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'},method=method)
            with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
        def rows(status='pending'):return api('/api/v1/ui/review?status='+status)['items']
        def seed(value):return api('/api/v1/ui/mask',{'text':'Open /srv/'+value+'/config.toml','commit':True})
        try:
            wait_for(lambda:api('/api/v1/ui/info'))
            settings=api('/api/v1/ui/analysis')['settings'];settings.update(provider='api',model='fixture-review',url=upstream+'/v1/chat/completions',enabled=True,background=False,auto_apply=False)
            settings['inference']['response_format']='prompt'
            cat=api('/api/v1/ui/categorization')['settings'];cat['provider']='none';api('/api/v1/ui/categorization',{'settings':cat},'PUT');api('/api/v1/ui/analysis',{'settings':settings},'PUT')
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'financial-fixture.md','text':'Private commercial conditions: Аванс 40%, цена 1 320,00 ₽.'}]})
            wait_for(lambda:len([i for i in rows() if i['origin']=='learning'])==2)
            original=seed('zorgreviewalpha');seed('zorgreviewhidden');seed('zorgreviewpublictool')
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(executable_path=shutil.which('chromium') or shutil.which('google-chrome'),headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1000},locale='ru-RU',color_scheme='dark');errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                try:
                    page.goto(base+'/ui/#analysis');page.locator('#analysis-enabled').wait_for()
                    wait_for(lambda:page.locator('#analysis-enabled').is_enabled())
                    assert page.locator('#analysis-response-format').input_value()=='prompt'
                    page.locator('#analysis-response-format').evaluate("e=>e.closest('details').open=true")
                    page.locator('#analysis-response-format').select_option('grammar');page.locator('#analysis-save').click()
                    wait_for(lambda:api('/api/v1/ui/analysis')['settings']['inference']['response_format']=='grammar')
                    page.locator('#analysis-response-format').select_option('prompt');page.locator('#analysis-save').click();wait_for(lambda:api('/api/v1/ui/analysis')['settings']['inference']['response_format']=='prompt')
                    checked=page.locator('#analysis-enabled').is_checked();help_button=page.locator('#analysis-form .help-check .setting-help-button').first
                    help_button.click();page.locator('#setting-help-popup').wait_for();assert page.locator('#analysis-enabled').is_checked()==checked
                    assert not page.locator('#analysis-enabled-help').is_visible()
                    page.keyboard.press('Escape');assert not page.locator('#setting-help-popup').is_visible()
                    page.evaluate("showTab('review')");page.locator('#review-list .review-card').first.wait_for()
                    assert 'Нет общей оценки' not in page.locator('#review-list').inner_text()
                    money=next(i for i in rows() if i['value']=='40%');assert money['kind']=='AMOUNT' and money['proposed_kind']=='PERSON' and money['categorization_state']=='disabled'
                    money_card=page.locator('[data-review-key="'+money['key']+'"]');assert 'тип не проверен' in money_card.inner_text()
                    target=next(i for i in rows() if i['value']=='zorgreviewalpha');selector='[data-review-key="'+target['key']+'"]';card=page.locator(selector)
                    rules_before_review=json.dumps(api('/api/v1/rules'))
                    card.get_by_role('button',name='Пересмотреть моделью',exact=True).click();page.locator('#review-confirm-run').click()
                    wait_for(lambda:any(i.get('recommendation',{}).get('state')=='succeeded' for i in rows() if i['key']==target['key']))
                    page.locator('#review-refresh').click();card.locator('.review-recommendation').wait_for()
                    assert json.dumps(api('/api/v1/rules'))==rules_before_review
                    assert seen_formats and not any(seen_formats)
                    assert rows()[0].get('origin')
                    card.get_by_role('button',name='Применить рекомендацию',exact=True).click()
                    assert 'Команда' in page.locator('#review-confirm').inner_text()
                    page.locator('#review-confirm-apply').click();page.wait_for_function('() => !ReviewUI.busy');wait_for(lambda:not any(i['key']==target['key'] for i in rows()))
                    page.locator('[data-review-view=history]').click();card=page.locator(selector);card.get_by_role('button',name='Отменить правило',exact=True).click();page.locator('#review-confirm-apply').click()
                    wait_for(lambda:any(i['key']==target['key'] for i in rows()))
                    restored=api('/api/v1/ui/restore',{'text':original['text']});assert 'zorgreviewalpha' in restored['text']
                    page.locator('[data-review-view=pending]').click()
                    hidden=next(i for i in rows() if i['value']=='zorgreviewhidden');hidden_card=page.locator('[data-review-key="'+hidden['key']+'"]')
                    before=json.dumps(api('/api/v1/rules'));hidden_card.get_by_role('button',name='Закрыть без правила',exact=True).click()
                    assert 'не создаёт правила' in page.locator('#review-confirm').inner_text();page.locator('#review-confirm-apply').click()
                    wait_for(lambda:not any(i['key']==hidden['key'] for i in rows()));assert json.dumps(api('/api/v1/rules'))==before
                    seed('zorgreviewhidden');assert not any(i['key']==hidden['key'] for i in rows())
                    page.locator('[data-review-view=history]').click();page.locator('[data-review-key="'+hidden['key']+'"]').get_by_role('button',name='Вернуть на проверку',exact=True).click();wait_for(lambda:any(i['key']==hidden['key'] for i in rows()))
                    page.locator('[data-review-view=pending]').click()
                    public=next(i for i in rows() if i['value']=='zorgreviewpublictool');public_card=page.locator('[data-review-key="'+public['key']+'"]')
                    public_card.get_by_role('button',name='Создать исключение',exact=True).click();assert 'KEEP' in page.locator('#review-confirm').inner_text();page.locator('#review-confirm-apply').click()
                    wait_for(lambda:not any(i['key']==public['key'] for i in rows()));assert 'zorgreviewpublictool' in seed('zorgreviewpublictool')['text']
                    # Failures must be explicit and must not turn into heuristic recommendations.
                    fail.set();page.locator('#review-refresh').click();card=page.locator(selector);card.get_by_role('button',name='Пересмотреть моделью',exact=True).click();page.locator('#review-confirm-run').click()
                    wait_for(lambda:any(i['key']==target['key'] and i.get('recommendation',{}).get('state')=='failed' for i in rows()))
                    page.locator('#review-refresh').click();assert 'RAW_BODY_MUST_NOT_LEAK' not in page.locator('#review-list').inner_text();fail.clear()
                    # A successful call can abstain: show why and prohibit automatic application.
                    seed('zorgreviewuncertain');page.locator('#review-refresh').click()
                    uncertain=next(i for i in rows() if i['value']=='zorgreviewuncertain')
                    uncertain_card=page.locator('[data-review-key="'+uncertain['key']+'"]')
                    before=json.dumps(api('/api/v1/rules'))
                    uncertain_card.get_by_role('button',name='Пересмотреть моделью',exact=True).click();page.locator('#review-confirm-run').click()
                    wait_for(lambda:any(i['key']==uncertain['key'] and i.get('recommendation',{}).get('state')=='needs_review' for i in rows()))
                    page.locator('#review-refresh').click();uncertain_card.locator('.review-recommendation').wait_for()
                    assert 'Модели недостаточно данных' in uncertain_card.inner_text()
                    assert uncertain_card.get_by_role('button',name='Применить рекомендацию',exact=True).count()==0
                    assert uncertain_card.locator('option[value="auto"]').is_disabled()
                    assert json.dumps(api('/api/v1/rules'))==before
                    # The event exposes a concrete reason and stable links, without leaking values in summaries.
                    api('/v1/chat/completions',{'model':'fixture-review','messages':[{'role':'user','content':'Read /srv/zorgrevieweventalpha/config.toml'}]})
                    event=wait_for(lambda:next((e for e in api('/api/v1/metrics')['traffic']['events'] if e['usage']['uncertain']),None))
                    assert 'zorgrevieweventalpha' not in json.dumps(api('/api/v1/metrics')['traffic'])
                    page.evaluate('(id)=>openInspector(id,"review")',event['id']);page.locator('#inspector-dialog[open]').wait_for();assert 'zorgrevieweventalpha' in page.locator('#inspector-dialog').inner_text()
                    page.locator('#inspector-dialog').get_by_role('button',name='Открыть связанные решения').first.click();page.locator('#review-list').filter(has_text='zorgrevieweventalpha').wait_for()
                    assert 'zorgreviewhidden' not in page.locator('#review-list').inner_text()
                    page.locator('#review-list').get_by_role('button',name='Показать все',exact=True).click()
                    # Confident builtin replacements can also be explicitly sent to review.
                    api('/v1/chat/completions',{'model':'fixture-review','messages':[{'role':'user','content':'Логин: m.reviewfixture'}]})
                    confident=wait_for(lambda:next((e for e in api('/api/v1/metrics')['traffic']['events'] if e['id']!=event['id']),None))
                    detail=api('/api/v1/ui/events/'+str(confident['id']))
                    index=next(i for i,item in enumerate(detail['usage']['masked_items']) if item['original']=='m.reviewfixture')
                    assert not any(i['value']=='m.reviewfixture' for i in rows())
                    page.evaluate('(id)=>openInspector(id,"replacements")',confident['id'])
                    page.locator('#inspector-dialog').get_by_role('button',name='Проверить это решение',exact=True).nth(index).click()
                    wait_for(lambda:any(i['value']=='m.reviewfixture' for i in rows()))
                    page.locator('#review-list').filter(has_text='m.reviewfixture').wait_for()
                    page.locator('#review-list').get_by_role('button',name='Показать все',exact=True).click()
                    # Selected records only; grouping is not a wildcard write operation.
                    page.locator('#review-groups').click();assert page.locator('#review-list .review-group').count()>0
                    page.locator('#review-groups').click();page.locator('#review-select-page').check();assert page.locator('#review-selection').is_visible()
                    page.locator('#review-selection').get_by_role('button',name='Снять выбор',exact=True).click()
                    for language in ['ru','en']:
                        page.locator(f'#language-switch label:has(input[value={language}])').click();wait_for(lambda:page.locator('html').get_attribute('lang')==language)
                        for theme in ['dark','light']:
                            page.locator(f'#theme-switch label:has(input[value={theme}])').click()
                            for width in [390,768,1440]:
                                page.set_viewport_size({'width':width,'height':1000});page.evaluate("showTab('review')");page.wait_for_timeout(150)
                                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(language,theme,width)
                                page.screenshot(path=str(args.output/f'review-{language}-{theme}-{width}.png'),full_page=True)
                    # A later confident categorizer cannot silently activate a numeric type correction.
                    cat.update(provider='local',model='qwen3-0.6b');api('/api/v1/ui/categorization',{'settings':cat},'PUT')
                    settings['auto_apply']=True;api('/api/v1/ui/analysis',{'settings':settings},'PUT')
                    api('/api/v1/ui/worker/start',{})
                    rules_before=json.dumps(api('/api/v1/rules'))
                    api('/api/v1/ui/analysis/documents',{'documents':[{'name':'numeric-validation.md','text':'Additional private estimate: Аванс 40%, цена 1 320,00 ₽.'}]})
                    corrected=wait_for(lambda:(items if len(items:=[i for i in rows('all') if i.get('source')=='numeric-validation.md'])==2 else None))
                    assert all(i['categorization_state']=='succeeded' and i['kind']=='AMOUNT' and i['state']=='pending' for i in corrected)
                    assert any(i['confidence'] is not None and i['confidence']>.85 for i in corrected)
                    assert all(i['classification_confidence']>.98 for i in corrected)
                    assert json.dumps(api('/api/v1/rules'))==rules_before
                    assert not errors,errors
                except Exception:
                    page.screenshot(path=str(args.output/'failure.png'),full_page=True);(args.output/'browser-errors.json').write_text(json.dumps(errors))
                    (args.output/'failure-state.json').write_text(json.dumps({'learning':api('/api/v1/ui/learning'),'review':rows('all')},ensure_ascii=False,indent=2));raise
                finally:browser.close()
            report={'passed':True,'checks':['help popup','honest category stages','async model review','prompt JSON profile roundtrip and API payload','explicit apply','rollback and restore','close and reopen','KEEP exception','no heuristic fallback','inconclusive review stays pending','event links and privacy','explicit review from event','numeric corrections require confirmation','selection and grouping','RU/EN themes and mobile']};(args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
        finally:release.set();process.terminate();process.wait(timeout=20);log.close();provider.shutdown();provider.server_close()

if __name__=='__main__':main()
