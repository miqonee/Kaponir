"""Exercise the optional review notebook in an existing, isolated console session."""

import json
from pathlib import Path


def check_dataset_ui(page):
    dataset_nav = page.get_by_role('navigation').get_by_role('button', name='Датасет', exact=True)
    dataset_nav.click()
    toggle = page.get_by_role('checkbox', name='Собирать примеры', exact=True)
    assert page.url.endswith('#dataset')
    assert page.get_by_role('heading', name='Датасет', exact=True).is_visible()
    assert not page.locator('#dataset-create').is_visible()
    assert not toggle.is_checked()
    toggle.check()
    page.locator('[data-tab="review"]').click()
    page.locator('#review-list .review-card').first.get_by_role('button', name='В датасет').click()
    assert page.locator('#dataset-dialog').is_visible()
    page.locator('#dataset-close').click()

    text = '🧾 Учебная цена 517 USD. Закрытая цена 517 USD.'
    value = '517 USD'
    dataset_nav.click()
    page.locator('#dataset-create').click()
    editor = page.locator('#dataset-text')
    editor.fill(text)
    page.locator('#dataset-kind').select_option('AMOUNT')
    for occurrence, action in enumerate(('keep', 'mask')):
        editor.evaluate('''(el, [v, n]) => {
            const i = n ? el.value.lastIndexOf(v) : el.value.indexOf(v);
            el.focus(); el.setSelectionRange(i, i + v.length);
        }''', [value, occurrence])
        page.locator(f'[data-mark="{action}"]').click()
    assert page.locator('#dataset-save').is_disabled()
    page.locator('#dataset-safe').check()
    page.locator('#dataset-save').click()
    page.locator('#dataset-dialog').wait_for(state='hidden')
    page.reload()
    assert page.url.endswith('#dataset')
    page.locator('#dataset-count').filter(has_text='Сохранено примеров: 1').wait_for()
    assert toggle.is_checked()
    page.locator('[data-tab="settings"]').click()
    page.locator('#tab-settings').get_by_role('button', name='Датасет', exact=True).click()
    assert page.url.endswith('#dataset')
    toggle.uncheck()
    assert not page.locator('#dataset-create').is_visible()
    page.locator('[data-tab="review"]').click()
    assert not page.locator('#dataset-new').is_visible()
    assert not page.locator('#review-list').get_by_role('button',name='В датасет').count()
    dataset_nav.click()
    assert page.evaluate('''async () => {
        try { await storeDatasetExample("disabled", []); } catch { return true; }
        return false;
    }''')
    with page.expect_download() as event:
        page.locator('#dataset-export').click()
    rows = Path(event.value.path()).read_text(encoding='utf-8').splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row['text'] == text
    spans = row['spans']
    assert [s['start_byte'] for s in spans] == [text.encode().find(value.encode()), text.encode().rfind(value.encode())]
    assert [s['action'] for s in spans] == ['keep', 'mask']
    assert all(text.encode()[s['start_byte']:s['end_byte']].decode() == value for s in spans)
