"use strict";

const scenarioLabels = {
  standard: "Стандартная проверка",
  A: "Определить тип и действие (A)",
  B: "Учесть политику приватности (B)",
  C: "Проверить конкретное вхождение (C)",
  D: "Объяснить решение по вхождению (D)",
};
const scenarioDescriptions = {
  A: "Базовая проверка предлагает действие, тип значения и область правила.",
  B: "Учитывает политику приватности при выборе действия, типа и области.",
  C: "Дополнительно отмечает точное вхождение и проверяет его тип и область.",
  D: "Проверяет выделенное вхождение и объясняет решение. Тип и область берутся из находки.",
};
let benchmarkRequest;
function loadScenarioBenchmarks() {
  if (!benchmarkRequest) benchmarkRequest = fetch("scenario-benchmarks.json").then(response => {
    if (!response.ok) throw new Error(response.statusText);
    return response.json();
  }).catch(error => { benchmarkRequest = null; throw error; });
  return benchmarkRequest;
}
function measuredModelId(id, models = WORKER.data?.models || {}) {
  const sha = models[id]?.sha256;
  return studyArtifacts[sha] || Object.keys(searchProfiles).find(key => sha && models[key]?.sha256 === sha) || id;
}
function scenarioOptions(selected) {
  return Object.entries(scenarioLabels).map(([value, label]) =>
    h("option", {value, selected: value === selected}, tr(label)));
}
function benchNumber(value) {
  return new Intl.NumberFormat(I18N.language, {maximumFractionDigits: 1}).format(value);
}
function benchMinutes(value) {
  return value == null ? "—" : tr("{{0}} мин", [benchNumber(value)]);
}
function rankedScenarios(rows, metric, best) {
  const target = best(...rows.map(row => row[metric]));
  return rows.filter(row => row[metric] === target).map(row => row.variant).join(", ");
}
function referenceScenarioTable(data) {
  const variants = Object.keys(scenarioDescriptions);
  const cohort = Object.values(data).filter(model => model.series === "native" &&
    variants.every(v => model.variants[v]?.answers === 32 && model.variants[v].minutes != null));
  if (!cohort.length) return null;
  const rows = variants.map(v => {
    const runs = cohort.map(model => model.variants[v]);
    const mean = runs.reduce((n, r) => n + r.minutes, 0) / runs.length;
    const total = runs.reduce((n, r) => n + r.private_mask + r.public_keep, 0);
    return {variant: v, minutes: mean, correct: total};
  });
  return h("details", {class: "scenario-comparison"}, h("summary", {}, tr("Среднее по стенду · {{0}} моделей", [cohort.length])),
    h("p", {}, tr("Быстрее всего · {{0}}. Меньше всего верных решений · {{1}}.", [
      rankedScenarios(rows, "minutes", Math.min), rankedScenarios(rows, "correct", Math.min)])),
    h("p", {class: "hint"}, tr("Каждая модель получила одинаковые 16 приватных и 16 публичных значений во всех четырёх сценариях. Время включает повторы запросов.")),
    benchmarkTable(["Сценарий", "Среднее время за 32", "Верные решения"],
      rows.map(r => [r.variant, benchMinutes(r.minutes), r.correct + " / " + (32 * cohort.length)])));
}
function benchmarkTable(headers, rows, selected) {
  return h("div", {class: "table-wrap"}, h("table", {},
    h("thead", {}, h("tr", {}, headers.map(title => h("th", {scope: "col"}, tr(title))))),
    h("tbody", {}, rows.map(([variant, ...cells]) => h("tr", {class: variant === selected ? "selected-scenario" : ""},
      h("th", {scope: "row"}, variant), cells.map(cell => h("td", {}, cell)))))));
}
function scenarioPriorities(rows, control, host) {
  if (rows.length !== 4 || !control) return null;
  const study = host.dataset.study;
  const priorities = [
    ["speed", "Быстрее закончить", (a,b) => a.minutes-b.minutes || b.correct-a.correct],
    ["privacy", "Скрыть больше приватного", (a,b) => b.private_mask-a.private_mask || b.public_keep-a.public_keep || a.minutes-b.minutes],
    ["public", "Меньше лишних скрытий", (a,b) => b.public_keep-a.public_keep || b.private_mask-a.private_mask || a.minutes-b.minutes],
  ];
  return h("fieldset", {class:"scenario-goals"}, h("legend", {}, tr("Что важнее при проверке?")),
    h("div", {class:"goal-grid"}, priorities.map(([goal, label, compare]) => {
      const best = [...rows].sort(compare)[0];
      const evidence = goal === "speed" ? tr("{{0}} за 32 значения", [benchMinutes(best.minutes)])
        : goal === "privacy" ? tr("Скрыто приватных · {{0}}/16", [best.private_mask])
          : tr("Сохранено публичных · {{0}}/16", [best.public_keep]);
      return h("button", {type:"button", class:"scenario-goal", "data-goal":goal, disabled:control.disabled,
        onclick:() => {
          const select = host.scenarioControl;
          if (host.dataset.study !== study || !select || select.disabled || select.value === best.variant) return;
          select.value = best.variant;
          select.dispatchEvent(new Event("change", {bubbles:true}));
        }}, h("strong", {}, tr(label)), h("span", {}, best.variant + " · " + evidence));
    })), h("p", {class:"hint"}, tr("Выбор подставит сценарий по замерам этой модели на стенде.")));
}
async function renderScenarioStudy(host, id, selected, control) {
  host.scenarioControl = control;
  const key = JSON.stringify([id || "", selected || "", !!control, !!control?.disabled, I18N.language]);
  if (host.dataset.study === key) return;
  host.dataset.study = key;
  const expanded = !!host.querySelector(".model-comparison[open]");
  const globalOpen = !!host.querySelector(".scenario-comparison[open]");
  const focusedGoal = host.contains(document.activeElement) ? document.activeElement.dataset.goal : null;
  let data;
  try { data = await loadScenarioBenchmarks(); }
  catch {
    if (host.dataset.study === key) {
      delete host.dataset.study;
      host.textContent = tr("Не удалось загрузить замеры сценариев.");
    }
    return;
  }
  if (host.dataset.study !== key) return;
  const restoreGoal = focusedGoal && host.contains(document.activeElement);
  host.replaceChildren();
  const model = Object.hasOwn(data, id) ? data[id] : null;
  if (selected && scenarioDescriptions[selected]) host.append(h("p", {}, tr(scenarioDescriptions[selected])));
  if (model) {
    const variant = selected || model.recommended, row = model.variants[variant];
    const summary = h("div", {class: "benchmark-summary"},
      h("strong", {}, tr("Рекомендация для модели · {{0}}", [model.recommended])));
    if (row) summary.append(h("span", {}, tr("{{0}} · верно {{1}} из 32 · {{2}} на стенде",
      [variant, row.private_mask + row.public_keep, benchMinutes(row.minutes)])));
    else summary.append(h("span", {}, tr("Для выбранного режима замера на этом профиле нет.")));
    const complete = Object.entries(model.variants).filter(([, m]) => m.minutes != null && m.answers === 32)
      .map(([v, m]) => ({...m, variant: v, correct: m.private_mask + m.public_keep}));
    const fastest = complete.length === 4 ? rankedScenarios(complete, "minutes", Math.min) : "";
    if (fastest) summary.append(h("span", {}, tr("Быстрее всего · {{0}}", [fastest])));
    const goals = scenarioPriorities(complete, control, host);
    if (goals) host.prepend(goals);
    else if (control) host.append(h("p", {class:"hint"}, tr("Сопоставимые замеры есть только для части сценариев. Выберите сценарий вручную.")));
    host.append(summary);
    const quick = model.variants[fastest], recommended = model.variants[model.recommended];
    if (quick && recommended && fastest !== model.recommended) {
      const comparison = (label, metric) => h("p", {class: "hint"}, tr(label,
        [model.recommended, recommended[metric], fastest, quick[metric]]));
      if (recommended.private_mask > quick.private_mask) host.append(comparison(
        "{{0}} скрыл {{1}} из 16 приватных значений, самый быстрый {{2}} — {{3}} из 16.", "private_mask"));
      else if (recommended.public_keep > quick.public_keep) host.append(comparison(
        "{{0}} сохранил {{1}} из 16 публичных значений, самый быстрый {{2}} — {{3}} из 16.", "public_keep"));
    }
    const rows = Object.keys(scenarioDescriptions).map(v => {
      const m = model.variants[v];
      return m ? [v, m.private_mask + "/16", m.public_keep + "/16", m.answers + "/32", benchMinutes(m.minutes)]
        : [v, "—", "—", "—", "—"];
    });
    const series = {archive: "Архивный профиль модели", native: "Испытание нативного профиля", tuned: "Замер настроенного профиля", epoch2: "Испытание Kaponir Privacy epoch-2"};
    host.append(h("details", {class: "model-comparison", open: expanded},
      h("summary", {}, tr("Сравнить A–D для модели")),
      complete.length === 4 ? h("p", {class: "hint"}, tr("Меньше всего верных решений · {{0}}", [rankedScenarios(complete, "correct", Math.min)])) : null,
      benchmarkTable(["Сценарий", "Скрыто приватных", "Сохранено публичных", "Получено ответов", "Время за 32"], rows, selected),
      h("p", {class: "hint"}, tr(series[model.series]) + ". " + tr("Время зависит от оборудования. Прочерк означает отсутствие замера."))));
  } else {
    host.append(h("p", {class: "hint"}, tr("Для этого подключения нет подтверждённых замеров модели.")));
  }
  const reference = referenceScenarioTable(data);
  if (reference) { reference.open = globalOpen; host.append(reference); }
  if (restoreGoal) host.querySelector('[data-goal="'+focusedGoal+'"]')?.focus({preventScroll:true});
}
