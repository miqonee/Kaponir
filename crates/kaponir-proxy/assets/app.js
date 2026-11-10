'use strict';
// Web UI kaponir. Без фреймворков и сборки: файл вшит в бинарник. Данные пользователя попадают
// в DOM только через textContent — никакого innerHTML со значениями.

const $ = (sel, el = document) => el.querySelector(sel);

// Ручной режим перекрывает системный; auto продолжает следовать настройке ОС.
const themeMedia = matchMedia('(prefers-color-scheme: dark)');
let themeMode = ['auto', 'light', 'dark'].includes(document.documentElement.dataset.themeMode) ? document.documentElement.dataset.themeMode : 'auto';
function applyTheme(mode) {
  themeMode = mode;
  const theme = mode === 'auto' ? (themeMedia.matches ? 'dark' : 'light') : mode;
  document.documentElement.dataset.themeMode = mode;
  document.documentElement.dataset.theme = theme;
  for (const input of document.querySelectorAll('input[name="interface-theme"]')) input.checked = input.value === mode;
  window.dispatchEvent(new CustomEvent('kaponir-themechange', { detail: { mode, theme } }));
}
applyTheme(themeMode);
for (const input of document.querySelectorAll('input[name="interface-theme"]')) {
  input.addEventListener('change', () => {
    if (!input.checked) return;
    applyTheme(input.value);
    try { localStorage.setItem('kaponir-theme', input.value); } catch {}
  });
}
themeMedia.addEventListener('change', () => { if (themeMode === 'auto') applyTheme('auto'); });
window.addEventListener('storage', event => {
  if (event.key === 'kaponir-theme' && ['auto', 'light', 'dark'].includes(event.newValue)) applyTheme(event.newValue);
});

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style') {
      for (const declaration of String(v).split(';')) {
        const colon = declaration.indexOf(':');
        if (colon > 0) el.style.setProperty(declaration.slice(0, colon).trim(), declaration.slice(colon + 1).trim());
      }
    }
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

const SCOPE_NAMES = { global: tr("корпоративный"), team: tr("команда"), personal: tr("личный") };
const SCOPE_TITLES = { global: tr("Корпоративные правила"), team: tr("Правила команды"), personal: tr("Личные правила") };
const VERDICTS = { mask: tr("маскировать"), keep: tr("оставить"), uncertain: tr("спорно") };
const MIN_VALUE = 3; // как MIN_KNOWN_LEN в ядре

const KIND_LABELS = {
  PROJ: "Проект",
  SVC: "Сервис",
  HOST: "Сервер / Хост",
  SECRET: "Секрет / Пароль",
  DOM: "Домен",
  SUB: "Поддомен",
  CLIENT: "Клиент",
  PERSON: "Сотрудник / ФИО",
  ORG: "Организация",
  USER: "Учётная запись",
  MODULE: "Модуль",
  EMAIL: "Email",
  PHONE: "Телефон",
  IP: "IP-адрес",
  INN: "ИНН",
  SNILS: "СНИЛС",
  OGRN: "ОГРН",
  BIK: "БИК",
  ACCT: "Банковский счёт",
  PASS: "Паспорт",
  CUSTOM: "Своё правило",
};

const KIND_TOOLTIPS = {
  PROJ: "Внутреннее кодовое название проекта или продукта (PROJ)",
  SVC: "Внутренний микросервис, подсистема или API-компонент (SVC)",
  HOST: "Имя сервера, узла инфраструктуры или кластера (HOST)",
  SECRET: "Пароль, API-ключ, токен доступа или секретная строка (SECRET)",
  DOM: "Корпоративный домен или внутренний адрес компании (DOM)",
  SUB: "Поддомен внутри корпоративной доменной зоны (SUB)",
  CLIENT: "Контрагент, заказчик или партнёр (CLIENT)",
  PERSON: "Фамилия, имя или отчество физического лица (PERSON)",
  ORG: "Название компании или юридического лица (ORG)",
  USER: "Логин пользователя или системная учётная запись (USER)",
  MODULE: "Внутренний пакет, библиотека или репозиторий (MODULE)",
  CUSTOM: "Пользовательская категория чувствительных данных (CUSTOM)",
};

const STANDARD_KINDS = ['PROJ', 'SVC', 'HOST', 'SECRET', 'DOM', 'CLIENT', 'PERSON', 'ORG', 'USER', 'MODULE', 'CUSTOM'];

function kindLabel(kind) {
  const k = String(kind || 'CUSTOM').toUpperCase();
  const human = KIND_LABELS[k];
  return human ? `${tr(human)} (${k})` : k;
}

function kindBadge(kind) {
  const k = String(kind || 'CUSTOM').toUpperCase();
  const human = KIND_LABELS[k];
  const tip = KIND_TOOLTIPS[k] ? tr(KIND_TOOLTIPS[k]) : k;
  return h('span', { class: 'kind-badge', title: tip },
    human ? tr(human) : k,
    human ? h('code', { text: k }) : null
  );
}

function defaultScopeForKind(kind) {
  const k = String(kind || '').toUpperCase();
  if (['SECRET', 'DOM', 'SUB', 'ORG', 'INN', 'OGRN', 'BIK', 'ACCT'].includes(k)) return 'global';
  if (['PROJ', 'SVC', 'HOST', 'MODULE', 'CLIENT'].includes(k)) return 'team';
  if (['PERSON', 'USER', 'EMAIL', 'PHONE', 'SNILS', 'PASS'].includes(k)) return 'personal';
  return ($('#j-scope') && $('#j-scope').value) || 'team';
}

window.kindLabel = kindLabel;
window.kindBadge = kindBadge;
window.defaultScopeForKind = defaultScopeForKind;

// ---------- API ----------

let token = sessionStorage.getItem('kaponir-token') || '';
let authWaiters = null;
let authCancelled = false;

function askToken() {
  if (authCancelled) return Promise.reject(new Error(tr("Токен доступа не введён")));
  if (!authWaiters) {
    authWaiters = [];
    const dlg = $('#auth');
    dlg.showModal();
    $('#auth-token').focus();
  }
  return new Promise((resolve, reject) => authWaiters.push({resolve, reject}));
}

$('#auth-form').addEventListener('submit', () => {
  token = $('#auth-token').value.trim();
  sessionStorage.setItem('kaponir-token', token);
  const w = authWaiters || [];
  authWaiters = null;
  w.forEach((r) => r.resolve());
});

$('#auth').addEventListener('cancel', () => {
  authCancelled = true;
  const waiters = authWaiters || []; authWaiters = null;
  waiters.forEach(w => w.reject(new Error(tr('Токен доступа не введён'))));
});
$('#access-settings').addEventListener('click', async () => {
  authCancelled = false;
  try { await askToken(); await loadInfo(); showTab(location.hash.slice(1) || 'overview'); }
  catch (error) { fail(error); }
});

async function api(path, { method = 'GET', body } = {}) {
  const headers = {};
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (token) headers.authorization = 'Bearer ' + token;
  const r = await fetch(path, { method, headers, signal: AbortSignal.timeout(45000), body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401) {
    await askToken();
    return api(path, { method, body });
  }
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    const messageKey=(data && data.error && data.error.message) || `HTTP ${r.status}`;
    const error=new Error(tr(messageKey));error.messageKey=messageKey;throw error;
  }
  return data;
}

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('err', err);
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, err ? 6000 : 3000);
}

const fail = (e) => toast(e.message || String(e), true);

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}

// ---------- шапка и вкладки ----------

let info = { editable: false, packs: [], stats: {} };

async function loadInfo() {
  info = await api('/api/v1/ui/info');
  const s = info.stats || {};
  $('#who').textContent = tr("{{0}} · vault: {{1}} · префикс {{2}}", [info.owner, s.entries ?? 0, info.prefix]);
  const badge = $('#review-badge');
  badge.textContent = s.journal_open || '';
  badge.hidden = !s.journal_open;
  document.body.classList.toggle('readonly', !info.editable);
  $('#rf').hidden = !info.editable;
  renderPacks();
  if (typeof setConnected === 'function') setConnected(true);
}

const TABS = { mask: () => {}, restore: () => {}, review: () => Promise.all([loadJournal(), loadInfo()]), rules: () => loadRules(), discover: () => renderDiscover(), metrics: () => loadMetrics() };

function showTab(name) {
  if (!TABS[name]) name = 'overview';
  const nav = name === 'restore' ? 'mask' : ['setup','discover','metrics','worker'].includes(name) ? 'settings' : name;
  for (const b of document.querySelectorAll('.tabs button')) {
    b.setAttribute('aria-selected', String(b.dataset.tab === nav));
    if (b.dataset.tab === nav) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  }
  for (const s of document.querySelectorAll('.tab')) s.hidden = s.id !== 'tab-' + name;
  hidePop();
  history.replaceState(null, '', '#' + name);
  if (typeof panelHeading === 'function') panelHeading(name);
  Promise.resolve(TABS[name]()).catch(fail);
}

for (const b of document.querySelectorAll('.tabs button')) b.addEventListener('click', () => showTab(b.dataset.tab));

// ---------- смещения ----------

// Ядро отдаёт байтовые смещения UTF-8, строки в JS индексируются в UTF-16.
function byteMap(text) {
  const map = [];
  for (let i = 0; i < text.length;) {
    const cp = text.codePointAt(i);
    const n = cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
    for (let k = 0; k < n; k++) map.push(i);
    i += cp > 0xffff ? 2 : 1;
  }
  map.push(text.length);
  return (b) => map[Math.min(b, map.length - 1)];
}

// ---------- всплывающее меню ----------

const pop = $('#pop');

function showPop(x, y, ...content) {
  pop.replaceChildren(...content);
  pop.hidden = false;
  const r = pop.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(x, innerWidth - r.width - 8)) + 'px';
  pop.style.top = (y + 12 + r.height > innerHeight ? Math.max(8, y - r.height - 8) : y + 12) + 'px';
  const first = pop.querySelector('input, button');
  if (first) first.focus({ preventScroll: true });
}

function hidePop() { pop.hidden = true; }

document.addEventListener('mousedown', (e) => { if (!pop.hidden && !pop.contains(e.target)) hidePop(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hidePop(); });

const lastKind = () => localStorage.getItem('kaponir-kind') || 'CUSTOM';
const normKind = (k) => (k || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();

function kindInput(value) {
  return h('input', { value: value || lastKind(), list: 'kinds', spellcheck: 'false', 'aria-label': tr("Тип токена") });
}

// Меню «замаскировать значение»: только здесь, в правила контура или «никогда не маскировать».
function maskMenu(value, x, y, suggestedKind) {
  value = value.trim();
  if (!value) return;
  if (value.includes('\n')) return toast(tr("Выделите значение в пределах одной строки"), true);
  if ([...value].length < MIN_VALUE) return toast(tr("Слишком короткое значение: нужно хотя бы {{0}} символа", [MIN_VALUE]), true);
  const kind = kindInput(suggestedKind);
  const go = (fn) => async () => {
    const k = normKind(kind.value);
    if (!k) return toast(tr("Укажите тип токена латиницей, например SVC"), true);
    localStorage.setItem('kaponir-kind', k);
    hidePop();
    try { await fn(k); } catch (e) { fail(e); }
  };
  const ro = !info.editable;
  showPop(x, y,
    h('div', { class: 'value', text: value }),
    h('div', { class: 'row' }, h('label', { text: tr("Тип") }), kind),
    h('div', { class: 'btns' },
      h('button', { class: 'primary', onclick: go(async (k) => { addAdhoc(value, k); }) }, tr("Только в этом тексте")),
      h('button', { disabled: ro, onclick: go((k) => addRule('personal', { kind: k, value })) }, tr("Всегда маскировать — личные правила")),
      h('button', { disabled: ro, onclick: go((k) => addRule('team', { kind: k, value })) }, tr("Всегда маскировать — правила команды")),
      h('div', { class: 'sep' }),
      h('button', { disabled: ro, onclick: go(() => addRule('personal', { kind: 'KEEP', action: 'keep', value })) }, tr("Никогда не маскировать — личные правила")),
    ),
  );
}

async function addRule(scope, rule, quiet = false) {
  await api('/api/v1/rules', { method: 'POST', body: { scope, rule } });
  if (!quiet) toast(tr("Правило добавлено: {{0}}", [SCOPE_TITLES[scope].toLowerCase()]));
  await afterRulesChanged();
}

async function afterRulesChanged() {
  await Promise.all([loadInfo(), runMask()]);
  if (!$('#tab-rules').hidden) await loadRules();
  if (!$('#tab-review').hidden) await loadJournal();
}

// ---------- Маскирование ----------

const M = { adhoc: [], result: null, text: '', seq: 0 };
const input = $('#m-input');
const backdrop = $('#m-backdrop');
const output = $('#m-output');
output.dataset.empty = tr("Здесь появится обезличенный текст");

function addAdhoc(value, kind) {
  if (!M.adhoc.some((a) => a.value === value)) M.adhoc.push({ value, kind });
  renderAdhoc();
  runMask();
}

function renderAdhoc() {
  const box = $('#m-adhoc');
  box.hidden = !M.adhoc.length;
  box.replaceChildren(
    h('span', { text: tr("Только в этом тексте:") }),
    ...M.adhoc.map((a, i) => h('span', { class: 'chip adhoc-chip', title: a.value },
      h('span', {}, a.value), h('span', { class: 'k', text: a.kind }),
      h('button', { title: tr("Убрать"), 'aria-label': tr("Убрать"), onclick: () => { M.adhoc.splice(i, 1); renderAdhoc(); runMask(); } }, '×'))),
  );
}

function maskBody(commit) {
  return {
    text: input.value,
    format: $('#m-format').value,
    scope: $('#m-scope').value,
    strategy: $('#m-strategy').value || null,
    adhoc: M.adhoc,
    commit,
  };
}

async function runMask() {
  const seq = ++M.seq;
  if (!input.value.trim()) {
    M.result = null;
    M.text = input.value;
    renderMask();
    return null;
  }
  $('#m-status').replaceChildren(h('span', { text: tr("анализ…") }));
  try {
    const res = await api('/api/v1/ui/mask', { method: 'POST', body: maskBody(false) });
    if (seq !== M.seq) return null;
    M.result = res;
    M.text = input.value;
    renderMask();
    return res;
  } catch (e) {
    if (seq === M.seq) $('#m-status').replaceChildren(h('span', { class: 'err', text: e.message }));
    return null;
  }
}

const runMaskSoon = debounce(runMask, 280);

const repClass = (r) => (r.recognizer === 'session' ? 's-adhoc' : 's-' + r.scope);

// Замены + оставленные спорные кандидаты, без пересечений, по возрастанию.
function spans() {
  const res = M.result;
  if (!res || M.text !== input.value) return [];
  const conv = byteMap(M.text);
  const out = res.replacements.map((r, i) => ({ s: conv(r.start), e: conv(r.end), rep: i }));
  const unc = res.explain
    .map((x, i) => ({ x, i }))
    .filter(({ x }) => x.verdict === 'uncertain' && !x.applied)
    .map(({ x, i }) => ({ s: conv(x.start), e: conv(x.end), unc: i }));
  for (const u of unc) {
    if (!out.some((o) => o.s < u.e && u.s < o.e)) out.push(u);
  }
  return out.sort((a, b) => a.s - b.s || b.e - a.e);
}

function renderMask() {
  const text = input.value;
  const sp = spans();
  const res = M.result && M.text === text ? M.result : null;

  // Подложка редактора: тот же текст, подсвеченные диапазоны.
  const bd = [];
  let pos = 0;
  for (const s of sp) {
    if (s.s < pos) continue;
    bd.push(text.slice(pos, s.s));
    const cls = s.rep !== undefined ? repClass(res.replacements[s.rep]) : 's-unc';
    bd.push(h('mark', { class: cls }, text.slice(s.s, s.e)));
    pos = s.e;
  }
  bd.push(text.slice(pos) + '\n');
  backdrop.replaceChildren(...bd);
  syncBackdrop();

  // Результат: токены вместо значений.
  if (!res) {
    output.replaceChildren();
  } else {
    const out = [];
    pos = 0;
    for (const s of sp) {
      if (s.s < pos) continue;
      out.push(text.slice(pos, s.s));
      if (s.rep !== undefined) {
        const r = res.replacements[s.rep];
        out.push(h('span', { class: `tok ${repClass(r)}${r.verdict === 'uncertain' ? ' unc' : ''}`, dataset: { rep: s.rep }, title: r.original }, r.token));
      } else {
        out.push(h('span', { class: 'keepunc', dataset: { unc: s.unc }, title: tr("спорно: оставлено как есть") }, text.slice(s.s, s.e)));
      }
      pos = s.e;
    }
    out.push(text.slice(pos));
    output.replaceChildren(...out);
  }

  // Статус.
  const st = [];
  if (res) {
    const keptUnc = res.explain.filter((x) => x.verdict === 'uncertain' && !x.applied).length;
    st.push(h('span', {}, tr("Замаскировано: "), h('b', { text: res.replacements.length })));
    if (res.uncertain) st.push(h('span', { class: 'warn' }, tr("спорных: {{0}}", [res.uncertain]) + (keptUnc ? tr(" (оставлено {{0}})", [keptUnc]) : '')));
    if (res.literals) st.push(h('span', { text: tr("строк вида токена во входе: {{0}} — не будут восстановлены", [res.literals]) }));
    st.push(h('span', { text: tr("предпросмотр — в vault сохраняется по кнопке «Сохранить и копировать» (Ctrl+Enter)") }));
  }
  $('#m-status').replaceChildren(...st);
  renderExplain(res);
}

function syncBackdrop() {
  backdrop.scrollTop = input.scrollTop;
  // Полоса прокрутки textarea сужает строку: подложка должна переносить так же.
  const sb = input.offsetWidth - input.clientWidth;
  backdrop.style.paddingRight = 12 + sb + 'px';
}

function evidenceNodes(ev) {
  return ev.map((e) => h('span', { class: 'ev ' + (e.logit > 0 ? 'pos' : 'neg'), text: `${e.source} ${e.logit > 0 ? '+' : ''}${e.logit.toFixed(1)}` }));
}

function renderExplain(res) {
  const t = $('#m-explain');
  if (!res || !res.explain.length) {
    t.replaceChildren(h('tr', {}, h('td', { class: 'empty', text: tr("Кандидатов нет") })));
    return;
  }
  const rows = [...res.explain].sort((a, b) => a.start - b.start || b.end - a.end);
  t.replaceChildren(
    h('thead', {}, h('tr', {}, ...['', tr("Решение"), 'p', tr("Тип"), tr("Распознаватель"), tr("Значение"), tr("Свидетельства")].map((c) => h('th', { text: c })))),
    h('tbody', {}, rows.map((x) => h('tr', { class: x.applied ? 'applied' : '' },
      h('td', { text: x.applied ? '✓' : '' }),
      h('td', {}, x.verdict === 'uncertain'
        ? h('span', { class: 'verdict-uncertain-box', title: tr("Вероятность в спорном диапазоне: применена политика uncertain") },
            h('strong', { text: `${tr("Спорно")} (p=${x.probability.toFixed(2)})` }),
            h('small', { text: tr("по политике uncertain={{0}}", [info.uncertain_policy || 'mask']) }))
        : h('span', { class: 'verdict ' + x.verdict, text: VERDICTS[x.verdict] || x.verdict })),
      h('td', { class: 'num', text: x.probability.toFixed(3) }),
      h('td', {}, kindBadge(x.kind)),
      h('td', { text: x.recognizer }),
      h('td', { class: 'mono', text: x.surface }),
      h('td', {}, evidenceNodes(x.evidence)),
    ))),
  );
}

input.addEventListener('input', () => {
  hidePop();
  renderMask(); // подложка сразу следует за текстом, подсветка — после ответа
  runMaskSoon();
});
input.addEventListener('scroll', () => { backdrop.scrollTop = input.scrollTop; });
new ResizeObserver(syncBackdrop).observe(input);
for (const id of ['#m-format', '#m-scope', '#m-strategy']) $(id).addEventListener('change', () => runMask());

// Click-to-mask в исходнике.
input.addEventListener('mouseup', (e) => {
  if (input.selectionStart === input.selectionEnd) return;
  const value = input.value.slice(input.selectionStart, input.selectionEnd);
  setTimeout(() => maskMenu(value, e.clientX, e.clientY), 0);
});
input.addEventListener('keyup', (e) => {
  if (!e.shiftKey || input.selectionStart === input.selectionEnd) return;
  const r = input.getBoundingClientRect();
  maskMenu(input.value.slice(input.selectionStart, input.selectionEnd), r.left + 40, r.top + 20);
});

// Click-to-mask в результате: выделение пропущенного текста, клик по токену или спорному месту.
output.addEventListener('mouseup', (e) => {
  const sel = getSelection();
  if (sel.isCollapsed || !output.contains(sel.anchorNode)) return;
  const touchesToken = [...output.querySelectorAll('.tok')].some((t) => sel.containsNode(t, true));
  if (touchesToken) return toast(tr("Выделите текст без токенов — токены уже замаскированы"), true);
  const value = sel.toString();
  setTimeout(() => maskMenu(value, e.clientX, e.clientY), 0);
});

output.addEventListener('click', (e) => {
  if (!getSelection().isCollapsed) return;
  const tok = e.target.closest('.tok');
  const unc = e.target.closest('.keepunc');
  if (tok) tokenMenu(M.result.replacements[+tok.dataset.rep], e.clientX, e.clientY);
  else if (unc) uncertainMenu(M.result.explain[+unc.dataset.unc], e.clientX, e.clientY);
});

function details(pairs) {
  return h('dl', {}, pairs.filter(([, v]) => v !== undefined && v !== '').flatMap(([k, v]) => [h('dt', { text: k }), h('dd', {}, v)]));
}

function tokenMenu(r, x, y) {
  const ro = !info.editable;
  const adhoc = r.recognizer === 'session';
  const keep = (scope) => async () => {
    hidePop();
    try {
      await addRule(scope, { kind: 'KEEP', action: 'keep', value: r.original }, true);
      const still = M.result && M.result.replacements.some((x) => x.original === r.original);
      toast(still
        ? tr("Правило добавлено, но значение по-прежнему маскируется: его закрывает корпоративное правило, детектор секретов или более длинная маска")
        : tr("Больше не маскируется: {{0}}", [SCOPE_TITLES[scope].toLowerCase()]), still);
    } catch (e) { fail(e); }
  };
  showPop(x, y,
    h('div', { class: 'value', text: r.original }),
    details([
      [tr("Токен"), h('code', { text: r.token })],
      [tr("Тип"), r.kind],
      [tr("Контур"), adhoc ? tr("только в этом тексте") : SCOPE_NAMES[r.scope]],
      [tr("Источник"), r.recognizer],
      ['p', r.probability.toFixed(3)],
      [tr("Решение"), VERDICTS[r.verdict]],
    ]),
    h('div', { class: 'btns' },
      h('button', { onclick: async () => { hidePop(); await copy(r.original); toast(tr("Оригинал скопирован")); } }, tr("Копировать оригинал")),
      adhoc
        ? h('button', { onclick: () => { hidePop(); M.adhoc = M.adhoc.filter((a) => a.value !== r.original); renderAdhoc(); runMask(); } }, tr("Не маскировать в этом тексте"))
        : [
          h('button', { disabled: ro, onclick: keep('personal') }, tr("Никогда не маскировать — личные правила")),
          h('button', { disabled: ro, onclick: keep('team') }, tr("Никогда не маскировать — правила команды")),
        ],
    ),
  );
}

function uncertainMenu(x, cx, cy) {
  const kind = kindInput(x.kind);
  const ro = !info.editable;
  const act = (fn) => async () => {
    const k = normKind(kind.value);
    if (!k) return toast(tr("Укажите тип токена латиницей"), true);
    hidePop();
    try { await fn(k); } catch (e) { fail(e); }
  };
  showPop(cx, cy,
    h('div', { class: 'value', text: x.surface }),
    details([
      [tr("Решение"), tr("спорно, оставлено как есть")],
      [tr("Источник"), x.recognizer],
      ['p', x.probability.toFixed(3)],
      [tr("Причины"), h('span', {}, evidenceNodes(x.evidence))],
    ]),
    h('div', { class: 'row' }, h('label', { text: tr("Тип") }), kind),
    h('div', { class: 'btns' },
      h('button', { class: 'primary', onclick: act(async (k) => addAdhoc(x.surface, k)) }, tr("Замаскировать только здесь")),
      h('button', { disabled: ro, onclick: act((k) => addRule('personal', { kind: k, value: x.surface })) }, tr("Всегда маскировать — личные правила")),
      h('button', { disabled: ro, onclick: act((k) => addRule('team', { kind: k, value: x.surface })) }, tr("Всегда маскировать — правила команды")),
      h('div', { class: 'sep' }),
      h('button', { disabled: ro, onclick: act(() => addRule('personal', { kind: 'KEEP', action: 'keep', value: x.surface })) }, tr("Это не секрет — личные правила")),
    ),
  );
}

async function commitMask() {
  if (!input.value.trim()) return;
  try {
    const res = await api('/api/v1/ui/mask', { method: 'POST', body: maskBody(true) });
    M.result = res;
    M.text = input.value;
    renderMask();
    await copy(res.text);
    toast(tr("Скопировано. В vault: {{0}} знач.", [res.replacements.length]) + (res.uncertain ? tr(", спорных {{0}} — см. «Разбор»", [res.uncertain]) : ''));
    await loadInfo();
  } catch (e) { fail(e); }
}

$('#m-commit').addEventListener('click', commitMask);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); commitMask(); }
});
$('#m-clear').addEventListener('click', () => {
  input.value = '';
  M.adhoc = [];
  renderAdhoc();
  runMask();
  input.focus();
});

const SAMPLE = `# deployment-prod.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prod-billing-auth
spec:
  template:
    spec:
      containers:
        - name: api
          image: harbor.infra.mycompany.ru/v2/secret-billing/api:1.4.2
          env:
            - name: DB_PASSWORD
              value: "hunter2-Xk9!"
            - name: DATABASE_DSN
              value: postgres://billing:pa55w0rd@pg-zorgbilling.mycompany.ru:5432/billing
          volumeMounts:
            - mountPath: /home/mgoloshanov/sber-pay-core/k8s/prod-values.yaml
              name: values
# Владелец: m.goloshanov@mycompany.ru, ИНН 7707083893, узел 10.50.12.15
`;

$('#m-sample').addEventListener('click', () => {
  input.value = SAMPLE;
  renderMask();
  runMask();
});

// ---------- Восстановление ----------

const rInput = $('#r-input');
const rOutput = $('#r-output');
rOutput.dataset.empty = tr("Здесь появится текст с реальными значениями");
let rSeq = 0;
let rText = '';

async function runRestore() {
  const seq = ++rSeq;
  if (!rInput.value.trim()) {
    rOutput.replaceChildren();
    $('#r-status').replaceChildren();
    rText = '';
    return;
  }
  try {
    const r = await api('/api/v1/ui/restore', { method: 'POST', body: { text: rInput.value, json_escape: $('#r-json').checked } });
    if (seq !== rSeq) return;
    rText = r.text;
    rOutput.textContent = r.text;
    const uniq = (a) => [...new Set(a)];
    const st = [h('span', {}, tr("Восстановлено: "), h('b', { text: r.restored }))];
    if (r.unknown.length) st.push(h('span', { class: 'warn', text: tr("нет в vault: {{0}}", [uniq(r.unknown).join(', ')]) }));
    if (r.denied.length) st.push(h('span', { class: 'err', text: tr("нет прав: {{0}}", [uniq(r.denied).join(', ')]) }));
    $('#r-status').replaceChildren(...st);
  } catch (e) {
    if (seq === rSeq) $('#r-status').replaceChildren(h('span', { class: 'err', text: e.message }));
  }
}

rInput.addEventListener('input', debounce(runRestore, 250));
$('#r-json').addEventListener('change', runRestore);
$('#r-copy').addEventListener('click', async () => {
  if (!rText) return;
  await copy(rText);
  toast(tr("Скопировано"));
});

// ---------- Разбор ----------

let jView = 'list';

for (const b of document.querySelectorAll('#j-view button')) {
  b.addEventListener('click', () => {
    jView = b.dataset.view;
    for (const x of document.querySelectorAll('#j-view button')) x.setAttribute('aria-pressed', String(x === b));
    loadJournal().catch(fail);
  });
}
$('#j-status').addEventListener('change', () => loadJournal().catch(fail));
$('#j-refresh').addEventListener('click', () => Promise.all([loadJournal(), loadInfo()]).catch(fail));

try {
  if ($('#j-auto-save')) {
    $('#j-auto-save').checked = localStorage.getItem('kaponir-auto-save-review') === '1';
    $('#j-auto-save').addEventListener('change', (e) => {
      try { localStorage.setItem('kaponir-auto-save-review', e.target.checked ? '1' : '0'); } catch {}
    });
  }
} catch {}

if ($('#j-auto-review')) {
  $('#j-auto-review').addEventListener('click', async () => {
    const btn = $('#j-auto-review');
    if (btn.disabled) return;
    btn.disabled = true;
    const autoSave = $('#j-auto-save') ? $('#j-auto-save').checked : false;
    try {
      const res = await api('/api/v1/ui/journal/auto-review', {
        method: 'POST',
        body: { auto_save: autoSave, threshold: 0.85 },
      });
      toast(tr("Авторазбор: классифицировано {{0}}, автоматически сохранено правил: {{1}}", [res.reviewed || 0, res.auto_saved || 0]));
      await afterRulesChanged();
    } catch (e) { fail(e); }
    finally { btn.disabled = false; }
  });
}

async function decide(body, label, rowEl = null, highlightClass = '') {
  try {
    if (rowEl && highlightClass) rowEl.classList.add(highlightClass);
    const scope = body.scope || $('#j-scope').value;
    const r = await api('/api/v1/journal/decide', { method: 'POST', body: { scope, ...body } });
    toast(tr("{{0}}: {{1}} зап.", [label, r.closed]));
    if (rowEl && highlightClass) {
      await new Promise((resolve) => setTimeout(resolve, 260));
    }
    await afterRulesChanged();
  } catch (e) {
    if (rowEl && highlightClass) rowEl.classList.remove(highlightClass);
    fail(e);
  }
}

function makeKindSelect(initialKind, disabled) {
  const current = String(initialKind || 'CUSTOM').toUpperCase();
  const options = STANDARD_KINDS.includes(current) ? STANDARD_KINDS : [current, ...STANDARD_KINDS];
  return h('select', { class: 'review-select', disabled, title: tr("Тип сущности") },
    ...options.map((k) => h('option', { value: k, selected: k === current, text: kindLabel(k) }))
  );
}

function makeScopeSelect(initialScope, disabled) {
  const scopes = [
    ['global', tr("Корпоративные (global)")],
    ['team', tr("Командные (team)")],
    ['personal', tr("Личные (personal)")],
  ];
  return h('select', { class: 'review-select', disabled, title: tr("Куда сохранить правило") },
    ...scopes.map(([val, label]) => h('option', { value: val, selected: val === initialScope, text: label }))
  );
}

function decisionButtons(sel, kindDefault, open, recommendedScope) {
  if (!open) return [];
  const ro = !info.editable;
  const kindSel = makeKindSelect(kindDefault, ro);
  const scopeSel = makeScopeSelect(recommendedScope || defaultScopeForKind(kindDefault), ro);
  let userChangedScope = false;
  scopeSel.addEventListener('change', () => { userChangedScope = true; });
  kindSel.addEventListener('change', () => {
    if (!userChangedScope) scopeSel.value = defaultScopeForKind(kindSel.value);
  });
  return [
    kindSel,
    scopeSel,
    h('button', {
      class: 'btn-confirm',
      disabled: ro,
      onclick: (ev) => decide({ ...sel, action: 'mask', kind: normKind(kindSel.value) || null, scope: scopeSel.value }, tr("Подтверждено (маскировать)"), ev.currentTarget.closest('tr, .card'), 'row-confirmed')
    }, tr("✓ Подтвердить")),
    h('button', {
      class: 'btn-reject',
      disabled: ro,
      onclick: (ev) => decide({ ...sel, action: 'keep', scope: scopeSel.value }, tr("Отклонено (не секрет)"), ev.currentTarget.closest('tr, .card'), 'row-rejected')
    }, tr("✕ Отказать")),
    h('button', {
      class: 'small ghost',
      onclick: (ev) => decide({ ...sel, action: 'dismiss', scope: scopeSel.value }, tr("Закрыто"), ev.currentTarget.closest('tr, .card'))
    }, tr("Скрыть")),
  ];
}

const STATUS_NAMES = { open: tr("открыто"), masked: tr("маскируется правилом"), kept: tr("оставлено правилом"), dismissed: tr("закрыто") };

function renderVerdictCell(r) {
  const isUncertain = (r.verdict || 'uncertain') === 'uncertain';
  const badge = isUncertain
    ? h('div', {
        class: 'verdict-uncertain-box',
        title: tr("Детектор не уверен на 100% (вероятность p={{0}} попала в спорный коридор). Применена политика по умолчанию uncertain={{1}}.", [r.probability.toFixed(2), r.applied])
      },
      h('strong', { text: `${tr("Спорно")} (p=${r.probability.toFixed(2)})` }),
      h('small', { text: r.applied === 'mask' ? tr("маскируется по политике uncertain=mask") : tr("оставляется по политике uncertain=keep") }))
    : h('span', { class: 'verdict ' + r.applied, text: `${VERDICTS[r.applied] || r.applied} (p=${r.probability.toFixed(2)})` });
  return h('div', {},
    badge,
    r.status !== 'open' ? h('div', { class: 'status-tag', text: STATUS_NAMES[r.status] || r.status }) : null
  );
}

async function loadJournal() {
  const status = $('#j-status').value;
  const rows = await api(`/api/v1/journal?status=${encodeURIComponent(status)}&limit=2000`);
  const list = $('#j-list');
  if (!rows.length) {
    list.replaceChildren(h('div', { class: 'card empty', text: status === 'open' ? tr("Спорных решений нет. Они появятся после «Сохранить и копировать», mask в CLI или запросов через прокси.") : tr("Записей нет") }));
    return;
  }
  if (jView === 'list') {
    list.replaceChildren(h('div', { class: 'review-table-wrap' }, h('table', { class: 'review-table' },
      h('thead', {}, h('tr', {}, ...[tr("Значение и контекст"), tr("Вердикт и политика"), tr("Источник"), tr("Тип сущности"), tr("Контур правил"), tr("Решение в 1 клик")].map((c) => h('th', { text: c })))),
      h('tbody', {}, rows.map((r) => {
        const ro = !info.editable;
        const open = r.status === 'open';
        const kindSel = open ? makeKindSelect(r.kind, ro) : kindBadge(r.kind);
        const recScope = r.recommended_scope || defaultScopeForKind(r.kind);
        const scopeSel = open ? makeScopeSelect(recScope, ro) : h('span', { class: 'tag', text: SCOPE_NAMES[recScope] || recScope });
        if (open) {
          let userChangedScope = false;
          scopeSel.addEventListener('change', () => { userChangedScope = true; });
          kindSel.addEventListener('change', () => {
            if (!userChangedScope) scopeSel.value = defaultScopeForKind(kindSel.value);
          });
        }
        const trRow = h('tr', {},
          h('td', { class: 'review-value-cell' },
            h('div', {}, h('span', { class: 'mono', text: r.value }), ' ', h('span', { class: 'tag', text: '×' + r.count })),
            r.context ? h('div', { class: 'context', text: r.context }) : null
          ),
          h('td', {}, renderVerdictCell(r)),
          h('td', {}, h('code', { text: r.recognizer })),
          h('td', {}, kindSel),
          h('td', {}, scopeSel),
          h('td', {}, open ? h('div', { class: 'review-actions' },
            h('button', {
              class: 'btn-confirm',
              disabled: ro,
              onclick: () => decide({ ids: [r.id], action: 'mask', kind: normKind(kindSel.value) || null, scope: scopeSel.value }, tr("Подтверждено (маскировать)"), trRow, 'row-confirmed')
            }, tr("✓ Подтвердить")),
            h('button', {
              class: 'btn-reject',
              disabled: ro,
              onclick: () => decide({ ids: [r.id], action: 'keep', scope: scopeSel.value }, tr("Отклонено (не секрет)"), trRow, 'row-rejected')
            }, tr("✕ Отказать")),
            h('button', {
              class: 'small ghost',
              onclick: () => decide({ ids: [r.id], action: 'dismiss', scope: scopeSel.value }, tr("Закрыто"), trRow)
            }, tr("Скрыть"))
          ) : h('span', { class: 'hint', text: STATUS_NAMES[r.status] || r.status })),
        );
        return trRow;
      })),
    )));
    return;
  }
  const groups = new Map();
  for (const r of rows) {
    const key = r.kind + '\u0000' + r.shape;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  const sorted = [...groups.values()].sort((a, b) => b.reduce((s, r) => s + r.count, 0) - a.reduce((s, r) => s + r.count, 0));
  list.replaceChildren(...sorted.map((g) => {
    const r0 = g[0];
    const total = g.reduce((s, r) => s + r.count, 0);
    const ps = g.map((r) => r.probability);
    const open = g.some((r) => r.status === 'open');
    const prange = Math.min(...ps) === Math.max(...ps) ? ps[0].toFixed(2) : `${Math.min(...ps).toFixed(2)}–${Math.max(...ps).toFixed(2)}`;
    return h('div', { class: 'card' },
      h('div', { class: 'group-head' },
        kindBadge(r0.kind),
        h('span', { class: 'shape', text: tr("форма ") + r0.shape }),
        h('span', { class: 'count', text: tr("{{0}} знач. · встречено {{1}}× · p {{2}} · {{3}}", [g.length, total, prange, r0.recognizer]) }),
      ),
      h('div', { class: 'samples' }, g.slice(0, 14).map((r) => h('span', { class: 'chip', title: r.context }, h('span', {}, r.value), h('span', { class: 'k', text: '×' + r.count }))),
        g.length > 14 ? h('span', { class: 'hint', text: tr("и ещё {{0}}", [g.length - 14]) }) : null),
      h('p', { class: 'context', text: r0.context }),
      h('div', { class: 'actions' }, decisionButtons(status === 'open' ? { shape: r0.shape, of_kind: r0.kind } : { ids: g.filter((r) => r.status === 'open').map((r) => r.id) }, r0.kind, open, r0.recommended_scope)),
    );
  }));
}

$('#j-template').addEventListener('submit', async (e) => {
  e.preventDefault();
  const t = $('#j-template-input').value.trim();
  if (!t) return;
  try {
    const r = await api('/api/v1/journal/template', { method: 'POST', body: { template: t, scope: $('#j-scope').value } });
    $('#j-template-input').value = '';
    toast(tr("Шаблон добавлен, закрыто записей: {{0}}", [r.closed]));
    await afterRulesChanged();
  } catch (err) { fail(err); }
});

// ---------- Первоначальная настройка ----------

const D = { documents: [], report: null, selected: new Set(), saved:false, busy:false };

async function discoverDocuments() {
  const files = [...$('#d-files').files];
  const documents = await Promise.all(files.map(async (file) => ({
    name: file.name,
    text: await file.text(),
    format: 'auto',
  })));
  const text = $('#d-text').value;
  if (text.trim()) documents.push({ name: tr("Вставленный текст"), text, format: $('#d-format').value });
  if (!documents.length) throw new Error(tr("Выберите файлы или вставьте документ"));
  return documents;
}

function updateDiscoverStatus() {
  if (!D.report) return;
  const r = D.report;
  const mode = {not_requested:'S1 не запрашивался',off:'S1 выключен: показаны локальные предложения',scored:'Оценки S1 получены',partial:'S1: оценена только часть примеров',unavailable:'S1 недоступен: предложения сохранены без оценок',no_candidates:'Нет кандидатов для S1',stored:'Сохранённая подборка'};
  $('#d-status').textContent = tr('{{0}} документ(ов) · выбрано {{1}} из {{2}}', [r.documents,D.selected.size,r.suggestions.length]) + ' · ' + tr(mode[r.s1_status] || 'S1 не запрашивался') + (r.truncated ? ' · '+tr('Достигнут лимит: показаны не все предложения') : '');
}

function renderDiscover() {
  const list = $('#d-list');
  const report = D.report;
  $('#d-apply').disabled = D.busy || !info.editable || !D.selected.size;
  $('#d-reject').disabled = D.busy || !info.editable || !D.selected.size;
  $('#d-all').disabled = D.busy || !report || !report.suggestions.length;
  $('#d-analyze').disabled = D.busy;
  $('#d-saved').disabled = D.busy;
  $('#d-none').disabled = !D.selected.size;
  if (!report) {
    $('#d-status').textContent = '';
    list.replaceChildren(h('div', { class: 'empty', text: tr("Здесь появятся предложения после локального анализа.") }));
    return;
  }
  updateDiscoverStatus();
  if (!report.suggestions.length) {
    list.replaceChildren(h('div', { class: 'empty', text: tr("Новых предложений нет. Уже замаскированные значения не предлагаются повторно.") }));
    return;
  }
  list.replaceChildren(h('div', { class: 'table-wrap discover-table' }, h('table', {},
    h('thead', {}, h('tr', {}, ...['', tr("Тип"), tr("Значение"), tr("Вхождений"), tr("Документов"), tr("Причина"), tr("Примеры и оценка S1")].map((x) => h('th', { text: x })))),
    h('tbody', {}, report.suggestions.map((s) => {
      const checkbox = h('input', { type: 'checkbox', disabled:D.busy, 'aria-label': tr("Выбрать ") + s.value });
      checkbox.checked = D.selected.has(s.id);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) D.selected.add(s.id); else D.selected.delete(s.id);
        updateDiscoverStatus();
        $('#d-apply').disabled = D.busy || !info.editable || !D.selected.size;
  $('#d-reject').disabled = D.busy || !info.editable || !D.selected.size;
        $('#d-none').disabled = !D.selected.size;
      });
      const scores=(s.examples||[]).map(e=>e.s1_score).filter(v=>v!=null);
      const scoreLabel=scores.length ? ' · S1: '+Math.min(...scores).toFixed(2)+'–'+Math.max(...scores).toFixed(2) : '';
      return h('tr', {},
        h('td', {}, checkbox), h('td', {}, kindBadge(s.kind)), h('td', { class: 'mono', text: s.value }),
        h('td', { class: 'num', text: s.count }), h('td', { class: 'num', text: s.documents }),
        h('td', { text: s.reason.split('; ').map(reason => reason.startsWith('кандидат детектора ') ? tr('Кандидат детектора {{0}}', [reason.slice('кандидат детектора '.length)]) : tr(reason)).join('; ') }), h('td', {}, h('details', {}, h('summary', {}, tr('Примеры: {{0}}', [(s.examples||[]).length])+scoreLabel),
          ...(s.examples||[]).map(e => h('div', {}, h('p', {class:'hint'}, e.document + ' · ' + e.start + '–' + e.end + ' · S1: ' + (e.s1_score == null ? '—' : e.s1_score.toFixed(2))), h('pre', {class:'mono'}, e.context))))));
    })))));
}

$('#d-analyze').addEventListener('click', async () => {
  const button = $('#d-analyze');
  if (D.busy) return;
  button.disabled = true;
  try {
    D.documents = await discoverDocuments();
    D.saved = $('#d-remember').checked;
    D.busy = true;
    D.report = null;
    D.selected.clear();
    renderDiscover();
    $('#d-status').textContent = tr($('#d-s1').checked ? 'Анализ и оценка S1…' : 'Локальный анализ…');
    D.report = await api('/api/v1/ui/discover', { method: 'POST', body: { documents: D.documents, use_s1:$('#d-s1').checked, remember:D.saved } });
    renderDiscover();
  } catch (e) { fail(e); } finally { D.busy=false; button.disabled = false; renderDiscover(); }
});

$('#d-all').addEventListener('click', () => {
  if (!D.report) return;
  D.selected = new Set(D.report.suggestions.map((s) => s.id));
  renderDiscover();
});
$('#d-none').addEventListener('click', () => { D.selected.clear(); renderDiscover(); });
async function decideDiscover(action) {
  if (!D.report || !D.selected.size || D.busy) return;
  D.busy = true; renderDiscover();
  try {
    const result = await api('/api/v1/ui/discover/apply', { method:'POST', body:{
      documents:D.saved ? [] : D.documents, ids:[...D.selected], scope:$('#d-scope').value, saved:D.saved, action,
    }});
    D.report = result.report; D.selected.clear();
    toast(tr(action === 'mask' ? 'Сохранено MASK-правил: {{0}}' : 'Сохранено отрицательных меток: {{0}}', [result.applied]));
    if (action === 'mask') await afterRulesChanged();
  } catch(e) { fail(e); }
  finally { D.busy=false; renderDiscover(); }
}
$('#d-apply').addEventListener('click', () => decideDiscover('mask'));
$('#d-reject').addEventListener('click', () => decideDiscover('not_sensitive'));
$('#d-saved').addEventListener('click', async () => {
  if (D.busy) return;
  D.busy=true;
  try {
    const result=await api('/api/v1/ui/discover/profile');
    D.report=result.report; D.saved=true; D.selected.clear();
  } catch(e) { fail(e); }
  finally { D.busy=false; renderDiscover(); }
});

async function analyzeProxySessions() {
  if (D.busy) return;
  showTab('discover');
  D.busy = true;
  renderDiscover();
  $('#d-status').textContent = tr("Анализ завершённых сессий прокси из Vault…");
  try {
    const res = await api('/api/v1/ui/discover/sessions', {
      method: 'POST',
      body: { use_s1: $('#d-s1') ? $('#d-s1').checked : false, remember: true },
    });
    D.report = res.report;
    D.saved = true;
    D.selected.clear();
    toast(tr("Разобрано сессий прокси: {{0}} · кандидатов: {{1}}", [res.sessions_analyzed || 0, (res.report && res.report.suggestions && res.report.suggestions.length) || 0]));
    await loadInfo();
  } catch (e) { fail(e); }
  finally { D.busy = false; renderDiscover(); }
}
window.analyzeProxySessions = analyzeProxySessions;
if ($('#d-sessions')) $('#d-sessions').addEventListener('click', analyzeProxySessions);
if ($('#settings-scan-sessions')) $('#settings-scan-sessions').addEventListener('click', analyzeProxySessions);
for (const id of ['d-files','d-text','d-format']) $('#'+id).addEventListener('change', () => {
  if (!D.saved && !D.busy) { D.report=null; D.selected.clear(); renderDiscover(); }
});

// ---------- Правила ----------

const PTYPE_HINTS = {
  value: [tr("Рога и Копыта"), tr("Точное значение по границам слова.")],
  template: ['{env:prod|stage|dev}-{SVC}-{hash}', tr("Слоты: {KIND} — чувствительная часть (заменяется токеном), {env:a|b} — перечисление, {hash}, {word}, {num}, {any} — каркас, который остаётся.")],
  regex: ['SEC-\\d{4,}', tr("Регулярное выражение (синтаксис Rust regex); совпадение маскируется целиком.")],
};

const rf = $('#rf');
const F = rf.elements;
function syncRuleForm() {
  const [ph, hint] = PTYPE_HINTS[F.ptype.value];
  F.pattern.placeholder = ph;
  $('#rf-hint').textContent = hint;
  const global = F.scope.value === 'global';
  F.enforced.disabled = !global;
  if (!global) F.enforced.checked = false;
  const keep = F.action.value === 'keep';
  // У шаблона тип задают слоты {SVC}.
  F.kind.disabled = keep || F.ptype.value === 'template';
  F.strategy.disabled = keep;
}
for (const n of ['ptype', 'scope', 'action']) rf[n].addEventListener('change', syncRuleForm);
syncRuleForm();

rf.addEventListener('submit', async (e) => {
  e.preventDefault();
  const keep = F.action.value === 'keep';
  const rule = {
    kind: keep ? 'KEEP' : normKind(F.kind.value),
    action: F.action.value,
    [F.ptype.value]: F.pattern.value,
    case_sensitive: F.case_sensitive.checked,
    enforced: F.enforced.checked,
    note: F.note.value.trim(),
  };
  if (!keep && F.strategy.value) rule.strategy = F.strategy.value;
  if (!rule.kind) return toast(tr("Укажите тип токена латиницей, например SVC"), true);
  try {
    await addRule(F.scope.value, rule);
    F.pattern.value = '';
    F.note.value = '';
  } catch (err) { fail(err); }
});

function rulePattern(r) {
  if (r.value != null) return [tr("значение"), r.value];
  if (r.template != null) return [tr("шаблон"), r.template];
  return ['regex', r.regex];
}

async function loadRules() {
  if (!info.editable) {
    $('#rules-list').replaceChildren(h('div', { class: 'card empty', text: tr("Этот сервер не управляет правилами (запущен без каталога правил).") }));
    return;
  }
  const sets = await api('/api/v1/rules');
  $('#rules-list').replaceChildren(...sets.map((set) => {
    const domains = set.file.corp.domains;
    const putDomains = async (list) => {
      try {
        await api(`/api/v1/rules/${set.scope}/domains`, { method: 'PUT', body: { domains: list } });
        await afterRulesChanged();
      } catch (e) { fail(e); }
    };
    const dInput = h('input', { placeholder: tr("новый домен, напр. corp.local"), spellcheck: 'false', 'aria-label': tr("Корпоративный домен") });
    const previewBox = h('div', { class: 'domain-preview' });
    const updateDomainPreview = () => {
      const sample = (dInput.value.trim() || domains[0] || 'company.com').replace(/^https?:\/\//i, '').replace(/\/.*$/, '');
      const parts = sample.split('.').filter(Boolean);
      const tld = parts.length > 1 ? parts[parts.length - 1] : 'com';
      previewBox.replaceChildren(
        h('span', { text: tr("Пример маскирования (До → После):") }),
        h('code', { text: `git.${sample}, ${sample}` }),
        h('span', { text: '→' }),
        h('code', { class: 'masked', text: `[SUB_1].[DOM_1].${tld}, [DOM_1].${tld}` }),
      );
    };
    dInput.addEventListener('input', updateDomainPreview);
    updateDomainPreview();
    const rules = set.file.rule || [];
    return h('div', { class: `card scope-card ${set.scope}` },
      h('h3', {}, SCOPE_TITLES[set.scope], h('span', { class: 'path', text: set.location })),
      h('div', { class: 'domains' },
        h('span', { class: 'hint', text: tr("Корпоративные домены:") }),
        domains.length ? domains.map((d) => h('span', { class: 'chip' }, h('span', {}, d),
          h('button', { title: tr("Удалить"), 'aria-label': tr("Удалить ") + d, onclick: () => putDomains(domains.filter((x) => x !== d)) }, '×'))) : h('span', { class: 'hint', text: tr("нет") }),
        h('form', { onsubmit: (e) => { e.preventDefault(); if (dInput.value.trim()) putDomains([...domains, dInput.value.trim()]); } },
          dInput, h('button', { class: 'small', type: 'submit' }, tr("Добавить"))),
        previewBox,
      ),
      rules.length
        ? h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, ...['№', tr("Действие"), tr("Тип"), tr("Что ищем"), '', tr("Заметка"), ''].map((c) => h('th', { text: c })))),
          h('tbody', {}, rules.map((r, i) => {
            const [pt, pv] = rulePattern(r);
            return h('tr', {},
              h('td', { class: 'num', text: r.id || i + 1 }),
              h('td', {}, h('span', { class: 'tag ' + (r.action || 'mask'), text: r.action === 'keep' ? tr("не маскировать") : tr("маскировать") })),
              h('td', {}, r.action === 'keep' ? '' : kindBadge(r.kind)),
              h('td', {}, h('span', { class: 'hint', text: pt + ' ' }), h('span', { class: 'mono', text: pv })),
              h('td', {},
                r.enforced ? h('span', { class: 'tag enf', text: 'enforced' }) : null, ' ',
                r.case_sensitive ? h('span', { class: 'tag', text: 'Aa' }) : null, ' ',
                r.strategy ? h('span', { class: 'tag', text: r.strategy }) : null),
              h('td', { class: 'hint', text: r.note || '' }),
              h('td', {}, h('button', {
                class: 'small danger',
                onclick: async () => {
                  if (!confirm(tr("Удалить правило «{{0}}»?", [pv]))) return;
                  try {
                    await api(`/api/v1/rules/${set.scope}/${i}`, { method: 'DELETE' });
                    toast(tr("Правило удалено"));
                    await afterRulesChanged();
                  } catch (e) { fail(e); }
                },
              }, tr("Удалить"))),
            );
          })),
        ))
        : h('div', { class: 'empty', text: tr("Правил нет") }),
    );
  }));
}

function renderPacks() {
  $('#packs').replaceChildren(info.packs.length
    ? h('table', {}, h('tbody', {}, info.packs.map((p) => h('tr', {},
      h('td', { class: 'mono', text: p.id }), h('td', {}, h('span', { class: 'tag', text: p.kind })),
      h('td', { class: 'num', text: 'v' + p.version }), h('td', { class: 'hint', text: tr(p.description) })))))
    : h('div', { class: 'empty', text: tr("Пакеты не загружены") }));
}

// ---------- старт ----------

async function startApp() {
  try {
    await loadInfo();
  } catch (e) { fail(e); }
  const tab = location.hash.slice(1);
  showTab(TABS[tab] ? tab : 'overview');
  renderAdhoc();
  renderMask();
}


// ---------- мониторинг ----------
async function loadMetrics() {
  const button = $('#metrics-refresh');
  button.disabled = true;
  try {
    const m = await api('/api/v1/metrics');
    const e = m.engine;
    const rows = [[tr("Операция"), tr("Вызовы"), tr("Ошибки"), tr("Среднее время, мс"), "P50, мс", "P95, мс", "P99, мс"]];
    for (const [name, op] of [[tr("HTTP до заголовков ответа"), m.http], [tr("Маскирование"), e.mask], [tr("Восстановление фрагментов"), e.restore], [tr("Батчи S1"), e.s1]]) {
      rows.push([
        name,
        op.calls,
        op.errors,
        op.calls ? (op.duration_us / op.calls / 1000).toFixed(2) : '—',
        op.calls && op.p50_us != null ? (op.p50_us / 1000).toFixed(2) : '—',
        op.calls && op.p95_us != null ? (op.p95_us / 1000).toFixed(2) : '—',
        op.calls && op.p99_us != null ? (op.p99_us / 1000).toFixed(2) : '—',
      ]);
    }
    const table = (rows) => h('table', {}, ...rows.map((row, i) => h('tr', {}, ...row.map(v => h(i ? 'td' : 'th', {}, v)))));
    $('#metrics-content').replaceChildren(table(rows), table([
      [tr("Показатель"), tr("Количество")], [tr("Замены"), e.replacements], [tr("Восстановленные значения"), e.restored],
      [tr("Неизвестные токены"), e.unknown_tokens], [tr("Отказы доступа к токенам"), e.denied_tokens],
      [tr("Кандидаты S1"), e.s1_candidates], [tr("Сбои потока провайдера"), m.stream_errors]
    ]));
    const codes = { timeout: tr("Истёк срок ожидания"), provider_error: tr("Ошибка провайдера"), invalid_response: tr("Некорректные оценки") };
    $('#metrics-events').replaceChildren(e.recent_s1_failures.length
      ? table([[tr("Время"), tr("Причина")], ...e.recent_s1_failures.slice().reverse().map(x => [new Date(x.unix_ms).toLocaleString(I18N.language), codes[x.code] || x.code])])
      : h('p', {}, tr("Сбоев S1 с запуска сервера не зарегистрировано.")));
    $('#metrics-status').textContent = tr("Обновлено: {{0}}. Сервер работает {{1}} с.", [new Date().toLocaleTimeString(I18N.language), m.uptime_seconds]);
  } catch (e) {
    $('#metrics-status').textContent = tr("Не удалось обновить данные. Показанные ранее значения могут быть устаревшими.");
    throw e;
  } finally { button.disabled = false; }
}
$('#metrics-refresh').addEventListener('click', () => loadMetrics().catch(fail));

function relocalizeConstants() {
  Object.assign(SCOPE_NAMES, { global: tr("корпоративный"), team: tr("команда"), personal: tr("личный") });
  Object.assign(SCOPE_TITLES, { global: tr("Корпоративные правила"), team: tr("Правила команды"), personal: tr("Личные правила") });
  Object.assign(VERDICTS, { mask: tr("маскировать"), keep: tr("оставить"), uncertain: tr("спорно") });
  Object.assign(STATUS_NAMES, { open: tr("открыто"), masked: tr("маскируется правилом"), kept: tr("оставлено правилом"), dismissed: tr("закрыто") });
  Object.assign(PTYPE_HINTS, {
  value: [tr("Рога и Копыта"), tr("Точное значение по границам слова.")],
  template: ['{env:prod|stage|dev}-{SVC}-{hash}', tr("Слоты: {KIND} — чувствительная часть (заменяется токеном), {env:a|b} — перечисление, {hash}, {word}, {num}, {any} — каркас, который остаётся.")],
  regex: ['SEC-\\d{4,}', tr("Регулярное выражение (синтаксис Rust regex); совпадение маскируется целиком.")],
});
}
