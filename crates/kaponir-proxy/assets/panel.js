"use strict";

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", "#i-" + name);
  svg.setAttribute("aria-hidden", "true");
  svg.append(use);
  return svg;
}
const PAGE_META = {
  overview: ["", "Обзор", ""],
  jobs: ["", "Задания", ""],
  events: ["", "Сессии", ""],
  connect: [
    "",
    "Поставщики и API",
    "Подключения и адреса API для ваших приложений.",
  ],
  analysis: ["", "Обнаружение", ""],
  worker: ["", "Служба и очередь", "Установка моделей, обработка файлов и состояние локальной службы."],
  categorization: ["", "Категоризация находок", ""],
  rules: ["", "Правила", ""],
  review: ["", "Находки", ""],
  discover: ["", "Найти значения", "Выберите текст, файл или сохранённые запросы и ответы прокси."],
  lab: ["", "Лаборатория", ""],
  mask: ["", "Проверка текста", ""],
  restore: ["", "Восстановление", ""],
  dataset: ["", "Датасет", "Проверьте разметку черновиков и выгрузите проверенные примеры для обучения."],
  settings: ["", "Настройки LLM", ""],
  proxy: ["", "Доступ и конфигурация", ""],
  setup: ["", "Настройка правил", ""],
  metrics: [
    "",
    "Диагностика",
    "Технические операции движка, включая лабораторию.",
  ],
};
function panelHeading(name, parent) {
  const [, title, description] = PAGE_META[name];
  $("#page-title").textContent = tr(parent === "lab" ? "Лаборатория" : title);
  $("#breadcrumb").textContent = tr(title);
  const parentLink = $("#breadcrumb-parent");
  const nested = name !== parent || name === "connect";
  parentLink.hidden = $("#breadcrumb-separator").hidden = !nested;
  if (nested) {
    parentLink.href = "#" + parent;
    parentLink.textContent = tr(parent === "connect" ? "Настройки прокси" : PAGE_META[parent][1]);
  }
  $("#page-description").textContent = tr(description);
  $("#page-description").hidden = !description;
  $("#page-actions").hidden = !["overview", "events"].includes(name);
  $("#lab-navigation").hidden = parent !== "lab";
  if (parent === "lab") syncRestoreAction();
  $("#learning-widget").hidden = name !== "worker";
  $("#llm-stage-note").hidden = !["analysis","categorization"].includes(name);
  $("#analysis-navigation").hidden = parent !== "settings";
  $("#proxy-navigation").hidden = parent !== "connect";
  $("#overview-refresh").hidden = name === "events";
  for (const b of document.querySelectorAll("#analysis-navigation a,#proxy-navigation a,#lab-navigation a")) {
    if (b.dataset.go === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  document.title = tr(title) + " · Kaponir";
}
for (const b of document.querySelectorAll("[data-go]"))
  b.addEventListener("click", (event) => {
    if (!followLocalLink(event)) return;
    if(b.dataset.go==="restore" && ["#mask","#lab","#discover"].includes(location.hash)) {
      rInput.value=restoreCandidate();
      runRestore();
    }
    if (b.dataset.go === "discover" && !D.busy)
      showDiscoverView(b.closest("#tab-worker") ? "saved" : "input");
    showTab(b.dataset.go);
  });
const narrowNav = matchMedia("(max-width:900px)");
function setSidebar(open) {
  $("#sidebar").classList.toggle("open", open);
  $("#sidebar").inert = narrowNav.matches && !open;
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
}
narrowNav.addEventListener("change", () => setSidebar(false));
document.addEventListener("focusin", e => {
  if ($("#sidebar").classList.contains("open") && !e.target.closest("#sidebar,#menu-toggle")) setSidebar(false);
});
$("#menu-toggle").addEventListener("click", () => {
  const open = !$("#sidebar").classList.contains("open");
  setSidebar(open);
  if (open) ($("#sidebar [aria-current]") || $("#sidebar a")).focus();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#sidebar").classList.contains("open")) {
    setSidebar(false); $("#menu-toggle").focus();
  }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#sidebar,#menu-toggle")) {
    setSidebar(false);
  }
});
window.addEventListener("hashchange", () => {
  if (location.hash !== "#content") showTab(location.hash.slice(1) || "overview", false);
});

let dashboard = null,
  dashboardPending = false,
  eventFilter = "all",
  eventsView = "sessions";
const expandedSessions = new Set();
const CATEGORY_LABELS = [
  "Пароли и ключи",
  "Персональные данные",
  "Инфраструктура",
  "Финансовые данные",
  "Контакты",
  "Другие значения",
];
function categoryColors() {
  return getComputedStyle(document.documentElement)
    .getPropertyValue("--category-colors")
    .split(",")
    .map((c) => c.trim());
}
function number(v) {
  return new Intl.NumberFormat(I18N.language).format(v || 0);
}
function time(v, options) {
  return new Date(v).toLocaleString(
    I18N.language,
    options || { hour: "2-digit", minute: "2-digit", second: "2-digit" },
  );
}
function periodBuckets() {
  const count = Number($("#period").value),
    current = Math.floor(Date.now() / 3600000);
  const saved = new Map(
    (dashboard?.traffic?.hours || []).map((b) => [b.hour, b]),
  );
  return Array.from(
    { length: count },
    (_, i) =>
      saved.get(current - count + 1 + i) || {
        hour: current - count + 1 + i,
        requests: 0,
        errors: 0,
        usage: { categories: [0, 0, 0, 0, 0, 0] },
      },
  );
}
function filteredEvents(applyFilter = true) {
  const cutoff =
    (Math.floor(Date.now() / 3600000) - Number($("#period").value) + 1) *
    3600000;
  const query =
    location.hash === "#events"
      ? ($("#events-search").value || "").toLowerCase()
      : "";
  return [
    ...(dashboard?.traffic?.active || []),
    ...(dashboard?.traffic?.events || []),
  ].filter(
    (e) =>
      e.time_ms >= cutoff &&
      (!applyFilter || eventFilter !== "issues" || hasIssue(e)) &&
      (!query ||
        [e.model, e.session_id, e.connection_id, e.provider, String(e.id)]
          .join(" ")
          .toLowerCase()
          .includes(query)),
  );
}
const reviewReasonLabels = {
  detector_uncertain: "Спорное решение детектора", legacy_uncertain: "Спорные решения без сохранённой связи",
  unknown_restore_token: "Неизвестный токен восстановления", restore_access_denied: "Нет доступа к восстановлению",
};
function requestNotes(event) {
  const notes = new Set();
  if (["error", "restore_error", "cancelled"].includes(event.outcome)) notes.add(outcomeLabel(event));
  if (Array.isArray(event.review_reasons)) {
    for (const reason of event.review_reasons) {
      if (reason.state !== "resolved") notes.add(tr(reviewReasonLabels[reason.code] || "Требует проверки"));
    }
  } else {
    for (const [key, label] of [["unknown", "Неизвестный токен восстановления"], ["denied", "Нет доступа к восстановлению"], ["uncertain", "Спорное решение детектора"]])
      if (event.usage?.[key]) notes.add(tr(label));
  }
  return [...notes];
}
function hasIssue(event) { return requestNotes(event).length > 0; }
function proxyUs(e) {
  return (
    (e.timings?.mask_us || 0) +
    (e.timings?.classifier_us || 0) +
    (e.timings?.restore_us || 0)
  );
}
function percentile(values, p) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * p) - 1)];
}
function renderCards(buckets) {
  const sum = (k) => buckets.reduce((n, b) => n + (b[k] || 0), 0),
    usage = (k) => buckets.reduce((n, b) => n + (b.usage?.[k] || 0), 0);
  const cards = [
    [
      "Запросы",
      sum("requests"),
      tr("В обработке: {{0}}", [number(dashboard.traffic.in_flight)]),
      "list",
    ],
    [
      "Замены",
      usage("replacements"),
      tr("Повторно из истории: {{0}}", [number(usage("cached_replacements"))]),
      "shield",
    ],
    [
      "Ошибки",
      sum("errors"),
      tr("Прервано: {{0}}", [number(sum("cancelled"))]),
      "eye-off",
    ],
    [
      "Токены LLM",
      usage("total_tokens"),
      tr("Вход: {{0}} · Выход: {{1}}", [
        number(usage("prompt_tokens")),
        number(usage("completion_tokens")),
      ]),
      "grid",
    ],
  ];
  $("#summary-cards").replaceChildren(
    ...cards.map(([title, value, caption, symbol]) =>
      h(
        "article",
        { class: "metric-card" },
        h(
          "div",
          { class: "metric-top" },
          h("span", {}, tr(title)),
          icon(symbol),
        ),
        h("strong", { class: "metric-value" }, number(value)),
        h("span", { class: "metric-caption" }, caption),
      ),
    ),
  );
  $("#summary-cards").setAttribute("aria-busy", "false");
  $("#welcome-banner").hidden = info.provider_configured === true;
  const pending = info.review_pending ?? info.stats?.journal_open ?? 0;
  const next = $("#home-open-pending");
  next.hidden = !pending;
  next.textContent = tr("Открыть на проверку · {{0}}", [number(pending)]);
  $("#home-open-document").classList.toggle("primary", !pending);
  $("#home-intake-note").textContent = tr(pending
    ? "Откройте исходный фрагмент и решите, какие значения нужно скрывать."
    : "Вставьте текст или откройте документ, чтобы увидеть первые значения для проверки.");
}
$("#home-open-pending").addEventListener("click", () => showReview());
function renderLatency() {
  const hours = Number($("#period").value),
    cutoff = (Math.floor(Date.now() / 3600000) - hours + 1) * 3600000;
  const events = (dashboard?.traffic?.events || []).filter(
    (e) => e.time_ms >= cutoff && e.timings && proxyUs(e) > 0,
  );
  const p50 = percentile(events.map(proxyUs), 0.5),
    p95 = percentile(events.map(proxyUs), 0.95);
  $("#latency-summary").replaceChildren(
    h("span", {}, tr("Обработка Kaponir")),
    h(
      "strong",
      {},
      p50 === null
        ? "—"
        : `p50 ${(p50 / 1000).toFixed(2)} ${tr("мс")} · p95 ${(p95 / 1000).toFixed(2)} ${tr("мс")}`,
    ),
    h(
      "span",
      { class: "hint" },
      tr("Выборка: {{0}} запросов", [number(events.length)]),
    ),
    h(
      "button",
      { class: "text-button", onclick: () => showTab("metrics") },
      tr("Диагностика"),
    ),
  );
}
function renderChart(buckets) {
  const chart = $("#traffic-chart");
  const groups =
    buckets.length > 24
      ? Array.from({ length: 7 }, (_, i) => {
          const slice = buckets.slice(i * 24, i * 24 + 24);
          return {
            hour: slice[0].hour,
            requests: slice.reduce((n, b) => n + b.requests, 0),
          };
        })
      : buckets;
  const hasRequests = groups.some((b) => b.requests);
  const max = Math.max(1, ...groups.map((b) => b.requests));
  const top = max <= 5 ? max : Math.ceil(max / 5) * 5;
  const bars = h("div", {
    class: "chart-bars",
    role: "group",
    "aria-label": tr("График реальных запросов"),
  });
  const tooltip = h("div", {
    class: "chart-tooltip",
    role: "status",
    hidden: true,
  });
  for (const bucket of groups) {
    const period = time(
      bucket.hour * 3600000,
      groups.length === 7
        ? { month: "short", day: "numeric" }
        : { month: "short", day: "numeric", hour: "2-digit" },
    );
    const value = tr("Запросов: {{0}}", [number(bucket.requests)]);
    const label = period + " · " + value;
    const showTip = () => {
      tooltip.replaceChildren(
        h("strong", {}, number(bucket.requests)),
        h("span", {}, period),
      );
      tooltip.hidden = false;
    };
    bars.append(
      h(
        "button",
        {
          class: "chart-bar",
          "aria-label": label,
          onmouseenter: showTip,
          onfocus: showTip,
          onmouseleave: () => {
            tooltip.hidden = true;
          },
          onblur: () => {
            tooltip.hidden = true;
          },
          onclick: () => {
            $("#traffic-period-note").textContent = label;
          },
        },
        h("span", {
          class: "chart-bar-fill",
          style: `height:${bucket.requests ? Math.max(3, (bucket.requests / top) * 100) : 0}%`,
        }),
      ),
    );
  }
  const axis = h(
    "div",
    { class: "chart-y-axis", "aria-hidden": "true" },
    h("span", {}, number(top)),
    h("span", {}, number(top / 2)),
    h("span", {}, "0"),
  );
  const labels = h(
    "div",
    { class: "chart-labels" },
    ...[
      groups[0],
      groups[Math.floor((groups.length - 1) / 2)],
      groups[groups.length - 1],
    ].map((b) =>
      h(
        "span",
        {},
        time(
          b.hour * 3600000,
          groups.length === 7
            ? { day: "numeric", month: "short" }
            : { hour: "2-digit", minute: "2-digit" },
        ),
      ),
    ),
  );
  chart.replaceChildren(
    h("div", { class: "chart-plot" }, axis, bars),
    labels,
    tooltip,
  );
  if (!hasRequests)
    chart.append(
      h(
        "div",
        { class: "chart-empty" },
        icon("plug"),
        h("strong", {}, tr("Пока нет запросов")),
        h("span", {}, tr("За выбранный период запросов нет.")),
      ),
    );
  $("#traffic-table").replaceChildren(
    h(
      "table",
      {},
      h(
        "thead",
        {},
        h("tr", {}, h("th", {}, tr("Время")), h("th", {}, tr("Запросы"))),
      ),
      h(
        "tbody",
        {},
        ...groups.map((b) =>
          h(
            "tr",
            {},
            h(
              "td",
              {},
              time(
                b.hour * 3600000,
                groups.length === 7
                  ? { day: "numeric", month: "short" }
                  : {
                      day: "numeric",
                      month: "short",
                      hour: "2-digit",
                      minute: "2-digit",
                    },
              ),
            ),
            h("td", { class: "num" }, number(b.requests)),
          ),
        ),
      ),
    ),
  );
  $("#traffic-period-note").textContent = tr("В обработке: {{0}}", [
    number(dashboard.traffic.in_flight),
  ]);
  const latencies = filteredEvents(false)
    .filter((e) => e.outcome !== "in_progress")
    .map((e) => e.duration_ms)
    .sort((a, b) => a - b);
  $("#latency-note").textContent = latencies.length
    ? tr("p95 последних событий: {{0}} мс", [
        number(latencies[Math.ceil(latencies.length * 0.95) - 1]),
      ])
    : tr("Задержка появится после первого запроса");
}
function renderCategories(buckets) {
  const counts = CATEGORY_LABELS.map((_, i) =>
    buckets.reduce((n, b) => n + b.usage.categories[i], 0),
  );
  if (!counts.some(Boolean)) {
    $("#category-breakdown").replaceChildren(
      h(
        "div",
        { class: "empty-state compact-empty" },
        icon("shield"),
        h("strong", {}, tr("Пока нечего скрывать")),
        h("p", {}, tr("Здесь появятся типы замен после первого запроса.")),
      ),
    );
    return;
  }
  const max = Math.max(1, ...counts);
  const colors = categoryColors();
  $("#category-breakdown").replaceChildren(
    ...CATEGORY_LABELS.map((label, i) =>
      h(
        "div",
        { style: `--category-color:${colors[i]}` },
        h(
          "div",
          { class: "category-line" },
          h(
            "span",
            {},
            h("i", { class: "category-dot", "aria-hidden": "true" }),
            tr(label),
          ),
          h("strong", {}, number(counts[i])),
        ),
        h(
          "div",
          { class: "category-track", "aria-hidden": "true" },
          h("div", {
            class: "category-fill",
            style: `width:${(counts[i] / max) * 100}%`,
          }),
        ),
      ),
    ),
  );
}
window.addEventListener("kaponir-themechange", () => {
  if (dashboard) renderCategories(periodBuckets());
});

function outcomeLabel(e) {
  const label = {ok:"Ответ получен", error:"Ошибка запроса", restore_error:"Сбой восстановления ответа",
    cancelled:"Прерван до завершения", in_progress:"Выполняется", needs_review:"Требует проверки"};
  return tr(label[e.outcome] || "Состояние неизвестно") + (e.status >= 400 ? " · HTTP " + e.status : "");
}
const collapsedRequestGroups = new Set(), issueGroups = new Set();
function toggleGroup(key, expanded, requests) {
  if (requests) {
    if (expanded) collapsedRequestGroups.delete(key); else collapsedRequestGroups.add(key);
  } else if (expanded) expandedSessions.add(key); else expandedSessions.delete(key);
  if (!expanded) issueGroups.delete(key);
  renderEventViews();
}
function requestRow(event, prefix) {
  const notes = requestNotes(event), outcome = outcomeLabel(event);
  const format = event.request_kind === "count_tokens" ? tr("Подсчёт токенов") : event.streaming ? "SSE" : "";
  const open = () => openInspector(event.id, ["error","restore_error","cancelled"].includes(event.outcome) ? "diagnostics" :
    notes.length ? "review" : "request");
  const state = h("td", {class:"traffic-status"}, h("span", {class:"event-status " + event.outcome}, outcome));
  const reasons = notes.filter(note => note !== outcome);
  if (reasons.length) state.append(h("small", {class:"request-reasons"}, reasons.join(" · ")));
  const action = h("button", {class:"text-button", "data-request-detail":event.id, onclick:open}, tr("Подробности"));
  return h("tr", {class:"session-children", id:prefix + "-request-" + event.id},
    h("th", {scope:"row"}, h("button", {class:"request-leaf", "data-request-id":event.id, onclick:open},
      tr("Запрос {{0}}", [event.id]))),
    h("td", {class:"traffic-time"}, time(event.started_at_ms || event.time_ms)),
    h("td", {class:"traffic-model"}, event.model || event.provider, format ? h("small", {class:"request-format"}, format) : null),
    h("td", {class:"num traffic-count"}, "—"),
    h("td", {class:"num traffic-masks"}, number(event.usage?.replacements)),
    state, h("td", {class:"traffic-action"}, action));
}
function sessionsTable(events, compact = false) {
  const groups = new Map();
  for (const e of events) {
    const key = e.session_id || "request-" + e.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const requests = !compact && eventsView === "requests";
  const onlyIssues = !compact && eventFilter === "issues";
  let entries = [...groups].map(([key, rows]) => {
    rows.sort((a,b) => b.time_ms - a.time_ms || b.id - a.id);
    return {key, rows, issues:rows.filter(hasIssue)};
  }).filter(group => !onlyIssues || group.issues.length).sort((a,b) => b.rows[0].time_ms - a.rows[0].time_ms);
  if (compact) entries = entries.slice(0,5);
  if (!entries.length) {
    const searching = !compact && $("#events-search").value.trim();
    return h("div", {class:"empty-state"}, icon("chat"),
      h("strong", {}, tr(onlyIssues ? "Запросов, требующих внимания, нет" : searching ? "Совпадений в сессиях нет" : "Сессий за период нет")),
      searching ? workButton("Очистить поиск", () => { $("#events-search").value=""; renderEventViews(); $("#events-search").focus(); }) :
        onlyIssues ? workButton("Все запросы", () => $("#events-filter [data-filter=all]").click()) : null);
  }
  const prefix = compact ? "recent" : "traffic";
  return h("table", {class:"sessions-table"}, h("thead", {}, h("tr", {},
    ["Сессия / запрос","Последняя активность","Модель","Запросы","Замены","Состояние","Действия"].map(title => h("th", {scope:"col"}, tr(title))))),
    entries.map(({key,rows,issues}) => {
      const latest = rows[0], active = rows.filter(e=>e.outcome === "in_progress").length;
      const expanded = requests ? !collapsedRequestGroups.has(key) : expandedSessions.has(key);
      const issueOnly = onlyIssues || issueGroups.has(key);
      const name = latest.session_id ? tr("Сессия {{0}}", [key.slice(0,8)]) : tr("Запрос {{0}}", [latest.id]);
      const trigger = h("button", {class:"session-toggle", "data-session-key":key, "aria-expanded":String(expanded),
        onclick:()=>toggleGroup(key,!expanded,requests), onkeydown:e=>{
          if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); toggleGroup(key,e.key === "ArrowRight",requests); }
        }}, icon("chevron"), name);
      const summary = h("td", {class:"traffic-status"});
      if (active) summary.append(h("span", {class:"event-status in_progress"}, tr("Выполняется · {{0}}", [active])));
      if (issues.length) {
        const counts = new Map();
        for (const e of issues) for (const note of requestNotes(e)) counts.set(note,(counts.get(note)||0)+1);
        const label = tr("Требуют внимания · {{0}} из {{1}}", [issues.length,rows.length]);
        summary.append(h("button", {class:"issue-button", "data-session-issues":key, "aria-expanded":String(expanded && issueOnly),
          title:[...counts].map(([note,n])=>note+" · "+n).join("\n"), onclick:()=>{
            if (expanded && issueOnly) { toggleGroup(key,false,requests); return; }
            issueGroups.add(key); toggleGroup(key,true,requests);
          }}, label, icon("chevron")));
      } else if (!active) summary.append(h("span", {class:"event-status ok"}, tr("Без замечаний")));
      const open = h("button", {class:"session-open", "data-session-open":key, title:tr("Открыть переписку"),
        "aria-label":tr("Открыть переписку") + " · " + name, onclick:()=>openSession(latest)}, icon("chat"), h("span", {},tr("Переписка")));
      return h("tbody", {}, h("tr", {class:"session-row"},
        h("th", {scope:"row", title:latest.session_source==="inferred" ? tr("Сгруппировано автоматически по началу переписки") : name},trigger),
        h("td", {class:"traffic-time"}, sessionTime(latest.time_ms)),
        h("td", {class:"traffic-model"}, [...new Set(rows.map(e=>e.model||e.provider))].join(", ")),
        h("td", {class:"num traffic-count"}, number(rows.length)), h("td", {class:"num traffic-masks"},
          number(rows.reduce((n,e)=>n+(e.usage?.replacements||0),0))), summary, h("td", {class:"traffic-action"}, open)),
        expanded ? (issueOnly ? issues : rows).map(e=>requestRow(e,prefix)) : []);
    }));
}
function renderEventViews() {
  if ($("#inspector-dialog").open) return;
  if ($("#session-dialog").open) { refreshOpenSession?.(); return; }
  const focused = document.activeElement;
  const origin = focused?.closest("#recent-events,#events-table");
  const attribute = ["data-request-id", "data-request-detail", "data-session-open", "data-session-issues", "data-session-key"]
    .find(attr => focused?.hasAttribute(attr));
  const value = attribute && focused.getAttribute(attribute);
  $("#events-filter-note").hidden = eventFilter !== "issues";
  $("#recent-events").replaceChildren(
    sessionsTable(filteredEvents(false), true),
  );
  $("#events-table").replaceChildren(
    sessionsTable(filteredEvents(false)),
  );
  if (origin && value) {
    const target = [
      ...origin.querySelectorAll("[" + attribute + "]"),
    ].find((el) => el.getAttribute(attribute) === value);
    (target || $("#events-refresh")).focus({ preventScroll: true });
  }
}
function inspectorComparison(insp,usage,response) {
  const pairs=response ? sessionReplyRanges(insp,usage) : sessionMaskRanges(insp.original_request || "",insp.masked_request || "",usage.masked_items);
  const original=pairs.map(r=>({...r,label:response ? "Восстановлено для клиента" : "Скрыто при передаче"}));
  const tokens=pairs.map(r=>({start:r.otherStart,end:r.otherEnd,masked:true,label:"Токен поставщика"}));
  function pane(title,value,ranges) {
    const pre=h("pre",{tabindex:0,"aria-label":tr(title)});
    const tools=h("div",{class:"inspector-pane-tools"});
    const copyButton=workButton("Копировать",async()=>{await copy(pre.textContent);toast(tr("Скопировано"));},"small ghost");
    tools.append(copyButton);
    function render(text,marks) {
      copyButton.disabled=!text;
      pre.replaceChildren(text ? markedDialogue(text,marks) : tr("Нет данных"));
    }
    render(value,ranges);
    return {pre,tools,render,section:h("section",{class:"inspector-pane"},
      h("div",{class:"inspector-pane-head"},h("span",{},tr(title)),tools),pre)};
  }
  const left=response ? pane("Ответ поставщика",insp.raw_response,tokens) : pane("Исходный запрос",insp.original_request,original);
  const right=response ? pane("Возвращено клиенту",insp.restored_response,original) : pane("Отправлено поставщику",insp.masked_request,tokens);
  const sync=h("input",{type:"checkbox",checked:localStorage.getItem("inspector-sync-scroll")!=="off"});
  const expected=new WeakMap();
  function align(from,to) {
    const distance=from.scrollHeight-from.clientHeight;
    if (!sync.checked || sync.disabled || distance<=0) return;
    to.scrollTop=(to.scrollHeight-to.clientHeight)*from.scrollTop/distance;
    expected.set(to,to.scrollTop);
  }
  for (const [from,to] of [[left.pre,right.pre],[right.pre,left.pre]]) {
    from.addEventListener("scroll",()=>{
      const target=expected.get(from);
      expected.delete(from);
      if (target==null || Math.abs(from.scrollTop-target)>1) align(from,to);
    },{passive:true});
  }
  sync.onchange=()=>{localStorage.setItem("inspector-sync-scroll",sync.checked?"on":"off");align(left.pre,right.pre);};
  const missing=!(response ? insp.raw_response && insp.restored_response : insp.original_request && insp.masked_request);
  sync.disabled=missing;
  const clear=response ? insp.restored_response : insp.original_request, wire=response ? insp.raw_response : insp.masked_request;
  const last=pairs.at(-1);
  const incomplete=!missing && clear.slice(last?.end || 0)!==wire.slice(last?.otherEnd || 0);
  const caption=incomplete ? tr(pairs.length ? "В сохранённом тексте удалось сопоставить только часть замен." : "Для подсветки нужны сведения о заменах из этого события.") :
    !pairs.length ? tr("Сохранённые тексты совпадают.") : tr(response ? "Подчёркнуты токены и восстановленные значения." : "Подчёркнуты скрытые значения и их токены.");
  const explanation=h("span",{class:"hint"},caption);
  explanation.hidden=missing;
  if (!response && insp.masked_payload_json) {
    let raw=false;
    const toggle=h("button",{type:"button",class:"small secondary","aria-pressed":"false",onclick:()=>{
      raw=!raw;
      toggle.setAttribute("aria-pressed",String(raw)); toggle.textContent=tr(raw?"Текст":"JSON");
      right.render(raw ? insp.masked_payload_json : insp.masked_request,raw ? [] : tokens);
      sync.disabled=missing || raw;
      explanation.hidden=raw || missing;
      if (!raw) align(left.pre,right.pre);
    }},"JSON");
    right.tools.prepend(toggle);
  }
  return h("div",{class:"inspector-comparison"},h("div",{class:"comparison-options"},explanation,
    h("label",{class:"check"},sync,tr("Синхронная прокрутка"))),h("div",{class:"inspector-panes"},left.section,right.section));
}

async function openInspector(id, initialTab = null, target = null) {
  try {
    const ev = await api("/api/v1/ui/events/" + id),
      insp = ev.inspector || {},
      dlg = $("#inspector-dialog");
    const nav = h("div", { class: "section-tabs inspector-tabs" }),
      body = h("div", { class: "inspector-body" });
    const tabs = {
      review: () => {
        const reasons = ev.review_reasons || [], nodes = [];
        const titles = reviewReasonLabels;
        for (const reason of reasons) {
          const tokens = reason.code === "unknown_restore_token" ? ev.usage?.unknown_tokens : reason.code === "restore_access_denied" ? ev.usage?.denied_tokens : null;
          nodes.push(h("article", { class: "panel review-card", tabindex:-1,"data-review-reason":reason.code }, h("strong", {}, tr(titles[reason.code] || reason.code)),
            h("p", {}, tr("Значений: {{0}}", [reason.count]), " · ", tr(reason.state === "resolved" ? "Проверка закрыта" : "Требует проверки")),
            reason.code === "legacy_uncertain" ? h("p", { class: "hint" }, tr("Старая версия сохранила число спорных решений, но не их связи с этим событием.")) : null,
            tokens?.length ? h("p", { class: "mono" }, tokens.join(" · ")) : null,
            reason.code === "restore_access_denied" ? h("p", { class: "hint" }, tr("Токен найден, но текущий пользователь не имеет доступа к его области. Проверьте настройки доступа; это не спорная оценка детектора.")) : null,
            reason.code === "unknown_restore_token" ? h("p", { class: "hint" }, tr("Для токена нет соответствия в Vault. Проверьте исходную сессию, хранилище и ответ поставщика.")) : null,
            reason.keys?.length ? h("button", { class: "secondary", onclick: () => { dlg.close(); showReview(reason.keys, reason.state === "resolved" ? "history" : "pending"); } }, tr("Открыть связанные решения")) : null));
        }
        for (const [index,reference] of (ev.usage?.review_items || []).entries()) nodes.push(h("div", { class: "review-card", tabindex:-1, "data-ref-index":index },
          h("code", {}, reference.value), h("p", {}, `${kindLabel(reference.kind)} · ${tr("Оценка детектора")}: ${(reference.probability * 100).toFixed(1)}%`),
          h("p", { class: "hint" }, tr(reference.applied === "mask" ? "В этом запросе значение замаскировано по политике." : "В этом запросе значение осталось в тексте.")),
          h("blockquote", { class: "candidate-fragment" }, candidateExcerpt(reference.context, reference.value, reference.context_span))));
        if (ev.error) nodes.push(h("div", { class: "event-error-banner" }, h("strong", {}, ev.error.kind), h("p", {}, ev.error.message)));
        body.replaceChildren(...(nodes.length ? nodes : [h("p", { class: "hint" }, tr("Причин для проверки не зарегистрировано"))]));
      },
      request: () => body.replaceChildren(inspectorComparison(insp,ev.usage || {},false)),
      response: () => body.replaceChildren(inspectorComparison(insp,ev.usage || {},true)),
      replacements: () => {
        const items = ev.usage?.masked_items || [];
        body.replaceChildren(
          h(
            "p",
            { class: "hint" },
            tr("Замены: {{0}} · Повторно из истории: {{1}}", [
              number(ev.usage?.replacements),
              number(ev.usage?.cached_replacements),
            ]),
          ),
          items.length
            ? h(
                "div",
                { class: "table-wrap" },
                h(
                  "table",
                  {},
                  h(
                    "thead",
                    {},
                    h(
                      "tr",
                      {},
                      ...["Токен", "Исходное значение", "Тип", "Основание"].map(
                        (t) => h("th", {}, tr(t)),
                      ),
                    ),
                  ),
                  h(
                    "tbody",
                    {},
                    ...items.map((it, itemIndex) =>
                      h(
                        "tr",
                        {},
                        h("td", { class: "mono" }, it.token),
                        h("td", { class: "mono" }, it.original),
                        h("td", {}, kindBadge(it.kind)),
                        h(
                          "td",
                          {},
                          h(
                            "div",
                            {},
                            it.recognizer || tr("Источник не сохранён"),
                          ),
                          h(
                            "small",
                            { class: "hint" },
                            it.origin?.type === "rule"
                              ? tr("Правило {{0}} · {{1}}", [
                                  it.origin.id,
                                  it.origin.scope,
                                ])
                              : it.from_cache
                                ? tr("Кэш истории диалога")
                                : (it.evidence || [])
                                    .map((x) => x.source)
                                    .join(", "),
                          ),
                          it.probability == null
                            ? null
                            : h(
                                "small",
                                { class: "hint" },
                                " p=" + it.probability.toFixed(2),
                              ),
                          h("button", {class:"text-button",disabled:it.protected||it.from_cache||it.origin?.type!=="builtin",
                            onclick:()=>queueEventReview(ev.id,itemIndex,dlg)},tr("Проверить это решение")),
                        ),
                      ),
                    ),
                  ),
                ),
              )
            : h("div", { class: "empty-state" }, tr("Замен нет")),
        );
      },
      diagnostics: () =>
        body.replaceChildren(
          h(
            "dl",
            { class: "diagnostic-grid" },
            ...[
              ["Состояние", outcomeLabel(ev)],
              ["HTTP", ev.status || "—"],
              [
                "Операция",
                ev.request_kind === "count_tokens"
                  ? tr("Подсчёт токенов")
                  : tr("Генерация"),
              ],
              ["Сессия", ev.session_id || "—"],
              ["Связь", ev.session_source || "—"],
              ["Kaponir", `${(proxyUs(ev) / 1000).toFixed(2)} ${tr("мс")}`],
              [
                "Поставщик",
                `${((ev.timings?.upstream_us || 0) / 1000).toFixed(0)} ${tr("мс")}`,
              ],
              ["Токены LLM", number(ev.usage?.total_tokens)],
              ["Попытки", (ev.attempts || []).join(" → ") || "—"],
            ].flatMap(([k, v]) => [h("dt", {}, tr(k)), h("dd", {}, v)]),
          ),
          ev.error
            ? h(
                "div",
                { class: "event-error-banner" },
                h("strong", {}, ev.error.kind),
                h("p", {}, ev.error.message),
              )
            : h("p", { class: "hint" }, tr("Ошибок не зарегистрировано")),
        ),
    };
    function select(key) {
      for (const b of nav.children)
        b.setAttribute("aria-pressed", String(b.dataset.key === key));
      tabs[key]();
    }
    for (const [key, label] of [
      ["request", "Запрос"],
      ["response", "Ответ"],
      ["replacements", "Замены"],
      ["review", "Проверка"],
      ["diagnostics", "Диагностика"],
    ])
      nav.append(
        h("button", { "data-key": key, onclick: () => select(key) }, tr(label)),
      );
    dlg.replaceChildren(
      h(
        "div",
        { class: "inspector-shell" },
        h(
          "div",
          { class: "inspector-header" },
          h(
            "div",
            {},
            h(
              "h3",
              {},
              tr("Запрос {{0}}", [ev.id]) + " · " + (ev.model || ev.provider),
            ),
            h(
              "p",
              {},
              time(ev.started_at_ms || ev.time_ms) + " · " + outcomeLabel(ev),
            ),
          ),
          h(
            "button",
            {
              class: "icon-button",
              "aria-label": tr("Закрыть"),
              onclick: () => dlg.close(),
            },
            "×",
          ),
        ),
        nav,
        body,
      ),
    );
    select(initialTab || (hasIssue(ev) ? "review" : "request"));
    if (!dlg.open) dlg.showModal();
    const record=Number.isInteger(target) ? body.querySelector('[data-ref-index="'+target+'"]') :
      [...body.querySelectorAll("[data-review-reason]")].find(node=>node.dataset.reviewReason===target);
    if (record) {
      record.focus({preventScroll:true});
      record.scrollIntoView({block:"center"});
    }
  } catch (e) {
    fail(e);
  }
}
window.openInspector = openInspector;
function renderDashboard() {
  if (!dashboard?.traffic) return;
  const buckets = periodBuckets();
  renderCards(buckets);
  renderChart(buckets);
  renderCategories(buckets);
  renderLatency();
  renderEventViews();
  const last = dashboard.traffic.events?.[0];
  const alert = $("#provider-alert");
  alert.hidden = !last || !["error", "restore_error"].includes(last.outcome);
  if (!alert.hidden)
    alert.replaceChildren(
      h(
        "span",
        {},
        tr("Последний запрос завершился ошибкой") +
          (last.status ? " · HTTP " + last.status : ""),
      ),
      h(
        "button",
        { class: "text-button", onclick: () => showTab("events") },
        tr("Открыть события"),
      ),
    );

  $("#updated-at").textContent = tr("Обновлено {{0}}", [time(Date.now())]);
}
function setConnected(online) {
  const state = $("#connection-state");
  state.classList.toggle("offline", !online);
  state.title = state.querySelector("span").textContent = tr(online ? "Прокси доступен" : "Нет соединения");
}
async function loadDashboard() {
  if (dashboardPending) return;
  dashboardPending = true;
  try {
    const [metrics] = await Promise.all([api("/api/v1/metrics"), loadInfo()]);
    dashboard = metrics;
    setConnected(true);
    $("#connection-error").hidden = true;
    renderDashboard();
  } catch (e) {
    setConnected(false);
    $("#connection-error").hidden = false;
    $("#connection-error").textContent = tr(
      "Не удалось обновить данные. Показанные ранее значения могут быть устаревшими.",
    );
    throw e;
  } finally {
    dashboardPending = false;
  }
}
for (const id of ["overview-refresh", "events-refresh"])
  $("#" + id).addEventListener("click", () => loadDashboard().catch(fail));
$("#period").addEventListener("change", renderDashboard);
$("#events-search").addEventListener("input", renderEventViews);
for (const b of $("#events-filter").querySelectorAll("button"))
  b.addEventListener("click", () => {
    eventFilter = b.dataset.filter;
    issueGroups.clear();
    for (const x of b.parentElement.children)
      x.setAttribute("aria-pressed", String(x === b));
    renderEventViews();
  });
for (const b of $("#events-view").children)
  b.addEventListener("click", () => {
    eventsView = b.dataset.view;
    issueGroups.clear();
    for (const x of b.parentElement.children)
      x.setAttribute("aria-pressed", String(x === b));
    renderEventViews();
  });
setInterval(() => {
  if (
    !document.hidden &&
    ["overview", "events"].includes(location.hash.slice(1))
  )
    loadDashboard().catch(() => {});
}, 5000);
function copyCode(code) {
  return h(
    "div",
    { class: "code-box" },
    h("pre", {}, code),
    h(
      "button",
      {
        "aria-label": tr("Копировать"),
        onclick: () => copy(code).then(() => toast(tr("Скопировано"))),
      },
      icon("copy"),
    ),
  );
}
function renderConnection() {
  return loadProviders();
}
function renderSettings() {
  $("#settings-info").replaceChildren(
    ...[
      ["Версия", info.version || "—"],
      [
        "Защита API-токеном",
        tr(info.api_token_required ? "Включена" : "Локальный доступ"),
      ],
      ["Записей в Vault", number(info.stats?.entries)],
      ["Активных правил", number(info.rules_count)],
    ].map(([k, v]) =>
      h(
        "div",
        { class: "setting-row" },
        h("span", {}, tr(k)),
        h("strong", {}, v),
      ),
    ),
  );
}
async function loadAnalysis() {
  await Promise.all([loadLearningSettings(), loadWorkerSummary(), loadLearningHistory()]);
  renderLearningWidget();
  if (location.hash === "#worker" && WORKER.data) renderWorker();
}
let rulesView = "list";
function showRulesView(view) {
  rulesView = info.editable ? view : "list";
  for (const button of $("#rules-view").querySelectorAll("[data-rules-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.rulesView === rulesView));
    button.disabled = button.dataset.rulesView === "audit" && !info.editable;
  }
  $("#rules-catalog").hidden = rulesView !== "list";
  $("#rules-audit").hidden = rulesView !== "audit";
  if (rulesView === "audit") return loadAudit().catch(fail);
}
const auditSelection = new Map();
let auditBusy = false;
function updateAuditSelection() {
  $("#audit-selection").hidden = !auditSelection.size;
  $("#audit-selected-count").textContent = tr("Выбрано: {{0}}", [
    auditSelection.size,
  ]);
  const selected = [...auditSelection.values()];
  $("#audit-disable").hidden = !selected.some(i => !i.rule.disabled);
  $("#audit-enable").hidden = !selected.some(i => i.rule.disabled);
  $("#audit-disable").disabled = $("#audit-enable").disabled = auditBusy;
}
async function changeAudit(items, disabled) {
  if (auditBusy) return;
  auditBusy = true;
  updateAuditSelection();
  try {
    const scopes = new Map();
    for (const item of items.filter(
      (i) => Boolean(i.rule.disabled) !== disabled,
    )) {
      if (!scopes.has(item.scope)) scopes.set(item.scope, []);
      scopes.get(item.scope).push(item);
    }
    for (const [scope, selected] of scopes)
      await api(
        scope === "memory"
          ? "/api/v1/ui/rules/audit/memory"
          : "/api/v1/ui/rules/audit",
        {
          method: "PUT",
          body:
            scope === "memory"
              ? {
                  ids: selected.map((i) => i.index),
                  revision: selected[0].revision,
                  disabled,
                }
              : {
                  scope,
                  indices: selected.map((i) => i.index),
                  revision: selected[0].revision,
                  disabled,
                },
        },
      );
    await afterRulesChanged();
    await loadAudit();
    toast(tr(disabled ? "Выбранные записи отключены" : "Выбранные записи снова включены"));
  } catch (e) {
    fail(e);
    await loadAudit().catch(() => {});
  } finally {
    auditBusy = false;
    updateAuditSelection();
  }
}
let auditLoad = 0;
async function loadAudit() {
  const request = ++auditLoad;
  const data = await api("/api/v1/ui/rules/audit");
  if (request !== auditLoad) return;
  const focusedRow = document.activeElement.closest("#audit-list tr");
  const focusedKey = focusedRow?.dataset.auditEntry;
  const focusedControl = document.activeElement.matches("input") ? "input" : "button";
  const wasBulkAction = $("#audit-selection").contains(document.activeElement);
  auditSelection.clear();
  updateAuditSelection();
  const reasons = {
    disabled: "Ранее отключено",
    weak_value: "Общее слово или публичное обозначение",
    invalid_ip: "Не похоже на полный IP-адрес",
    short_domain: "Слишком короткое значение для домена",
    generated_rule: "Создано из найденного кандидата",
    type_format_conflict: "Тип правила противоречит формату значения",
    ambiguous_calendar_name: "Возможное имя из календарной даты",
    unconfirmed_path_project: "Часть пути была сохранена как проект",
    session_memory: "Значение из памяти сессии",
  };
  const selectAll = h("input", {
    type: "checkbox",
    "aria-label": tr("Выбрать все"),
    disabled: !info.editable,
    onchange: (e) => {
      for (const item of data.items) {
        const key = item.scope + ":" + item.index;
        if (e.target.checked) auditSelection.set(key, item);
        else auditSelection.delete(key);
      }
      for (const cb of $("#audit-list").querySelectorAll("tbody input"))
        cb.checked = e.target.checked;
      updateAuditSelection();
    },
  });
  const rows = data.items.map((item) => {
    const checkbox = h("input", {
      type: "checkbox",
      "aria-label": tr("Выбрать ") + rulePattern(item.rule)[1],
      disabled: !info.editable,
      onchange: (e) => {
        const key = item.scope + ":" + item.index;
        if (e.target.checked) auditSelection.set(key, item);
        else auditSelection.delete(key);
        selectAll.checked = auditSelection.size === data.items.length;
        selectAll.indeterminate = auditSelection.size > 0 && !selectAll.checked;
        updateAuditSelection();
      },
    });
    return h(
      "tr",
      {dataset:{auditEntry:item.scope + ":" + item.index}},
      h("td", {}, checkbox),
      h(
        "td",
        { class: "mono" },
        item.rule.value || item.rule.regex || item.rule.template,
      ),
      h("td", {}, kindBadge(item.rule.kind)),
      h("td", {}, tr(reasons[item.detail_reason || item.reason] || item.reason)),
      h(
        "td",
        {},
        item.scope === "memory" ? tr("Память сессии") : scopeLabel(item.scope),
        h(
          "small",
          { class: "hint" },
          item.rule.note === "Память сессии · источник не сохранён"
            ? tr("Исходный запрос неизвестен")
            : item.rule.note || tr("Источник не сохранён"),
        ),
      ),
      h("td", {}, h("span", {class:"audit-state " + (item.rule.disabled ? "off" : "on")},
        tr(item.rule.disabled ? "Отключено" : "Включено"))),
      h("td", {class:"audit-action"}, h("div", {class:"audit-buttons"}, h("button", {
        type:"button", class:"small secondary", disabled:!info.editable,
        onclick:() => changeAudit([item], !item.rule.disabled),
      }, tr(item.rule.disabled ? "Включить снова" : "Отключить")), h("button", {
        type:"button", class:"small danger", disabled:!info.editable,
        onclick:() => confirmRuleDelete(item, item.index, rulePattern(item.rule)[1], true),
      }, tr(item.scope === "memory" ? "Удалить из памяти" : "Удалить правило")))),
    );
  });
  $("#audit-list").replaceChildren(
    rows.length
      ? h(
          "table",
          {},
          h(
            "thead",
            {},
            h(
              "tr",
              {},
              h("th", {}, selectAll),
              ...["Значение", "Тип", "Причина проверки", "Источник", "Состояние", "Действие"].map(
                (t) => h("th", {}, tr(t)),
              ),
            ),
          ),
          h("tbody", {}, ...rows),
        )
      : h(
          "div",
          { class: "empty-state" },
          icon("check"),
          h("strong", {}, tr("Записей для проверки нет")),
        ),
  );
  if (focusedKey) {
    const row = $("#audit-list").querySelector('[data-audit-entry="' + CSS.escape(focusedKey) + '"]');
    (row?.querySelector(focusedControl) || $("#audit-refresh")).focus({preventScroll:true});
  } else if (wasBulkAction) {
    $("#audit-refresh").focus({preventScroll:true});
  }
}
$("#audit-disable").addEventListener("click", () =>
  changeAudit([...auditSelection.values()], true),
);
$("#audit-enable").addEventListener("click", () =>
  changeAudit([...auditSelection.values()], false),
);
for (const b of $("#rules-view").querySelectorAll("[data-rules-view]"))
  b.addEventListener("click", () => showRulesView(b.dataset.rulesView));
$("#audit-refresh").addEventListener("click", () => loadAudit().catch(fail));
const W = {
  step: 0,
  mode: "proxy",
  domains: "",
  projects: "",
  plan: null,
  busy: false,
};
function captureWizard() {
  const selected = $('input[name="setup-mode"]:checked');
  if (selected) W.mode = selected.value;
  if ($("#setup-domains")) W.domains = $("#setup-domains").value;
  if ($("#setup-projects")) W.projects = $("#setup-projects").value;
}
function setupAnswers() {
  return {
    domains: W.domains.split(/[\s,;]+/).filter(Boolean),
    projects: W.projects
      .split(/\n/)
      .map((x) => x.trim())
      .filter(Boolean),
  };
}
function renderWizard() {
  $("#wizard-progress").replaceChildren(
    ...["Как использовать", "Ваши данные", "Проверка"].map((label, i) =>
      h(
        "span",
        { class: "wizard-step" + (i <= W.step ? " active" : "") },
        `0${i + 1} · ` + tr(label),
      ),
    ),
  );
  const body = $("#wizard-body");
  $("#wizard-back").hidden = W.step === 0;
  $("#wizard-next").textContent = tr(
    W.step === 2 ? "Применить настройки" : "Продолжить",
  );
  $("#wizard-next").disabled = W.busy || (W.step === 2 && !info.editable);
  if (W.step === 0)
    body.replaceChildren(
      h("span", { class: "eyebrow" }, tr("ШАГ 1 ИЗ 3")),
      h("h2", {}, tr("Как вы будете использовать Kaponir?")),
      h(
        "p",
        {},
        tr(
          "Это поможет выбрать инструкции. Все инструменты останутся доступны.",
        ),
      ),
      h(
        "div",
        { class: "choice-grid" },
        ...[
          [
            "proxy",
            "Через API-прокси",
            "Автоматическая защита приложений и агентов",
          ],
          [
            "manual",
            "Через чат в браузере",
            "Подготовка текста и восстановление ответа",
          ],
        ].map(([value, title, desc]) => {
          const radio = h("input", {
            class: "sr-only",
            type: "radio",
            name: "setup-mode",
            value,
          });
          radio.checked = W.mode === value;
          return h(
            "label",
            { class: "choice" },
            radio,
            h(
              "span",
              { class: "choice-top" },
              h(
                "span",
                { class: "choice-icon" },
                icon(value === "proxy" ? "plug" : "flask"),
              ),
              icon("check"),
            ),
            h("strong", {}, tr(title)),
            h("span", {}, tr(desc)),
          );
        }),
      ),
    );
  if (W.step === 1) {
    body.replaceChildren(
      h("span", { class: "eyebrow" }, tr("ШАГ 2 ИЗ 3")),
      h("h2", {}, tr("Что важно скрывать у вас?")),
      h(
        "p",
        {},
        tr(
          "Базовые детекторы уже включены. Добавьте известные вам корпоративные значения или оставьте поля пустыми.",
        ),
      ),
      h(
        "label",
        {},
        tr("Внутренние домены"),
        h("input", {
          id: "setup-domains",
          placeholder: "corp.example, internal.example",
          value: W.domains,
          spellcheck: "false",
        }),
      ),
      h(
        "label",
        {},
        tr("Конфиденциальные названия проектов"),
        h(
          "textarea",
          {
            id: "setup-projects",
            placeholder: tr("Одно название на строку"),
            spellcheck: "false",
          },
          W.projects,
        ),
      ),
      h(
        "p",
        { class: "hint" },
        tr(
          "Укажите только конфиденциальные названия. Эти значения будут маскироваться во всех текстах по точному совпадению.",
        ),
      ),
    );
  }
  if (W.step === 2) {
    body.replaceChildren(
      h("span", { class: "eyebrow" }, tr("ШАГ 3 ИЗ 3")),
      h("h2", {}, tr("Проверьте перед сохранением")),
      h(
        "p",
        {},
        tr(
          "Добавим только личные MASK-правила. Существующие настройки сохранятся, внешние модели не подключаются.",
        ),
      ),
      h(
        "div",
        { class: "wizard-preview" },
        h("strong", {}, tr("Домены: {{0}}", [W.plan?.domains.length || 0])),
        h("p", {}, W.plan?.domains.join(", ") || tr("Не добавляются")),
        h("strong", {}, tr("Проекты: {{0}}", [W.plan?.rules.length || 0])),
        h(
          "p",
          {},
          W.plan?.rules.map((r) => r.value).join(", ") || tr("Не добавляются"),
        ),
      ),
    );
    if (!info.editable)
      body.append(
        h(
          "p",
          { class: "err" },
          tr(
            "Этот сервер не управляет правилами (запущен без каталога правил).",
          ),
        ),
      );
  }
}
$("#wizard-back").addEventListener("click", () => {
  captureWizard();
  W.step = Math.max(0, W.step - 1);
  renderWizard();
});
$("#wizard-skip").addEventListener("click", () => {
  captureWizard();
  showTab("overview");
});
$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (W.busy) return;
  captureWizard();
  W.busy = true;
  $("#wizard-next").disabled = true;
  try {
    if (W.step === 0) W.step = 1;
    else if (W.step === 1) {
      const result = await api("/api/v1/ui/setup", {
        method: "POST",
        body: setupAnswers(),
      });
      W.plan = result.plan;
      W.step = 2;
    } else {
      await api("/api/v1/ui/setup", {
        method: "POST",
        body: { ...setupAnswers(), apply: true },
      });
      await loadInfo();
      toast(tr("Настройки применены"));
      W.step = 0;
      showTab(W.mode === "manual" ? "mask" : "connect");
      return;
    }
    renderWizard();
  } catch (error) {
    fail(error);
  } finally {
    W.busy = false;
    $("#wizard-next").disabled = W.step === 2 && !info.editable;
  }
});

Object.assign(TABS, {
  overview: loadDashboard,
  events: loadDashboard,
  connect: renderConnection,
  settings: () => {},
  proxy: renderSettings,
  setup: renderWizard,
  analysis: loadAnalysis,
  rules: () => Promise.all([loadRules(), showRulesView(rulesView)]),
});
for (const input of $("#language-switch").querySelectorAll("input"))
  input.checked = input.value === I18N.language;
$("#language-switch").addEventListener("change", async (e) => {
  await window.uiReady;
  if (!e.target.checked) return;
  captureWizard();
  I18N.setLanguage(e.target.value);
  relocalizeConstants();
  localizeReviewKinds();
  I18N.apply();
  refreshFilePickers();
  renderAdhoc();
  renderMask();
  renderDiscover();
  syncRuleForm();
  if (LearningUI.analysis) renderLearningForms();
  renderLearningWidget();
  if (typeof renderReview === "function") renderReview(true);
  if (WORKER.data) renderWorker();
  await loadInfo().catch(fail);
  showTab(location.hash.slice(1) || "overview", false);
});
// The same versioned JSON is accepted by `kaponir config import`.
$("#config-export").addEventListener("click", async () => {
  const button = $("#config-export");
  button.disabled = true;
  try {
    const bundle = await api("/api/v1/ui/config/export", {
      method: "POST",
      body: { include_secrets: $("#config-secrets").checked },
    });
    const text = JSON.stringify(bundle, null, 2);
    $("#config-json").value = text;
    downloadJson(text, "kaponir-settings.json");
    $("#config-status").textContent = tr("Настройки экспортированы");
  } catch (e) {
    $("#config-status").textContent = e.message;
    fail(e);
  } finally {
    button.disabled = false;
  }
});
function previewSettingsImport() {
  const status=$("#config-status");
  try {
    const bundle=JSON.parse($("#config-json").value);
    if(bundle.format!=="kaponir-settings" || !bundle.config || !bundle.rules || !bundle.providers){status.textContent="";return;}
    const collections=Object.entries(bundle.rules).map(([scope,rules])=>scopeLabel(scope)+" · "+(rules.rule||[]).length);
    const connections=bundle.providers.connections || [];
    status.textContent=tr("Импорт заменит настройки установки, правила и подключения")+" · "+collections.join(" / ")+" · "+tr("Подключения · {{0}}",[connections.length]);
  }catch{status.textContent="";}
}
$("#config-json").addEventListener("input",previewSettingsImport);
$("#config-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    if (file.size > 1024 * 1024)
      throw new Error(tr("Файл настроек больше 1 МБ"));
    $("#config-json").value = await file.text();
    previewSettingsImport();
  } catch (e) {
    fail(e);
  }
});
$("#config-import").addEventListener("click", async () => {
  const button = $("#config-import");
  button.disabled = true;
  $("#config-status").textContent = "";
  try {
    let bundle;
    try {
      bundle = JSON.parse($("#config-json").value);
    } catch {
      throw new Error(tr("Некорректный JSON настроек"));
    }
    await api("/api/v1/ui/config/import", { method: "POST", body: bundle });
    window.dispatchEvent(new Event("kaponir-settings-imported"));
    await loadInfo();
    renderSettings();
    $("#config-json").value = "";
    $("#config-file").value = "";
    refreshFilePickers();
    $("#config-status").textContent = tr(
      "Настройки применены. Перезапустите сервер для обновления всех параметров, включая адрес и доступ.",
    );
  } catch (e) {
    $("#config-status").textContent = e.message;
    fail(e);
  } finally {
    button.disabled = false;
  }
});
