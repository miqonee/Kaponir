"""Check the rules catalog, audit, download and guarded deletion against a real server."""
import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright
from validate_console import choose_preference, free_port, wait_for


def seed(api):
    rules = [
        ('global', dict(id='office-code', kind='CUSTOM', regex='FIN-[0-9]{4}', note='Номер финансовой заявки', enforced=True)),
        ('team', dict(id='orion', kind='PROJ', value='Orion', note='Внутренний проект', case_sensitive=True)),
        ('team', dict(id='tracker', kind='SVC', value='issue-tracker', note='Сервис команды')),
        ('personal', dict(id='review-questions', kind='PROJ', value='questions', note='kaponir discover')),
        ('personal', dict(id='public-sample', action='keep', value='open-example', note='Публичный пример из документации')),
        ('team', dict(id='old-project', kind='PROJ', value='ArchiveDemo', disabled=True, note='Завершённый проект')),
    ]
    for scope, rule in rules:
        api('/api/v1/rules', {'scope': scope, 'rule': rule})
    api('/api/v1/rules/team/domains', {'domains': ['company.example']}, 'PUT')


def check(page, api, output):
    states = page.locator('#rules-state')
    rows = page.locator('#rules-list tbody tr:visible')
    def state(value):
        states.locator(f'label:has(input[value="{value}"])').click()

    expect(rows).to_have_count(4)
    expect(states.locator('input[value=mask]')).to_be_checked()
    expect(page.locator('#tab-rules [data-go=mask]')).to_have_count(0)
    expect(page.locator('#rules-match-count')).to_have_text('Показано 4 из 6')
    page.screenshot(path=output/'after-list.png', animations='disabled')

    state('keep')
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text('open-example')
    page.locator('#rules-scope').select_option('team')
    expect(rows).to_have_count(0)
    expect(page.locator('#rules-empty')).to_be_visible()
    page.locator('#rules-clear').click()
    expect(rows).to_have_count(4)
    page.locator('#rules-query').fill('Сервис')
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text('issue-tracker')

    # Download all scopes even when search results contain just one row.
    with page.expect_download() as download:
        page.locator('#rules-export').click()
    assert download.value.suggested_filename == 'kaponir-rules.json'
    bundle = json.loads(download.value.path().read_text())
    assert bundle == {'format': 'kaponir-rules', 'version': 1,
                      'rules': {s['scope']: s['file'] for s in api('/api/v1/rules')}}
    page.locator('#rules-clear').click()
    state('all')
    before = rows.locator('.rule-subject strong').all_text_contents()
    page.locator('#rules-sort').click()
    assert rows.locator('.rule-subject strong').all_text_contents() == before[::-1]

    page.locator('#rules-new').click()
    form = page.locator('#rule-editor')
    expect(form.locator('[name=pattern]')).to_be_focused()
    form.locator('[name=pattern]').fill('CedarDesk')
    form.locator('[name=kind]').fill('PROJ')
    form.locator('[name=scope]').select_option('team')
    expect(form.locator('[name=enforced]')).to_be_hidden()
    form.locator('[name=note]').fill('Внутренний проект команды')
    form.locator('[name=case_sensitive]').check()
    form.screenshot(path=output/'after-editor.png', animations='disabled')
    form.locator('[name=action]').select_option('keep')
    expect(form.locator('[name=kind]')).to_be_hidden()
    expect(form.locator('[name=strategy]')).to_be_hidden()
    form.locator('[name=scope]').select_option('global')
    expect(form.locator('[name=enforced]')).to_be_visible()
    form.locator('[name=scope]').select_option('team')
    form.locator('[name=action]').select_option('mask')
    form.get_by_role('button', name='Создать правило').click()
    expect(form).to_be_hidden()
    created = rows.filter(has_text='CedarDesk')
    expect(created).to_have_count(1)
    expect(created).to_contain_text('Команда')

    # Keep a confirmation open while a different client deletes an earlier row.
    created.get_by_role('button', name='Удалить правило «CedarDesk»').click()
    dialog = page.locator('#rule-delete')
    expect(dialog.get_by_role('button', name='Отмена')).to_be_focused()
    team = next(s for s in api('/api/v1/rules') if s['scope'] == 'team')
    api('/api/v1/rules/team/0?revision='+team['revision'], method='DELETE')
    dialog.get_by_role('button', name='Удалить правило', exact=True).click()
    expect(page.locator('#toast')).to_contain_text('Список изменился')
    expect(created).to_have_count(1)
    created.get_by_role('button', name='Удалить правило «CedarDesk»').click()
    page.keyboard.press('Escape')
    expect(created).to_have_count(1)
    created.get_by_role('button', name='Удалить правило «CedarDesk»').click()
    dialog.get_by_role('button', name='Удалить правило', exact=True).click()
    expect(created).to_have_count(0)
    assert any(r.get('id') == 'tracker' for s in api('/api/v1/rules') for r in s['file']['rule'])

    mask = lambda: api('/api/v1/anonymize', {'text': 'questions', 'session': 'rules-audit'})['text']
    token = mask()
    assert token != 'questions'
    page.locator('[data-rules-view=audit]').click()
    audit = page.locator('#audit-list tbody tr')
    rule = audit.filter(has_text='questions').filter(has_text='kaponir discover')
    expect(rule.locator('.audit-state')).to_have_text('Включено')
    rule.get_by_role('button', name='Отключить').click()
    expect(rule.locator('.audit-state')).to_have_text('Отключено')
    expect(rule.get_by_role('button', name='Включить снова')).to_be_focused()
    expect(rule).to_contain_text('Общее слово или публичное обозначение')
    assert mask() == 'questions'
    assert api('/api/v1/deanonymize', {'text': token, 'session': 'rules-audit'})['text'] == 'questions'
    rule.get_by_role('checkbox').check()
    expect(page.locator('#audit-disable')).to_be_hidden()
    page.locator('#audit-enable').click()
    expect(rule.locator('.audit-state')).to_have_text('Включено')
    assert mask() != 'questions'
    page.screenshot(path=output/'after-audit.png', animations='disabled')

    page.locator('[data-rules-view=list]').click()
    state('disabled')
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text('ArchiveDemo')
    choose_preference(page, 'language', 'en')
    expect(page.locator('#rules-export')).to_have_text('Download all rules')
    expect(rows).to_contain_text('Disabled')
    choose_preference(page, 'theme', 'light')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#rules-new').click()
    expect(form.get_by_role('button', name='Create rule')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('#rules-new')).to_be_focused()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    base = f'http://127.0.0.1:{free_port()}'
    def api(path, body=None, method=None):
        req = Request(base+path, data=None if body is None else json.dumps(body).encode(), method=method,
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=20) as res:
            return json.load(res)
    env = {k: v for k, v in os.environ.items() if not k.startswith('KAPONIR_')}
    with tempfile.TemporaryDirectory() as home, sync_playwright() as pw:
        subprocess.run([args.binary.resolve(), '--home', home, 'init'], env=env, capture_output=True, check=True)
        browser = pw.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        @contextmanager
        def server(binary):
            proc = subprocess.Popen([binary.resolve(), '--home', home, 'serve', '--listen', base[7:]],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_for(lambda: api('/api/v1/ui/info'))
                yield
            finally:
                proc.terminate()
                proc.wait(timeout=15)
        for baseline, binary in [(True, args.baseline), (False, args.binary)]:
            if not binary:
                continue
            with server(binary):
                if baseline or not args.baseline:
                    seed(api)
                page = browser.new_page(viewport={'width': 1440, 'height': 960}, locale='ru-RU', color_scheme='dark')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base+'/ui/#rules')
                expect(page.locator('#rules-list')).to_contain_text('Orion')
                if baseline:
                    page.screenshot(path=args.output/'before-list.png', animations='disabled')
                    page.locator('#rule-editor summary').click()
                    page.locator('#rf [name=pattern]').fill('CedarDesk')
                    page.locator('#rf [name=kind]').fill('PROJ')
                    page.locator('#rf [name=scope]').select_option('team')
                    page.locator('#rf [name=note]').fill('Внутренний проект команды')
                    page.locator('#rf [name=case_sensitive]').check()
                    page.locator('#rule-editor').screenshot(path=args.output/'before-editor.png')
                else:
                    check(page, api, args.output)
                assert not errors, errors
                page.close()
        browser.close()
    print('Verified filtering, sorting, all-scope export, creation, guarded deletion and audit re-enabling.')


if __name__ == '__main__':
    main()
