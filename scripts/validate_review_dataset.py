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
    page.reload()
    expect(page.locator('#d-method-summary')).to_contain_text('Поиск и проверка · sample-model')
    expect(page.locator('#d-method-summary')).to_contain_text(base)
    page.locator('#d-settings summary').click()
    page.locator('#d-scenario').select_option('D')
    expect(page.locator('#d-method-summary')).to_contain_text(' · D')
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
    assert jobs == [{'mode': 'configured', 'source': '@sessions', 'revision': cat['revision'], 'scenario': 'D'}]
    try:
        api('/api/v1/ui/analysis/documents', {'revision': cat['revision']-1, 'documents': [
            {'name': 'memo.txt', 'text': 'Synthetic stale request'}]})
    except HTTPError as error:
        assert 'configuration_changed' in error.read().decode()
    else:
        raise AssertionError('a stale model binding was accepted')


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
                assert not errors, errors
                browser.close()
        finally:
            proc.terminate()
            proc.wait()
    print('Verified opt-in, grounded decisions, deferral, untouched repeats, historical collection and shared search settings.')


if __name__ == '__main__':
    main()
