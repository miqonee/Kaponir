"""Browser checks for the panel shell, session hierarchy and attention filters."""
from copy import deepcopy
import time

from playwright.sync_api import expect
from validate_console import choose_preference


def use_sample_traffic(page):
    """Provide repeatable request metadata. No conversation text is needed for the list."""
    def serve(route):
        response = route.fetch()
        data = response.json()
        now = int(time.time() * 1000)
        example = next(iter(data['traffic']['events']), {})
        rows = []
        for index in range(23):
            row = deepcopy(example)
            row.update(id=index + 1, session_id='8b31a6f0902c' if index < 22 else 'fe25abc941',
                       session_source='inferred', started_at_ms=now - index * 40000 - 1000,
                       time_ms=now - index * 40000, model='demo-chat', provider='openai',
                       outcome='ok', status=200, review_reasons=[], usage={'replacements': 3})
            if index in (0, 1):
                row.update(outcome='error', status=502)
            elif index in (2, 3):
                row.update(outcome='cancelled', status=0)
            elif index in (4, 5):
                row['review_reasons'] = [{'code':'detector_uncertain', 'count':4, 'state':'pending'}]
            elif index == 6:
                row['review_reasons'] = [{'code':'detector_uncertain', 'count':2, 'state':'resolved'}]
            rows.append(row)
        data['traffic'].update(active=[], events=rows)
        route.fulfill(response=response, json=data)
    page.route('**/api/v1/metrics', serve)
    return serve


def check_navigation(page, output):
    page.set_viewport_size({'width':1440, 'height':900})
    choose_preference(page, 'language', 'ru')
    choose_preference(page, 'theme', 'light')
    sidebar = page.locator('#sidebar')
    expect(sidebar.locator('[data-tab=jobs]')).to_have_count(0)
    sidebar.get_by_role('link', name='Находки').click()
    page.locator('#review-query').fill('return-from-lab')
    page.locator('#review-navigation [data-go=discover]').click()
    lab = page.locator('#lab-navigation')
    expect(sidebar.get_by_role('link',name='Лаборатория')).to_have_attribute('aria-current','page')
    expect(lab.locator('[data-go=discover]')).to_have_attribute('aria-current','page')
    lab.get_by_role('link',name='Проверка текста').click()
    expect(page.locator('#m-input')).to_be_visible()
    page.go_back()
    expect(page.locator('#d-text')).to_be_visible()
    lab.get_by_role('link',name='К находкам').click()
    expect(page.locator('#review-query')).to_have_value('return-from-lab')
    page.locator('#review-query').fill('')
    sidebar.get_by_role('link', name='Настройки LLM').click()
    expect(page.locator('#workspace-model-choice')).to_be_visible()
    expect(page.locator('#proxy-navigation')).to_be_hidden()
    for dest in ['analysis', 'categorization', 'worker', 'settings']:
        link = page.locator(f'#analysis-navigation [data-go={dest}]')
        link.click()
        expect(link).to_have_attribute('aria-current','page')
        expect(sidebar.locator('[data-tab=settings]')).to_have_attribute('aria-current','page')
    sidebar.get_by_role('link', name='Настройки прокси').click()
    connection = page.locator('#tab-connect .provider-events')
    connection.click()
    expect(page.locator('#tab-events')).to_be_visible()
    assert page.locator('#events-search').input_value()
    expect(page.locator('#events-table .session-row').first).to_be_visible()
    page.locator('#events-search').fill('no-such-connection')
    page.locator('#events-table').get_by_role('button',name='Очистить поиск').click()
    expect(page.locator('#events-search')).to_be_focused()
    sidebar.get_by_role('link', name='Настройки прокси').click()
    expect(page.locator('#analysis-navigation')).to_be_hidden()
    page.locator('#proxy-navigation [data-go=proxy]').click()
    expect(page.locator('#proxy-access')).to_be_visible()
    page.locator('#proxy-navigation [data-go=metrics]').click()
    page.go_back()
    expect(page.locator('#proxy-access')).to_be_visible()
    page.reload()
    expect(page.locator('#proxy-access')).to_be_visible()
    page.locator('#job-indicator').click()
    expect(page.locator('#job-preview')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('#job-preview')).to_be_hidden()
    expect(page.locator('#job-indicator')).to_be_focused()
    page.locator('#job-indicator').click()
    page.locator('#job-preview').get_by_role('link', name='Все задания').click()
    expect(page.locator('#tab-jobs')).to_be_visible()
    jobs_api = '**/api/v1/ui/jobs?*'
    page.route(jobs_api, lambda route: route.fulfill(status=503, json={}))
    try:
        page.evaluate('refreshJobs()')
        expect(page.locator('#job-indicator')).to_contain_text('Статус недоступен')
        page.locator('#job-indicator').click()
        expect(page.locator('#job-preview-list')).to_contain_text('Статус недоступен')
        retry = page.locator('#job-preview-list').get_by_role('button', name='Повторить')
        retry.focus()
        page.evaluate('refreshJobs()')
        expect(retry).to_be_focused()
    finally:
        page.unroute(jobs_api)
    page.locator('#job-preview-list').get_by_role('button', name='Повторить').click()
    expect(page.locator('#job-indicator')).not_to_contain_text('Статус недоступен')
    page.keyboard.press('Escape')

    sidebar.locator('[data-tab=rules]').click()
    page.locator('[data-rules-view=list]').click()
    query = page.locator('#rules-query')
    query.fill('kaponir discover')
    rows = page.locator('#rules-list [data-rule-search]:visible')
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text('questions')
    original_id = rows.get_attribute('id')
    page.locator('#rules-scope').select_option('personal')
    query.fill('questions')
    expect(rows).to_have_attribute('id', original_id)
    query.fill('unmatched-example')
    expect(page.locator('#rules-match-count')).to_contain_text('0')
    expect(rows).to_have_count(0)
    page.locator('#rules-clear').click()
    expect(query).to_be_focused()
    expect(query).to_have_value('')
    expect(page.locator('#rules-scope')).to_have_value('')

    serve = use_sample_traffic(page)
    try:
        sidebar.locator('[data-tab=events]').click()
        root = page.locator('#events-table')
        issue = root.locator('[data-session-issues="8b31a6f0902c"]')
        expect(issue).to_have_text('Требуют внимания · 6 из 22')
        expect(root.locator('.session-row')).to_have_count(2)
        by_session = root.locator('.session-row').first
        assert by_session.locator('.session-open').bounding_box()['y'] < by_session.bounding_box()['y'] + 18
        page.screenshot(path=str(output / 'sessions-summary.png'))
        issue.click()
        expect(root.locator('.session-children')).to_have_count(6)
        expect(root.locator('.session-children').filter(has_text='HTTP 502')).to_have_count(2)
        expect(root.locator('.session-children').filter(has_text='Прерван до завершения')).to_have_count(2)
        expect(root.locator('.session-children').filter(has_text='Спорное решение детектора')).to_have_count(2)
        page.locator('#events-view [data-view=requests]').click()
        expect(root.locator('.session-children')).to_have_count(23)
        page.locator('#events-filter [data-filter=issues]').click()
        expect(root.locator('.session-children')).to_have_count(6)
        expect(root.locator('.session-row')).to_have_count(1)
        page.screenshot(path=str(output / 'requests-attention.png'))
        group_toggle=root.locator('.session-toggle').first
        group_toggle.focus()
        page.keyboard.press('ArrowLeft')
        expect(root.locator('.session-children')).to_have_count(0)
        page.keyboard.press('ArrowRight')
        expect(root.locator('.session-children')).to_have_count(6)
        page.locator('#events-search').fill('no-such-session')
        expect(root).to_contain_text('Запросов, требующих внимания, нет')
        page.locator('#events-search').fill('')
        page.locator('#events-filter [data-filter=all]').click()
        page.locator('#events-view [data-view=sessions]').click()
    finally:
        page.unroute('**/api/v1/metrics', serve)

    routes = ['overview','mask','restore','events','review','discover','dataset','rules','setup',
              'jobs','settings','analysis','categorization','worker','connect','proxy','metrics']
    for width in [390,1440]:
        page.set_viewport_size({'width':width,'height':900})
        for route in routes:
            page.evaluate('(route) => showTab(route)',route)
            expect(page.locator('#tab-'+route)).to_be_visible()
            assert page.locator('main > section.tab:visible').count() == 1, route
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (route,width)
    page.set_viewport_size({'width':390,'height':844})
    expect(page.locator('#breadcrumb')).to_be_visible()
    assert page.locator('.topbar').bounding_box()['height'] <= 62
    page.locator('#job-indicator').click()
    preview = page.locator('#job-preview')
    expect(preview).to_be_visible()
    box = preview.bounding_box()
    assert box['x'] >= 0 and box['x'] + box['width'] <= 390
    page.keyboard.press('Escape')
    page.locator('#menu-toggle').click()
    sidebar.get_by_role('link', name='Датасет').click()
    expect(page.locator('#tab-dataset')).to_be_visible()
    expect(sidebar).to_have_js_property('inert', True)
    page.locator('#menu-toggle').click()
    page.keyboard.press('Escape')
    expect(page.locator('#menu-toggle')).to_be_focused()
    checked = page.locator('.dataset-filter button').nth(1)
    checked.click()
    expect(checked).to_have_attribute('aria-pressed', 'true')
    expect(checked).to_be_focused()
