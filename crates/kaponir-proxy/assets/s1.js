'use strict';

const S1 = { data: null, busy: false, dirty: false };
const s1Levels = ['l0', 'l1', 'l2'];
const selectedS1Level = () => {
  const el = $('#s1-disclosure');
  if (!el) return 'l2';
  return s1Levels[Number(el.value)] || 'l2';
};
const s1Modes = { off: 'Детекторы и правила', shadow: 'Теневой режим', active: 'Активный режим' };

function syncS1Visibility() {
  for(const input of document.querySelectorAll('#s1-form input,#s1-form select'))input.disabled=S1.busy||!S1.data?.editable;
  $('#s1-clear-key').disabled=S1.busy||!S1.data?.editable||!S1.data?.key_saved;
  $('#s1-save').disabled=S1.busy||!S1.data?.editable;

  // LLM Engine Section
  const llmEngine = $('#analysis-llm-engine')?.value || 'none';
  if ($('#analysis-llm-none-note')) $('#analysis-llm-none-note').hidden = llmEngine !== 'none';
  if ($('#analysis-llm-local-section')) $('#analysis-llm-local-section').hidden = llmEngine !== 'local';
  if ($('#analysis-llm-external-section')) $('#analysis-llm-external-section').hidden = llmEngine !== 'external';
  if (llmEngine === 'local') {
    const isOnline = Boolean(WORKER.data?.online);
    if ($('#analysis-worker-offline-alert')) $('#analysis-worker-offline-alert').hidden = isOnline;
    if ($('#analysis-worker-online-msg')) $('#analysis-worker-online-msg').hidden = !isOnline;
  }

  // S1 / Choice Section
  const provider = $('#s1-provider').value;
  const external = provider === 'http';
  const enabled = provider !== 'none';
  $('#s1-http-fields').hidden = !external;
  $('#s1-model-fields').hidden = !enabled;
  if ($('#s1-test')) $('#s1-test').hidden = !external;
  $('#s1-url').required = external;
  const proto = $('#s1-protocol').value;
  const needsModel = proto === 'systemone' || proto === 'openai';
  $('#s1-model-field').hidden = !needsModel;
  $('#s1-model').required = external && needsModel;
  $('#s1-model').placeholder = proto === 'openai' ? 'deepseek-chat / qwen2.5-7b-instruct' : 'provider/model';
  $('#s1-url').placeholder = proto === 'openai' ? 'https://api.deepseek.com/v1/chat/completions' : 'https://s1.example/score';
  if ($('#s1-shadow-note')) $('#s1-shadow-note').hidden = !external || $('#s1-mode').value !== 'shadow';
  if ($('#s1-local-note')) $('#s1-local-note').hidden = provider !== 'ngram';
}

function renderS1(preserveDraft=false) {
  const draft=preserveDraft?[...document.querySelectorAll('#s1-form input,#s1-form select')].map(el=>[el,el.value,el.checked]):null;
  const data = S1.data;
  if (!data) return;
  const s = data.settings;
  $('#s1-provider').value = s.provider;
  const local = $('#s1-provider option[value="ngram"]');
  local.disabled = !data.local_model_available;
  local.textContent = tr(data.local_model_available ? 'Локальная оценка n-gram' : 'Локальная модель — нужно обучение');
  $('#s1-local-note').textContent = tr('Модель обучается локально командой `kaponir s1 train`. Данные наружу не отправляются.');
  $('#s1-protocol').value = s.http.protocol;
  $('#s1-timeout').value = s.http.timeout_ms;
  $('#s1-url').value = s.http.url || '';
  $('#s1-model').value = s.http.model || '';
  $('#s1-key').value = '';
  $('#s1-key').placeholder = tr(data.key_saved ? 'Сохранённый ключ останется без изменений' : 'Введите ключ, если он требуется');
  $('#s1-key-note').textContent = tr(data.key_saved
    ? 'Ключ сохранён в зашифрованном vault. Он не возвращается в браузер и не записывается в config.toml. Пустое поле сохранит его.'
    : 'Ключ хранится в зашифрованном vault, не возвращается в браузер и не записывается в config.toml.');
  $('#s1-clear-key').checked = false;
  $('#s1-clear-key').disabled = !data.key_saved;
  $('#s1-mode').value = s.shadow ? 'shadow' : 'active';
  $('#s1-disclosure').value = Math.max(0, s1Levels.indexOf(s.http.disclosure));
  $('#s1-weight').value = s.weight;
  $('#s1-keep').value = s.keep_below;
  $('#s1-context').value = s.context_bytes;
  $('#s1-status').textContent = tr(s1Modes[data.mode] || s1Modes.off);
  $('#s1-status').dataset.mode = data.mode;
  $('#s1-save').disabled = S1.busy || !data.editable;
  $('#s1-save').textContent = tr(S1.busy ? 'Применяем…' : 'Сохранить и применить');
  if (!data.editable) $('#s1-result').textContent = tr('Этот сервер работает в режиме только для чтения.');
  if(draft)for(const [el,value,checked] of draft){el.value=value;el.checked=checked;}
  syncS1Visibility();
}

async function loadS1() {
  S1.data = await api('/api/v1/ui/s1');
  renderS1(S1.dirty);
}

for (const id of ['s1-provider', 's1-protocol', 's1-mode', 'analysis-llm-engine']) {
  $('#' + id)?.addEventListener('change', syncS1Visibility);
}
$('#analysis-worker-inline-start')?.addEventListener('click', async () => {
  const btn = $('#analysis-worker-inline-start');
  btn.disabled = true;
  btn.textContent = tr('Запускается…');
  try {
    await api('/api/v1/ui/worker/start', { method: 'POST', body: {} });
    await loadWorkerSummary();
    syncS1Visibility();
  } catch (err) {
    fail(err);
  } finally {
    btn.disabled = false;
    btn.textContent = tr('Включить воркер');
  }
});
$('#s1-clear-key')?.addEventListener('change', event => {
  if (event.target.checked) $('#s1-key').value = '';
});
$('#s1-key')?.addEventListener('input', () => {
  if ($('#s1-key').value) $('#s1-clear-key').checked = false;
});
for (const button of document.querySelectorAll('#tab-s1 [data-go]')) {
  button.addEventListener('click', () => showTab(button.dataset.go));
}

for(const event of ['input','change'])$('#s1-form').addEventListener(event,()=>{S1.dirty=true;$('#s1-result').textContent=tr('Есть несохранённые изменения');});
$('#s1-form').addEventListener('click',e=>{if(e.target.closest('[data-s1-level]'))S1.dirty=true;});

$('#s1-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (S1.busy || !S1.data) return;
  S1.busy = true;
  syncS1Visibility();
  $('#s1-result').textContent = '';
  $('#s1-result').classList.remove('err');
  $('#s1-save').disabled = true;
  $('#s1-save').textContent = tr('Применяем…');
  const current = S1.data.settings;
  const settings = {
    ...current,
    provider: $('#s1-provider').value,
    shadow: $('#s1-mode').value === 'shadow',
    weight: Number($('#s1-weight').value),
    keep_below: Number($('#s1-keep').value),
    context_bytes: Number($('#s1-context').value),
    http: {
      ...current.http,
      url: $('#s1-url').value.trim(),
      protocol: $('#s1-protocol').value,
      model: $('#s1-model').value.trim() || null,
      timeout_ms: Number($('#s1-timeout').value),
      disclosure: selectedS1Level(),
    },
  };
  const key = $('#s1-key').value.trim();
  try {
    S1.data = await api('/api/v1/ui/s1', {
      method: 'PUT',
      body: { settings, ...(key ? { key } : {}), clear_key: $('#s1-clear-key').checked },
    });
    await loadInfo();
    S1.dirty=false;
    renderS1();
    $('#s1-result').textContent = tr('Настройки сохранены и применены. Доступность внешнего S1 будет видна при реальных запросах.');
    toast(tr('Настройки S1 применены'));
  } catch (error) {
    $('#s1-result').textContent = error.message;
    $('#s1-result').classList.add('err');
    fail(error);
  } finally {
    S1.busy = false;
    renderS1(true);
  }
});

Object.assign(TABS, { analysis: loadAnalysis });
