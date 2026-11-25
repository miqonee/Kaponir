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
  FNAME: "Имя",
  LNAME: "Фамилия",
  MNAME: "Отчество",
  PATH: "Часть пути",
  AMOUNT: "Финансовое значение",
  ID: "Идентификатор",
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

const STANDARD_KINDS = Object.keys(KIND_LABELS);

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
  if (['DOM', 'ORG', 'CLIENT'].includes(k)) return 'global';
  if (['PROJ', 'SVC', 'HOST', 'MODULE', 'PATH', 'SUB'].includes(k)) return 'team';
  return 'personal';
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
for (const button of [$('#access-settings'), $('#proxy-access')]) button.addEventListener('click', async () => {
  authCancelled = false;
  try { await askToken(); await loadInfo(); showTab(location.hash.slice(1) || 'overview'); }
  catch (error) { fail(error); }
});

async function api(path, { method = 'GET', body, timeoutMs = 45000 } = {}) {
  const headers = {};
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (token) headers.authorization = 'Bearer ' + token;
  const r = await fetch(path, { method, headers, signal: AbortSignal.timeout(timeoutMs), body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401) {
    await askToken();
    return api(path, { method, body, timeoutMs });
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
  const pending = info.review_pending ?? s.journal_open;
  badge.textContent = pending || '';
  badge.hidden = !pending;
  document.body.classList.toggle('readonly', !info.editable);
  $('#rules-new').disabled = !info.editable;
  $('#rules-export').disabled = !info.editable;
  renderPacks();
  if (typeof setConnected === 'function') setConnected(true);
}

const TABS = { mask: () => {}, restore: () => {}, review: () => Promise.all([loadJournal(), loadInfo()]), rules: () => loadRules(), discover: () => { renderDiscover(); loadSearchMethod(); }, metrics: () => loadMetrics() };

function showTab(name, push = true) {
  const route = name || 'overview';
  if (route.startsWith('jobs/')) name = 'jobs';
  if (name === 's1') name = 'analysis';
  if (name === 'lab') name = 'mask';
  if (!TABS[name]) name = 'overview';
  const nav = {mask:'lab',restore:'lab',discover:'lab',analysis:'settings',worker:'settings',categorization:'settings',proxy:'connect',metrics:'connect',setup:'rules'}[name] || name;
  const changed = $('#tab-' + name).hidden;
  if (changed) for (const dialog of document.querySelectorAll('.inspector-dialog[open], .rules-dialog[open]')) dialog.close();
  for (const b of document.querySelectorAll('.tabs [data-tab]')) {
    if (b.dataset.tab === nav) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  }
  for (const s of document.querySelectorAll('.tab')) s.hidden = s.id !== 'tab-' + name;
  hidePop();
  const hash = '#' + (name === 'jobs' ? route : name);
  if (location.hash !== hash) history[push ? 'pushState' : 'replaceState'](null, '', hash);
  if (typeof panelHeading === 'function') panelHeading(name, nav);
  if (typeof setSidebar === 'function' && (changed || push || !document.activeElement.closest('.sidebar-preferences'))) setSidebar(false);
  const preview = $('#job-preview');
  if (preview?.matches('[popover]') && preview.hidePopover) preview.hidePopover();
  if (changed && push) {
    window.scrollTo(0, 0);
    $('#page-title').focus({preventScroll:true});
  }
  return Promise.resolve(TABS[name]()).catch(fail);
}

let tabGroupCount = 0;
function contentTabs(label, pages, selected = pages[0].key) {
  const prefix = "content-tabs-" + (++tabGroupCount);
  const tabs = h("div", {class:"content-tab-list", role:"tablist", "aria-label":tr(label)});
  const panels = h("div", {class:"content-tab-panels"});
  function activate(index, focus = false) {
    Array.from(tabs.children).forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      panels.children[i].hidden = i !== index;
    });
    if (focus) tabs.children[index].focus({preventScroll:true});
  }
  pages.forEach(({key, title, content}, index) => {
    const id = prefix + "-" + key;
    tabs.append(h("button", {type:"button", id, role:"tab", "data-pane":key, "aria-controls":id+"-panel",
      onclick:()=>activate(index)}, tr(title)));
    panels.append(h("section", {id:id+"-panel", role:"tabpanel", tabindex:0, "aria-labelledby":id}, content));
  });
  tabs.addEventListener("keydown", event => {
    const index = Array.from(tabs.children).indexOf(event.target);
    const count = pages.length;
    const next = {ArrowLeft:(index+count-1)%count, ArrowRight:(index+1)%count, Home:0, End:count-1}[event.key];
    if (index < 0 || next === undefined) return;
    event.preventDefault();
    activate(next, true);
  });
  activate(Math.max(0, pages.findIndex(page => page.key === selected)));
  return h("div", {class:"content-tabs"}, tabs, panels);
}

function followLocalLink(event) {
  if (!event.currentTarget.matches('a')) return true;
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) return false;
  event.preventDefault();
  return true;
}
for (const b of document.querySelectorAll('.tabs [data-tab]')) b.addEventListener('click', (event) => {
  if (!followLocalLink(event)) return;
  if (b.dataset.tab === 'review' && (ReviewUI.keys || ReviewUI.filterSet || ReviewUI.jobId || ReviewUI.learningJob)) showReview();
  else showTab(b.dataset.tab);
});

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

function candidateMark(text) {
  return h('mark', { class: 'candidate-match' }, text);
}

// A saved span is relative to this already-redacted excerpt, in UTF-8 bytes.
function candidateExcerpt(text, value, span) {
  const fragment = document.createDocumentFragment();
  if (!text || !value) {
    fragment.append(text || tr('Контекст не сохранён'));
    return fragment;
  }
  if (Array.isArray(span) && span.length === 2 && span.every(Number.isSafeInteger)
      && span[0] >= 0 && span[0] < span[1]) {
    const index = byteMap(text);
    const start = index(span[0]), end = index(span[1]);
    const surface = text.slice(start, end);
    if (surface.toLowerCase() === value.toLowerCase() || surface === value.replace(/[\r\n]/g, ' ')) {
      fragment.append(text.slice(0, start), candidateMark(surface), text.slice(end));
      return fragment;
    }
  }
  // Older records have no offsets. Show literal matches without attributing an ambiguous occurrence.
  let cursor = 0, count = 0;
  for (let at = text.indexOf(value); at >= 0; at = text.indexOf(value, cursor)) {
    fragment.append(text.slice(cursor, at), candidateMark(value));
    cursor = at + value.length;
    count++;
  }
  fragment.append(text.slice(cursor));
  let note = 'В сохранённом фрагменте этого значения нет.';
  if (count === 1) note = 'Совпадение найдено по тексту. Точное вхождение не сохранено.';
  if (count > 1) note = 'Подсвечены все совпадения. Точное вхождение не сохранено.';
  fragment.append(h('small', { class: 'excerpt-note' }, tr(note)));
  return fragment;
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
  M.persisted=false;
  const seq = ++M.seq;
  if (!input.value.trim()) {
    M.result = null;
    M.text = input.value;
    renderMask();
    return null;
  }
  $('#m-status').replaceChildren(h('span', { text: tr("анализ…") }));
  const body = maskBody(false);
  try {
    const res = await api('/api/v1/ui/mask', { method: 'POST', body });
    if (seq !== M.seq || body.text !== input.value) return null;
    M.result = res;
    M.text = body.text;
    renderMask();
    return res;
  } catch (e) {
    if (seq === M.seq) $('#m-status').replaceChildren(h('span', { class: 'err', text: e.message }));
    return null;
  }
}

let maskTimer;
const runMaskSoon = () => { clearTimeout(maskTimer); maskTimer = setTimeout(runMask, 280); };

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

function restoreCandidate() {
  const raw=input.value;
  const codec=info.prefix && new RegExp(info.prefix+"[_-][a-z][a-z0-9]{0,23}[_-](?:h[a-f0-9]{6,16}|[0-9]{1,12})","i");
  if(codec?.test(raw.replaceAll(String.fromCharCode(92),"")))return raw;
  return M.persisted && M.text===raw && M.result?.replacements?.length ? M.result.text : "";
}
function syncRestoreAction() {
  $("#lab-navigation [data-go=restore]").hidden=location.hash!=="#restore" && !restoreCandidate();
}
function renderMask() {
  syncRestoreAction();
  const text = input.value;
  const sp = spans();
  const res = M.result && M.text === text ? M.result : null;
  $('#m-commit').disabled = !text.trim() || !!M.committing;
  $('#m-clear').disabled = !text;
  $('#tab-mask .explain').hidden = !res?.explain?.length;

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
    st.push(h('span', { text: tr(M.persisted ?
      (res.replacements.length ? "Токены сохранены. Можно восстановить исходный текст." : "Проверка сохранена.") :
      "Предпросмотр. Чтобы сохранить токены и скопировать текст, нажмите Ctrl+Enter.") }));
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
  const source = M.text, index = byteMap(source);
  function example(x) {
    const detail = h('details', { class: 'candidate-example' }, h('summary', { class: 'mono' }, x.surface));
    detail.addEventListener('toggle', () => {
      if (!detail.open || detail.childElementCount > 1) return;
      detail.append(h('blockquote', { class: 'candidate-fragment' },
        source.slice(index(Math.max(0, x.start - 80)), index(x.start)),
        candidateMark(source.slice(index(x.start), index(x.end))),
        source.slice(index(x.end), index(x.end + 80))));
      if (!res.replacements.some(r => r.start <= x.start && x.end <= r.end)) {
        detail.append(workButton('Замаскировать только здесь', () => addAdhoc(x.surface, x.kind), 'secondary'));
      }
    });
    return detail;
  }
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
      h('td', {}, example(x)),
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
  if (!input.value.trim() || M.committing) return;
  clearTimeout(maskTimer);
  const body = maskBody(true);
  const signature = JSON.stringify(body);
  M.committing = true;
  $('#m-commit').disabled = true;
  try {
    const res = await api('/api/v1/ui/mask', { method: 'POST', body });
    if (signature === JSON.stringify(maskBody(true))) {
      ++M.seq;
      M.persisted=true;
      M.result = res;
      M.text = body.text;
      renderMask();
    }
    await copy(res.text);
    toast(tr("Скопировано. В vault: {{0}} знач.", [res.replacements.length]) + (res.uncertain ? " · " + tr("На проверку в находках · {{0}}", [res.uncertain]) : ''));
    await loadInfo();
  } catch (e) { fail(e); }
  finally { M.committing=false; $('#m-commit').disabled=!input.value.trim(); }
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

async function loadJournal() { if (typeof loadReview === 'function' && location.hash === '#review') await loadReview(true); }

// ---------- Первоначальная настройка ----------

const D = { documents: [], report: null, selected: new Set(), saved:false, busy:false, view:'input', views:{} };

function syncDiscoveryActions() {
  for (const id of ['d-apply','d-reject','d-none']) $('#'+id).hidden=!D.selected.size;
  $('#d-scope').closest('label').hidden=!D.selected.size;
  document.querySelector('#discover-result-pane .selection-help').hidden=!D.selected.size;
}
async function showDiscoverView(view, skipLoad=false) {
  if(D.busy)return;
  D.views[D.view]={report:D.report,selected:new Set(D.selected),saved:D.saved,documents:D.documents,scope:D.scope};
  D.view=view;
  const previous=D.views[view];
  D.scope=view==="saved"?tr("Сохранённые находки"):previous?.scope||"";
  D.report=previous?.report||null;D.selected=previous?.selected||new Set();D.saved=view==='saved'||!!previous?.saved;D.documents=previous?.documents||[];
  const sessions = view === 'sessions';
  $('#discover-input-pane').hidden = view === 'saved' || (sessions && !!D.sessionTarget);
  $('#d-document-input').hidden = sessions;
  $('#d-session-input').hidden = !sessions;
  $('#d-format').closest('label').hidden = sessions;
  $('#d-input-view').setAttribute('aria-pressed',String(view==='input'));$('#d-saved').setAttribute('aria-pressed',String(view==='saved'));
  $('#d-sessions').setAttribute('aria-pressed',String(view==='sessions'));
  if (view !== 'saved') showDocumentScenario();
  if(view==='saved'&&!skipLoad){D.busy=true;renderDiscover();try{const r=await api('/api/v1/ui/discover/profile');D.report=r.report;D.selected.clear();}catch(e){fail(e);}finally{D.busy=false;}}
  renderDiscover();
}
$('#d-input-view').addEventListener('click',()=>showDiscoverView('input'));

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
  syncDiscoveryActions();
  if (!D.report) return;
  const r = D.report;
  const mode = {not_requested:'S1 не запрашивался',off:'Значения найдены локальными детекторами',scored:'Оценки S1 получены',partial:'S1: оценена только часть примеров',unavailable:'Значения сохранены. Оценка S1 недоступна.',no_candidates:'Нет кандидатов для S1',stored:'Сохранённая подборка'};
  $('#d-status').textContent = (D.view === 'sessions' ? tr('Кандидатов: {{0}} · выбрано {{1}}', [r.suggestions.length, D.selected.size]) : tr('{{0}} документ(ов) · выбрано {{1}} из {{2}}', [r.documents,D.selected.size,r.suggestions.length])) + ' · ' + tr(mode[r.s1_status] || 'S1 не запрашивался') + (r.truncated ? ' · '+tr('Показана часть найденных значений') : '');
}

function renderDiscover() {
  const foundKeys=D.report?.finding_keys||[];
  $('#discover-result-pane').hidden = D.view === 'input' && !D.report && !D.busy;
  $('#d-back-sessions').hidden = D.view !== 'sessions';
  $("#discover-findings").hidden=!foundKeys.length;
  $("#discover-collect").hidden=!foundKeys.length;
  $("#discover-collect").textContent=tr("В датасет все {{0}}",[foundKeys.length]);
  $("#d-source-summary").textContent=D.scope||"";
  const destination = $("#discover-destination");
  destination.hidden = !D.report || !D.report.suggestions.length;
  if (!destination.hidden) destination.textContent = tr(foundKeys.length ?
    "Значения сохранены в находках. Откройте их для проверки или добавьте в датасет." :
    "Предпросмотр документа. Для создания правила выберите значение и область действия.");
  syncDiscoveryActions();
  const list = $('#d-list');
  const report = D.report;
  $('#d-apply').disabled = D.busy || !info.editable || !D.selected.size;
  $('#d-reject').disabled = D.busy || !info.editable || !D.selected.size;
  $('#d-all').disabled = D.busy || !report || !report.suggestions.length;
  $('#d-analyze').disabled = D.busy || D.loadingMethod || ($('#d-mode').value === 'background' && !(D.method?.analysis_enabled && D.method.analysis));
  for (const id of ['d-saved', 'd-input-view', 'd-sessions']) $('#'+id).disabled = D.busy;
  $('#d-none').disabled = !D.selected.size;
  if (!report) {
    $('#d-status').textContent = '';
    list.replaceChildren(h('div', { class: 'empty', text: tr("Здесь появятся найденные значения.") }));
    return;
  }
  updateDiscoverStatus();
  if (!report.suggestions.length) {
    list.replaceChildren(h('div', { class: 'empty', text: tr("Новых значений для проверки нет.") }));
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
        h('td', { text: s.reason.split('; ').map(reason => reason.startsWith('кандидат детектора ') ? tr('Кандидат детектора {{0}}', [reason.slice('кандидат детектора '.length)]) : tr(reason)).join('; ') }), h('td', {}, h('details', {class:'document-examples'}, h('summary', {}, tr('Фрагменты · {{0}}', [(s.examples||[]).length])+scoreLabel),
          ...(s.examples||[]).map(e => h('div', {}, h('p', {class:'hint'}, e.document + ' · ' + e.start + '–' + e.end + ' · S1: ' + (e.s1_score == null ? '—' : e.s1_score.toFixed(2))), h('pre', {class:'mono candidate-fragment'}, candidateExcerpt(e.context, s.value, e.context_span)))))));
    })))));
}

function methodSummary(analysis, category, selected) {
  const display = source => {
    const name = source.provider === 'local' ? modelName(source.model) : source.model;
    const origin = source.connection || (source.provider === 'local' ? 'CPU' : '');
    return [source.provider === 's1' ? 'S1' : '', name, origin].filter(Boolean).join(' · ');
  };
  if (!analysis) return tr('Обнаружение выключено');
  if (!category) return tr('Поиск · {{0}}. Категоризация выключена.', [display(analysis)]);
  const scenario = ['local','api'].includes(category.provider)
    ? (selected || category.scenario || 'standard') : null;
  const shared = analysis.model === category.model && analysis.provider === category.provider &&
    analysis.connection_id === category.connection_id;
  const stages = shared ? tr('Поиск и проверка · {{0}}', [display(analysis)]) :
    tr('Поиск · {{0}}. Проверка · {{1}}', [display(analysis), display(category)]);
  return [stages, scenario === 'standard' ? tr('Стандартная проверка') : scenario].filter(Boolean).join(' · ');
}
async function loadSearchMethod() {
  const request = D.methodRequest = (D.methodRequest || 0) + 1;
  D.loadingMethod = true;
  showDocumentScenario();
  try {
    const cfg = await api('/api/v1/ui/workspace/model');
    if (request !== D.methodRequest) return;
    if (D.method?.analysis?.revision !== cfg.analysis?.revision) $("#d-scenario").value = "";
    D.method = cfg;
    if (!D.overrideMethod) $('#d-mode').value = cfg.analysis_enabled && cfg.analysis ? 'background' : 'fast';
    $('#d-launch-status').replaceChildren();
  } catch (error) {
    if (request !== D.methodRequest) return;
    D.method = null;
    $('#d-mode').value = 'fast';
    $('#d-launch-status').replaceChildren(document.createTextNode(tr('Настройки модели недоступны. Доступен поиск детекторами.')), workButton('Повторить', loadSearchMethod));
  } finally {
    if (request === D.methodRequest) { D.loadingMethod = false; showDocumentScenario(); }
  }
}
function showDocumentScenario() {
  const background = $('#d-mode').value === 'background';
  const cfg = D.method, category = cfg?.categorization;
  const supported = background && ['local','api'].includes(category?.provider);
  $('#d-scenario-field').hidden = !supported;
  $('#d-scenario').disabled = !supported;
  $('#d-fast-options').hidden = background;
  $('#d-remember').closest('label').hidden = D.view === 'sessions';
  $('#d-remember').closest('label').nextElementSibling.hidden = D.view === 'sessions';
  $('#d-analyze').disabled = D.busy || D.loadingMethod || (background && !(cfg?.analysis_enabled && cfg.analysis));
  $('#d-method-summary').textContent = D.loadingMethod ? tr('Загружаем настройки…') : background
    ? methodSummary(cfg?.analysis_enabled ? cfg.analysis : null, category, $('#d-scenario').value)
    : tr($('#d-s1').checked ? 'Быстрые детекторы и оценка S1' : 'Быстрые детекторы');
  const remoteSearch = background && cfg?.analysis_enabled && cfg.analysis?.provider === 'api';
  const remoteReview = background && ['api','s1'].includes(category?.provider);
  $('#d-file-destination').textContent = D.loadingMethod ? '' : remoteSearch
    ? tr('При поиске текст получит {{0}}.', [cfg.analysis.connection])
    : remoteReview ? tr('При проверке данные получит {{0}}.', [category.connection])
      : $('#d-s1').checked && !background ? tr('Для оценки будет вызван настроенный S1.') : tr('Поиск выполняется средствами Kaponir.');
  if (supported && $('#d-settings').open)
    renderScenarioStudy($('#d-scenario-study'), category.provider === 'local' ? measuredModelId(category.model) : null,
      $('#d-scenario').value || category.scenario || 'standard', $('#d-scenario'), category.provider === 'api');
}
$('#d-settings').addEventListener('toggle', showDocumentScenario);
$('#d-mode').addEventListener('change', () => { D.overrideMethod = true; showDocumentScenario(); });
$('#d-scenario').addEventListener('change', showDocumentScenario);
$('#d-s1').addEventListener('change', showDocumentScenario);
$('#analysis-open-document').addEventListener('click', () => {
  showTab('discover');
  if (D.view !== 'input') showDiscoverView('input');
  D.overrideMethod = true;
  $('#d-mode').value = 'background';
  showDocumentScenario();
});
$('#d-analyze').addEventListener('click', async () => {
  const button = $('#d-analyze');
  if (button.disabled || D.busy) return;
  if (D.view === 'sessions' && $('#d-mode').value === 'fast') return analyzeProxySessions(null, $('#d-s1').checked);
  D.busy = true;
  button.disabled = true;
  $('#d-launch-status').replaceChildren();
  try {
    if (D.view === 'sessions') {
      const worker = await api('/api/v1/ui/worker');
      if (!worker.enabled || !worker.online) {
        $('#d-launch-status').replaceChildren(document.createTextNode(tr('Для поиска по сессиям включите службу анализа.')),
          workButton('Открыть службу анализа', () => showTab('worker')));
        return;
      }
      const scenario = $('#d-scenario').disabled ? '' : $('#d-scenario').value || D.method.categorization.scenario;
      const job = await api('/api/v1/ui/worker/jobs', {method:'POST',body:{mode:'configured',source:'@sessions',
        revision:D.method.analysis.revision, ...(scenario ? {scenario} : {})}});
      D.overrideMethod = false;
      showTab('jobs/file/'+encodeURIComponent(job.id));
      return;
    }
    D.documents = await discoverDocuments();
    D.scope=D.documents.map(doc=>doc.name).join(", ");
    D.saved = $('#d-remember').checked;
    D.busy = true;
    D.report = null;
    D.selected.clear();
    renderDiscover();
    $('#d-status').textContent = tr($('#d-s1').checked ? 'Анализ и оценка S1…' : 'Локальный анализ…');
    if ($('#d-mode').value === 'background') {
      const scenario = $('#d-scenario').disabled ? '' : $('#d-scenario').value || D.method.categorization.scenario;
      const queued=await api('/api/v1/ui/analysis/documents',{method:'POST',body:{documents:D.documents, revision:D.method.analysis.revision, ...(scenario ? {scenario} : {})}});
      D.busy=false;D.overrideMethod=false;renderDiscover();
      $('#d-status').textContent=queued.queued?tr('Поставлено в очередь: {{0}}',[queued.queued]):tr('Документы уже обработаны или находятся в очереди');
      showTab(queued.ids?.length === 1 ? "jobs/learning/" + encodeURIComponent(queued.ids[0]) : "jobs");
      return;
    }
    D.report = await api('/api/v1/ui/discover', { method: 'POST', body: { documents: D.documents, use_s1:$('#d-s1').checked, remember:D.saved } });
    D.overrideMethod = false;
    renderDiscover();
  } catch (e) {
    if (e.messageKey === 'configuration_changed' || e.message === 'configuration_changed') await loadSearchMethod();
    $('#d-launch-status').textContent = $('#d-mode').value === 'background' ? learningError(e.messageKey || e.message) : e.message;
  } finally { D.busy = false; renderDiscover(); showDocumentScenario(); }
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
    if (D.view === "saved") D.report = result.report;
    else {
      D.report.suggestions = D.report.suggestions.filter(s => !D.selected.has(s.id));
      if (D.report.finding_keys?.length) {
        const remaining = new Set(result.report.finding_keys);
        D.report.finding_keys = D.report.finding_keys.filter(key => remaining.has(key));
      }
    }
    D.selected.clear();
    toast(tr(action === 'mask' ? 'Сохранено MASK-правил: {{0}}' : 'Сохранено отрицательных меток: {{0}}', [result.applied]));
    if (action === 'mask') await afterRulesChanged();
  } catch(e) { fail(e); }
  finally { D.busy=false; renderDiscover(); }
}
$('#d-apply').addEventListener('click', () => {
  const dialog=h('dialog',{class:'review-confirm'});
  const selected=D.report.suggestions.filter(s=>D.selected.has(s.id));
  dialog.append(h('div',{class:'review-confirm-body'},h('h2',{},tr('Проверить изменения')),
    h('p',{},tr('Будут созданы правила маскирования · {{0}}',[selected.length]),' · ',scopeLabel($('#d-scope').value)),
    h('ul',{class:'review-confirm-list'},selected.map(s=>h('li',{},s.value,' · ',kindLabel(s.kind)))),
    workButton('Применить изменения',()=>{dialog.close();return decideDiscover('mask');},'primary'),
    workButton('Отмена',()=>dialog.close())));
  dialog.onclose=()=>dialog.remove();document.body.append(dialog);dialog.showModal();
});
$('#d-reject').addEventListener('click', () => decideDiscover('not_sensitive'));
$('#d-saved').addEventListener('click',()=>showDiscoverView('saved'));

async function analyzeProxySessions(target = null, useS1 = false) {
  if (D.busy) { toast(tr("Дождитесь окончания текущего поиска.")); return; }
  const name = target?.session ? tr("Сессия {{0}}", [target.session]) :
    target?.request ? tr("Запрос {{0}}", [target.request]) : tr("Все сохранённые сессии");
  showTab('discover');
  D.sessionTarget = target;
  await showDiscoverView('sessions', true);
  D.busy = true; D.report = null; D.saved = true; D.selected.clear(); D.scope = name;
  renderDiscover();
  $('#d-status').textContent = tr("Ищем значения в сохранённых запросах и ответах…");
  try {
    const res = await api('/api/v1/ui/discover/sessions', {
      method: 'POST', body: {target, use_s1: useS1, remember: true},
    });
    D.report = res.report;
    D.scope = name + ' · ' + tr("Проверено сессий · {{0}}", [res.sessions_analyzed]);
    if (!res.sessions_analyzed) D.scope += ' · ' + tr(res.report.truncated
      ? "Текст превышает лимит одного поиска." : "Сохранённых текстов для поиска нет.");
    await loadInfo();
  } catch (e) { D.scope = name + ' · ' + e.message; fail(e); }
  finally { D.busy = false; renderDiscover(); }
}
window.analyzeProxySessions = analyzeProxySessions;
$('#d-sessions').addEventListener('click', () => {
  if (D.busy) return;
  if (D.sessionTarget) { delete D.views.sessions; if (D.view === 'sessions') { D.report = null; D.scope = ''; D.selected.clear(); } }
  D.sessionTarget = null;
  showDiscoverView('sessions');
});
$('#events-scan-all').addEventListener('click', () => analyzeProxySessions());
if ($('#settings-scan-sessions')) $('#settings-scan-sessions').addEventListener('click', () => analyzeProxySessions());
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
  F.enforced.closest("label").hidden = !global;
  F.enforced.disabled = !global;
  if (!global) F.enforced.checked = false;
  const keep = F.action.value === 'keep';
  // У шаблона тип задают слоты {SVC}.
  F.kind.disabled = keep || F.ptype.value === 'template';
  F.strategy.disabled = keep;
  F.kind.closest('label').hidden = F.kind.disabled;
  F.strategy.closest('label').hidden = F.strategy.disabled;
  F.pattern.setCustomValidity('');
}
for (const n of ['ptype', 'scope', 'action']) rf[n].addEventListener('change', syncRuleForm);
syncRuleForm();

const ruleEditor = $('#rule-editor');
$('#rules-new').onclick = () => {
  rf.reset();
  F.scope.value = ruleScope;
  F.action.value = selectedRuleState().value === 'keep' ? 'keep' : 'mask';
  $('#rf-error').textContent = '';
  F.kind.setCustomValidity('');
  syncRuleForm();
  ruleEditor.showModal();
};
$('#rf-cancel').onclick = () => ruleEditor.close();
F.kind.addEventListener('input', () => F.kind.setCustomValidity(''));
F.pattern.addEventListener('input', () => F.pattern.setCustomValidity(''));
rf.addEventListener('submit', async (event) => {
  event.preventDefault();
  const keep = F.action.value === 'keep';
  const scope = F.scope.value;
  const rule = {
    kind: keep ? 'KEEP' : normKind(F.kind.value),
    action: F.action.value,
    [F.ptype.value]: F.pattern.value,
    case_sensitive: F.case_sensitive.checked,
    enforced: F.enforced.checked,
    note: F.note.value.trim(),
  };
  if (!keep && F.strategy.value) rule.strategy = F.strategy.value;
  if (!rule.kind && F.kind.disabled) rule.kind = 'CUSTOM';
  F.kind.setCustomValidity(rule.kind ? '' : tr("Укажите тип токена латиницей, например SVC"));
  F.pattern.setCustomValidity(rule[F.ptype.value].trim() ? '' : tr("Введите значение для поиска"));
  if (!rf.reportValidity()) return;
  const submit = rf.querySelector('[type=submit]');
  if (submit.disabled) return;
  submit.disabled = true;
  $('#rf-error').textContent = '';
  try {
    await api('/api/v1/rules', {method:'POST', body:{scope, rule}});
    chooseRuleScope(scope);
    if (selectedRuleState().value !== 'all') selectRuleState(rule.action);
    if (!ruleSearchText(rule).includes($('#rules-query').value.trim().toLocaleLowerCase())) $('#rules-query').value = '';
    ruleEditor.close();
    toast(tr("Правило добавлено: {{0}}", [SCOPE_TITLES[scope].toLowerCase()]));
    await afterRulesChanged();
  } catch (error) {
    if (ruleEditor.open) $('#rf-error').textContent = error.message;
    else fail(error);
  } finally {
    submit.disabled = false;
  }
});

function rulePattern(r) {
  if (r.value != null) return [tr("значение"), r.value];
  if (r.template != null) return [tr("шаблон"), r.template];
  return [tr("Регулярное выражение"), r.regex];
}

const selectedRuleState = () => $('#rules-state input:checked');
const scopeInputs = [...$('#rules-scope').querySelectorAll('input')];
let ruleScope = sessionStorage.getItem('kaponir-rules-scope');
let ruleSection = 'rules';

function chooseRuleScope(scope, remember = true) {
  const input = scopeInputs.find(input => input.value === scope);
  if (!input) return;
  ruleScope = input.value;
  input.checked = true;
  if (remember) sessionStorage.setItem('kaponir-rules-scope', ruleScope);
  $('#rule-scope-title').textContent = SCOPE_TITLES[ruleScope];
  $('#rule-scope-title').title = input.parentElement.title;
  for (const group of $('#rules-domains').children) group.hidden = group.dataset.scope !== ruleScope;
  filterRules();
}
$('#rules-scope').addEventListener('change', event => chooseRuleScope(event.target.value));

const sectionTabs = [...$('#rule-sections').querySelectorAll('[role=tab]')];
const nativeFocusGroup = 'focusGroup' in HTMLElement.prototype;
function showRuleSection(section) {
  if (section === ruleSection) return;
  ruleSection = section;
  sessionStorage.setItem('kaponir-rules-section', section);
  for (const tab of sectionTabs) {
    const active = tab.dataset.section === section;
    tab.setAttribute('aria-selected', String(active));
    tab.toggleAttribute('focusgroupstart', active);
    if (nativeFocusGroup) tab.removeAttribute('tabindex');
    else tab.tabIndex = active ? 0 : -1;
    $('#scope-' + tab.dataset.section).hidden = !active;
  }
  $('#rules-new').hidden = section !== 'rules';
  if ($('#rules-list table').tBodies.length) filterRules();
}
for (const tab of sectionTabs) {
  tab.addEventListener('focus', () => showRuleSection(tab.dataset.section));
  tab.addEventListener('click', () => showRuleSection(tab.dataset.section));
}
if (!nativeFocusGroup) $('#rule-sections').addEventListener('keydown', event => {
  const index = sectionTabs.indexOf(event.target);
  const next = {ArrowLeft:1-index, ArrowRight:1-index, Home:0, End:1}[event.key];
  if (index < 0 || next === undefined) return;
  event.preventDefault();
  sectionTabs[next].focus();
});
if (nativeFocusGroup) for (const tab of sectionTabs) tab.removeAttribute('tabindex');
if (sessionStorage.getItem('kaponir-rules-section') === 'domains') showRuleSection('domains');

function selectRuleState(value) {
  $('#rules-state input[value="' + value + '"]').checked = true;
}
function ruleSearchText(rule) {
  return [rule.id, rulePattern(rule)[1], rule.kind, rule.kind && kindLabel(rule.kind), rule.note]
    .filter(Boolean).join(' ').toLocaleLowerCase();
}
function filterRules() {
  const query = $('#rules-query').value.trim().toLocaleLowerCase();
  const state = selectedRuleState().value;
  let count = 0, total = 0;
  const otherMatches = [];
  for (const group of $('#rules-list table').tBodies) {
    const scope = group.dataset.scope;
    group.hidden = scope !== ruleScope;
    let matches = 0;
    for (const row of group.rows) {
      row.hidden = (state !== 'all' && state !== row.dataset.state) || !row.dataset.ruleSearch.includes(query);
      if (!row.hidden) matches++;
    }
    const stats = $('#rules-scope input[value="' + scope + '"]').parentElement.querySelector('small');
    stats.textContent = query && ruleSection === 'rules'
      ? tr("Совпадений · {{0}} из {{1}}", [number(matches), number(group.rows.length)])
      : tr("Правил {{0}} · доменов {{1}}", [number(group.rows.length), number(group.dataset.domains)]);
    if (scope === ruleScope) {
      count = matches;
      total = group.rows.length;
    } else if (query && matches) {
      otherMatches.push([scope, matches]);
    }
  }
  $('#rules-match-count').textContent = tr("Показано {{0}} из {{1}}", [number(count), number(total)]);
  $('#rules-clear').hidden = !query && state === 'mask';
  $('#rules-list .table-wrap').hidden = count === 0;
  const empty = $('#rules-empty');
  empty.hidden = count > 0;
  if (count) return;
  empty.querySelector('strong').textContent = tr(total ? "В этой группе нет правил по выбранным фильтрам" : "В этой группе пока нет правил");
  const links = $('#rules-other-matches');
  links.replaceChildren();
  if (!query && total && state !== 'all') {
    links.append(h('button', {type:'button', class:'text-button', onclick:() => {
      selectRuleState('all'); filterRules(); selectedRuleState().focus();
    }}, tr("Показать все правила группы")));
  }
  if (otherMatches.length) links.append(h('p', {}, tr("Совпадения в других группах")),
    ...otherMatches.map(([scope, matches]) => h('button', {type:'button', class:'text-button', onclick:() => {
      chooseRuleScope(scope); $('#rules-query').focus();
    }}, SCOPE_TITLES[scope] + ' · ' + number(matches))));
}
function resetRuleFilters() {
  $('#rules-query').value = '';
  selectRuleState('mask');
  filterRules();
  $('#rules-query').focus();
}
$('#rules-clear').onclick = resetRuleFilters;
$('#rules-query').addEventListener('input', filterRules);
$('#rules-state').addEventListener('change', filterRules);

let ruleSortAscending = true;
function sortRuleRows(rows) {
  const collator = new Intl.Collator(I18N.language, {numeric:true, sensitivity:'base'});
  return rows.sort((a, b) => collator.compare(a.dataset.value, b.dataset.value) * (ruleSortAscending ? 1 : -1));
}
$('#rules-sort').onclick = () => {
  ruleSortAscending = !ruleSortAscending;
  $('#rules-sort').closest('th').setAttribute('aria-sort', ruleSortAscending ? 'ascending' : 'descending');
  $('#rules-sort-arrow').textContent = ruleSortAscending ? '↑' : '↓';
  for (const group of $('#rules-list table').tBodies)
    group.replaceChildren(...sortRuleRows(Array.from(group.rows)));
};

function downloadJson(text, name) {
  const url = URL.createObjectURL(new Blob([text], {type:'application/json'}));
  const link = h('a', {href:url, download:name});
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$('#rules-export').onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const sets = await api('/api/v1/rules');
    const rules = Object.fromEntries(sets.map(({scope, file}) => [scope, file]));
    downloadJson(JSON.stringify({format:'kaponir-rules', version:1, rules}, null, 2) + '\n', 'kaponir-rules.json');
    toast(tr("Файл со всеми правилами готов"));
  } catch (error) { fail(error); }
  finally { button.disabled = !info.editable; }
};

const ruleDelete = $('#rule-delete');
let deleteTarget;
function confirmRuleDelete(set, index, value, fromAudit = false) {
  deleteTarget = {scope:set.scope, index, revision:set.revision, fromAudit};
  const memory = set.scope === 'memory';
  $('#rule-delete-title').textContent = tr(memory ? "Удалить значение из памяти?" : "Удалить правило?");
  ruleDelete.querySelector('[value=delete]').textContent = tr(memory ? "Удалить из памяти" : "Удалить правило");
  $('#rule-delete-scope').textContent = memory
    ? tr("Kaponir забудет это значение во всех сессиях. Старые токены по-прежнему можно раскрыть.")
    : tr("Правило будет удалено из области «{{0}}».", [scopeLabel(set.scope)]);
  $('#rule-delete-value').textContent = value;
  $('#rule-delete-error').textContent = '';
  ruleDelete.showModal();
}
ruleDelete.querySelector('form').addEventListener('submit', async (event) => {
  if (event.submitter?.value !== 'delete') return;
  event.preventDefault();
  const button = event.submitter;
  if (button.disabled) return;
  const {scope, index, revision, fromAudit} = deleteTarget;
  const memory = scope === 'memory';
  const focusList = () => $(fromAudit ? '#audit-refresh' : '#rules-query').focus({preventScroll:true});
  button.disabled = true;
  try {
    if (memory) {
      await api('/api/v1/ui/rules/audit/memory', {method:'DELETE', body:{ids:[index],revision}});
    } else {
      await api('/api/v1/rules/' + scope + '/' + index + '?revision=' + encodeURIComponent(revision), {method:'DELETE'});
    }
    ruleDelete.close();
    if (!memory) await afterRulesChanged();
    if (fromAudit) await loadAudit();
    focusList();
    toast(tr(memory ? "Значение удалено из памяти сессий" : "Правило удалено"));
  } catch (error) {
    if (['rule_list_changed', 'memory changed; refresh the audit'].includes(error.messageKey)) {
      ruleDelete.close();
      await (fromAudit ? loadAudit() : loadRules()).catch(fail);
      focusList();
      fail(error);
    } else if (ruleDelete.open) {
      $('#rule-delete-error').textContent = error.message;
    } else {
      fail(error);
    }
  } finally { button.disabled = false; }
});

function ruleRow(set, rule, index) {
  const [type, value] = rulePattern(rule);
  const action = rule.action || 'mask';
  const state = rule.disabled ? 'disabled' : action;
  const flags = [type];
  if (rule.enforced) flags.push(tr("Запрет переопределения"));
  if (rule.case_sensitive) flags.push(tr("Учитывать регистр"));
  if (rule.strategy) flags.push(rule.strategy === 'variable' ? tr("Переменная") : rule.strategy.toUpperCase());
  const source = /^(learn-|review-|discovery-)/.test(rule.id || '');
  return h('tr', {id:'rule-'+set.scope+'-'+(rule.id || 'index-'+index), tabindex:-1,
      dataset:{ruleSearch:ruleSearchText(rule), value, scope:set.scope, state}},
    h('td', {class:'rule-subject'}, h('strong', {class:'mono', title:rule.id || ''}, value),
      h('small', {}, flags.join(' · ')), rule.note ? h('span', {class:'rule-note'}, rule.note) : null,
      source ? workButton('Исходные находки', () => openRuleFindings(rule.id, set.scope), 'text-button') : null),
    h('td', {}, h('span', {class:'rule-action '+state}, tr(action === 'keep' ? 'Оставлять открытым' : 'Маскировать')),
      rule.disabled ? h('small', {class:'rule-off'}, tr('Отключено')) : null),
    h('td', {}, action === 'mask' ? kindBadge(rule.kind) : '—'),
    h('td', {class:'rule-row-actions'}, h('button', {class:'text-button rule-remove', type:'button',
      'aria-label':tr("Удалить правило «{{0}}»", [value]), onclick:() => confirmRuleDelete(set, index, value)}, tr("Удалить"))));
}
let rulesLoad = 0;
async function loadRules() {
  const request = ++rulesLoad;
  const table = $('#rules-list table');
  const notice = $('#rules-notice');
  if (!info.editable) {
    table.replaceChildren(table.tHead);
    $('#rules-domains').replaceChildren();
    $('#rules-workspace').hidden = true;
    notice.textContent = tr("Этот сервер не управляет правилами (запущен без каталога правил).");
    notice.hidden = false;
    return;
  }
  let sets;
  try {
    sets = await api('/api/v1/rules');
  } catch (error) {
    if (request !== rulesLoad) return;
    notice.textContent = error.message;
    notice.hidden = false;
    throw error;
  }
  if (request !== rulesLoad) return;
  const groups = scopeInputs.map(input => {
    const scope = input.value;
    const set = sets.find(set => set.scope === scope) || {scope, file:{rule:[], corp:{domains:[]}}};
    input.parentElement.title = set.location || '';
    return set;
  });
  const domainForms = $('#rules-domains');
  const drafts = new Map([...domainForms.querySelectorAll('input')].map(input => [input.id, input.value]));
  domainForms.replaceChildren(...groups.map(set => domainPanel(set, drafts.get('domain-' + set.scope))));
  table.replaceChildren(table.tHead, ...groups.map(set => {
    const rows = (set.file.rule || []).map((rule, index) => ruleRow(set, rule, index));
    return h('tbody', {dataset:{scope:set.scope, domains:set.file.corp.domains.length}}, ...sortRuleRows(rows));
  }));
  if (!groups.some(set => set.scope === ruleScope)) {
    const initial = groups.find(set => set.file.rule?.some(r => !r.disabled && r.action !== 'keep')) ||
      groups.find(set => set.file.rule?.length || set.file.corp.domains.length);
    ruleScope = initial?.scope || 'personal';
  }
  chooseRuleScope(ruleScope, false);
  notice.hidden = true;
  $('#rules-workspace').hidden = false;
}

function domainPanel(set, draft = '') {
  const domains = set.file.corp.domains;
  const id = 'domain-' + set.scope;
  const input = h('input', {id, name:'domain', required:true, spellcheck:'false',
    placeholder:'company.example', value:draft});
  const preview = h('div', {class:'domain-preview'});
  const normalize = domain => domain.trim().replace(/^\.+|\.+$/g, '').toLowerCase();
  function updatePreview() {
    input.setCustomValidity('');
    const sample = normalize(input.value.trim() || domains[0] || 'company.example');
    const suffix = sample.split('.').slice(1).join('.');
    preview.hidden = !suffix || /[\s/:@]/.test(sample);
    preview.replaceChildren(h('span', {}, tr("Пример маскирования")), h('code', {}, 'git.' + sample),
      h('span', {}, '→'), h('code', {class:'masked'}, '[SUB_1].[DOM_1].' + suffix));
  }
  input.addEventListener('input', updatePreview);
  updatePreview();
  const form = h('form', {class:'domain-add', onsubmit:event => {
    event.preventDefault();
    const domain = normalize(input.value);
    if (!domain.includes('.') || /[\s/:@"']/.test(domain)) {
      input.setCustomValidity(tr("Введите домен без протокола и пути, например company.example"));
    } else if (domains.some(saved => normalize(saved) === domain)) {
      input.setCustomValidity(tr("Этот домен уже есть в выбранной группе"));
    }
    if (input.reportValidity()) save([...domains, domain], true);
  }}, h('label', {class:'field', for:id}, h('span', {}, tr("Домен")), input),
    h('button', {class:'primary', type:'submit'}, tr("Добавить домен")));
  const list = domains.length ? h('ul', {class:'scope-domain-list'}, ...domains.map(domain =>
    h('li', {}, h('code', {}, domain), h('button', {type:'button', class:'text-button',
      'aria-label':tr("Удалить ") + domain, onclick:() => save(domains.filter(d => d !== domain), false)}, tr("Удалить")))))
    : h('p', {class:'hint'}, tr("В этой группе пока нет доменов"));
  const panel = h('div', {class:'domain-editor', dataset:{scope:set.scope}}, form, list, preview);
  let saving = false;
  async function save(next, adding) {
    if (saving) return;
    saving = true;
    const controls = panel.querySelectorAll('input, button');
    for (const control of controls) control.disabled = true;
    try {
      const query = set.revision ? '?revision=' + encodeURIComponent(set.revision) : '';
      await api('/api/v1/rules/' + set.scope + '/domains' + query, {method:'PUT', body:{domains:next}});
      if (adding) input.value = '';
      await afterRulesChanged();
      toast(tr(adding ? "Домен добавлен в группу «{{0}}»" : "Домен удалён из группы «{{0}}»", [SCOPE_TITLES[set.scope]]));
    } catch (error) {
      if (error.messageKey === 'rule_list_changed') {
        await loadRules().catch(fail);
        toast(tr("Список доменов обновлён. Проверьте его и повторите действие."), true);
      } else fail(error);
    } finally {
      saving = false;
      for (const control of controls) control.disabled = false;
      if (document.activeElement === document.body && ruleScope === set.scope && ruleSection === 'domains' &&
          !$('#tab-rules').hidden && !$('#rules-catalog').hidden) document.getElementById(id)?.focus({preventScroll:true});
    }
  }
  return panel;
}

function renderPacks() {
  $('#packs').replaceChildren(info.packs.length
    ? h('table', {}, h('tbody', {}, info.packs.map((p) => h('tr', {},
      h('td', { class: 'mono', text: p.id }), h('td', {}, h('span', { class: 'tag', text: p.kind })),
      h('td', { class: 'num', text: 'v' + p.version }), h('td', { class: 'hint', text: tr(p.description) })))))
    : h('div', { class: 'empty', text: tr("Пакеты не загружены") }));
}

// ---------- старт ----------

function refreshFilePickers() {
  for (const input of document.querySelectorAll('input[type="file"]')) {
    let picker = input.closest('.file-picker');
    if (!picker) {
      picker = h('span', {class: 'file-picker'}, h('span', {class: 'file-button', 'aria-hidden': 'true'}),
        h('span', {class: 'file-name', 'aria-hidden': 'true'}));
      input.before(picker); picker.append(input);
      input.addEventListener('change', refreshFilePickers);
    }
    picker.querySelector('.file-button').textContent = tr(input.multiple ? 'Выбрать файлы' : 'Выбрать файл');
    const names = Array.from(input.files, file => file.name);
    const caption = picker.querySelector('.file-name');
    caption.textContent = names.length > 1 ? tr('Выбрано файлов: {{0}}', [names.length]) : names[0] || tr(input.multiple ? 'Файлы не выбраны' : 'Файл не выбран');
    caption.title = names.join(', ');
  }
}

async function startApp() {
  refreshFilePickers();
  try {
    await loadInfo();
  } catch (e) { fail(e); }
  const tab = location.hash.slice(1);
  showTab(tab || 'overview', false);
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
  Object.assign(PTYPE_HINTS, {
  value: [tr("Рога и Копыта"), tr("Точное значение по границам слова.")],
  template: ['{env:prod|stage|dev}-{SVC}-{hash}', tr("Слоты: {KIND} — чувствительная часть (заменяется токеном), {env:a|b} — перечисление, {hash}, {word}, {num}, {any} — каркас, который остаётся.")],
  regex: ['SEC-\\d{4,}', tr("Регулярное выражение (синтаксис Rust regex); совпадение маскируется целиком.")],
});
}
