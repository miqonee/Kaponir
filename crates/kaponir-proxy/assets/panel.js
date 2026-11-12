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
  overview: ["", "Дашборд", ""],
  events: ["", "События", "Сессии и запросы к моделям."],
  connect: [
    "",
    "Поставщики и API",
    "Подключения и адреса API для ваших приложений.",
  ],
  analysis: ["", "LLM и Анализ", "Анализ запросов, модели и распределение правил."],
  worker: ["", "LLM и Анализ", "Фоновый воркер и очередь."],
  categorization: ["", "LLM и Анализ", "Категоризация найденных данных."],
  rules: ["", "Правила", ""],
  review: ["", "Разбор решений", "Спорные находки и подтверждённые решения."],
  discover: ["", "Правила из документов", ""],
  mask: ["", "Лаборатория", ""],
  restore: ["", "Лаборатория", ""],
  settings: ["", "Настройки", ""],
  setup: ["", "Настройка правил", ""],
  metrics: [
    "",
    "Диагностика",
    "Технические операции движка, включая лабораторию.",
  ],
};
function panelHeading(name) {
  const [, title, description] = PAGE_META[name];
  $("#page-title").textContent = tr(title);
  $("#breadcrumb").textContent = tr(title);
  $("#page-description").textContent = tr(description);
  $("#page-description").hidden = !description;
  $("#page-actions").hidden = !["overview", "events"].includes(name);
  $("#lab-navigation").hidden = !["mask", "restore"].includes(name);
  $("#learning-widget").hidden = !["analysis", "categorization", "worker"].includes(name);
  $("#analysis-navigation").hidden = !["analysis", "categorization", "worker"].includes(name);
  for (const b of document.querySelectorAll(
    "#lab-navigation [data-go],#analysis-navigation [data-go]",
  ))
    b.setAttribute("aria-pressed", String(b.dataset.go === name));
  $("#sidebar").classList.remove("open");
  $("#menu-toggle").setAttribute("aria-expanded", "false");
  document.title = tr(title) + " · Kaponir";
}
for (const b of document.querySelectorAll("[data-go]"))
  b.addEventListener("click", () => showTab(b.dataset.go));
$("#menu-toggle").addEventListener("click", () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    $("#sidebar").classList.remove("open");
    $("#menu-toggle").setAttribute("aria-expanded", "false");
  }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#sidebar,#menu-toggle")) {
    $("#sidebar").classList.remove("open");
    $("#menu-toggle").setAttribute("aria-expanded", "false");
  }
});
window.addEventListener("hashchange", () =>
  showTab(location.hash.slice(1) || "overview", false),
);

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
function hasIssue(e) {
  if (e.review_reasons) return ["error", "restore_error", "cancelled"].includes(e.outcome) || e.review_reasons.some((reason) => reason.state !== "resolved");
  return (
    ["error", "restore_error", "cancelled"].includes(e.outcome) ||
    e.usage?.unknown ||
    e.usage?.denied ||
    e.usage?.uncertain
  );
}
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
}
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

function eventOutcome(e) {
  return e.outcome;
}
function outcomeLabel(e) {
  return tr(
    {
      ok: "Обработан",
      error: "Ошибка",
      restore_error: "Ошибка восстановления",
      cancelled: "Прерван",
      in_progress: "Выполняется",
      needs_review: "Требует проверки",
    }[eventOutcome(e)] || "Ошибка",
  );
}
function eventsTable(events, compact = false) {
  if (!events.length)
    return h(
      "div",
      { class: "empty-state" },
      icon("list"),
      h("strong", {}, tr("Запросов за период нет")),
    );
  const titles = compact
    ? ["Время", "Модель", "Замены", "Результат", ""]
    : ["Время", "Модель", "Замены", "Kaponir / Поставщик", "Результат", ""];
  return h(
    "table",
    {},
    h("thead", {}, h("tr", {}, ...titles.map((t) => h("th", {}, tr(t))))),
    h(
      "tbody",
      {},
      ...events.map((e) =>
        h(
          "tr",
          {},
          h(
            "td",
            { class: "mono" },
            time(e.started_at_ms || e.time_ms),
            e.streaming ? h("small", { class: "hint" }, " · SSE") : null,
            e.request_kind === "count_tokens"
              ? h("small", { class: "hint" }, tr("Подсчёт токенов"))
              : null,
          ),
          h(
            "td",
            {},
            h("div", { class: "model-name" }, e.model || e.provider),
            h("small", { class: "hint" }, e.connection_id || e.provider),
          ),
          h(
            "td",
            { class: "num" },
            number(e.usage?.replacements),
            e.usage?.cached_replacements
              ? h(
                  "small",
                  { class: "hint" },
                  " +" +
                    number(e.usage.cached_replacements) +
                    " " +
                    tr("из истории"),
                )
              : null,
          ),
          ...(compact
            ? []
            : [
                h(
                  "td",
                  { class: "mono" },
                  `${(proxyUs(e) / 1000).toFixed(2)} / ${((e.timings?.upstream_us || 0) / 1000).toFixed(0)} ${tr("мс")}`,
                ),
              ]),
          h(
            "td",
            {},
            h(
              "span",
              { class: "event-status " + eventOutcome(e) },
              outcomeLabel(e),
            ),
            (e.review_reasons || []).length ? h("button", { class: "text-button event-review-link", onclick: () => openInspector(e.id, "review") },
              tr(e.review_reasons.some((reason) => reason.state !== "resolved") ? "Проверка: {{0}} причин" : "Проверка закрыта: {{0}}", [e.review_reasons.length])) : null,
            e.status >= 400
              ? h("small", { class: "hint" }, " HTTP " + e.status)
              : null,
          ),
          h(
            "td",
            {},
            h(
              "button",
              {
                class: "small ghost",
                onclick: () => openInspector(e.id),
                "aria-label": tr("Открыть запрос {{0}}", [e.id]),
                "data-request-id": e.id,
              },
              icon("arrow"),
            ),
          ),
        ),
      ),
    ),
  );
}
function sessionsTable(events, compact = false) {
  const groups = new Map();
  for (const e of events) {
    const key = e.session_id || "request-" + e.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  let entries = [...groups.entries()].sort(
    (a, b) =>
      Math.max(...b[1].map((e) => e.time_ms)) -
      Math.max(...a[1].map((e) => e.time_ms)),
  );
  if (!compact && eventFilter === "issues")
    entries = entries.filter(([, es]) => es.some(hasIssue));
  if (compact) entries = entries.slice(0, 5);
  if (!entries.length)
    return h(
      "div",
      { class: "empty-state" },
      icon("list"),
      h("strong", {}, tr("Сессий за период нет")),
    );
  const rows = [];
  for (const [key, es] of entries) {
    es.sort((a, b) => b.time_ms - a.time_ms || b.id - a.id);
    const latest = es[0],
      inferred = latest.session_source === "inferred",
      isSession = !!latest.session_id;
    const expanded = expandedSessions.has(key),
      errors = es.filter(hasIssue).length;
    const name = isSession
      ? tr("Сессия {{0}}", [key.slice(0, 8)])
      : tr("Запрос {{0}}", [latest.id]);
    const trigger = h(
      "button",
      {
        class: "session-toggle",
        "data-session-key": key,
        "aria-expanded": String(expanded),
        onclick: () => {
          expandedSessions.has(key)
            ? expandedSessions.delete(key)
            : expandedSessions.add(key);
          renderEventViews();
        },
      },
      icon(expanded ? "chevron" : "arrow"),
      h("span", {}, name),
    );
    rows.push(
      h(
        "tr",
        { class: "session-row" },
        h(
          "td",
          {},
          trigger,
          inferred
            ? h(
                "small",
                {
                  class: "hint session-method",
                  title: tr(
                    "По началу диалога; одинаковые начала могут совпадать",
                  ),
                },
                tr("Автоматически"),
              )
            : null,
        ),
        h("td", { class: "mono" }, time(latest.time_ms)),
        h(
          "td",
          { class: "model-name" },
          [...new Set(es.map((e) => e.model || e.provider))].join(", "),
        ),
        h("td", { class: "num" }, number(es.length)),
        h(
          "td",
          { class: "num" },
          number(es.reduce((n, e) => n + (e.usage?.replacements || 0), 0)),
        ),
        h(
          "td",
          {},
          h(
            "span",
            {
              class:
                "event-status " +
                (errors
                  ? "error"
                  : es.some((e) => e.outcome === "in_progress")
                    ? "in_progress"
                    : "ok"),
            },
            errors
              ? tr("Проблем: {{0}}", [errors])
              : es.some((e) => e.outcome === "in_progress")
                ? tr("Выполняется")
                : tr("Обработан"),
          ),
        ),
      ),
    );
    if (expanded)
      rows.push(
        h(
          "tr",
          { class: "session-children" },
          h("td", { colspan: 6 }, eventsTable(es, false)),
        ),
      );
  }
  return h(
    "table",
    { class: "sessions-table" },
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        ...[
          "Сессия / запрос",
          "Последняя активность",
          "Модель",
          "Запросы",
          "Замены",
          "Состояние",
        ].map((t) => h("th", {}, tr(t))),
      ),
    ),
    h("tbody", {}, ...rows),
  );
}
function renderEventViews() {
  if ($("#inspector-dialog").open) return;
  const focused = document.activeElement;
  const origin = focused?.closest("#recent-events,#events-table");
  const attribute = focused?.hasAttribute("data-request-id")
    ? "data-request-id"
    : "data-session-key";
  const value = focused?.getAttribute(attribute);
  $("#recent-events").replaceChildren(
    sessionsTable(filteredEvents(false), true),
  );
  $("#events-table").replaceChildren(
    eventsView === "sessions"
      ? sessionsTable(filteredEvents(false))
      : eventsTable(filteredEvents()),
  );
  if (origin && value) {
    const target = [
      ...origin.querySelectorAll("[data-request-id],[data-session-key]"),
    ].find((el) => el.getAttribute(attribute) === value);
    (target || $("#events-refresh")).focus({ preventScroll: true });
  }
}
async function openInspector(id, initialTab = null) {
  try {
    const ev = await api("/api/v1/ui/events/" + id),
      insp = ev.inspector || {},
      dlg = $("#inspector-dialog");
    const nav = h("div", { class: "section-tabs inspector-tabs" }),
      body = h("div", { class: "inspector-body" });
    const pane = (title, value) =>
      h(
        "section",
        { class: "inspector-pane" },
        h(
          "div",
          { class: "inspector-pane-head" },
          h("span", {}, tr(title)),
          h(
            "button",
            {
              class: "small ghost",
              onclick: () =>
                copy(value || "").then(() => toast(tr("Скопировано"))),
            },
            tr("Копировать"),
          ),
        ),
        h("pre", {}, value || tr("Нет данных")),
      );
    const tabs = {
      review: () => {
        const reasons = ev.review_reasons || [], nodes = [];
        const titles = { detector_uncertain: "Спорное решение детектора", legacy_uncertain: "Спорные решения без сохранённой связи", unknown_restore_token: "Неизвестный токен восстановления", restore_access_denied: "Нет доступа к восстановлению" };
        for (const reason of reasons) {
          const tokens = reason.code === "unknown_restore_token" ? ev.usage?.unknown_tokens : reason.code === "restore_access_denied" ? ev.usage?.denied_tokens : null;
          nodes.push(h("article", { class: "panel review-card" }, h("strong", {}, tr(titles[reason.code] || reason.code)),
            h("p", {}, tr("Значений: {{0}}", [reason.count]), " · ", tr(reason.state === "resolved" ? "Проверка закрыта" : "Требует проверки")),
            reason.code === "legacy_uncertain" ? h("p", { class: "hint" }, tr("Старая версия сохранила число спорных решений, но не их связи с этим событием.")) : null,
            tokens?.length ? h("p", { class: "mono" }, tokens.join(" · ")) : null,
            reason.code === "restore_access_denied" ? h("p", { class: "hint" }, tr("Токен найден, но текущий пользователь не имеет доступа к его области. Проверьте настройки доступа; это не спорная оценка детектора.")) : null,
            reason.code === "unknown_restore_token" ? h("p", { class: "hint" }, tr("Для токена нет соответствия в Vault. Проверьте исходную сессию, хранилище и ответ поставщика.")) : null,
            reason.keys?.length ? h("button", { class: "secondary", onclick: () => { dlg.close(); showReview(reason.keys, reason.state === "resolved" ? "history" : "pending"); } }, tr("Открыть связанные решения")) : null));
        }
        for (const reference of ev.usage?.review_items || []) nodes.push(h("div", { class: "review-card" },
          h("code", {}, reference.value), h("p", {}, `${kindLabel(reference.kind)} · ${tr("Оценка детектора")}: ${(reference.probability * 100).toFixed(1)}%`),
          h("p", { class: "hint" }, tr(reference.applied === "mask" ? "В этом запросе значение замаскировано по политике." : "В этом запросе значение осталось в тексте.")),
          h("blockquote", {}, reference.context)));
        if (ev.error) nodes.push(h("div", { class: "event-error-banner" }, h("strong", {}, ev.error.kind), h("p", {}, ev.error.message)));
        body.replaceChildren(...(nodes.length ? nodes : [h("p", { class: "hint" }, tr("Причин для проверки не зарегистрировано"))]));
      },
      request: () => {
        let raw = false;
        const right = pane("Отправлено поставщику", insp.masked_request);
        const toggle = h(
          "button",
          {
            class: "small secondary",
            onclick: () => {
              raw = !raw;
              right.querySelector("pre").textContent =
                (raw ? insp.masked_payload_json : insp.masked_request) ||
                tr("Нет данных");
              toggle.textContent = tr(raw ? "Текст" : "JSON");
            },
          },
          "JSON",
        );
        right.querySelector(".inspector-pane-head").append(toggle);
        body.replaceChildren(
          h(
            "div",
            { class: "inspector-panes" },
            pane("Исходный запрос", insp.original_request),
            right,
          ),
        );
      },
      response: () =>
        body.replaceChildren(
          h(
            "div",
            { class: "inspector-panes" },
            pane("Ответ поставщика", insp.raw_response),
            pane("Возвращено клиенту", insp.restored_response),
          ),
        ),
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
  $("#connection-state").classList.toggle("offline", !online);
  $("#connection-state span").textContent = tr(
    online ? "Прокси доступен" : "Нет соединения",
  );
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
    for (const x of b.parentElement.children)
      x.setAttribute("aria-pressed", String(x === b));
    renderEventViews();
  });
for (const b of $("#events-view").children)
  b.addEventListener("click", () => {
    eventsView = b.dataset.view;
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
}
let rulesView = "list";
function showRulesView(view) {
  rulesView = view;
  for (const b of $("#rules-view").children)
    b.setAttribute("aria-pressed", String(b.dataset.rulesView === view));
  for (const el of $("#tab-rules").children)
    if (el.id !== "rules-view")
      el.hidden =
        el.id === "rules-audit"
          ? view !== "audit"
          : view === "audit" || (el.id === "rf" && !info.editable);
  if (view === "audit") loadAudit().catch(fail);
}
const auditSelection = new Map();
let auditBusy = false;
function updateAuditSelection() {
  $("#audit-selection").hidden = !auditSelection.size;
  $("#audit-selected-count").textContent = tr("Выбрано: {{0}}", [
    auditSelection.size,
  ]);
  $("#audit-disable").disabled =
    auditBusy || ![...auditSelection.values()].some((i) => !i.rule.disabled);
  $("#audit-enable").disabled =
    auditBusy || ![...auditSelection.values()].some((i) => i.rule.disabled);
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
  } catch (e) {
    fail(e);
    await loadAudit().catch(() => {});
  } finally {
    auditBusy = false;
    updateAuditSelection();
  }
}
async function loadAudit() {
  const data = await api("/api/v1/ui/rules/audit");
  auditSelection.clear();
  updateAuditSelection();
  const reasons = {
    disabled: "Отключено",
    weak_value: "Недостаточно оснований",
    invalid_ip: "Не похоже на полный IP-адрес",
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
      "aria-label": tr("Выбрать ") + (item.rule.value || item.rule.kind),
      disabled: !info.editable,
      onchange: (e) => {
        const key = item.scope + ":" + item.index;
        if (e.target.checked) auditSelection.set(key, item);
        else auditSelection.delete(key);
        selectAll.checked = auditSelection.size === data.items.length;
        updateAuditSelection();
      },
    });
    return h(
      "tr",
      {},
      h("td", {}, checkbox),
      h(
        "td",
        { class: "mono" },
        item.rule.value || item.rule.regex || item.rule.template,
      ),
      h("td", {}, kindBadge(item.rule.kind)),
      h("td", {}, tr(reasons[item.reason] || item.reason)),
      h(
        "td",
        {},
        item.scope === "memory" ? tr("Память сессии") : item.scope,
        h(
          "small",
          { class: "hint" },
          item.rule.note === "Память сессии · источник не сохранён"
            ? tr(item.rule.note)
            : item.rule.note || tr("Источник не сохранён"),
        ),
      ),
      h(
        "td",
        {},
        h(
          "button",
          {
            class: "small secondary",
            disabled: !info.editable,
            onclick: () => changeAudit([item], !item.rule.disabled),
          },
          tr(item.rule.disabled ? "Восстановить" : "Отключить"),
        ),
      ),
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
              ...["Значение", "Тип", "Причина проверки", "Источник", ""].map(
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
          h("strong", {}, tr("Подозрительных правил не найдено")),
        ),
  );
}
$("#audit-disable").addEventListener("click", () =>
  changeAudit([...auditSelection.values()], true),
);
$("#audit-enable").addEventListener("click", () =>
  changeAudit([...auditSelection.values()], false),
);
for (const b of $("#rules-view").children)
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
  settings: renderSettings,
  setup: renderWizard,
  analysis: loadAnalysis,
  rules: async () => {
    await loadRules();
    showRulesView(rulesView);
  },
});
for (const input of $("#language-switch").querySelectorAll("input"))
  input.checked = input.value === I18N.language;
$("#language-switch").addEventListener("change", async (e) => {
  captureWizard();
  I18N.setLanguage(e.target.value);
  relocalizeConstants();
  I18N.apply();
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
    const url = URL.createObjectURL(
      new Blob([text], { type: "application/json" }),
    );
    const link = h("a", { href: url, download: "kaponir-settings.json" });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $("#config-status").textContent = tr("Настройки экспортированы");
  } catch (e) {
    $("#config-status").textContent = e.message;
    fail(e);
  } finally {
    button.disabled = false;
  }
});
$("#config-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    if (file.size > 1024 * 1024)
      throw new Error(tr("Файл настроек больше 1 МБ"));
    $("#config-json").value = await file.text();
    $("#config-status").textContent = "";
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
