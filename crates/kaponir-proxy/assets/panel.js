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
  return Array.from({ length: hours }, (_, i) => buckets.get(current - hours + 1 + i) || {
    hour: current - hours + 1 + i,
    requests: 0,
    errors: 0,
    blocked: 0,
    cancelled: 0,
    usage: { replacements: 0, cached_replacements: 0, restored: 0, unknown: 0, denied: 0, uncertain: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, categories: [0,0,0,0,0,0] },
    timings_sum: { mask_us: 0, classifier_us: 0, upstream_us: 0, restore_us: 0 },
  });
}
function filteredEvents(applyFilter=true) {
  const cutoff = (Math.floor(Date.now()/3600000) - Number($('#period').value) + 1)*3600000;
  return (dashboard?.traffic?.events || []).filter(e => e.time_ms >= cutoff && (!applyFilter || eventFilter !== 'issues' || e.outcome !== 'ok' || e.usage.unknown || e.usage.denied || e.usage.uncertain || e.error));
}

function activeArchMode() {
  const s1Provider = S1?.data?.settings?.provider || (info.s1_mode && info.s1_mode !== 'off' ? 'active' : 'none');
  if (s1Provider === 'http') return 'byo_llm';
  if (s1Provider === 'ngram') return 'local_cpu';
  if (info.s1_mode && info.s1_mode !== 'off') return 'local_cpu';
  return 'no_llm';
}

async function quickSwitchMode(modeKey) {
  if (modeKey === 'byo_llm') {
    showTab('s1');
    return;
  }
  if (!info.editable) {
    showTab('s1');
    return;
  }
  try {
    const current = S1.data || await api('/api/v1/ui/s1');
    const settings = { ...current.settings };
    if (modeKey === 'no_llm') {
      settings.provider = 'none';
    } else if (modeKey === 'local_cpu') {
      settings.provider = 'ngram';
      settings.mode = 'active';
    }
    S1.data = await api('/api/v1/ui/s1', { method: 'PUT', body: { settings, key: null, clear_key: false } });
    await loadInfo();
    renderSystemPulse();
    renderWorkflowOverview();
    toast(modeKey === 'no_llm'
      ? tr("Включён чистый локальный режим (No-LLM): работают только быстрые детекторы и правила")
      : tr("Включён автономный режим CPU (локальный классификатор S1 + детекторы)"));
  } catch (e) { fail(e); }
}

function computeProxyOverheadMs() {
  const e = dashboard?.engine;
  const maskP50 = (e?.mask?.p50_us || 0) / 1000;
  const restoreP50 = (e?.restore?.p50_us || 0) / 1000;
  const maskP95 = (e?.mask?.p95_us || 0) / 1000;
  const restoreP95 = (e?.restore?.p95_us || 0) / 1000;
  const maskP99 = (e?.mask?.p99_us || 0) / 1000;
  const restoreP99 = (e?.restore?.p99_us || 0) / 1000;
  return {
    p50: maskP50 + restoreP50,
    p95: maskP95 + restoreP95,
    p99: maskP99 + restoreP99,
  };
}

function renderSystemPulse() {
  const pulse = $('#system-pulse');
  if (!pulse) return;
  const mode = activeArchMode();
  const modeLabels = {
    no_llm: tr("Чистый режим (No-LLM)"),
    local_cpu: tr("Автономный CPU (Qwen3 / n-gram)"),
    byo_llm: tr("Корпоративный BYO LLM / S1"),
  };
  const ov = computeProxyOverheadMs();
  const openCount = info?.stats?.open_journal || 0;
  const uniqueCount = info?.unique_entities ?? info?.stats?.entries ?? 0;
  const rulesCount = info?.rules_count ?? 0;

  pulse.replaceChildren(
    h('span', { class: 'pulse-item ok' },
      h('span', { class: 'pulse-label', text: tr("Прокси:") }),
      h('strong', { text: tr("Активен") })
    ),
    h('button', { type: 'button', class: 'pulse-item', onclick: () => showTab('s1'), title: tr("Настроить режим классификатора и S1") },
      h('span', { class: 'pulse-label', text: tr("Режим:") }),
      h('strong', { text: modeLabels[mode] })
    ),
    h('button', { type: 'button', class: 'pulse-item', onclick: () => showTab('metrics'), title: tr("Чистое время работы Kaponir без учёта ожидания внешней LLM") },
      h('span', { class: 'pulse-label', text: tr("Чистый оверхед:") }),
      h('strong', { text: `P50 ${ov.p50.toFixed(2)} ${tr("мс")} · P95 ${ov.p95.toFixed(2)} ${tr("мс")}` })
    ),
    h('button', { type: 'button', class: 'pulse-item', onclick: () => showTab('rules') },
      h('span', { class: 'pulse-label', text: tr("Под защитой:") }),
      h('strong', { text: tr("{{0}} сущн. · {{1}} прав.", [number(uniqueCount), number(rulesCount)]) })
    ),
    h('button', { type: 'button', class: 'pulse-item ' + (openCount > 0 ? 'warn' : ''), onclick: () => showTab('review') },
      h('span', { class: 'pulse-label', text: tr("На разборе:") }),
      h('strong', { text: number(openCount) })
    ),
    h('button', { type: 'button', class: 'pulse-item', onclick: () => showTab('worker') },
      h('span', { class: 'pulse-label', text: tr("Фоновый анализ:") }),
      h('strong', { text: tr("Qwen3 CPU / Сессии") })
    )
  );
}
window.renderSystemPulse = renderSystemPulse;

function renderWorkflowOverview() {
  const el = $('#workflow-overview');
  if (!el) return;
  const mode = activeArchMode();
  const openCount = info?.stats?.open_journal || 0;
  el.replaceChildren(
    h('article', { class: 'workflow-panel' },
      h('h3', { text: tr("Сквозной сценарий работы (Workflow)") }),
      h('p', { text: tr("От подключения прокси до прозрачного контроля каждого запроса в LLM.") }),
      h('div', { class: 'workflow-steps' },
        h('button', { type: 'button', class: 'workflow-step', onclick: () => showTab('connect') },
          h('span', { class: 'step-idx', text: '01' }),
          h('strong', { text: tr("Вход и прокси") }),
          h('span', { text: tr("Подключите API-ключ или подайте документы") })
        ),
        h('button', { type: 'button', class: 'workflow-step', onclick: () => showTab('discover') },
          h('span', { class: 'step-idx', text: '02' }),
          h('strong', { text: tr("Фоновый поиск") }),
          h('span', { text: tr("Анализ сессий (@sessions) и документов на CPU") })
        ),
        h('button', { type: 'button', class: 'workflow-step', onclick: () => showTab('review') },
          h('span', { class: 'step-idx', text: '03' }),
          h('strong', { text: tr("Разбор решений") }),
          h('span', { text: openCount ? tr("Ожидают подтверждения: {{0}}", [number(openCount)]) : tr("Авторазбор и триаж в 1 клик") })
        ),
        h('button', { type: 'button', class: 'workflow-step', onclick: () => showTab('events') },
          h('span', { class: 'step-idx', text: '04' }),
          h('strong', { text: tr("Инспектор запросов") }),
          h('span', { text: tr("Проверка обезличенного текста в LLM") })
        )
      )
    ),
    h('article', { class: 'modes-panel' },
      h('h3', { text: tr("Архитектурный режим работы") }),
      h('p', { text: tr("Выберите баланс автономности: чистые алгоритмы, локальная микро-LLM на CPU или внешняя модель.") }),
      h('div', { class: 'mode-cards' },
        h('button', { type: 'button', class: 'mode-card' + (mode === 'no_llm' ? ' active' : ''), onclick: () => quickSwitchMode('no_llm') },
          h('span', { class: 'mode-tag', text: 'No-LLM' }),
          h('strong', { text: tr("Чистый локальный") }),
          h('span', { text: tr("Только быстрые локальные алгоритмы и правила, 0 вызовов нейросетей") })
        ),
        h('button', { type: 'button', class: 'mode-card' + (mode === 'local_cpu' ? ' active' : ''), onclick: () => quickSwitchMode('local_cpu') },
          h('span', { class: 'mode-tag', text: 'CPU Micro-LLM' }),
          h('strong', { text: tr("Автономный CPU") }),
          h('span', { text: tr("Самообучаемый n-gram + текстовая модель Qwen3-0.6B/0.8B на CPU") })
        ),
        h('button', { type: 'button', class: 'mode-card' + (mode === 'byo_llm' ? ' active' : ''), onclick: () => quickSwitchMode('byo_llm') },
          h('span', { class: 'mode-tag', text: 'BYO LLM / S1' }),
          h('strong', { text: tr("Корпоративная LLM") }),
          h('span', { text: tr("DeepSeek / Qwen / OpenAI API или SystemOne SOS choice") })
        )
      )
    )
  );
}

function renderOverheadPanel(buckets) {
  const panel = $('#overhead-panel');
  if (!panel) return;
  const ov = computeProxyOverheadMs();
  const events = filteredEvents(false);
  let maskUs = 0, s1Us = 0, restoreUs = 0, upstreamUs = 0;
  if (events.length) {
    for (const ev of events) {
      maskUs += ev.timings?.mask_us || 0;
      s1Us += ev.timings?.classifier_us || 0;
      restoreUs += ev.timings?.restore_us || 0;
      upstreamUs += ev.timings?.upstream_us || Math.max(0, (ev.duration_ms || 0) * 1000 - (ev.timings?.mask_us || 0) - (ev.timings?.restore_us || 0));
    }
    maskUs /= events.length;
    s1Us /= events.length;
    restoreUs /= events.length;
    upstreamUs /= events.length;
  } else {
    maskUs = dashboard?.engine?.mask?.p50_us || 350;
    s1Us = dashboard?.engine?.s1?.p50_us || 0;
    restoreUs = dashboard?.engine?.restore?.p50_us || 180;
    upstreamUs = 850000;
  }
  const totalUs = Math.max(1, maskUs + s1Us + restoreUs + upstreamUs);
  const pct = (u) => Math.max(1.5, Math.min(96, (u / totalUs) * 100)).toFixed(1) + '%';

  panel.replaceChildren(
    h('div', { class: 'overhead-head' },
      h('div', {},
        h('h2', { text: tr("Чистый оверхед прокси и водопад задержек (Latency Waterfall)") }),
        h('p', { text: tr("Разделение локальной обработки Kaponir (маскирование + демаскировка) и времени ожидания внешней LLM.") })
      ),
      h('div', { class: 'percentile-badges' },
        h('span', { class: 'p-badge highlight' }, h('span', { text: 'P50 (медиана):' }), `${ov.p50.toFixed(2)} ${tr("мс")}`),
        h('span', { class: 'p-badge' }, h('span', { text: 'P95:' }), `${ov.p95.toFixed(2)} ${tr("мс")}`),
        h('span', { class: 'p-badge' }, h('span', { text: 'P99:' }), `${ov.p99.toFixed(2)} ${tr("мс")}`)
      )
    ),
    h('div', { class: 'waterfall-bar', role: 'img', 'aria-label': tr("Водопад задержек запроса") },
      h('div', { class: 'wf-seg mask', style: `width:${pct(maskUs)}`, title: `${tr("Маскирование")}: ${(maskUs / 1000).toFixed(2)} ${tr("мс")}` }),
      s1Us > 0 ? h('div', { class: 'wf-seg s1', style: `width:${pct(s1Us)}`, title: `${tr("Классификатор S1")}: ${(s1Us / 1000).toFixed(2)} ${tr("мс")}` }) : null,
      h('div', { class: 'wf-seg restore', style: `width:${pct(restoreUs)}`, title: `${tr("Демаскировка")}: ${(restoreUs / 1000).toFixed(2)} ${tr("мс")}` }),
      h('div', { class: 'wf-seg upstream', style: `width:${pct(upstreamUs)}`, title: `${tr("Ожидание внешней LLM")}: ${(upstreamUs / 1000).toFixed(1)} ${tr("мс")}` })
    ),
    h('div', { class: 'waterfall-legend' },
      h('span', {}, h('i', { class: 'wf-dot mask' }), tr("Маскирование (локально):"), h('b', { text: `${(maskUs / 1000).toFixed(2)} ${tr("мс")}` })),
      h('span', {}, h('i', { class: 'wf-dot s1' }), tr("Классификатор (S1):"), h('b', { text: `${(s1Us / 1000).toFixed(2)} ${tr("мс")}` })),
      h('span', {}, h('i', { class: 'wf-dot restore' }), tr("Демаскировка ответа:"), h('b', { text: `${(restoreUs / 1000).toFixed(2)} ${tr("мс")}` })),
      h('span', {}, h('i', { class: 'wf-dot upstream' }), tr("Ожидание внешней LLM (Upstream):"), h('b', { text: events.length ? `${(upstreamUs / 1000).toFixed(1)} ${tr("мс")}` : '—' }))
    )
  );
}

function renderCards(buckets) {
  const sum = key => buckets.reduce((a,b) => a + (b[key] || 0), 0);
  const usage = key => buckets.reduce((a,b) => a + (b.usage?.[key] || 0), 0);
  const uniqueEntities = info?.unique_entities ?? info?.stats?.entries ?? 0;
  const rulesCount = info?.rules_count ?? 0;
  const newReps = usage('replacements');
  const cachedReps = usage('cached_replacements');
  const promptTok = usage('prompt_tokens');
  const compTok = usage('completion_tokens');
  const totalTok = usage('total_tokens') || (promptTok + compTok);

  const cards = [
    [
      tr("Сущностей под защитой"),
      uniqueEntities,
      tr("Уникальных в Vault · активных правил: {{0}}", [number(rulesCount)]),
      'shield',
      false,
    ],
    [
      tr("Подстановок в контексте"),
      newReps,
      tr("Новых в запросах · +{{0}} из кэша истории чата", [number(cachedReps)]),
      'lock',
      true,
    ],
    [
      tr("Восстановлено в ответах"),
      usage('restored'),
      tr("Запросов через прокси: {{0}}", [number(sum('requests'))]),
      'refresh',
      false,
    ],
    [
      tr("Токены LLM (всего)"),
      totalTok,
      tr("Вход: {{0}} · Выход: {{1}} · Ошибок: {{2}}", [number(promptTok), number(compTok), number(sum('errors'))]),
      'list',
      sum('errors') > 0,
    ],
  ];
  $('#summary-cards').replaceChildren(...cards.map(([title, value, caption, symbol, highlight], i) =>
    h('article', { class: 'metric-card' + (i === 1 ? ' featured' : '') + (i === 3 && highlight ? ' attention' : '') },
      h('div', { class: 'metric-top' }, h('span', {}, title), icon(symbol)),
      h('strong', { class: 'metric-value' }, number(value)),
      h('span', { class: 'metric-caption' }, caption))));
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
  if (e.error) return 'error';
  if (e.outcome === 'ok' && (e.usage?.unknown || e.usage?.denied || e.usage?.uncertain)) return 'needs_review';
  return e.outcome;
}

function eventDiagnosticNote(e) {
  if (e.error) {
    const statusStr = e.error.status ? `HTTP ${e.error.status}` : '';
    const urlStr = e.error.upstream_url ? ` · ${e.error.upstream_url}` : '';
    return h('div', { class: 'event-diag-detail err' },
      `[${e.error.kind || 'upstream_error'}] ${statusStr}${urlStr}: ${e.error.message || ''}`
    );
  }
  const reasons = [];
  if (e.usage?.unknown_tokens?.length) {
    reasons.push(tr("Неизвестный токен в ответе: {{0}}", [e.usage.unknown_tokens.slice(0, 3).join(', ')]));
  } else if (e.usage?.unknown) {
    reasons.push(tr("Неизвестных токенов в ответе: {{0}}", [e.usage.unknown]));
  }
  if (e.usage?.denied_tokens?.length) {
    reasons.push(tr("Отказ доступа к токену: {{0}}", [e.usage.denied_tokens.slice(0, 3).join(', ')]));
  } else if (e.usage?.denied) {
    reasons.push(tr("Отказов доступа: {{0}}", [e.usage.denied]));
  }
  if (e.usage?.uncertain) {
    reasons.push(tr("Спорных кандидатов (uncertain): {{0}}", [e.usage.uncertain]));
  }
  if (!reasons.length) return null;
  return h('div', { class: 'event-diag-detail warn' }, reasons.join(' · '));
}

async function openInspector(eventId) {
  const dlg = $('#inspector-dialog');
  if (!dlg) return;
  try {
    const ev = await api(`/api/v1/ui/events/${eventId}`);
    const insp = ev.inspector || {};
    const t = ev.timings || {};
    const maskMs = ((t.mask_us || 0) / 1000).toFixed(2);
    const s1Ms = ((t.classifier_us || 0) / 1000).toFixed(2);
    const restoreMs = ((t.restore_us || 0) / 1000).toFixed(2);
    const upstreamMs = ((t.upstream_us || 0) / 1000).toFixed(1);
    const items = ev.usage?.masked_items || [];

    let showRawJson = false;
    const maskedPre = h('pre', { text: insp.masked_request || tr("Нет данных (тело запроса не содержало текста)") });
    const toggleJsonBtn = insp.masked_payload_json
      ? h('button', {
          type: 'button',
          class: 'small ghost',
          onclick: (e) => {
            showRawJson = !showRawJson;
            maskedPre.textContent = showRawJson ? insp.masked_payload_json : (insp.masked_request || '');
            e.currentTarget.textContent = showRawJson ? tr("Показать текст промпта") : tr("Показать полный JSON payload");
          }
        }, tr("Показать полный JSON payload"))
      : null;

    dlg.replaceChildren(
      h('div', { class: 'inspector-shell' },
        h('div', { class: 'inspector-header' },
          h('div', {},
            h('h3', { text: tr("Инспектор запроса #{{0}} · {{1}}", [ev.id, insp.model || ev.model || ev.provider]) }),
            h('p', { text: `${time(ev.time_ms)} · HTTP ${ev.status || '—'} · ${tr("Прокси")}: ${(Number(maskMs) + Number(restoreMs)).toFixed(2)} ${tr("мс")} · LLM: ${upstreamMs} ${tr("мс")} · ${tr("Токены")}: ${number(ev.usage?.total_tokens || 0)} (${number(ev.usage?.prompt_tokens || 0)} → ${number(ev.usage?.completion_tokens || 0)})` })
          ),
          h('button', { type: 'button', class: 'secondary', onclick: () => dlg.close() }, tr("Закрыть"))
        ),
        h('div', { class: 'inspector-body' },
          ev.error ? h('div', { class: 'event-error-banner' },
            h('strong', { text: tr("Техническая диагностика сбоя провайдера: {{0}} (HTTP {{1}})", [ev.error.kind || 'error', ev.error.status || ev.status || '—']) }),
            ev.error.upstream_url ? h('div', {}, tr("Адрес: "), h('code', { text: ev.error.upstream_url })) : null,
            h('div', { text: ev.error.message || '' })
          ) : null,
          eventDiagnosticNote(ev),
          items.length ? h('details', { open: true },
            h('summary', {}, h('strong', { text: tr("Подстановки и замаскированные сущности ({{0}})", [items.length]) })),
            h('div', { class: 'table-wrap' },
              h('table', {},
                h('thead', {}, h('tr', {}, ...[tr("Токен в LLM"), tr("Исходное значение"), tr("Тип"), tr("Вердикт"), tr("Источник")].map((col) => h('th', { text: col })))),
                h('tbody', {}, ...items.map((it) => h('tr', {},
                  h('td', {}, h('code', { text: it.token })),
                  h('td', { class: 'mono', text: it.original }),
                  h('td', {}, window.kindBadge ? window.kindBadge(it.kind) : it.kind),
                  h('td', { text: it.verdict || 'mask' }),
                  h('td', { text: it.from_cache ? tr("Кэш истории диалога") : tr("Новое в этом сообщении") })
                )))
              )
            )
          ) : null,
          h('div', { class: 'inspector-panes' },
            h('div', { class: 'inspector-pane' },
              h('div', { class: 'inspector-pane-head' },
                h('span', { text: tr("1. Исходный запрос (от приложения)") }),
                h('button', { type: 'button', class: 'small ghost', onclick: () => copy(insp.original_request || '').then(() => toast(tr("Скопировано"))) }, tr("Копировать"))
              ),
              h('pre', { text: insp.original_request || tr("Нет данных") })
            ),
            h('div', { class: 'inspector-pane' },
              h('div', { class: 'inspector-pane-head' },
                h('span', { text: tr("2. Обезличенный запрос (ушёл в LLM)") }),
                h('div', { class: 'review-actions' },
                  toggleJsonBtn,
                  h('button', { type: 'button', class: 'small ghost', onclick: () => copy(showRawJson ? insp.masked_payload_json : (insp.masked_request || '')).then(() => toast(tr("Скопировано"))) }, tr("Копировать"))
                )
              ),
              maskedPre
            ),
            h('div', { class: 'inspector-pane' },
              h('div', { class: 'inspector-pane-head' },
                h('span', { text: tr("3. Сырой ответ LLM (с токенами)") }),
                h('button', { type: 'button', class: 'small ghost', onclick: () => copy(insp.raw_response || '').then(() => toast(tr("Скопировано"))) }, tr("Копировать"))
              ),
              h('pre', { text: insp.raw_response || tr("Ответ не получен или пуст") })
            ),
            h('div', { class: 'inspector-pane' },
              h('div', { class: 'inspector-pane-head' },
                h('span', { text: tr("4. Восстановленный ответ (возвращён клиенту)") }),
                h('button', { type: 'button', class: 'small ghost', onclick: () => copy(insp.restored_response || '').then(() => toast(tr("Скопировано"))) }, tr("Копировать"))
              ),
              h('pre', { text: insp.restored_response || tr("Ответ не получен или пуст") })
            )
          )
        )
      )
    );
    dlg.showModal();
  } catch (e) { fail(e); }
}
window.openInspector = openInspector;

function eventsTable(events, compact=false) {
  if (!events.length) return h('div',{class:'empty-state'},icon('list'),h('strong',{},tr('Здесь появятся запросы прокси')),h('p',{},tr('Отправьте запрос из подключённого приложения. Ручные проверки здесь не отображаются.')));
  const titles = compact
    ? ['Время', 'Провайдер / Модель', 'Скрыто (новые / кэш)', 'Результат и диагностика', '']
    : ['Время', 'Провайдер / Модель', 'Скрыто (новые / кэш)', 'Восстановлено', 'Тайминг (Прокси / LLM)', 'Результат и диагностика', ''];
  const outcomes={ok:'Обработан',error:'Ошибка',restore_error:'Ошибка восстановления',cancelled:'Прерван',needs_review:'Требует проверки'};
  return h('table',{},h('thead',{},h('tr',{},...titles.map(t=>h('th',{},tr(t))))),h('tbody',{},...events.map(e=>{
    const items = e.usage?.masked_items || [];
    const proxyMs = (((e.timings?.mask_us || 0) + (e.timings?.restore_us || 0) + (e.timings?.classifier_us || 0)) / 1000).toFixed(2);
    const llmMs = e.timings?.upstream_us ? (e.timings.upstream_us / 1000).toFixed(0) : e.duration_ms;
    return h('tr',{},
      h('td',{class:'mono'},time(e.time_ms)),
      h('td',{},
        h('div',{},e.provider==='openai'?'OpenAI-compatible':'Anthropic', e.model ? h('code',{text:' · '+e.model}) : null),
        e.connection_id?h('small',{class:'event-connection',title:e.connection_id},e.connection_id):null
      ),
      h('td',{},
        h('div',{class:'mono'},
          number(e.usage.replacements),
          e.usage.cached_replacements ? h('span',{class:'hint',text:` (+${number(e.usage.cached_replacements)} ${tr("из кэша")})`}) : null
        ),
        items.length ? h('div',{class:'event-tokens-preview'},
          ...items.slice(0, 4).map(it => h('span',{class:'event-tok-chip',title:`${it.kind}: ${it.token}`},it.token)),
          items.length > 4 ? h('span',{class:'hint',text:`+${items.length - 4}`}) : null
        ) : null
      ),
      ...(compact?[]:[
        h('td',{class:'mono'},number(e.usage.restored)),
        h('td',{class:'mono'},
          h('div',{},`${tr("Прокси")}: ${proxyMs} ${tr("мс")}`),
          h('div',{class:'hint'},`LLM: ${number(llmMs)} ${tr("мс")}`)
        )
      ]),
      h('td',{},
        h('span',{class:'event-status '+eventOutcome(e),title:tr('HTTP {{0}} · передача начата: {{1}}',[e.status||'—',e.sent?tr('да'):tr('нет')])},tr(outcomes[eventOutcome(e)]||'Ошибка')),
        eventDiagnosticNote(e)
      ),
      h('td',{},
        h('button',{type:'button',class:'small secondary',onclick:()=>openInspector(e.id)},tr("Инспектор"))
      )
    );
  })));
}
function renderDashboard() {
  renderSystemPulse();
  renderWorkflowOverview();
  if (!dashboard?.traffic) return;
  const buckets = periodBuckets(); renderCards(buckets); renderOverheadPanel(buckets); renderChart(buckets); renderCategories(buckets);
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
    ['Версия',info.version||'—'],
    ['S1',tr(modes[info.s1_mode]||'Выключен')],
    ['Политика спорных (uncertain)',info.uncertain_policy || 'mask'],
    ['Защита API-токеном',tr(info.api_token_required?'Включена':'Локальный доступ')],
    ['Уникальных сущностей в Vault',number(info.unique_entities ?? info.stats?.entries ?? 0)],
    ['Активных правил',number(info.rules_count ?? 0)],
  ].map(([key,value])=>h('div',{class:'setting-row'},h('span',{},tr(key)),h('strong',{},value))));

  if ($('#settings-s1-summary')) {
    const modeLabel = tr(modes[info.s1_mode] || 'Выключен');
    $('#settings-s1-summary').replaceChildren(
      h('div', { class: 'worker-summary-row' }, h('span', { text: tr("Режим классификатора:") }), h('strong', { text: modeLabel })),
      h('div', { class: 'worker-summary-row' }, h('span', { text: tr("Самообучение на решениях:") }), h('strong', { text: tr("Автоматически (замкнутый цикл)") })),
      h('div', { class: 'worker-summary-row' }, h('span', { text: tr("Назначение:") }), h('strong', { text: tr("Триаж спорных записей и рекомендация контура") }))
    );
  }
  if (window.updateSettingsWorkerSummary) {
    window.updateSettingsWorkerSummary();
  }
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
