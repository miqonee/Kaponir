'use strict';

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS(svg.namespaceURI, 'use');
  use.setAttribute('href', '#i-' + name); svg.setAttribute('aria-hidden', 'true'); svg.append(use); return svg;
}
const PAGE_META = {
  overview: ['РАБОТА ПРОКСИ', 'Обзор защиты', 'Что происходит между вашим приложением и моделью.'],
  connect: ['БЫСТРЫЙ СТАРТ', 'Поставщики и подключение', 'Добавьте ключ поставщика. Получите защищённый адрес API для приложения.'],
  s1: ['ИНТЕЛЛЕКТУАЛЬНАЯ ЗАЩИТА', 'Модель S1', 'Настройте дополнительную оценку кандидатов и контролируйте раскрытие данных.'],
  events: ['РАБОТА ПРОКСИ', 'События', 'Результаты обработки запросов без содержимого сообщений.'],
  rules: ['ПОЛИТИКА ЗАЩИТЫ', 'Правила', 'Укажите, какие данные важны именно для вашей компании.'],
  review: ['ПОЛИТИКА ЗАЩИТЫ', 'Разбор решений', 'Уточняйте спорные случаи. Прокси продолжает работать автоматически.'],
  mask: ['ИНСТРУМЕНТЫ', 'Лаборатория', 'Проверка детекторов и работа с чат-ботом через браузер.'],
  restore: ['ИНСТРУМЕНТЫ', 'Лаборатория', 'Верните исходные значения в обезличенный ответ.'],
  worker: ['АДАПТАЦИЯ', 'Фоновый анализ документов', 'Отдельная очередь: OCR и сбор кандидатов без ожидания в запросах агента.'],
  settings: ['УПРАВЛЕНИЕ', 'Настройки', 'Параметры пространства, диагностика и адаптация.'],
  discover: ['АДАПТАЦИЯ', 'Правила из документов', 'Поиск кандидатов, необязательная оценка S1 и подтверждение правил.'],
  setup: ['БЫСТРЫЙ СТАРТ', 'Настроим под вашу работу', 'Несколько вопросов. Любой шаг можно пропустить.'],
  metrics: ['ДИАГНОСТИКА', 'Технические показатели', 'Операции движка, состояние S1 и диагностика восстановления.'],
};
function panelHeading(name) {
  const [eyebrow, title, description] = PAGE_META[name];
  $('#page-eyebrow').textContent = tr(eyebrow); $('#page-title').textContent = tr(title);
  $('#page-description').textContent = tr(description); $('#breadcrumb').textContent = tr(title);
  $('#page-actions').hidden = !['overview', 'events'].includes(name);
  $('#lab-navigation').hidden = !['mask', 'restore'].includes(name);
  for (const button of $('#lab-navigation').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.go === name));
  $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false');
  document.title = tr(title) + ' · Kaponir';
}
for (const button of document.querySelectorAll('[data-go]')) button.addEventListener('click', () => showTab(button.dataset.go));
$('#menu-toggle').addEventListener('click', () => {
  const open = $('#sidebar').classList.toggle('open'); $('#menu-toggle').setAttribute('aria-expanded', String(open));
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') { $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); } });
document.addEventListener('click', e => { if (!e.target.closest('#sidebar,#menu-toggle')) { $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); } });
window.addEventListener('hashchange', () => { const name = location.hash.slice(1); if (TABS[name]) showTab(name); });

let dashboard = null, dashboardPending = false, eventFilter = 'all';
const CATEGORY_LABELS = ['Пароли и ключи', 'Персональные данные', 'Инфраструктура', 'Финансовые данные', 'Контакты', 'Другие значения'];
// Slots validated for adjacent categories on light and dark surfaces.
function categoryColors() {
  return getComputedStyle(document.documentElement).getPropertyValue('--category-colors').split(',').map(color => color.trim());
}
function number(value) { return new Intl.NumberFormat(I18N.language).format(value); }
function time(value, options) { return new Date(value).toLocaleString(I18N.language, options || { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
function periodBuckets() {
  const hours = Number($('#period').value), current = Math.floor(Date.now() / 3600000);
  const buckets = new Map((dashboard?.traffic?.hours || []).map(b => [b.hour, b]));
  return Array.from({ length: hours }, (_, i) => buckets.get(current - hours + 1 + i) || { hour: current - hours + 1 + i, requests: 0, errors: 0, blocked: 0, cancelled: 0, usage: { replacements: 0, restored: 0, unknown: 0, denied: 0, categories: [0,0,0,0,0,0] } });
}
function filteredEvents(applyFilter=true) {
  const cutoff = (Math.floor(Date.now()/3600000) - Number($('#period').value) + 1)*3600000;
  return (dashboard?.traffic?.events || []).filter(e => e.time_ms >= cutoff && (!applyFilter || eventFilter !== 'issues' || e.outcome !== 'ok' || e.usage.unknown || e.usage.denied));
}
function renderCards(buckets) {
  const sum = key => buckets.reduce((a,b) => a + (b[key] || 0), 0);
  const usage = key => buckets.reduce((a,b) => a + b.usage[key], 0);
  const cards = [
    ['Запросы через прокси', sum('requests'), 'Завершены, отклонены или прерваны', 'plug'],
    ['Скрыто значений', usage('replacements'), 'Замены в запросах прокси', 'shield'],
    ['Восстановлено', usage('restored'), 'Значения в ответах модели', 'refresh'],
    ['Ошибки обработки', sum('errors'), 'Прервано клиентом: {{0}}', 'list', sum('cancelled')],
  ];
  $('#summary-cards').replaceChildren(...cards.map(([title, value, caption, symbol, extra], i) =>
    h('article', { class: 'metric-card' + (i === 1 ? ' featured' : '') + (i === 3 && value > 0 ? ' attention' : '') },
      h('div', { class: 'metric-top' }, h('span', {}, tr(title)), icon(symbol)),
      h('strong', { class: 'metric-value' }, number(value)),
      h('span', { class: 'metric-caption' }, tr(caption, [number(extra || 0)])))));
  $('#summary-cards').setAttribute('aria-busy', 'false');
  $('#welcome-banner').hidden = (dashboard.traffic?.total.requests || 0) > 0;
}
function renderChart(buckets) {
  const chart = $('#traffic-chart');
  const groups = buckets.length > 24 ? Array.from({length: 7}, (_, i) => {
    const slice = buckets.slice(i*24, i*24+24);
    return {hour:slice[0].hour, requests:slice.reduce((n,b)=>n+b.requests,0)};
  }) : buckets;
  const hasRequests = groups.some(b => b.requests);
  const max = Math.max(1, ...groups.map(b => b.requests));
  const top = max <= 5 ? max : Math.ceil(max / 5) * 5;
  const bars = h('div', { class: 'chart-bars', role: 'group', 'aria-label': tr('График реальных запросов') });
  const tooltip = h('div', { class: 'chart-tooltip', role: 'status', hidden: true });
  for (const bucket of groups) {
    const period = time(bucket.hour*3600000, groups.length === 7 ? { month:'short', day:'numeric' } : { month:'short', day:'numeric', hour:'2-digit' });
    const value = tr('Запросов: {{0}}', [number(bucket.requests)]);
    const label = period + ' · ' + value;
    const showTip = () => {
      tooltip.replaceChildren(h('strong', {}, number(bucket.requests)), h('span', {}, period));
      tooltip.hidden = false;
    };
    bars.append(h('button', { class:'chart-bar', 'aria-label':label, onmouseenter:showTip, onfocus:showTip,
      onmouseleave:()=>{ tooltip.hidden=true; }, onblur:()=>{ tooltip.hidden=true; },
      onclick: () => { $('#traffic-period-note').textContent=label; } },
      h('span', { class:'chart-bar-fill', style:`height:${bucket.requests ? Math.max(3,bucket.requests/top*100) : 0}%` })));
  }
  const axis = h('div', { class:'chart-y-axis', 'aria-hidden':'true' }, h('span', {}, number(top)), h('span', {}, number(top/2)), h('span', {}, '0'));
  const labels = h('div', {class:'chart-labels'}, ...[groups[0], groups[Math.floor((groups.length-1)/2)], groups[groups.length-1]].map(b => h('span', {}, time(b.hour*3600000, groups.length===7 ? {day:'numeric',month:'short'} : {hour:'2-digit',minute:'2-digit'}))));
  chart.replaceChildren(h('div', {class:'chart-plot'}, axis, bars), labels, tooltip);
  if (!hasRequests) chart.append(h('div', {class:'chart-empty'}, icon('plug'), h('strong', {}, tr('Пока нет запросов')), h('span', {}, tr('Подключите приложение — график появится автоматически.')), h('button', {class:'text-button', 'data-go':'connect', onclick:()=>showTab('connect')}, tr('Подключить приложение'))));
  $('#traffic-table').replaceChildren(h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, tr('Время')), h('th', {}, tr('Запросы')))), h('tbody', {}, ...groups.map(b => h('tr', {}, h('td', {}, time(b.hour*3600000, groups.length===7 ? {day:'numeric',month:'short'} : {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})), h('td', {class:'num'}, number(b.requests)))))));
  $('#traffic-period-note').textContent = tr('В обработке: {{0}}', [number(dashboard.traffic.in_flight)]);
  const latencies = filteredEvents(false).map(e=>e.duration_ms).sort((a,b)=>a-b);
  $('#latency-note').textContent = latencies.length ? tr('p95 последних событий: {{0}} мс', [number(latencies[Math.ceil(latencies.length*.95)-1])]) : tr('Задержка появится после первого запроса');
}
function renderCategories(buckets) {
  const counts = CATEGORY_LABELS.map((_, i) => buckets.reduce((n,b)=>n+b.usage.categories[i],0));
  if (!counts.some(Boolean)) {
    $('#category-breakdown').replaceChildren(h('div', {class:'empty-state compact-empty'}, icon('shield'), h('strong', {}, tr('Пока нечего скрывать')), h('p', {}, tr('Здесь появятся типы замен после первого запроса.'))));
    return;
  }
  const max = Math.max(1, ...counts);
  const colors = categoryColors();
  $('#category-breakdown').replaceChildren(...CATEGORY_LABELS.map((label,i) => h('div', {style:`--category-color:${colors[i]}`},
    h('div',{class:'category-line'},h('span',{},h('i',{class:'category-dot','aria-hidden':'true'}),tr(label)),h('strong',{},number(counts[i]))),
    h('div',{class:'category-track','aria-hidden':'true'},h('div',{class:'category-fill',style:`width:${counts[i]/max*100}%`})) )));
}
window.addEventListener('kaponir-themechange', () => { if (dashboard) renderCategories(periodBuckets()); });
function eventOutcome(e) {
  if (e.outcome === 'ok' && (e.usage.unknown || e.usage.denied)) return 'needs_review';
  return e.outcome;
}
function eventsTable(events, compact=false) {
  if (!events.length) return h('div',{class:'empty-state'},icon('list'),h('strong',{},tr('Здесь появятся запросы прокси')),h('p',{},tr('Отправьте запрос из подключённого приложения. Ручные проверки здесь не отображаются.')));
  const titles = compact ? ['Время','Провайдер','Скрыто','Результат'] : ['Время','Провайдер','Скрыто','Восстановлено','Длительность','Результат'];
  const outcomes={ok:'Обработан',error:'Ошибка',restore_error:'Ошибка восстановления',cancelled:'Прерван',needs_review:'Требует проверки'};
  return h('table',{},h('thead',{},h('tr',{},...titles.map(t=>h('th',{},tr(t))))),h('tbody',{},...events.map(e=>h('tr',{},
    h('td',{class:'mono'},time(e.time_ms)),h('td',{},e.provider==='openai'?'OpenAI-compatible':'Anthropic',e.connection_id?h('small',{class:'event-connection',title:e.connection_id},e.connection_id):null),
    h('td',{class:'mono'},number(e.usage.replacements)),
    ...(compact?[]:[h('td',{class:'mono'},number(e.usage.restored)),h('td',{class:'mono'},number(e.duration_ms)+' '+tr('мс'))]),
    h('td',{},h('span',{class:'event-status '+eventOutcome(e),title:tr('HTTP {{0}} · передача начата: {{1}}',[e.status||'—',e.sent?tr('да'):tr('нет')])},tr(outcomes[eventOutcome(e)]||'Ошибка')))))));
}
function renderDashboard() {
  if (!dashboard?.traffic) return;
  const buckets = periodBuckets(); renderCards(buckets); renderChart(buckets); renderCategories(buckets);
  $('#recent-events').replaceChildren(eventsTable(filteredEvents(false).slice(0,5),true));
  $('#events-table').replaceChildren(eventsTable(filteredEvents()));
  $('#updated-at').textContent = tr('Обновлено {{0}} · с запуска сервера',[time(Date.now())]);
}
function setConnected(online) {
  $('#connection-state').classList.toggle('offline',!online);
  $('#connection-state span').textContent=tr(online?'Прокси доступен':'Нет соединения');
}
async function loadDashboard() {
  if (dashboardPending) return;
  dashboardPending = true;
  try {
    dashboard=await api('/api/v1/metrics');
    setConnected(true);$('#connection-error').hidden=true;renderDashboard();
  } catch (error) {
    setConnected(false); $('#connection-error').hidden=false;
    $('#connection-error').textContent=tr('Не удалось обновить данные. Показанные ранее значения могут быть устаревшими.');
    throw error;
  } finally { dashboardPending=false; }
}
for(const id of ['overview-refresh','events-refresh']) $('#'+id).addEventListener('click',()=>loadDashboard().catch(fail));
$('#period').addEventListener('change',renderDashboard);
for(const button of $('#events-filter').querySelectorAll('button')) button.addEventListener('click',()=>{
  eventFilter=button.dataset.filter;
  for(const b of $('#events-filter').querySelectorAll('button')) b.setAttribute('aria-pressed',String(b===button));
  if(dashboard) $('#events-table').replaceChildren(eventsTable(filteredEvents()));
});
setInterval(()=>{if(!document.hidden && ['overview','events'].includes(location.hash.slice(1)))loadDashboard().catch(()=>{});},10000);

function copyCode(code) {
  return h('div',{class:'code-box'},h('pre',{},code),h('button',{'aria-label':tr('Копировать'),title:tr('Копировать'),onclick:()=>copy(code).then(()=>toast(tr('Скопировано'))).catch(fail)},icon('copy')));
}
function renderConnection() { return loadProviders(); }
function renderSettings() {
  const modes={off:'Выключен',shadow:'Теневой режим',active:'Активный режим'};
  $('#settings-info').replaceChildren(...[
    ['Версия',info.version||'—'],['S1',tr(modes[info.s1_mode]||'Выключен')],
    ['Защита API-токеном',tr(info.api_token_required?'Включена':'Локальный доступ')],
    ['Сохранённых соответствий',number(info.stats?.entries||0)],
  ].map(([key,value])=>h('div',{class:'setting-row'},h('span',{},tr(key)),h('strong',{},value))));
}

const W={step:0, mode:'proxy', domains:'', projects:'', plan:null, busy:false};
function captureWizard() {
  const selected=$('input[name="setup-mode"]:checked'); if(selected)W.mode=selected.value;
  if($('#setup-domains'))W.domains=$('#setup-domains').value;
  if($('#setup-projects'))W.projects=$('#setup-projects').value;
}
function setupAnswers() { return {domains:W.domains.split(/[\s,;]+/).filter(Boolean), projects:W.projects.split(/\n/).map(x=>x.trim()).filter(Boolean)}; }
function renderWizard() {
  $('#wizard-progress').replaceChildren(...['Как использовать','Ваши данные','Проверка'].map((label,i)=>h('span',{class:'wizard-step'+(i<=W.step?' active':'')},`0${i+1} · `+tr(label))));
  const body=$('#wizard-body');$('#wizard-back').hidden=W.step===0;$('#wizard-next').textContent=tr(W.step===2?'Применить настройки':'Продолжить');
  $('#wizard-next').disabled=W.busy || (W.step===2&&!info.editable);
  if(W.step===0)body.replaceChildren(h('span',{class:'eyebrow'},tr('ШАГ 1 ИЗ 3')),h('h2',{},tr('Как вы будете использовать Kaponir?')),h('p',{},tr('Это поможет выбрать инструкции. Все инструменты останутся доступны.')),
    h('div',{class:'choice-grid'},...[
      ['proxy','Через API-прокси','Автоматическая защита приложений и агентов'],
      ['manual','Через чат в браузере','Подготовка текста и восстановление ответа'],
    ].map(([value,title,desc])=>{const radio=h('input',{class:'sr-only',type:'radio',name:'setup-mode',value});radio.checked=W.mode===value;return h('label',{class:'choice'},radio,h('span',{class:'choice-top'},h('span',{class:'choice-icon'},icon(value==='proxy'?'plug':'flask')),icon('check')),h('strong',{},tr(title)),h('span',{},tr(desc)));})));
  if(W.step===1){
    body.replaceChildren(h('span',{class:'eyebrow'},tr('ШАГ 2 ИЗ 3')),h('h2',{},tr('Что важно скрывать у вас?')),h('p',{},tr('Базовые детекторы уже включены. Добавьте известные вам корпоративные значения или оставьте поля пустыми.')),
      h('label',{},tr('Внутренние домены'),h('input',{id:'setup-domains',placeholder:'corp.example, internal.example',value:W.domains,spellcheck:'false'})),
      h('label',{},tr('Конфиденциальные названия проектов'),h('textarea',{id:'setup-projects',placeholder:tr('Одно название на строку'),spellcheck:'false'},W.projects)),
      h('p',{class:'hint'},tr('Укажите только конфиденциальные названия. Эти значения будут маскироваться во всех текстах по точному совпадению.')));
  }
  if(W.step===2){
    body.replaceChildren(h('span',{class:'eyebrow'},tr('ШАГ 3 ИЗ 3')),h('h2',{},tr('Проверьте перед сохранением')),h('p',{},tr('Добавим только личные MASK-правила. Существующие настройки сохранятся, внешние модели не подключаются.')),
      h('div',{class:'wizard-preview'},h('strong',{},tr('Домены: {{0}}',[W.plan?.domains.length||0])),h('p',{},W.plan?.domains.join(', ')||tr('Не добавляются')),
        h('strong',{},tr('Проекты: {{0}}',[W.plan?.rules.length||0])),h('p',{},W.plan?.rules.map(r=>r.value).join(', ')||tr('Не добавляются'))));
    if(!info.editable)body.append(h('p',{class:'err'},tr('Этот сервер не управляет правилами (запущен без каталога правил).')));
  }
}
$('#wizard-back').addEventListener('click',()=>{captureWizard();W.step=Math.max(0,W.step-1);renderWizard();});
$('#wizard-skip').addEventListener('click',()=>{captureWizard();showTab('overview');});
$('#setup-form').addEventListener('submit',async e=>{
  e.preventDefault();if(W.busy)return;captureWizard();W.busy=true;$('#wizard-next').disabled=true;
  try {
    if(W.step===0)W.step=1;
    else if(W.step===1){const result=await api('/api/v1/ui/setup',{method:'POST',body:setupAnswers()});W.plan=result.plan;W.step=2;}
    else {await api('/api/v1/ui/setup',{method:'POST',body:{...setupAnswers(),apply:true}});await loadInfo();toast(tr('Настройки применены'));W.step=0;showTab(W.mode==='manual'?'mask':'connect');return;}
    renderWizard();
  }catch(error){fail(error);}finally{W.busy=false;$('#wizard-next').disabled=W.step===2&&!info.editable;}
});
Object.assign(TABS,{overview:loadDashboard,events:loadDashboard,connect:renderConnection,settings:()=>{renderSettings();},setup:renderWizard});

for(const input of $('#language-switch').querySelectorAll('input')) input.checked=input.value===I18N.language;
$('#language-switch').addEventListener('change',async e=>{
  captureWizard();I18N.setLanguage(e.target.value);relocalizeConstants();I18N.apply();
  renderAdhoc();renderMask();renderDiscover();syncRuleForm();
  await loadInfo().catch(fail);renderSettings();renderConnection();if(S1.data)renderS1();if(dashboard)renderDashboard();
  showTab(location.hash.slice(1)||'overview');
});

// The same versioned JSON is accepted by `kaponir config import`.
$('#config-export').addEventListener('click', async () => {
  const button = $('#config-export'); button.disabled = true;
  try {
    const bundle = await api('/api/v1/ui/config/export', {method:'POST',body:{include_secrets:$('#config-secrets').checked}});
    const text = JSON.stringify(bundle, null, 2);
    $('#config-json').value = text;
    const url = URL.createObjectURL(new Blob([text], {type:'application/json'}));
    const link = h('a', {href:url,download:'kaponir-settings.json'});
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('#config-status').textContent = tr('Настройки экспортированы');
  } catch(e) { $('#config-status').textContent = e.message; fail(e); } finally { button.disabled = false; }
});
$('#config-file').addEventListener('change', async e => {
  const file = e.target.files[0]; if (!file) return;
  try {
    if (file.size > 1024 * 1024) throw new Error(tr('Файл настроек больше 1 МБ'));
    $('#config-json').value = await file.text();
    $('#config-status').textContent = '';
  } catch(e) { fail(e); }
});
$('#config-import').addEventListener('click', async () => {
  const button = $('#config-import'); button.disabled = true;
  $('#config-status').textContent = ''; 
  try {
    let bundle;
    try { bundle = JSON.parse($('#config-json').value); }
    catch { throw new Error(tr('Некорректный JSON настроек')); }
    await api('/api/v1/ui/config/import', {method:'POST',body:bundle});
    window.dispatchEvent(new Event('kaponir-settings-imported'));
    await loadInfo();
    renderSettings();
    $('#config-json').value = '';
    $('#config-file').value = '';
    $('#config-status').textContent = tr('Настройки применены. Перезапустите сервер для обновления всех параметров, включая адрес и доступ.');
  } catch(e) { $('#config-status').textContent = e.message; fail(e); } finally { button.disabled = false; }
});
