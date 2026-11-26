#!/usr/bin/env python3
"""Live console regression against a local fixture provider (requires Playwright).

python3 scripts/validate_console.py target/debug/kaponir --output artifacts/console
No external models or provider accounts are used.
"""
import argparse
import re
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for(check, timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except Exception as error:
            last = error
        time.sleep(.1)
    raise AssertionError(f'condition timed out: {last}')


def choose_preference(page, preference, value):
    menu = page.locator('#menu-toggle')
    if menu.is_visible() and menu.get_attribute('aria-expanded') != 'true':
        menu.click()
    page.locator(f'#{preference}-switch label:has(input[value="{value}"])').click()
    if menu.is_visible() and menu.get_attribute('aria-expanded') == 'true':
        menu.click()


def check_session_conversation(page, api, session_id, output):
    from playwright.sync_api import expect
    page.set_viewport_size({'width': 1440, 'height': 960})
    choose_preference(page, 'language', 'ru')
    api('/v1/chat/completions', {'model': 'fixture-model', 'messages': [
        {'role': 'user', 'content': 'Другая смета: 98 500 ₽.'}]}, {'X-Kaponir-Session': 'unrelated'})
    page.locator('#sidebar').get_by_role('link', name='Сессии', exact=True).click()
    expect(page.locator('#page-title')).to_have_text('Сессии')
    button = page.locator('#events-table [data-session-open="' + session_id + '"]')
    button.click()
    dialog = page.locator('#session-dialog')
    expect(dialog.locator('select option')).to_have_count(2)
    requests = dialog.get_by_role('radio')
    expect(requests).to_have_count(2)
    assert dialog.locator('.conversation-requests').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
    assert dialog.locator('.conversation-requests label span').first.bounding_box()['width'] > 100
    expect(requests.last).to_be_checked()
    requests.last.focus()
    page.keyboard.press('ArrowUp')
    expect(requests.first).to_be_checked()
    expect(dialog.locator('.session-messages')).to_contain_text('ConsoleFixture_47')
    expect(dialog.locator('.session-message').first.locator('mark.masked')).to_contain_text('ConsoleFixture_47')
    page.keyboard.press('ArrowDown')
    expect(requests.last).to_be_checked()
    expect(dialog.locator('.session-messages')).to_contain_text('125 000 ₽')
    dialog.get_by_role('button', name='Предыдущее сообщение').click()
    expect(dialog.locator('.session-messages')).to_contain_text('ConsoleFixture_47')
    expect(dialog.locator('.session-messages')).not_to_contain_text('125 000 ₽')
    expect(dialog.get_by_role('button', name='Предыдущее сообщение')).to_be_disabled()
    dialog.get_by_role('button', name='Следующее сообщение').click()
    expect(dialog.locator('.session-message').first).to_contain_text('Проверь смету. Бюджет 125 000 ₽.')
    expect(dialog.locator('.session-message').nth(1)).to_contain_text('Смета принята. Бюджет 125 000 ₽.')
    history = dialog.locator('.session-history')
    expect(history.locator('.message-content')).not_to_be_visible()
    history.locator('summary').click()
    expect(history.locator('.message-content')).to_contain_text('ConsoleFixture_47')
    history.locator('summary').click()
    dialog.screenshot(path=str(output / 'session-conversation.png'))
    # Distinguish two identical literals by their transmitted position and the recorded UTF-8 context.
    marks = page.evaluate("""() => {
        const root=document.createElement('div');
        root.append(...sessionQuestion({inspector:{original_request:'[user]\\nёж Atlas и Atlas',masked_request:'[user]\\nёж ANON_1 и Atlas',last_user_offset:0},
          usage:{masked_items:[{original:'Atlas',token:'ANON_1'}],review_items:[{value:'Atlas',context:'и Atlas',context_span:[3,8]}]}}));
        return [...root.querySelectorAll('mark')].map(m=>[m.textContent,m.classList.contains('masked'),m.classList.contains('uncertain'),m.getAttribute('aria-description')]);
    }""")
    assert marks == [['Atlas',True,False,'Скрыто при передаче'],['Atlas',False,True,'Спорное решение']], marks
    page.set_viewport_size({'width': 390, 'height': 844})
    assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
    expect(dialog.get_by_role('button', name='Найти значения в этой сессии')).to_be_visible()
    page.set_viewport_size({'width': 1440, 'height': 960})
    with page.expect_response(lambda r: r.url.endswith('/discover/sessions') and r.request.method == 'POST') as scanned:
        dialog.get_by_role('button', name='Найти значения в этой сессии').click()
    assert scanned.value.request.post_data_json['target'] == {'session': session_id}
    assert scanned.value.request.post_data_json['use_s1'] is False
    assert scanned.value.json()['sessions_analyzed'] == 1
    expect(page.locator('#d-list')).to_contain_text('125 000 ₽')
    expect(page.locator('#d-list')).not_to_contain_text('98 500 ₽')
    expect(page.locator('#discover-input-pane')).not_to_be_visible()
    page.locator('#discover-findings').click()
    expect(page.locator('#review-list')).to_contain_text('125 000 ₽')
    expect(page.locator('#review-list')).not_to_contain_text('98 500 ₽')
    unknown = api('/api/v1/ui/discover/sessions', {'target': {'session': 'unrecorded-session'}, 'remember': False, 'use_s1': False})
    assert unknown['sessions_analyzed'] == 0 and not unknown['report']['suggestions']
    page.locator('[data-tab=events]').click()
    with page.expect_response(lambda r: r.url.endswith('/discover/sessions') and r.request.method == 'POST') as all_sessions:
        page.locator('#events-scan-all').click()
    assert all_sessions.value.json()['sessions_analyzed'] == 2
    expect(page.locator('#d-list')).to_contain_text('98 500 ₽')
    page.get_by_role('button', name='К списку сессий').click()
    expect(page.locator('#page-title')).to_have_text('Сессии')

    question = 'Проверь параметры Helm.'
    chain = [{'role':'user', 'content':question}]
    def send_step():
        api('/v1/chat/completions', {'model':'fixture-model', 'messages':chain}, {'X-Kaponir-Session':'agent-steps'})
    send_step()
    for index in range(5):
        call_id = f'read-{index}'
        chain.extend([{'role':'assistant', 'content':None, 'tool_calls':[{'id':call_id, 'type':'function',
                       'function':{'name':'read_file','arguments':'{}'}}]},
                      {'role':'tool','tool_call_id':call_id,'content':"dbPassword: 'test''fixture'"}])
        send_step()
    latest = max(api('/api/v1/ui/events')['events'], key=lambda row:row['id'])
    page.locator('#events-refresh').click()
    page.locator('#events-table [data-session-open="'+latest['session_id']+'"]').click()
    expect(dialog.locator('.conversation-totals')).to_have_text('Сообщений пользователя · 1 · Вызовов API · 6')
    expect(dialog.locator('.session-message').first).to_contain_text(question)
    expect(dialog.locator('.conversation-timeline .reply')).to_have_count(6)
    expect(dialog.locator('.conversation-tool summary code')).to_have_text(['read_file'] * 5)
    steps = dialog.locator('.session-steps')
    steps.locator('summary').focus()
    page.keyboard.press('Enter')
    expect(steps.get_by_text('Результат инструмента', exact=True)).to_have_count(5)
    steps.locator('li').last.get_by_role('button', name=f'Запрос {latest["id"]}', exact=True).click()
    wire = page.locator('#inspector-dialog .inspector-pane').last.locator('pre')
    expect(wire.locator('mark').filter(has_text='ANON_SECRET_').first).to_be_visible()
    expect(wire).not_to_contain_text("test''fixture")
    page.keyboard.press('Escape')
    expect(steps.locator('li').first).to_be_visible()
    page.keyboard.press('Escape')


def check_replayed_question(page, api, output):
    from playwright.sync_api import expect
    question = 'Проверь конфигурацию.'
    history = [{'role':'user','content':question}]
    session = {'X-Kaponir-Session':'replayed-question'}
    def send():
        return api('/v1/chat/completions', {'model':'fixture-model','messages':history}, session)
    first = send()['choices'][0]['message']
    second = send()['choices'][0]['message']  # the very same human submission on the wire
    history.extend([second, {'role':'tool','tool_call_id':'read-settings','content':'resources.limits.memory = 512Mi'}])
    send()
    latest = max(api('/api/v1/ui/events')['events'], key=lambda r:r['id'])
    page.locator('#events-refresh').click()
    page.locator('#events-table [data-session-open="'+latest['session_id']+'"]').click()
    dialog = page.locator('#session-dialog')
    expect(dialog.locator('.conversation-totals')).to_have_text('Сообщений пользователя · 1 · Вызовов API · 3')
    expect(dialog.locator('.session-message:not(.reply) .message-content')).to_have_text(question)
    timeline = dialog.locator('.conversation-timeline')
    replies = timeline.locator('.reply .message-content')
    expect(replies).to_have_text([first['content'],second['content'],'Лимит памяти задан.'])
    expect(timeline).not_to_contain_text('private reasoning fixture')
    tool = timeline.locator('.conversation-tool')
    expect(tool).to_have_count(1)
    expect(tool.locator('summary')).to_contain_text('read_file')
    tool.locator('summary').focus()
    page.keyboard.press('Enter')
    expect(tool.locator('pre')).to_have_text(['{"path":"values.yaml"}','resources.limits.memory = 512Mi'])
    send()  # replay a tool callback while its details are open
    expect(dialog.locator('.conversation-totals')).to_have_text('Сообщений пользователя · 1 · Вызовов API · 4',timeout=15000)
    expect(replies).to_have_text([first['content'],second['content'],'Лимит памяти задан.','Проверка завершена.'])
    expect(tool).to_have_count(1)
    expect(tool.locator('pre').last).to_be_visible()
    expect(tool.locator('summary')).to_be_focused()
    expect(dialog.locator('.session-message:not(.reply)')).to_have_count(1)
    dialog.screenshot(path=str(output/'conversation-agent-turn.png'))
    history.extend([{'role':'assistant','content':'Проверка завершена.'},{'role':'user','content':question}])
    send()  # a genuine next user turn, even though the words are identical
    expect(dialog.locator('.conversation-totals')).to_have_text('Сообщений пользователя · 2 · Вызовов API · 5',timeout=15000)
    dialog.get_by_role('button',name='Следующее сообщение').click()
    expect(replies).to_have_text(['Проверяю конфигурацию ещё раз.'])
    expect(tool).to_have_count(0)
    expect(dialog.locator('.conversation-requests label strong')).to_have_text([question,question])
    page.keyboard.press('Escape')
    # Service calls, parallel replies and a failed call have distinct timeline semantics.
    activity = page.evaluate('''() => {
        const makeRow=(id,start,end,outcome="ok")=>({id,started_at_ms:start,time_ms:end,outcome});
        const rows=[makeRow(1,10,40),makeRow(2,20,30),makeRow(3,50,60,"error"),{...makeRow(4,10,11),request_kind:"count_tokens"}];
        const details=new Map(rows.slice(0,2).map(r=>[r.id,{reply:{parts:[{kind:"text",text:String(r.id)}]}}]));
        const result={id:"r",name:"read",result:"original"};
        details.get(1).tools=[result];
        details.get(2).tools=[result,{...result,result:"changed"}];
        return conversationActivity(rows,details).map(a=>[a.row.id,a.kind]);
    }''')
    assert activity == [[1,'result'],[2,'result'],[2,'text'],[1,'text'],[3,'status']], activity
    return latest['session_id']


def check_session_inspector(page, api, base):
    from playwright.sync_api import expect
    session = {'X-Kaponir-Session':'inspector-regression'}
    api('/api/v1/rules', {'scope':'team','rule':{'kind':'PROJ','value':'InspectorProject'}})
    source = 'Проверка InspectorProject.\n' + '\n'.join(f'Строка {i} отчёта.' for i in range(70))
    past = {'role':'user','content':'Открой /srv/qzworkitem/log.txt перед публикацией.'}
    api('/v1/chat/completions', {'model':'fixture-model','messages':[past,
        {'role':'assistant','content':'Проверю журнал.'},{'role':'user','content':source}]}, session)
    api('/v1/chat/completions', {'model':'fixture-model','messages':[
        {'role':'user','content':'Верни первое значение сравнения.'}]}, session)
    event = max(api('/api/v1/metrics')['traffic']['events'], key=lambda row:row['id'])
    detail = api('/api/v1/ui/events/'+str(event['id']))
    assert not detail['usage']['masked_items'] and len(detail['usage']['restore_marks']) == 1
    page.context.grant_permissions(['clipboard-read','clipboard-write'], origin=base)
    page.locator('#sidebar [data-tab=events]').click()
    entry = page.locator('#events-table [data-session-open="'+event['session_id']+'"]')
    entry.click()
    dialog = page.locator('#session-dialog')
    dialog.get_by_role('button', name='Подробности запроса').click()
    viewer = page.locator('#inspector-dialog')
    viewer.locator('[data-key=response]').click()
    panes = viewer.locator('.inspector-pane pre')
    expect(panes.last.locator('mark')).to_have_text('InspectorProject')
    expect(panes.first.locator('mark')).to_have_text(detail['usage']['restore_marks'][0]['token'])
    page.keyboard.press('Escape')
    entry.click()
    dialog.locator('.conversation-requests').get_by_role('button', name='Спорное решение детектора').first.click()
    focused = dialog.locator('.session-history button:focus')
    expect(focused).to_have_text('qzworkitem')
    focused.press('Enter')
    expect(viewer.locator('.review-card:focus code')).to_have_text('qzworkitem')
    viewer.locator('[data-key=request]').click()
    expect(panes.first.locator('mark').filter(has_text='InspectorProject')).to_have_count(1)
    expect(panes.last.locator('mark').filter(has_text='PROJ')).to_have_count(1)
    panes.first.evaluate('el=>{el.scrollTop=el.scrollHeight}')
    wait_for(lambda:panes.last.evaluate('el=>el.scrollTop>0 && Math.abs(el.scrollHeight-el.clientHeight-el.scrollTop)<2'))
    panes.last.evaluate('el=>{el.scrollTop=0}')
    wait_for(lambda:panes.first.evaluate('el=>el.scrollTop<1'))
    sync = viewer.get_by_role('checkbox', name='Синхронная прокрутка')
    sync.uncheck()
    viewer.locator('[data-key=response]').click()
    expect(sync).not_to_be_checked()
    viewer.locator('[data-key=request]').click()
    sync.check()
    viewer.get_by_role('button', name='JSON').click()
    expect(sync).to_be_disabled()
    viewer.locator('.inspector-pane').last.get_by_role('button', name='Копировать').click()
    assert json.loads(page.evaluate('navigator.clipboard.readText()'))['model'] == 'fixture-model'
    viewer.get_by_role('button', name='Текст', exact=True).click()
    expect(sync).to_be_enabled()
    page.keyboard.press('Escape')
    page.keyboard.press('Escape')


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--output', type=Path, default=Path('artifacts/console'))
    args = parser.parse_args()
    binary = args.binary.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    seen = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            raw = json.dumps({'data': [{'id': 'fixture-model'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(payload)
            assert not self.headers.get('X-Kaponir-Turn')
            text = payload['messages'][-1]['content']
            extra = {}
            if any(m.get('role')=='user' and m.get('content')=='Проверь конфигурацию.' for m in payload['messages']):
                previous = sum(any(m.get('content')=='Проверь конфигурацию.' for m in q['messages']) for q in seen) - 1
                text = ['Посмотрю настройки контейнера.','Прочитаю лимиты из values.yaml.',
                        'Лимит памяти задан.','Проверка завершена.','Проверяю конфигурацию ещё раз.'][previous]
                if previous == 1:
                    extra = {'reasoning_content':'private reasoning fixture','tool_calls':[{'id':'read-settings','type':'function',
                        'function':{'name':'read_file','arguments':'{"path":"values.yaml"}'}}]}
            if text == 'Верни первое значение сравнения.':
                text = re.search(r'ANON_PROJ_\d+',seen[-2]['messages'][-1]['content'])[0]
            if text.startswith('Проверь смету. '):
                text = 'Смета принята. ' + text.removeprefix('Проверь смету. ')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if payload.get('stream') else 'application/json')
            self.end_headers()
            if payload.get('stream'):
                time.sleep(.7)
                for letter in text:
                    chunk = {'choices': [{'index': 0, 'delta': {'content': letter}}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                    self.wfile.flush()
                    time.sleep(.01)
                usage = {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}
                self.wfile.write(('data: ' + json.dumps(usage) + '\n\ndata: ' + json.dumps(usage) + '\n\ndata: [DONE]\n\n').encode())
            else:
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': text, **extra}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}).encode())

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    upstream = f'http://127.0.0.1:{provider.server_port}'
    with tempfile.TemporaryDirectory(prefix='kaponir-console-') as directory:
        home = Path(directory)
        env = {key:value for key,value in os.environ.items() if not key.startswith('KAPONIR_')}
        env['KAPONIR_WORKER_PYTHON'] = sys.executable
        subprocess.run([binary, '--home', home, 'init'], check=True, capture_output=True, env=env)
        port, worker_port = free_port(), free_port()
        base = f'http://127.0.0.1:{port}'
        env['KAPONIR_WORKER_LISTEN'] = f'127.0.0.1:{worker_port}'
        process = None

        def api(path, body=None, headers=None):
            request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json', **(headers or {})})
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.headers.get('Content-Type', '').startswith('text/event-stream'):
                    return response.read().decode()
                return json.load(response)

        log = (args.output / 'server.log').open('w')

        def start():
            nonlocal process
            process = subprocess.Popen([binary, '--home', home, 'serve', '--listen', f'127.0.0.1:{port}', '--openai-upstream', upstream], stdout=log, stderr=log, env=env)
            wait_for(lambda: api('/api/v1/ui/info'))

        def stop():
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=30)

        try:
            start()
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=shutil.which('chromium') or shutil.which('google-chrome'), headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, locale='ru-RU', color_scheme='dark')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/ui/#overview')
                page.locator('#summary-cards .metric-value').first.wait_for()
                assert page.locator('.task-home').bounding_box()['y'] < 300
                assert page.locator('#summary-cards').bounding_box()['y'] < 800
                api('/api/v1/connections', {'kind': 'openai', 'name': 'Fixture', 'base_url': upstream, 'api_key': 'fixture-key', 'pool': True, 'models': ['fixture-model'], 'auto_models': False})
                page.locator('#overview-refresh').click()
                page.wait_for_function("() => document.querySelector('#welcome-banner').hidden")
                assert api('/api/v1/metrics')['traffic']['total']['requests'] == 0

                first = {'model': 'fixture-model', 'stream': True, 'messages': [{'role': 'user', 'content': 'password: ConsoleFixture_47!Secret'}]}
                responses = []
                thread = threading.Thread(target=lambda: responses.append(api('/v1/chat/completions', first, {'X-Kaponir-Session': 'console-session'})))
                thread.start()
                wait_for(lambda: api('/api/v1/metrics')['traffic']['active'])
                page.locator('#tab-overview [data-go="events"]').first.click()
                page.locator('#events-table .event-status.in_progress').first.wait_for()
                thread.join(timeout=20)
                assert not thread.is_alive()
                second = {'model': 'fixture-model', 'messages': [*first['messages'], {'role': 'assistant', 'content': 'Готово 📄'}, {'role': 'user', 'content': 'Проверь смету. Бюджет 125 000 ₽.'}]}
                api('/v1/chat/completions', second, {'X-Kaponir-Session': 'console-session'})
                traffic = api('/api/v1/metrics')['traffic']
                assert traffic['total']['requests'] == 2
                assert traffic['total']['usage']['total_tokens'] == 24
                assert traffic['total']['usage']['cached_replacements'] > 0
                assert len({e['session_id'] for e in traffic['events']}) == 1
                assert 'ConsoleFixture_47!Secret' not in json.dumps(seen)
                assert 'ConsoleFixture_47!Secret' not in json.dumps(traffic)
                page.locator('#events-refresh').click()
                wait_for(lambda: page.locator('#events-table .session-row').count() == 1)
                page.locator('#events-table .session-toggle').first.click()
                page.locator('#events-table .session-children button').first.click()
                page.locator('#inspector-dialog[open]').wait_for()
                assert 'ConsoleFixture_47!Secret' in page.locator('#inspector-dialog').inner_text()
                page.keyboard.press('Escape')
                assert not page.locator('#inspector-dialog').is_visible()
                assert page.evaluate('Boolean(document.activeElement.dataset.requestId)')

                api('/api/v1/rules', {'scope': 'personal', 'rule': {'kind': 'PROJ', 'value': 'questions', 'note': 'kaponir discover'}})
                original = api('/api/v1/anonymize', {'text': 'questions', 'session': 'audit-chat'})
                page.locator('[data-tab="rules"]').click()
                page.locator('[data-rules-view="audit"]').click()
                page.locator('#audit-list button', has_text='Отключить').first.click()
                page.locator('#audit-list button', has_text='Включить снова').first.wait_for()
                assert api('/api/v1/anonymize', {'text': 'questions', 'session': 'audit-chat'})['text'] == 'questions'
                assert api('/api/v1/deanonymize', {'text': original['text'], 'session': 'audit-chat'})['text'] == 'questions'
                api('/api/v1/rules', {'scope': 'personal', 'rule': {'kind': 'PROJ', 'value': 'character'}})
                legacy = api('/api/v1/anonymize', {'text': 'character', 'session': 'legacy-chat'})
                request = urllib.request.Request(base + '/api/v1/rules/personal/1', method='DELETE')
                urllib.request.urlopen(request).close()
                memory = next(i for i in api('/api/v1/ui/rules/audit')['items'] if i['scope'] == 'memory' and i['rule']['value'] == 'character')
                request = urllib.request.Request(base + '/api/v1/ui/rules/audit/memory', method='PUT', data=json.dumps({'ids':[memory['index']], 'revision':memory['revision'], 'disabled':True}).encode(), headers={'Content-Type':'application/json'})
                urllib.request.urlopen(request).close()
                assert api('/api/v1/anonymize', {'text':'character','session':'legacy-chat'})['text'] == 'character'


                page.locator('[data-tab="settings"]').click()
                page.locator('#analysis-navigation [data-go="analysis"]').click()
                page.locator('#analysis-provider').select_option('api')
                page.locator('#analysis-url').fill('http://127.0.0.1:9999/v1/chat/completions')
                page.locator('#analysis-worker-toggle').click()
                page.wait_for_function("() => document.querySelector('#analysis-worker-state').textContent==='Служба запущена'", timeout=20000)
                assert api('/api/v1/ui/worker')['enabled']
                assert not any(m['downloaded'] for m in api('/api/v1/ui/worker')['models'].values())
                page.locator('#analysis-navigation [data-go="worker"]').click()
                page.locator('#worker-toggle').click()
                page.wait_for_function("() => document.querySelector('#worker-toggle').textContent==='Запустить обработку'")
                assert not api('/api/v1/ui/worker')['enabled']

                page.locator('[data-tab="review"]').click()
                page.locator('#tab-review [data-go="discover"]').click()
                picker = page.locator('.file-picker').filter(has=page.locator('#d-files'))
                assert picker.locator('.file-button').inner_text() == 'Выбрать файлы'
                page.locator('#d-files').set_input_files({'name': 'смета.txt', 'mimeType': 'text/plain', 'buffer': b'Budget'})
                assert picker.locator('.file-name').inner_text() == 'смета.txt'
                page.locator('#d-files').set_input_files([])
                page.locator('#d-text').fill('Проект: Лунная Слива')
                page.locator('#discover-input-pane .analysis-options summary').click()
                page.locator('#d-remember').check()
                page.locator('#d-analyze').click()
                page.locator('#d-list input[type=checkbox]').first.wait_for()
                assert not page.locator('#d-apply').is_visible()
                page.locator('#d-list input[type=checkbox]').first.check()
                assert page.locator('#d-apply').is_visible()
                page.locator('#d-saved').click()
                page.wait_for_function("() => document.querySelector('#discover-input-pane').hidden")
                page.locator('#d-input-view').click()
                assert page.locator('#d-text').input_value() == 'Проект: Лунная Слива'
                choose_preference(page, 'language', 'en')
                page.wait_for_function("() => document.documentElement.lang==='en'")
                assert page.locator('#d-text').input_value() == 'Проект: Лунная Слива'
                expect(page.locator('#page-title')).to_have_text('Lab')
                expect(page.locator('#lab-navigation [aria-current=page]')).to_have_text('Find values')
                assert page.locator('#analysis-url').input_value() == 'http://127.0.0.1:9999/v1/chat/completions'
                page.evaluate("showTab('overview')")
                for theme in ['dark', 'light']:
                    choose_preference(page, 'theme', theme)
                    page.screenshot(path=str(args.output / f'overview-{theme}-en-1440.png'), full_page=True)
                page.evaluate("showTab('discover')")

                choose_preference(page, 'language', 'ru')

                for width in [390, 768, 1440]:
                    page.set_viewport_size({'width': width, 'height': 1000})
                    for theme in ['dark', 'light']:
                        choose_preference(page, 'theme', theme)
                        for tab in ['overview', 'events', 'analysis', 'categorization', 'worker', 'discover', 'mask', 'rules', 'settings']:
                            page.evaluate('(tab)=>showTab(tab)', tab)
                            page.wait_for_timeout(120)
                            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), (width, theme, tab)
                            assert page.locator('.tabs [aria-current=page]').count() == 1
                            page.screenshot(path=str(args.output / f'{tab}-{theme}-{width}.png'), full_page=True)
                page.evaluate("showTab('analysis')")
                page.evaluate("showTab('discover')")
                page.go_back()
                page.wait_for_function("() => location.hash==='#analysis' && !document.querySelector('#tab-analysis').hidden")
                check_session_conversation(page, api, traffic['events'][0]['session_id'], args.output)
                replayed_session = check_replayed_question(page, api, args.output)
                check_session_inspector(page, api, base)
                from validate_navigation import check_navigation
                check_navigation(page, args.output)
                assert not errors, errors
                browser.close()
            api('/api/v1/ui/worker/start', {})
            stop()
            start()
            wait_for(lambda: api('/api/v1/ui/worker').get('enabled'))
            assert api('/api/v1/metrics')['traffic']['total']['requests'] == 16
            restored_rows = sorted((row for row in api('/api/v1/ui/events')['events'] if row['session_id']==replayed_session),key=lambda row:row['id'])
            assert len(restored_rows)==5 and len({r['turn']['id'] for r in restored_rows})==2
            restored = api('/api/v1/ui/events/conversation',{'ids':[r['id'] for r in restored_rows]})
            assert restored[1]['reply']['parts'][1]['name']=='read_file'
            assert restored[2]['tools'][0]['result']=='resources.limits.memory = 512Mi'
            assert api('/api/v1/anonymize', {'text':'character','session':'legacy-chat'})['text'] == 'character'
            assert api('/api/v1/deanonymize', {'text':legacy['text'],'session':'legacy-chat'})['text'] == 'character'
            api('/api/v1/ui/worker/stop', {})
            print('Console checks passed: sessions, SSE, privacy, rule audit, worker, restart, documents, RU/EN, agent turns and English dashboard captures.')
        finally:
            stop()
            log.close()
            provider.shutdown()


if __name__ == '__main__':
    main()
