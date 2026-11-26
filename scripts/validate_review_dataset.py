"""Verify decisions, exact excerpts, draft backfill and search defaults in a fresh vault."""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright
from validate_console import free_port, wait_for


def exercise(page, api, base):
    review = '/api/v1/ui/review'
    dataset = '/api/v1/ui/dataset'

    def findings(value, status='all'):
        return [item for item in api(review + '?' + urlencode({'q': value, 'status': status}))['items']
                if item['value'] == value]

    def decide(item, action, **body):
        return api(f"{review}/{item['origin']}/{item['id']}/{action}", body)

    def exact(row):
        return api(dataset + '?' + urlencode({'example': row['dataset_id']}))['items'][0]

    def project(name, index=0):
        api('/api/v1/ui/discover', {'remember': True, 'documents': [
            {'name': f'memo-{name}-{index}.txt', 'text': f'📁 Запись {index}\nПроект: {name}\nВладелец уточняется.'}]})

    def open_value(name):
        page.locator('#review-list').get_by_role('button', name=name, exact=True).click()
        expect(page.locator('#finding-detail h2')).to_have_text(name)

    # This decision predates the opt-in. The dataset can backfill it later.
    project('studynorth')
    decide(findings('studynorth')[0], 'mask', kind='PROJ', scope='team')
    assert api(dataset)['total'] == 0 and not api(dataset)['capture_review']
    for i in range(3):
        project('studycedar', i)
    for name in ['studyiris', 'studyquartz', 'studywillow', 'studylater', 'studymanual', 'studyorigin']:
        project(name)

    page.goto(base + '/ui/#review')
    expect(page.locator('#review-capture')).to_be_enabled()
    page.locator('#review-capture').check()
    wait_for(lambda: api(dataset)['capture_review'])
    page.reload()
    expect(page.locator('#review-capture')).to_be_checked()
    expect(page.locator('#review-navigation [data-review-view]')).to_have_count(2)
    page.locator('#review-query').fill('study')
    open_value('studylater')
    page.locator('#finding-detail').get_by_role('button', name='Отложить', exact=True).click()
    expect(page.locator('#finding-detail h2')).not_to_have_text('studylater')
    expect(page.locator('#review-list .finding-value').last).to_have_text('studylater')
    assert not findings('studylater')[0]['in_dataset']

    open_value('studycedar')
    expect(page.locator('#finding-detail mark')).to_have_text('studycedar')
    page.locator('#finding-detail').get_by_role('button', name=re.compile('^Скрывать ·')).click()
    expect(page.locator('#review-result')).to_contain_text('проверено 1, черновиков 2')
    expect(page.locator('#finding-detail h2')).not_to_have_text('studycedar')
    cedar = findings('studycedar', 'history')
    assert sorted(row['dataset_status'] for row in cedar) == ['checked', 'draft', 'draft']
    checked = next(exact(r) for r in cedar if r['dataset_status'] == 'checked')
    for row in cedar:
        example = exact(row)
        assert example['finding'] == row['key'] and example['text'] == row['context']
    span = checked['spans'][0]
    assert checked['text'].encode()[span['start_byte']:span['end_byte']].decode() == 'studycedar'
    assert span['action'] == 'mask' and span['kind'] == 'PROJ'

    open_value('studyiris')
    page.locator('#finding-detail').get_by_role('button', name='Исправить', exact=True).click()
    page.locator('#decision-action').select_option('keep')
    page.locator('#review-confirm-apply').click()
    expect(page.locator('#review-result')).to_contain_text('проверено 1, черновиков 0')
    kept = findings('studyiris', 'history')[0]
    assert kept['reason'] == 'human_keep' and exact(kept)['spans'][0]['action'] == 'keep'
    open_value('studyorigin')
    page.locator('#finding-detail [data-pane=origin]').click()
    page.locator('#finding-detail').get_by_role('button', name='Исправить', exact=True).click()
    page.locator('#decision-action').select_option('keep')
    page.locator('#review-confirm-apply').click()
    expect(page.locator('#review-result')).to_contain_text('проверено 0, черновиков 1')
    assert findings('studyorigin', 'history')[0]['dataset_status'] == 'draft'
    rule_count = api('/api/v1/rules')
    try:
        decide(findings('studymanual')[0], 'mask', seen_excerpt='stale-excerpt')
    except HTTPError as error:
        assert 'review_stale' in error.read().decode()
    else:
        raise AssertionError('stale excerpt was applied')
    assert api('/api/v1/rules') == rule_count

    for name in ['studyquartz', 'studywillow']:
        page.locator('#review-list tr').filter(has=page.get_by_role('button', name=name, exact=True)).get_by_role('checkbox').check()
    page.locator('#review-selection').get_by_role('button', name=re.compile('^Создать правила маскирования')).click()
    page.locator('#review-confirm-apply').click()
    expect(page.locator('#review-result')).to_contain_text('проверено 0, черновиков 2')
    assert all(findings(name, 'history')[0]['dataset_status'] == 'draft' for name in ['studyquartz','studywillow'])

    # Manual JSONL edits take precedence, also when the same action is retried.
    checked['spans'][0]['action'] = 'keep'
    api(dataset + '/import', {'items':[checked]})
    chosen = next(r for r in cedar if r['dataset_status'] == 'checked')
    decide(chosen, 'mask', seen_excerpt=chosen['excerpt_id'])
    assert exact(chosen)['spans'][0]['action'] == 'keep'

    page.locator('[data-review-view=history]').click()
    open_value('studyiris')
    expect(page.locator('#finding-detail')).to_contain_text('Проверено в датасете')
    page.locator('[data-tab=dataset]').click()
    expect(page.locator('#dataset-capture')).to_be_checked()
    page.locator('#dataset-collect').click()
    expect(page.locator('#dataset-feedback')).to_contain_text('Добавлено')
    historical = findings('studynorth', 'history')[0]
    assert exact(historical)['status'] == 'draft' and exact(historical)['spans'][0]['action'] == 'mask'
    assert not findings('studymanual')[0]['in_dataset']
    page.locator('#dataset-collect-source').select_option('all')
    page.locator('#dataset-collect').click()
    wait_for(lambda: findings('studymanual')[0]['in_dataset'])
    assert exact(findings('studymanual')[0])['spans'][0]['action'] == 'review'
    assert exact(chosen)['spans'][0]['action'] == 'keep'
    page.locator('#dataset-collect').click()
    expect(page.locator('#dataset-feedback')).to_contain_text('Добавлено 0')

    page.goto(base + '/ui/#discover')
    expect(page.locator('#d-method-summary')).to_have_text('Быстрые детекторы')
    expect(page.locator('#d-mode')).to_be_hidden()
    launches = []
    page.on('request', lambda request: launches.append(request.url)
            if request.method == 'POST' and request.url.endswith(('/discover/sessions', '/worker/jobs')) else None)
    page.locator('#d-sessions').click()
    expect(page.locator('#d-session-input')).to_be_visible()
    assert not launches
    page.locator('#d-analyze').click()
    expect(page.locator('#d-source-summary')).to_contain_text('Проверено сессий')
    analysis = api('/api/v1/ui/analysis')['settings']
    analysis.update(provider='api', enabled=True, background=False, model='sample-model', url=base + '/v1')
    api('/api/v1/ui/analysis', {'settings':analysis, 'key':'fixture-key', 'use_for_categorization':True}, 'PUT')
    cat = api('/api/v1/ui/categorization')
    assert cat['key_saved'] and cat['settings']['provider'] == 'api' and cat['settings']['http']['model'] == 'sample-model'
    assert cat['settings']['scenario'] == 'D'
    page.reload()
    expect(page.locator('#d-method-summary')).to_contain_text('Поиск и проверка · sample-model')
    expect(page.locator('#d-method-summary')).to_contain_text(base)
    page.locator('#d-settings summary').click()
    expect(page.locator('#d-scenario')).to_be_hidden()
    page.locator('#d-sessions').click()
    page.route('**/api/v1/ui/worker', lambda route: route.fulfill(json={'enabled': False, 'online': False}))
    page.locator('#d-analyze').click()
    expect(page.locator('#d-launch-status')).to_contain_text('включите службу анализа')
    page.unroute('**/api/v1/ui/worker')
    page.route('**/api/v1/ui/worker', lambda route: route.fulfill(json={'enabled': True, 'online': True}))
    jobs = []
    def queue(route):
        jobs.append(route.request.post_data_json)
        route.fulfill(json={'id': 'session-fixture'})
    page.route('**/api/v1/ui/worker/jobs', queue)
    page.locator('#d-analyze').click()
    expect(page).to_have_url(re.compile(r'#jobs/file/session-fixture$'))
    assert jobs == [{'mode': 'configured', 'source': '@sessions', 'revision': cat['revision']}]
    try:
        api('/api/v1/ui/analysis/documents', {'revision': cat['revision']-1, 'documents': [
            {'name': 'memo.txt', 'text': 'Synthetic stale request'}]})
    except HTTPError as error:
        assert 'configuration_changed' in error.read().decode()
    else:
        raise AssertionError('a stale model binding was accepted')


def seed_path_group(api):
    root = '/home/group.demo/alice.smith/'
    suffixes = ['.local/bin/tool', 'IdeaProjects/bucket-remover/recreate_buckets.sh', 'IdeaProjects/bucket-remover/buckets.txt']
    for i, suffix in enumerate([*suffixes, suffixes[-1]]):
        path = root + suffix
        text = f'📁 Фрагмент {i+1}. Read file {path}, lines 1–32.'
        api('/api/v1/ui/discover', {'remember': True, 'documents': [
            {'name': f'path-{i}.md', 'text': text, 'model_candidates': [{'value': path, 'kind': 'PATH', 'quote': text}]}]})
    return root, suffixes


def check_individual_path(page, api, base):
    review = '/api/v1/ui/review'
    for user, whole in [('single.account', False), ('exact.account', True)]:
        root = f'/home/group.demo/{user}/'
        path = root + '.local/bin/kaponir'
        for i in range(3):
            text = f'PID {1000+i} CMD {path} serve'
            api('/api/v1/ui/discover', {'remember':True, 'documents':[{'name':f'{user}-{i}.log',
                'text':text,'model_candidates':[{'value':path,'kind':'PATH','quote':text}]}]})
        def rows(status):
            return api(review+'?'+urlencode({'q':path,'status':status}))['items']
        item = rows('pending')[0]
        assert item['account_directory'] == root and len(item['occurrence_keys']) == 3
        snapshot = api(review+'/selection', {'q':path,'status':'pending'})
        selected = api(review+'?'+urlencode({'snapshot':snapshot['snapshot'],'status':'selection'}))
        assert selected['items'][0]['account_directory'] == root
        page.goto(base+'/ui/#review')
        page.locator('#review-query').fill(path)
        page.locator('#review-list').get_by_role('button', name=path, exact=True).click()
        card = page.locator('#finding-detail')
        expect(card.locator('.candidate-fragment mark')).to_have_text(root[:-1])
        if whole:
            card.get_by_role('button', name='Исправить').click()
            dialog = page.locator('#review-confirm')
            dialog.get_by_label('Что скрывать').select_option('whole')
            dialog.get_by_role('button', name='Применить решение').click()
        else:
            card.get_by_role('button', name=re.compile('^Скрывать каталог')).click()
        wait_for(lambda: not rows('pending'))
        decided = rows('history')
        assert len(decided) == 3
        assert all(r['document_rule'].get('path_prefix') == (None if whole else root) for r in decided)
        masked = api('/api/v1/ui/mask', {'text':path, 'commit':False})['text']
        assert ('.local/bin/kaponir' in masked) != whole and user not in masked


def check_path_group(page, api, base):
    root, suffixes = seed_path_group(api)
    review = '/api/v1/ui/review'
    def query(status):
        return api(review+'?'+urlencode({'status':status,'q':root,'kind':'PATH'}))
    group = next(row for row in query('pending')['items'] if row.get('path_group'))
    paths = group['path_group']['paths']
    assert sorted(p['value'] for p in paths) == sorted(root+s for s in suffixes), paths
    assert sum(p['count'] for p in paths) == 4 and len(group['occurrence_keys']) == 4
    assert group['path_group']['prefix'] == root
    saved = api(review+'/selection', {'status':'pending','q':root,'kind':'PATH'})
    assert saved['grouped'] and saved['total'] == 1
    frozen = api(review+'?'+urlencode({'snapshot':saved['snapshot'],'status':'selection'}))
    assert frozen['items'][0]['path_group']['prefix'] == root
    draft = api('/api/v1/ui/dataset/collect', {'snapshot':saved['snapshot'],'request_id':'path-prefix-drafts'})
    assert draft['added'] == 1 and draft['new_occurrences'] == 4, draft
    try:
        api(review+'/path-prefix', {'prefix':'/home/','keys':group['occurrence_keys'],'scope':group['scope']})
    except HTTPError as exc:
        assert exc.code == 400
    else:
        raise AssertionError('accepted a generic root')
    page.goto(base+'/ui/#review')
    page.reload()
    page.locator('#review-query').fill(root)
    def open_group():
        page.locator('#review-list').get_by_role('button', name=root, exact=True).click()
    open_group()
    card = page.locator('#finding-detail')
    expect(card.locator('.path-examples li')).to_have_count(3)
    card.get_by_label('Каталог для правила').select_option(root+'IdeaProjects/bucket-remover/')
    expect(card.locator('.path-examples li')).to_have_count(2)
    card.get_by_role('button', name='Разобрать по одному').click()
    expect(page.locator('#review-list')).to_contain_text('buckets.txt')
    expect(page.locator('#review-list')).not_to_contain_text('.local/bin/tool')
    page.goto(base+'/ui/#review')
    page.reload()
    page.locator('#review-query').fill(root)
    open_group()
    page.locator('#review-capture').check()
    card.get_by_role('button', name='Скрывать этот каталог').click()
    wait_for(lambda: not any(row.get('path_group') for row in query('pending')['items']))
    history = [r for r in query('history')['items'] if r.get('document_rule',{}).get('path_prefix') == root]
    assert len(history) == 4, history
    assert len({row['document_rule']['id'] for row in history}) == 1
    for row in history:
        example = api('/api/v1/ui/dataset?'+urlencode({'example':row['dataset_id']}))['items'][0]
        span = example['spans'][0]
        assert example['status'] == 'draft' and example['value'] == root[:-1]
        assert span['action'] == 'mask' and example['text'].encode()[span['start_byte']:span['end_byte']].decode() == root[:-1]
    replay = api(review+'/path-prefix', {'prefix':root,'keys':group['occurrence_keys'],'scope':group['scope']})
    assert replay['already_covered']
    future = root+'notes/tomorrow.txt'
    masked = api('/api/v1/anonymize', {'text':future,'session':'directory-check'})['text']
    assert masked.endswith('/notes/tomorrow.txt') and root[:-1] not in masked, masked
    assert api('/api/v1/deanonymize', {'text':masked,'session':'directory-check'})['text'] == future
    api('/api/v1/rules', {'scope':'team','rule':{'kind':'PROJ','id':'independent-project','value':'PrivateSuffix'}})
    independent = api('/api/v1/anonymize', {'text':root+'notes/PrivateSuffix.txt','session':'independent'})['text']
    assert 'PrivateSuffix' not in independent and '/notes/' in independent, independent
    lookalike = root[:-1]+'-other/notes/tomorrow.txt'
    unrelated = api('/api/v1/anonymize', {'text':lookalike,'session':'lookalike'})['text']
    assert not unrelated.startswith(masked.split('/')[0]+'/'), unrelated
    active = history[0]['document_rule']
    api(f"/api/v1/rules/{active['scope']}/{active['index']}", method='DELETE')
    assert any(row.get('path_group') for row in query('pending')['items'])


def check_directory_boundary_error(page, api, base):
    root = '/work/boundary-case/'
    for filename, lead in [('good.txt', ''), ('nested.txt', '/var/lib')]:
        value = root + filename
        text = f'Read {lead}{value}, then verify.'
        api('/api/v1/ui/discover', {'remember': True, 'documents': [{
            'name': filename, 'text': text,
            'model_candidates': [{'value': value, 'kind': 'PATH', 'quote': text}],
        }]})
    before = api('/api/v1/rules')
    page.goto(base+'/ui/#review')
    page.reload()
    page.locator('#review-query').fill(root)
    page.locator('#review-list').get_by_role('button', name=root, exact=True).click()
    detail = page.locator('#finding-detail')
    result = page.locator('#review-result')
    errors = page.locator('#review-errors')
    viewport = page.viewport_size
    for width in (1440, 390):
        page.set_viewport_size({**viewport, 'width': width})
        document_top = 'element => element.getBoundingClientRect().top + scrollY'
        before_y = detail.evaluate(document_top)
        detail.get_by_role('button', name='Скрывать этот каталог').click()
        expect(result).to_have_text('Ошибок · 1')
        assert abs(detail.evaluate(document_top) - before_y) <= 1
        expect(result.locator('blockquote')).to_have_count(0)
        trigger = page.get_by_role('button', name='Разобрать ошибки', exact=True)
        trigger.click()
        expect(errors).to_be_visible()
        expect(errors).to_contain_text('Правило каталога пропускает этот фрагмент')
        expect(errors.locator('blockquote')).to_be_hidden()
        errors.locator('summary').click()
        expect(errors.locator('blockquote mark')).to_have_text(root+'nested.txt')
        expect(errors.get_by_role('button', name='Изменить правило')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        errors.get_by_role('button', name='Закрыть', exact=True).click()
        expect(trigger).to_be_focused()
    page.set_viewport_size(viewport)
    assert api('/api/v1/rules') == before


def check_directory_rule_conflict(page, api, base):
    account = 'overlap.fixture'
    root = f'/home/group.demo/{account}/'
    for filename in ('notes.txt', 'report.txt'):
        value = root + filename
        text = f'Read {value}, then verify.'
        api('/api/v1/ui/discover', {'remember': True, 'documents': [{
            'name': filename, 'text': text,
            'model_candidates': [{'value': value, 'kind': 'PATH', 'quote': text}],
        }]})
    api('/api/v1/rules', {'scope': 'global', 'rule': {
        'id': 'fixture-account-mask', 'kind': 'USER', 'value': account,
    }})
    before = api('/api/v1/rules')
    page.goto(base+'/ui/#review')
    page.reload()
    page.locator('#review-query').fill(root)
    page.locator('#review-list').get_by_role('button', name=root, exact=True).click()
    page.locator('#finding-detail').get_by_role('button', name='Скрывать этот каталог').click()
    page.get_by_role('button', name='Разобрать ошибки', exact=True).click()
    errors = page.locator('#review-errors')
    expect(errors).to_contain_text('Действующее правило перекрывает маску каталога')
    expect(errors.locator('.rule-conflict')).to_contain_text('Корпоративные')
    expect(errors.locator('.rule-conflict code')).to_have_text(account)
    errors.get_by_role('button', name='Открыть правило', exact=True).click()
    expect(page.locator('#rule-global-fixture-account-mask')).to_be_focused()
    assert api('/api/v1/rules') == before


def check_rule_update(page, api, base):
    value = '/work/rule-edit/notes/report.txt'
    text = f'Read cached{value}, then verify.'
    original = {'id': 'rule-edit-fixture', 'kind': 'PATH', 'value': value,
                'case_sensitive': True, 'note': 'Keep this rule identity'}
    api('/api/v1/ui/discover', {'remember': True, 'documents': [{
        'name': 'rule-edit.txt', 'text': text,
        'model_candidates': [{'value': value, 'kind': 'PATH', 'quote': text}],
    }]})
    api('/api/v1/rules', {'scope': 'team', 'rule': original})

    def saved_rule():
        team = next(s for s in api('/api/v1/rules') if s['scope'] == 'team')
        rule, = [r for r in team['file']['rule'] if r.get('id') == original['id']]
        return rule

    page.goto(base+'/ui/#review')
    page.reload()
    page.locator('#review-query').fill(value)
    row = page.locator('#review-list tr').filter(has=page.get_by_role('button', name=value, exact=True))
    row.get_by_role('button', name=value, exact=True).click()
    expect(page.locator('#finding-detail .rule-conflict code')).to_have_text(value)
    row.get_by_role('checkbox').check()
    page.locator('#review-selection').get_by_role('button', name=re.compile('^Создать правила маскирования')).click()
    page.locator('#review-confirm-apply').click()
    result = page.locator('#review-result')
    expect(result).to_have_text('Ошибок · 1')
    trigger = page.get_by_role('button', name='Разобрать ошибки', exact=True)
    trigger.click()
    errors = page.locator('#review-errors')
    expect(errors.locator('.rule-conflict code')).to_have_text(value)
    expect(errors.locator('blockquote')).to_be_hidden()
    errors.locator('summary').click()
    expect(errors.locator('blockquote mark')).to_have_text(value)
    edit = errors.get_by_role('button', name='Изменить правило', exact=True)
    editor = page.locator('#rule-editor')

    def edit_pattern():
        edit.click()
        expect(editor).to_be_visible()
        expect(editor.locator('#rf-pattern')).to_have_value(value)
        expect(editor.locator('#rf-excerpt mark')).to_have_text(value)
        editor.locator('#rf-ptype').select_option('regex')

    edit_pattern()
    editor.locator('#rf-pattern').fill('(')
    editor.get_by_role('button', name='Сохранить изменения').click()
    expect(editor.locator('#rf-error')).to_contain_text('regex parse error')
    assert saved_rule()['value'] == value

    pattern = f'cached({re.escape(value)})'
    api('/api/v1/rules', {'scope': 'team', 'rule': {'value': 'concurrent-fixture-rule'}})
    editor.locator('#rf-pattern').fill(pattern)
    editor.get_by_role('button', name='Сохранить изменения').click()
    expect(editor.locator('#rf-error')).to_contain_text('Список изменился')
    assert saved_rule()['value'] == value

    editor.get_by_role('button', name='Отмена').click()
    edit_pattern()
    editor.locator('#rf-pattern').fill(pattern)
    editor.get_by_role('button', name='Сохранить изменения').click()
    expect(editor).not_to_be_visible()
    expect(errors).to_be_hidden()
    expect(trigger).to_be_hidden()
    expect(row).to_have_count(0)
    expected = {k: v for k, v in original.items() if k != 'value'}
    assert saved_rule() == {**expected, 'action': 'mask', 'regex': pattern}
    masked = api('/api/v1/ui/mask', {'text': text, 'commit': False})
    assert any(r['original'] == value and r['origin']['id'] == original['id']
               for r in masked['replacements']), masked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    args = parser.parse_args()
    base = f'http://127.0.0.1:{free_port()}'
    env = {k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')}
    def api(path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(base+path, data=data, method=method, headers={'Content-Type':'application/json'}), timeout=20) as res:
            return json.load(res)
    with tempfile.TemporaryDirectory() as home:
        cli = [str(args.binary.resolve()), '--home', home]
        subprocess.run(cli + ['init'], env=env, capture_output=True, check=True)
        proc = subprocess.Popen(cli + ['serve', '--listen', base.split('//')[1]], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(lambda: api('/api/v1/ui/info'))
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
                page = browser.new_page(viewport={'width':1440, 'height':980}, locale='ru-RU')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                exercise(page, api, base)
                check_path_group(page, api, base)
                check_individual_path(page, api, base)
                check_directory_boundary_error(page, api, base)
                check_directory_rule_conflict(page, api, base)
                check_rule_update(page, api, base)
                assert not errors, errors
                browser.close()
        finally:
            proc.terminate()
            proc.wait()
    print('Verified dataset capture and backfill, directory grouping, rule updates, individual review, scoped masking, replay and undo.')


if __name__ == '__main__':
    main()
