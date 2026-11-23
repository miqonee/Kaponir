'use strict';

// This opt-in notebook belongs to one browser profile. Only explicit saves touch IndexedDB.
const datasetModeKey = 'kaponir-dataset-mode';
const datasetDialog = $('#dataset-dialog');
const datasetExamples = {
  USER: 'user.demo', DOM: 'unit.example', IP: '192.0.2.10', EMAIL: 'member@example.org',
  HOST: 'demo-node', AMOUNT: '517 USD', SECRET: 'sample_key_R8v2', PHONE: '+1 202 555 0148',
  PATH: 'sample-folder', PROJ: 'sample-project', MODULE: 'sample_module', SVC: 'sample-service',
  ORG: 'Demo Studio', CLIENT: 'demo-client', PERSON: 'Alex Example', SUB: 'demo', CUSTOM: 'sample-42',
};
function datasetMode() {
  try { return localStorage.getItem(datasetModeKey); } catch { return null; }
}
function updateDatasetMode() {
  const mode = datasetMode();
  const enabled = mode === 'on';
  $('#dataset-enable').checked = enabled;
  $('#dataset-create').hidden = !enabled;
  $('#dataset-files').hidden = !mode;
  if (mode) refreshDatasetCount();
  if (!enabled && datasetDialog.open) datasetDialog.close();
  if (typeof ReviewUI !== 'undefined' && ReviewUI.data) renderReview(true);
}
async function refreshDatasetCount() {
  if (datasetMode() === null) return;
  try {
    const count = await datasetOperation('readonly', (store, done) => {
      const req = store.count();
      req.onsuccess = () => done(req.result);
    });
    $('#dataset-count').textContent = tr('Сохранено примеров: {{0}}', [count]);
  } catch (err) { $('#dataset-count').textContent = err.message; }
}
function openDatasetDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('kaponir-reviewed-examples', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('examples', {keyPath: 'text'});
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function datasetOperation(mode, operation) {
  const db = await openDatasetDB();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('examples', mode);
      let result, problem;
      tx.oncomplete = () => resolve(result);
      tx.onabort = () => reject(problem || tx.error || new Error(tr('Запись не сохранена')));
      try {
        operation(tx.objectStore('examples'), value => { result = value; }, message => {
          problem = new Error(tr(message)); tx.abort();
        });
      } catch (error) { problem = error; tx.abort(); }
    });
  } finally { db.close(); }
}
async function storeDatasetExample(text, spans) {
  await datasetOperation('readwrite', (store, _done, stop) => {
    const req = store.get(text);
    req.onsuccess = () => {
      if (datasetMode() !== 'on') return stop('Сбор выключен');
      const row = req.result || {
        id: 'feedback/' + Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join(''),
        group: 'field-feedback', source: 'synthetic-curated',
        language: /[а-яё]/i.test(text) ? 'ru' : 'en', format: 'text', text, spans: [],
      };
      try { if (typeof JSON.parse(text) === 'object') row.format = 'json'; } catch { /* plain text */ }
      for (const span of spans) {
        row.spans = row.spans.filter(s => s.start_byte !== span.start_byte || s.end_byte !== span.end_byte);
        if (row.spans.some(s => s.start_byte < span.end_byte && span.start_byte < s.end_byte))
          return stop('Выделения пересекаются');
        row.spans.push(span);
      }
      row.spans.sort((a, b) => a.start_byte - b.start_byte);
      store.put(row);
    };
  });
}
function openDatasetExample(item) {
  if (datasetMode() !== 'on') return;
  const kind = Object.hasOwn(datasetExamples, item?.kind) ? item.kind : 'CUSTOM';
  const example = datasetExamples[kind];
  const sample = item?.context?.includes(example) ? example + '-demo' : example;
  const actionLabels = {mask:tr('Скрыть'), keep:tr('Оставить открытым'), review:tr('Требует проверки')};
  const text = $('#dataset-text'), kindField = $('#dataset-kind'), checked = $('#dataset-safe');
  const spans = [];
  const suggestions = $('#dataset-proposals'), suggest = $('#dataset-suggest');
  suggestions.replaceChildren();
  let pending = false;
  // Never copy a quote into the editor unless we can replace the selected source value.
  text.value = item?.value && item.context?.includes(item.value)
    ? item.context.split(item.value).join(sample) : '';
  kindField.replaceChildren(...Object.keys(datasetExamples).map(k => h('option', {value:k}, kindLabel(k))));
  kindField.value = kind;
  const encoder = new TextEncoder();
  function render() {
    text.readOnly = spans.length > 0;
    $('#dataset-marks').replaceChildren(...spans.map((s, i) => h('li', {},
      h('button', {class:'ghost', onclick:() => {
        const toIndex = byteMap(text.value);
        text.focus();
        text.setSelectionRange(toIndex(s.start_byte), toIndex(s.end_byte));
      }}, s.value + ' · ' + s.kind + ' · ' + actionLabels[s.action]),
      h('button', {class:'ghost', onclick:() => { spans.splice(i, 1); render(); }}, tr('Убрать отметку')))));
    $('#dataset-save').disabled = !checked.checked || !spans.length;
    suggest.disabled = pending || !checked.checked || !text.value.trim();

  }
  checked.checked = false;
  checked.onchange = render;
  text.oninput = () => { checked.checked = false; suggestions.replaceChildren(); render(); };
  const status = $('#dataset-error');
  status.textContent = '';
  for (const button of datasetDialog.querySelectorAll('[data-mark]')) button.onclick = () => {
    const start = text.selectionStart, end = text.selectionEnd;
    const value = text.value.slice(start, end);
    if (!value.trim()) { status.textContent = tr('Сначала выделите значение в тексте'); return; }
    const span = {value, kind:kindField.value, action:button.dataset.mark,
      start_byte:encoder.encode(text.value.slice(0, start)).length, end_byte:encoder.encode(text.value.slice(0, end)).length};
    const existing = spans.find(s => s.start_byte === span.start_byte && s.end_byte === span.end_byte);
    if (existing) Object.assign(existing, span);
    else if (spans.some(s => s.start_byte < span.end_byte && span.start_byte < s.end_byte)) {
      status.textContent = tr('Выделения пересекаются'); return;
    } else spans.push(span);
    status.textContent = ''; render();
  };
  suggest.onclick = async function askModel() {
    if (!checked.checked || pending || datasetMode() !== 'on') return;
    const excerpt = text.value;
    pending = true;
    suggestions.replaceChildren();
    status.textContent = tr('Qwen проверяет фрагмент…');
    render();
    try {
      const result = await api('/api/v1/ui/worker/privacy-suggest', {
        method:'POST', body:{text:excerpt}, timeoutMs:300000,
      });
      if (suggest.onclick !== askModel || text.value !== excerpt || !checked.checked) return;
      const toIndex = byteMap(excerpt);
      for (const proposal of result.spans) {
        const button = h('button', {class:'secondary', onclick:() => {
          text.focus();
          text.setSelectionRange(toIndex(proposal.start_byte), toIndex(proposal.end_byte));
          kindField.value = proposal.kind;
          // Let the reviewer choose any of the three actions with this occurrence selected.
        }}, tr('Вхождение {{0}}', [proposal.occurrence]) + ' · ' + proposal.value + ' · ' + actionLabels[proposal.action]);
        suggestions.append(button);
      }
      status.textContent = tr(result.spans.length ? 'Выберите подсказку, затем подтвердите действие выше' : 'Qwen не отметила значений');
    } catch (err) {
      if (suggest.onclick === askModel) status.textContent = tr(err.messageKey || err.message);
    } finally {
      pending = false;
      if (suggest.onclick === askModel) render();
    }
  };
  $('#dataset-save').onclick = async () => {
    if (!checked.checked || !spans.length || datasetMode() !== 'on') return;
    $('#dataset-save').disabled = true;
    try {
      await storeDatasetExample(text.value, [...spans]);
      datasetDialog.close(); toast(tr('Пример сохранён')); refreshDatasetCount();
    } catch (err) { status.textContent = err.message; render(); }
  };
  render();
  datasetDialog.showModal();
  text.focus();
  const first = text.value.indexOf(sample);
  if (first >= 0 && first === text.value.lastIndexOf(sample)) text.setSelectionRange(first, first + sample.length);
}
$('#dataset-enable').onchange = e => {
  try { localStorage.setItem(datasetModeKey, e.target.checked ? 'on' : 'off'); }
  catch (err) { fail(err); }
  updateDatasetMode();
};
$('#dataset-create').onclick = () => openDatasetExample();
$('#dataset-close').onclick = () => datasetDialog.close();
datasetDialog.onclose = () => {
  $('#dataset-text').value = '';
  $('#dataset-marks').replaceChildren();
  $('#dataset-save').onclick = $('#dataset-suggest').onclick = null;
  $('#dataset-proposals').replaceChildren();
  for (const button of datasetDialog.querySelectorAll('[data-mark]')) button.onclick = null;
  $('#dataset-safe').onchange = $('#dataset-text').oninput = null;
};
$('#dataset-export').onclick = async () => {
  try {
    const rows = await datasetOperation('readonly', (store, done) => {
      const req = store.getAll(); req.onsuccess = () => done(req.result);
    });
    if (!rows.length) { toast(tr('Подборка пуста')); return; }
    const blob = new Blob(rows.map(row => JSON.stringify(row) + '\n'), {type:'application/x-ndjson'});
    const url = URL.createObjectURL(blob);
    h('a', {href:url, download:'kaponir-feedback.jsonl'}).click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) { fail(err); }
};
TABS.dataset = refreshDatasetCount;
window.addEventListener('storage', e => { if (!e.key || e.key === datasetModeKey) updateDatasetMode(); });
updateDatasetMode();
