#!/usr/bin/env python3
"""Browser/API regression for explicit review actions and asynchronous LLM recommendations. No model downloads."""
import argparse,csv,json,os,re,shutil,subprocess,tempfile,threading,time,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
from validate_console import choose_preference, free_port, wait_for
from validate_dataset import check_dataset_ui

def check_local_presets(page, api, output):
    analysis = api('/api/v1/ui/analysis')['settings']
    categorization = api('/api/v1/ui/categorization')['settings']
    try:
        page.locator('#analysis-provider').select_option('local')
        model = page.locator('#analysis-local-model')
        options = set(model.locator('option').evaluate_all('(options) => options.map(o => o.value)'))
        with (Path(__file__).resolve().parents[1]/'docs/experiments/model-search-final.csv').open() as f:
            rows = list(csv.DictReader(f))
        page.wait_for_function("() => document.querySelector('#analysis-local-model optgroup')?.label === 'Загружены'")
        ready = model.locator('optgroup').first.locator('option')
        assert set(ready.evaluate_all('(options) => options.map(o => o.value)')) == {'qwen3.5-2b','qwen3-0.6b'}
        hidden = {'huihui-qwen3.5-2b-abliterated','tiny-aya-global'}
        assert not options.intersection(hidden)
        for row in rows:
            if row['model'] in hidden:
                continue
            model.select_option(row['model'])
            preset = page.evaluate("chosenLocalPreset('analysis')")
            if not row['context_tokens']:
                assert preset is None
                continue
            settings = preset['inference']
            assert settings['context_tokens'] == int(row['context_tokens'])
            assert settings['max_output_tokens'] == int(row['primary_output_tokens'])
            g = settings['local_generation']
            if not row['temperature_text']:
                assert g is None
                continue
            for key,column in [('temperature','temperature_text'),('top_k','top_k'),('top_p','top_p'),('min_p','min_p')]:
                assert g[key] == float(row[column]), (row['model'],key)
            assert g['seed'] == (int(row['seed']) if row['seed'] else None)
            if row['prompt_threads']:
                assert g['prompt_threads'] == int(row['prompt_threads'])
                assert g['generation_threads'] == int(row['generation_threads'])
        page.locator('#analysis-discovery-mode').evaluate("e => e.value = 'values'")
        page.locator('#analysis-local-model').select_option('gemma4-e4b-qat')
        assert page.locator('#analysis-discovery-mode').input_value() == 'typed'
        assert page.locator('#analysis-context-tokens').input_value() == '16384'
        assert page.locator('#analysis-timeout').input_value() == '1200'
        page.locator('#analysis-local-model').select_option('qwen3.5-2b')
        assert page.locator('#analysis-context-tokens').input_value() == '4096'
        page.locator('#analysis-output-tokens').evaluate("e => e.closest('details').open = true")
        assert not page.locator('#analysis-preset').is_visible()
        page.locator('#analysis-output-tokens').fill('1300')
        page.locator('#analysis-preset').click()
        assert page.locator('#analysis-output-tokens').input_value() == '1600'
        assert not page.locator('#analysis-preset').is_visible()
        page.evaluate("window.probeLimits=[]; const nativeTimeout=AbortSignal.timeout.bind(AbortSignal); AbortSignal.timeout=ms=>{ window.probeLimits.push(ms); return nativeTimeout(ms); }")
        page.locator('#analysis-test').click()
        page.locator('#analysis-result').filter(has_text='Токенизатор ответил').wait_for()
        assert 315000 in page.evaluate('window.probeLimits')
        page.locator('#analysis-output-tokens').fill('1400')
        page.locator('#analysis-save').click()
        wait_for(lambda: api('/api/v1/ui/analysis')['settings']['model'] == 'qwen3.5-2b')
        page.reload()
        page.wait_for_function("() => document.querySelector('#analysis-output-tokens').value === '1400'")
        assert api('/api/v1/ui/analysis')['settings']['inference']['local_generation']['top_k'] == 40
        page.evaluate("showTab('categorization')")
        page.locator('#categorization-provider').select_option('local')
        for row in rows:
            if row['model'] in hidden: continue
            page.locator('#categorization-local-model').select_option(row['model'])
            assert page.locator('#categorization-scenario').input_value() == row['best_abcd_variant']
        page.locator('#categorization-local-model').select_option('gemma4-e4b-it-q4km')
        study = page.locator('#categorization-scenario-study')
        expect(study.locator('.benchmark-summary')).to_contain_text('32 из 32')
        expect(study.locator('.scenario-goal')).to_have_count(3)
        for goal, variant in [('speed','B'), ('privacy','D'), ('public','D')]:
            button = study.locator('[data-goal='+goal+']')
            button.focus()
            button.press('Enter')
            expect(page.locator('#categorization-scenario')).to_have_value(variant)
            expect(button).to_be_focused()
        assert api('/api/v1/ui/categorization')['settings']['scenario'] == categorization['scenario']
        model_tab = study.get_by_role('tab',name='Замеры модели')
        model_tab.click()
        model_tab.press('ArrowRight')
        cohort_tab = study.get_by_role('tab',name='Среднее по стенду')
        expect(cohort_tab).to_be_focused()
        expect(cohort_tab).to_have_attribute('aria-selected','true')
        expect(study.get_by_role('tabpanel')).to_have_count(1)
        cohort_tab.press('ArrowLeft')
        expect(model_tab).to_be_focused()
        expect(study.locator('.model-comparison tbody tr')).to_have_count(4)
        page.locator('#categorization-scenario').select_option('B')
        expect(study.locator('.benchmark-summary')).to_contain_text('25 из 32')
        expect(study.locator('.selected-scenario th')).to_have_text('B')
        study.get_by_role('tab',name='Среднее по стенду').click()
        expect(study.locator('.scenario-comparison')).to_contain_text('160 / 256')
        page.locator('#categorization-local-model').select_option('nanbeige42-3b')
        expect(study.locator('.benchmark-summary')).to_contain_text('21 из 32')
        expect(study.locator('.selected-scenario')).to_contain_text('24/32')
        expect(study.locator('.scenario-goal')).to_have_count(0)
        expect(study.locator('.model-comparison tbody tr').first).to_contain_text('—')
        assert page.locator('#categorization-response-format').input_value() == 'grammar'
        assert page.evaluate("chosenLocalPreset('categorization').inference.local_generation.completion_schema")
        page.locator('#categorization-local-model').select_option('gemma4-e4b-qat')
        page.locator('#categorization-save').click()
        wait_for(lambda: api('/api/v1/ui/categorization')['settings']['model'] == 'gemma4-e4b-qat')
        settings = api('/api/v1/ui/categorization')['settings']
        assert settings['scenario'] == 'D'
        page.locator('#categorization-scenario').select_option('C')
        page.locator('#categorization-save').click()
        wait_for(lambda:api('/api/v1/ui/categorization')['settings']['scenario'] == 'C')
        page.reload()
        page.wait_for_function("() => document.querySelector('#categorization-scenario').value === 'C'")
        page.locator('#categorization-preset').click()
        assert page.locator('#categorization-scenario').input_value() == 'D'
        page.locator('#analysis-navigation [data-go=analysis]').click()
        page.locator('#analysis-open-document').click()
        if page.locator('#d-settings').get_attribute('open') is None:
            page.locator('#d-settings > summary').click()
        assert page.locator('#d-mode').input_value() == 'background'
        assert page.locator('#d-scenario').is_visible()
        expect(page.locator('#d-scenario-study .benchmark-summary')).to_contain_text('C · верно 26 из 32')
        if page.locator('#d-settings').get_attribute('open') is None:
            page.locator('#d-settings > summary').click()
        page.locator('#d-mode').select_option('fast')
        assert not page.locator('#d-scenario').is_visible()
        assert settings['http']['timeout_ms'] == 1200000
        assert settings['inference']['local_generation']['reasoning_budget'] == 2048
        assert settings['inference']['local_generation']['temperature'] == 1
        assert settings['inference']['response_format'] == 'grammar'
        page.locator('[data-tab=settings]').click()
        top = page.locator('#workspace-models')
        top.get_by_role('combobox', name='Модель поиска и проверки').select_option('qwen3.5-2b')
        expect(top.locator('#workspace-scenario')).to_have_value('B')
        expect(top.locator('.benchmark-summary')).to_contain_text('Рекомендация для модели · B')
        expect(top.locator('#workspace-scenario-study')).to_contain_text('B скрыл 9 из 16 приватных значений, самый быстрый D — 5 из 16.')
        top.locator('#workspace-scenario').select_option('A')
        with page.expect_response(lambda r: r.url.endswith('/workspace/model') and r.request.method == 'PUT') as shared:
            top.get_by_role('button', name='Использовать для поиска и проверки').click()
        assert shared.value.ok and api('/api/v1/ui/categorization')['settings']['scenario'] == 'A'
        page.reload()
        expect(top.locator('#workspace-scenario')).to_have_value('A')
        top.locator('#workspace-scenario').select_option('B')
        top.get_by_role('tab',name='Замеры модели').click()
        top.get_by_role('tab',name='Среднее по стенду').click()
        top.locator('#workspace-model-choice').screenshot(path=str(output / 'scenario-comparison.png'))
        before = api('/api/v1/ui/analysis')['settings']['inference']
        top.locator('#workspace-scenario').select_option('C')
        with page.expect_response(lambda r: r.url.endswith('/workspace/model') and r.request.method == 'PUT') as scenario_only:
            top.get_by_role('button', name='Сохранить сценарий').click()
        assert scenario_only.value.ok and scenario_only.value.request.post_data_json == {'scenario': 'C'}
        assert api('/api/v1/ui/analysis')['settings']['inference'] == before
        separate = api('/api/v1/ui/analysis')['settings']
        separate['model'] = 'qwen3-0.6b'
        api('/api/v1/ui/analysis', {'settings': separate}, 'PUT')
        page.reload()
        expect(top.locator('#workspace-scenario')).to_have_value('C')
        top.locator('#workspace-scenario').select_option('B')
        top.get_by_role('button', name='Сохранить сценарий').click()
        wait_for(lambda: api('/api/v1/ui/categorization')['settings']['scenario'] == 'B')
        assert api('/api/v1/ui/analysis')['settings']['model'] == 'qwen3-0.6b'
        slots = api('/api/v1/ui/workspace/model')['slots']
        suggested = 1 if slots > 1 else 2
        check_url = '**/api/v1/ui/worker'
        def with_measurement(route):
            response = route.fetch()
            data = response.json()
            data['calibration'] = {'state':'succeeded', 'model':'qwen3.5-2b', 'recommended_slots':suggested}
            route.fulfill(response=response, json=data)
        page.route(check_url, with_measurement)
        try:
            page.reload()
            model_pick = top.get_by_role('combobox',name='Модель поиска и проверки')
            model_pick.select_option('qwen3.5-2b')
            apply = top.get_by_role('button',name=f'Применить {suggested} слотов')
            expect(apply).to_be_visible()
            model_pick.select_option('qwen3-0.6b')
            expect(apply).to_be_hidden()
            parallel = top.get_by_role('spinbutton',name='Параллельные запросы')
            save = top.get_by_role('button',name='Сохранить',exact=True)
            expect(save).to_be_disabled()
            parallel.fill('11')
            expect(save).to_be_disabled()
            parallel.fill(str(suggested))
            expect(save).to_be_enabled()
            assert api('/api/v1/ui/workspace/model')['slots'] == slots
        finally:
            page.unroute(check_url)
    finally:
        for role, saved in [('analysis',analysis),('categorization',categorization)]:
            api('/api/v1/ui/'+role,{'settings':saved},'PUT')
        page.goto(page.url.split('#')[0]+'#analysis')
        page.reload()
        page.wait_for_function("() => document.querySelector('#analysis-provider').value === 'api'")


def check_manual_mask(page, base, api):
    for role in ('analysis', 'categorization'):
        path = '/api/v1/ui/' + role
        settings = api(path)['settings']
        settings['provider'] = 'none'
        if role == 'analysis':
            settings.update(enabled=False, background=False, auto_apply=False)
        api(path, {'settings': settings}, 'PUT')

    names = ['zorghandfirst', 'zorghandsecond', 'zorghandthird']
    for name in names:
        api('/api/v1/ui/mask', {'text': f'📂 Каталог /srv/{name}/config.toml', 'commit': True})
    jobs = []
    page.on('request', lambda r: jobs.append(r.url) if r.method == 'POST' and r.url.endswith('/review/jobs') else None)
    page.goto('about:blank')
    page.goto(base + '/ui/#review')
    choose_preference(page, 'language', 'ru')
    review = page.locator('#tab-review')
    filters = review.locator('.finding-filters')
    if not filters.evaluate('el => el.open'):
        filters.locator('summary').click()
    review.locator('#review-query').fill('zorghand')
    select_all = review.get_by_role('button', name='Выбрать все 3 по фильтру')
    expect(select_all).to_be_visible()
    review.get_by_role('button', name=names[0], exact=True).click()
    detail = review.locator('#finding-detail')
    expect(detail.locator('blockquote mark')).to_have_text(names[0])
    expect(detail.locator('blockquote')).to_contain_text('/srv/' + names[0] + '/config.toml')
    detail.get_by_role('button', name='Исправить', exact=True).click()
    confirm = page.locator('#review-confirm')
    expect(confirm.locator('code')).to_have_text(names[0])
    confirm.get_by_role('button', name='Отмена').click()
    detail.get_by_role('button', name='Назад к списку').click()
    select_all.click()
    third = review.get_by_role('checkbox', name='Выбрать значение: ' + names[2])
    expect(third).to_be_checked()
    third.uncheck()
    review.get_by_role('button', name='Создать правила маскирования · 2').click()
    expect(confirm.locator('code')).to_have_count(2)
    assert sorted(confirm.locator('code').all_text_contents()) == names[:2]
    confirm.locator('#review-confirm-apply').click()
    expect(review.locator('#review-result')).to_contain_text('Применено: 2')
    masked = api('/api/v1/ui/mask', {'text': ' · '.join(names)})
    assert {r['original'] for r in masked['replacements']} == set(names[:2])
    assert names[2] in masked['text'] and not jobs

    many = ['zorgmanualpage' + chr(97 + i // 26) + chr(97 + i % 26) for i in range(251)]
    for start in range(0, len(many), 100):
        paths = '\n'.join('/srv/' + name + '/config.toml' for name in many[start:start + 100])
        api('/api/v1/ui/mask', {'text': paths, 'commit': True})
    review.locator('#review-query').fill('zorgmanualpage')
    review.get_by_role('button', name='Выбрать все 251 по фильтру').click()
    # A live list refresh must not cancel the user's multi-page selection.
    bulk_read = re.compile(r'/api/v1/ui/review\?snapshot=[^&]+&status=all&offset=0&limit=250$')
    held = []
    page.route(bulk_read, lambda route: held.append(route))
    with page.expect_request(bulk_read):
        review.get_by_role('button', name='Создать правила маскирования · 251').click()
    page.evaluate('loadReview()')
    held.pop().continue_()
    page.unroute(bulk_read)
    expect(confirm).to_be_visible()
    expect(confirm.locator('code')).to_have_count(len(many))
    assert set(confirm.locator('code').all_text_contents()) == set(many)
    confirm.get_by_role('button', name='Отмена').click()

    page.goto(base + '/ui/#discover')
    if page.locator('#d-settings').get_attribute('open') is None:
        page.locator('#d-settings > summary').click()
    page.locator('#d-mode').select_option('fast')
    page.locator('#d-text').fill('📄 Цена 125 000 ₽. Доплата 125 000 ₽.')
    page.locator('#d-analyze').click()
    row = page.locator('#d-list tr').filter(has=page.get_by_role('cell', name='125 000 ₽', exact=True))
    row.locator('summary').click()
    expect(row.locator('pre')).to_have_count(2)
    for index, prefix in enumerate(('Цена ', 'Доплата ')):
        mark = row.locator('pre').nth(index).locator('mark')
        expect(mark).to_have_text('125 000 ₽')
        assert mark.evaluate('m => m.previousSibling.textContent').endswith(prefix)
    page.goto(base + '/ui/#mask')
    page.locator('#m-input').fill('📄 Файл /srv/zorghandthird/config.toml')
    page.locator('#tab-mask .explain > summary').click()
    candidate = page.locator('#m-explain details').filter(has_text=names[2]).first
    candidate.locator('summary').click()
    expect(candidate.locator('mark')).to_have_text(names[2])
    public_url = 'http://github.com/openai'
    page.locator('#m-input').fill(public_url)
    public_candidate = page.locator('#m-explain details').filter(has_text='openai')
    public_candidate.locator('summary').click()
    expect(public_candidate.locator('mark')).to_have_text('openai')
    with page.expect_response(lambda r: r.url.endswith('/ui/mask') and bool(r.request.post_data_json.get('adhoc'))) as preview:
        public_candidate.get_by_role('button', name='Замаскировать только здесь').click()
    assert {r['original'] for r in preview.value.json()['replacements']} == {'openai'}
    expect(page.locator('#m-output')).not_to_contain_text('openai')
    assert api('/api/v1/ui/mask', {'text': public_url})['text'] == public_url
    page.locator('#m-input').press('Control+Enter')
    expect(page.locator('#m-status')).to_contain_text('Токены сохранены')
    expect(page.locator('#lab-navigation [data-go=restore]')).to_be_visible()
    page.locator('#m-clear').click()
    expect(page.locator('#m-commit')).to_be_disabled()
    expect(page.locator('#lab-navigation [data-go=restore]')).to_be_hidden()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary',type=Path);parser.add_argument('--output',type=Path,default=Path('artifacts/review'));args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fail=threading.Event();release=threading.Event();release.set();seen=[];seen_formats=[]
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def respond(self,data,status=200):
            raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            self.respond({'configured':True,'online':True,'enabled':True,'state':'ready','can_manage':False,
                'model_verification':True, 'models':{'qwen3-0.6b':{'downloaded':True,'state':'ready'},'qwen3.5-2b':{'downloaded':True,'state':'ready'}},'runtime_ready':True} if self.path=='/status' else {'data':[{'id':'fixture-review'}]})
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
            if self.path in ['/enable','/disable']:self.respond({'enabled':True});return
            if self.path=='/evaluate':
                if body.get('operation') == 'probe':
                    assert body['input'] == {} and body['timeout_seconds'] == 300
                    self.respond({'ready':True})
                    return
                if 'items' in body['input']:
                    packet = body['input']
                    policy = 'Trusted anonymization policy' in body['prompt']
                    marked = 'marked_record' in packet['items'][0]
                    typed = 'kind' in body['schema']['properties']['decisions']['items']['properties']
                    scenario = ('C' if typed else 'D') if marked else ('B' if policy else 'A')
                    assert policy == (scenario != 'A')
                    decisions = []
                    for item in packet['items']:
                        if marked: assert f"[[TARGET:{item['id']}]]{item['value']}[[/TARGET:{item['id']}]]" in item['marked_record']
                        row = {'id':item['id'],'verdict':'mask','reason':f'Fixture scenario {scenario}'}
                        if typed: row.update(kind='AMOUNT',scope='team',evidence=item['value'])
                        decisions.append(row)
                    self.respond({'decisions':list(reversed(decisions))})
                    return
                self.respond({'decisions':[{'kind':'AMOUNT','scope':'personal','confidence':.99} for _ in body['input']['candidates']]});return
            if body.get('response_format') or any(isinstance(m.get('content'),str) and 'JSON Schema:' in m['content'] for m in body.get('messages',[])):
                seen_formats.append('response_format' in body)
                content=json.loads(body['messages'][-1]['content']);seen.append(content)
                if 'items' in content:
                    release.wait(timeout=10)
                    if fail.is_set():self.respond({'error':'RAW_BODY_MUST_NOT_LEAK'},503);return
                    result={'decisions':[{'id':item['id'],'action':'review' if 'uncertain' in item['value'] else 'keep' if 'publictool' in item['value'] else 'mask','kind':'NOT_A_KIND' if 'invalidanswer' in item['value'] else 'PROJ','scope':'team','confidence':.97,'reason':'Not enough context to establish confidentiality.' if 'uncertain' in item['value'] else 'Confirmed private project in source context.'} for item in content['items']]}
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
                    page.goto(base+'/ui/#settings')
                    expect(page.locator('#page-title')).to_have_text('Настройки LLM')
                    check_dataset_ui(page)
                    def card_for(item):
                        page.locator('#review-list').get_by_role('button',name=item['value'],exact=True).click()
                        return page.locator('#finding-detail [data-review-key="'+item['key']+'"]')
                    def return_to_findings():
                        page.locator('#jobs-content').get_by_role('button', name='Вернуться к выборке').click()
                        page.locator('#review-list .finding-value').first.wait_for()

                    page.locator('[data-tab="settings"]').click()
                    page.locator('#analysis-navigation [data-go="analysis"]').click();page.locator('#analysis-enabled').wait_for()
                    wait_for(lambda:page.locator('#analysis-enabled').is_enabled())
                    check_local_presets(page, api, args.output)
                    assert page.locator('#analysis-response-format').input_value()=='prompt'
                    page.locator('#analysis-response-format').evaluate("e=>e.closest('details').open=true")
                    page.locator('#analysis-response-format').select_option('grammar');page.locator('#analysis-save').click()
                    wait_for(lambda:api('/api/v1/ui/analysis')['settings']['inference']['response_format']=='grammar')
                    page.locator('#analysis-response-format').select_option('prompt');page.locator('#analysis-save').click();wait_for(lambda:api('/api/v1/ui/analysis')['settings']['inference']['response_format']=='prompt')
                    checked=page.locator('#analysis-enabled').is_checked();help_button=page.locator('#analysis-form .help-check .setting-help-button').first
                    help_button.click();page.locator('#setting-help-popup').wait_for();assert page.locator('#analysis-enabled').is_checked()==checked
                    assert not page.locator('#analysis-enabled-help').is_visible()
                    page.keyboard.press('Escape');assert not page.locator('#setting-help-popup').is_visible()
                    page.evaluate("showTab('review')");page.locator('#review-list .finding-value').first.wait_for()
                    assert 'Нет общей оценки' not in page.locator('#review-list').inner_text()
                    money=next(i for i in rows() if i['value']=='40%');assert money['kind']=='AMOUNT' and money['proposed_kind']=='PERSON' and money['categorization_state']=='disabled'
                    money_card=card_for(money)
                    money_card.get_by_role('tab',name='Происхождение').click()
                    expect(money_card.locator('.origin-fields')).to_contain_text('тип не проверен')
                    target=next(i for i in rows() if i['value']=='zorgreviewalpha');card=card_for(target)
                    rules_before_review=json.dumps(api('/api/v1/rules'))
                    card.get_by_role('button',name='Получить предложение ИИ',exact=True).click()
                    wait_for(lambda:any(i.get('recommendation',{}).get('state')=='succeeded' for i in rows() if i['key']==target['key']))
                    return_to_findings();card=card_for(target);card.locator('.decision-summary').wait_for()
                    assert json.dumps(api('/api/v1/rules'))==rules_before_review
                    assert seen_formats and not any(seen_formats)
                    assert rows()[0].get('origin')
                    first_proposal = next(i for i in rows() if i['key']==target['key'])['proposal']['id']
                    page.locator('#review-advice-filter').check()
                    card = card_for(target)
                    expect(card.locator('.decision-summary')).to_contain_text('Команда')
                    card.get_by_role('button',name='Отклонить предложение',exact=True).click()
                    wait_for(lambda:not any(i['key']==target['key'] for i in api('/api/v1/ui/review?status=proposals')['items']))
                    assert json.dumps(api('/api/v1/rules')) == rules_before_review
                    page.reload()
                    card = card_for(target)
                    expect(card.locator('.decision-summary')).to_contain_text('Предложение отклонено')
                    card.get_by_role('button',name='Получить предложение ИИ',exact=True).click()
                    wait_for(lambda:any(i['key']==target['key'] and i.get('proposal',{}).get('id')!=first_proposal and not i['proposal']['declined'] for i in rows()))
                    endpoint='/api/v1/ui/review/'+target['origin']+'/'+target['id']+'/accept-proposal'
                    try:
                        api(endpoint, {'proposal_id':first_proposal})
                    except urllib.error.HTTPError as error:
                        assert error.code == 400
                    else:
                        raise AssertionError('accepted a superseded proposal')
                    return_to_findings()
                    page.locator('#review-advice-filter').check()
                    card_for(target).locator('[data-accept-proposal]').click()
                    wait_for(lambda:not any(i['key']==target['key'] for i in rows()))
                    expect(page.locator('#review-confirm')).to_be_hidden()
                    # Hold the response after persistence to open another card while the UI is still saving.
                    rollback = []
                    rollback_url = '**/ui/review/*/*/rollback'
                    def hold_rollback(route):
                        rollback.append((route, route.fetch()))
                    page.route(rollback_url, hold_rollback)
                    page.locator('[data-review-view=history]').click();card=card_for(target);card.get_by_role('button',name='Отменить правило',exact=True).click();page.locator('#review-confirm-apply').click()
                    expect(page.locator('#finding-detail')).to_have_js_property('inert', True)
                    wait_for(lambda:any(i['key']==target['key'] for i in rows()))
                    restored=api('/api/v1/ui/restore',{'text':original['text']});assert 'zorgreviewalpha' in restored['text']
                    page.locator('[data-review-view=pending]').click()
                    hidden=next(i for i in rows() if i['value']=='zorgreviewhidden');hidden_card=card_for(hidden)
                    route, response = rollback.pop()
                    route.fulfill(response=response)
                    page.unroute(rollback_url, hold_rollback)
                    expect(page.locator('#finding-detail')).to_have_js_property('inert', False)
                    before=json.dumps(api('/api/v1/rules'));hidden_card.get_by_role('button',name='Исправить',exact=True).click();page.locator('#review-confirm').get_by_label('Действие',exact=True).select_option('dismiss')
                    assert 'перейдёт в «Разобранные»' in page.locator('#review-confirm').inner_text();page.locator('#review-confirm-apply').click()
                    wait_for(lambda:not any(i['key']==hidden['key'] for i in rows()));assert json.dumps(api('/api/v1/rules'))==before
                    seed('zorgreviewhidden');assert not any(i['key']==hidden['key'] for i in rows())
                    page.locator('[data-review-view=history]').click();card_for(hidden).get_by_role('button',name='Вернуть на проверку',exact=True).click();wait_for(lambda:any(i['key']==hidden['key'] for i in rows()))
                    page.locator('[data-review-view=pending]').click()
                    public=next(i for i in rows() if i['value']=='zorgreviewpublictool');public_card=card_for(public)
                    public_card.get_by_role('button',name='Исправить',exact=True).click();page.locator('#review-confirm').get_by_label('Действие',exact=True).select_option('keep');expect(page.locator('.decision-effect')).to_contain_text('точное исключение')
                    with page.expect_response(lambda r: r.url.endswith('/keep') and r.request.method == 'POST') as kept:
                        page.locator('#review-confirm-apply').click()
                    assert kept.value.ok
                    wait_for(lambda:not any(i['key']==public['key'] for i in rows()));assert 'zorgreviewpublictool' in seed('zorgreviewpublictool')['text']
                    # Failures must be explicit and must not turn into heuristic recommendations.
                    fail.set();page.locator('#review-refresh').click();card=card_for(target)
                    card.get_by_role('tab',name='Происхождение').click()
                    card.get_by_role('button',name='Запросить повторную проверку',exact=True).click()
                    wait_for(lambda:any(i['key']==target['key'] and i.get('recommendation',{}).get('state')=='failed' for i in rows()))
                    return_to_findings();assert 'RAW_BODY_MUST_NOT_LEAK' not in page.locator('#review-list').inner_text();fail.clear()
                    # A successful call can abstain: show why and prohibit automatic application.
                    seed('zorgreviewuncertain');page.locator('#review-refresh').click()
                    uncertain=next(i for i in rows() if i['value']=='zorgreviewuncertain')
                    uncertain_card=card_for(uncertain)
                    before=json.dumps(api('/api/v1/rules'))
                    uncertain_card.get_by_role('button',name='Получить предложение ИИ',exact=True).click()
                    wait_for(lambda:any(i['key']==uncertain['key'] and i.get('recommendation',{}).get('state')=='needs_review' for i in rows()))
                    return_to_findings();uncertain_card=card_for(uncertain);uncertain_card.locator('.decision-summary').wait_for()
                    assert 'ИИ просит решить вручную' in uncertain_card.inner_text()
                    assert uncertain_card.locator('[data-accept-proposal]').count()==0
                    expect(uncertain_card.get_by_role('button', name='Исправить', exact=True)).to_be_enabled()
                    assert json.dumps(api('/api/v1/rules'))==before
                    # One invalid row must not erase a usable neighboring recommendation.
                    seed('zorgreviewvalidanswer');seed('zorgreviewinvalidanswer');page.locator('#review-refresh').click()
                    mixed=[i for i in rows() if i['value'] in ['zorgreviewvalidanswer','zorgreviewinvalidanswer']]
                    assert len(mixed)==2
                    for item in mixed:
                        page.locator('#review-list').get_by_role('checkbox',name='Выбрать значение: '+item['value']).check()
                    page.locator('#review-selection').get_by_role('button',name='Проверить моделью · 2',exact=True).click()
                    wait_for(lambda:all(any(i['key']==item['key'] and i.get('recommendation',{}).get('state')==('failed' if 'invalidanswer' in item['value'] else 'succeeded') for i in rows()) for item in mixed))
                    page.locator('#jobs-content').get_by_role('button',name='Ошибки · 1',exact=True).wait_for()
                    return_to_findings()
                    for item in mixed:
                        mixed_card=card_for(item)
                        expected = 0 if 'invalidanswer' in item['value'] else 1
                        wait_for(lambda: mixed_card.locator('[data-accept-proposal]').count() == expected)
                    assert page.locator('#review-selection').get_by_role('button',name='Принять предложения',exact=True).count()==0
                    assert json.dumps(api('/api/v1/rules'))==before
                    page.locator('#review-selection').get_by_role('button',name='Снять выбор',exact=True).click()
                    # The event exposes a concrete reason and stable links, without leaking values in summaries.
                    api('/v1/chat/completions',{'model':'fixture-review','messages':[{'role':'user','content':'Read /srv/zorgrevieweventalpha/config.toml'}]})
                    event=wait_for(lambda:next((e for e in api('/api/v1/metrics')['traffic']['events'] if e['usage']['uncertain']),None))
                    assert 'zorgrevieweventalpha' not in json.dumps(api('/api/v1/metrics')['traffic'])
                    page.evaluate('(id)=>openInspector(id,"review")',event['id']);page.locator('#inspector-dialog[open]').wait_for();assert 'zorgrevieweventalpha' in page.locator('#inspector-dialog').inner_text()
                    page.locator('#inspector-dialog').get_by_role('button',name='Открыть связанные решения').first.click();page.locator('#review-list').filter(has_text='zorgrevieweventalpha').wait_for()
                    wait_for(lambda:'zorgreviewhidden' not in page.locator('#review-list').inner_text())
                    page.locator('#review-list').get_by_role('button',name='Все находки',exact=True).click()
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
                    page.locator('#review-list').get_by_role('button',name='Все находки',exact=True).click()
                    # Selected records only; grouping is not a wildcard write operation.
                    page.locator('#review-groups').click();assert page.locator('#review-list .review-group').count()>0
                    page.locator('#review-groups').click();page.locator('#review-select-page').check();assert page.locator('#review-selection').is_visible()
                    page.locator('#review-selection').get_by_role('button',name='Снять выбор',exact=True).click()
                    for language in ['ru','en']:
                        choose_preference(page, 'language', language);wait_for(lambda:page.locator('html').get_attribute('lang')==language)
                        for theme in ['dark','light']:
                            choose_preference(page, 'theme', theme)
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
                    # The document control overrides only this run. Retry deduplicates within the same scenario.
                    page.evaluate("showTab('analysis')")
                    page.locator('#analysis-open-document').click()
                    page.locator('#d-text').fill('Private estimate. Аванс 40%, цена 1 320,00 ₽.\nPublic sample: 40%.')
                    for variant in 'ABCD':
                        page.evaluate("showTab('discover')")
                        if page.locator('#d-settings').get_attribute('open') is None:
                            page.locator('#d-settings > summary').click()
                        page.locator('#d-scenario').select_option(variant)
                        with page.expect_response(lambda r:r.url.endswith('/analysis/documents') and r.request.method == 'POST') as response:
                            page.locator('#d-analyze').click()
                        job_id = response.value.json()['ids'][0]
                        found = wait_for(lambda: (group if len(group := [i for i in rows('all') if i.get('job_id') == job_id]) == 2 else None))
                        assert all(i.get('confidence') is None and i['state'] == 'pending' for i in found)
                        for item in found:
                            ambiguous = variant in 'CD' and item['value'] == '40%'
                            judgment = item['scenario_result']
                            assert judgment['verdict'] == ('review' if ambiguous else 'mask')
                            assert judgment['reason'] == ('target_occurrence_uncertain' if ambiguous else 'Fixture scenario '+variant)
                            assert (judgment['kind'] is None) == (ambiguous or variant == 'D')
                    assert api('/api/v1/ui/categorization')['settings']['scenario'] == 'standard'
                    page.evaluate("showTab('discover')")
                    with page.expect_response(lambda r:r.url.endswith('/analysis/documents') and r.request.method == 'POST') as response:
                        page.locator('#d-analyze').click()
                    assert response.value.json()['queued'] == 0
                    check_manual_mask(page, base, api)
                    assert not errors,errors
                except Exception:
                    page.screenshot(path=str(args.output/'failure.png'),full_page=True);(args.output/'browser-errors.json').write_text(json.dumps(errors))
                    (args.output/'failure-state.json').write_text(json.dumps({'learning':api('/api/v1/ui/learning'),'review':rows('all')},ensure_ascii=False,indent=2));raise
                finally:browser.close()
            report={'passed':True,'checks':['help popup','honest category stages','async model review','prompt JSON profile roundtrip and API payload','explicit apply','rollback and restore','close and reopen','KEEP exception','no heuristic fallback','inconclusive review stays pending','invalid rows preserve valid neighbors','event links and privacy','explicit review from event','numeric corrections require confirmation','selection and grouping','RU/EN themes and mobile',
                'single and bulk masking without a model', 'selection across server pages',
                'occurrence highlights in findings, documents and text', 'local-only text masking']}
            (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report))
        finally:release.set();process.terminate();process.wait(timeout=20);log.close();provider.shutdown();provider.server_close()

if __name__=='__main__':main()
