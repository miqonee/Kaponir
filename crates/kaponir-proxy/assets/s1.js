"use strict";

const LearningUI = {
  analysis: null,
  categorization: null,
  history: null,
  dirty: new Set(),
  busy: new Set(),
  view: "pending",
  timer: null,
};
const learningStates = {
  queued: "В очереди",
  running: "Выполняется",
  succeeded: "Обработано",
  failed: "Ошибка",
  evicted: "Вытеснено",
  expired: "Истёк срок",
  pending: "На подтверждение",
  active: "Правило действует",
  rejected: "Отклонено",
  rolled_back: "Отменено",
  rolling_back: "Отменяется",
  applying: "Применяется",
  conflict: "Есть правило",
};
const learningErrors = {
  learning_queue_full: "Очередь заполнена. Повторите после освобождения места.",
  interrupted_application:
    "Применение было прервано. Требуется подтверждение или повтор задания.",
  analysis_provider_required: "Выберите и сохраните источник модели",
  analysis_disabled: "Включите фоновое обнаружение в настройках",
  read_only: "Этот сервер работает в режиме только для чтения.",
  model_timeout: "Модель не ответила вовремя",
  model_connection_failed: "Не удалось подключиться к модели",
  model_http_error: "Модель вернула ошибку HTTP",
  local_model_unavailable:
    "Локальная модель недоступна. Проверьте веса и воркер.",
  categorization_disabled: "Категоризация выключена",
  categorization_failed: "Не удалось категоризировать находку",
  choice_protocol_required:
    "Для категоризации нужен OpenAI или SystemOne Choice",
  classifier_keep: "Классификатор считает значение публичным",
  previously_rejected: "Ранее отклонено пользователем",
  existing_rule: "Для значения уже есть правило",
  configuration_changed: "Подключение изменилось. Повторите задание.",
  invalid_model_json: "Модель вернула некорректный JSON",
  invalid_candidate_response: "Модель нарушила формат находок или допустимые типы. Попробуйте режим «Только значения» в дополнительных параметрах.",
  invalid_classification_response: "Некорректный ответ категоризации",
  model_response_truncated: "Ответ модели обрезан",
  model_context_budget_exceeded: "Вход и лимит ответа превышают контекст модели. Уменьшите вход или увеличьте контекст в дополнительных параметрах.",
  model_input_budget_too_small: "В целевой вход не помещается инструкция модели. Увеличьте целевой вход или уменьшите контекст фрагмента в дополнительных параметрах.",
  invalid_inference_settings: "Проверьте контекст, лимит ответа, резерв, размер пакета и формат JSON.",
  invalid_chunk_settings: "Проверьте пределы разбиения в дополнительных параметрах.",
  document_fragment_limit: "Достигнут предел обращений на документ. Найденное сохранено на подтверждение. Увеличьте предел или разделите документ.",
  model_fragment_cannot_split: "Фрагмент уже нельзя уменьшить. Проверьте контекст, лимит ответа и совместимость модели с JSON.",
  analysis_partial: "Находка из незавершённого анализа. Требуется подтверждение.",
  model_context_or_schema_error: "Среда модели отклонила запрос: проверьте объём контекста и совместимость формата ответа.",
  model_runtime_http_error: "Среда локальной модели вернула ошибку HTTP.",
  model_runtime_start_failed: "Среда не смогла запустить модель. Проверьте совместимость GGUF с этой версией llama.cpp и доступную память.",
  analysis_failed: "Анализ не завершён. Проверьте настройки модели.",
  source_too_large: "Документ слишком большой",
  evaluation_input_too_large: "Исходник вместе с контекстом превышает допустимый размер запроса к локальной модели.",
  document_chunk_limit: "Разделите документ на меньшие части",
  candidate_limit: "Слишком много находок. Разделите документ.",
  job_not_retryable: "Исходник недоступен для повтора",
  rule_was_edited: "Правило изменено вручную. Проверьте его в разделе правил.",
};
const learningError = (code) => tr(learningErrors[code] || code || "—");
const bytesMiB = (bytes) =>
  bytes == null ? "—" : `${(bytes / 1048576).toFixed(1)} MiB`;

function learningFormVisibility() {
  const a = $("#analysis-provider").value,
    c = $("#categorization-provider").value;
  $("#analysis-local-fields").hidden = a !== "local";
  $("#analysis-api-fields").hidden = a !== "api";
  $("#categorization-local-fields").hidden = c !== "local";
  $("#categorization-api-fields").hidden = c !== "s1";
  $("#categorization-none-note").hidden = c !== "none";
  $("#categorization-inference-settings").hidden = c === "none" || (c === "s1" && $("#categorization-protocol").value !== "openai");
  $("#categorization-timeout").max = $("#categorization-protocol").value === "openai" ? "300000" : "60000";
  $("#analysis-url").required = a === "api";
  $("#analysis-api-model").required = a === "api";
  $("#categorization-url").required = c === "s1";
  $("#categorization-api-model").required = c === "s1";
  for (const role of ["analysis", "categorization"]) {
    const data = LearningUI[role],
      busy = LearningUI.busy.has(role),
      form = $(`#${role}-form`);
    for (const input of form.querySelectorAll("input,select,button"))
      input.disabled = busy || !data?.editable;
    $(`#${role}-clear-key`).disabled =
      busy || !data?.editable || !data?.key_saved;
    $(`#${role}-test`).hidden = (role === "analysis" ? a : c) === "none";
    const local = (role === "analysis" ? a : c) === "local",
      button = $(`#${role}-test`);
    button.textContent = tr(local ? "Проверить запуск" : "Проверить подключение");
    if (local) button.disabled = busy || !data?.editable || !WORKER.data?.enabled || !!localModelIssue(selectedModel(role));
  }
  $("#analysis-threshold").disabled ||= !$("#analysis-auto").checked;
  if (typeof renderModelCards === "function") renderModelCards();
}
function renderLearningForms() {
  for (const role of ["analysis", "categorization"]) {
    const data = LearningUI[role];
    if (!data || LearningUI.dirty.has(role)) continue;
    const s = data.settings;
    for (const [key, field] of Object.entries({context_tokens:"context-tokens", max_output_tokens:"output-tokens", reserve_tokens:"reserve-tokens", batch_size:"batch-size"}))
      $(`#${role}-${field}`).value = s.inference?.[key] ?? ({context_tokens:4096,max_output_tokens:1600,reserve_tokens:64,batch_size:2})[key];
    $(`#${role}-response-format`).value = s.inference?.response_format || "auto";
    ensureModelChoice(canonicalModel(s.model));
    $(`#${role}-provider`).value = s.provider;
    if (role === "analysis") {
      $("#analysis-enabled").checked = s.enabled;
      $("#analysis-local-model").value =
        s.provider === "local" ? canonicalModel(s.model) : "qwen3-0.6b";
      $("#analysis-api-model").value = s.provider === "api" ? s.model : "";
      $("#analysis-url").value = s.url;
      $("#analysis-background").checked = s.background;
      $("#analysis-auto").checked = s.auto_apply;
      $("#analysis-threshold").value = Math.round(s.threshold * 100);
      $("#analysis-timeout").value = s.timeout_seconds;
      $("#analysis-parallelism").value = s.parallelism;
      $("#analysis-chunk-strategy").value = s.chunking?.strategy || "auto";
      $("#analysis-discovery-mode").value = s.chunking?.discovery_mode || "typed";
      for (const [key, field] of Object.entries({target_input_tokens:"target-input", context_hint_tokens:"hint-tokens", max_candidates:"max-candidates", max_fragments:"max-fragments"}))
        $(`#analysis-${field}`).value = s.chunking?.[key] ?? ({target_input_tokens:0,context_hint_tokens:0,max_candidates:24,max_fragments:256})[key];
    } else {
      $("#categorization-local-model").value = canonicalModel(s.model);
      $("#categorization-protocol").value = s.http.protocol;
      $("#categorization-api-model").value = s.http.model || "";
      $("#categorization-url").value = s.http.url;
      $("#categorization-timeout").value = s.http.timeout_ms;
    }
    $(`#${role}-key`).value = "";
    $(`#${role}-clear-key`).checked = false;
    $(`#${role}-key`).placeholder = tr(
      data.key_saved
        ? "Сохранённый ключ останется без изменений"
        : "Введите ключ, если он требуется",
    );
  }
  learningFormVisibility();
}
function readLearningForm(role) {
  const saved = LearningUI[role]?.settings || {};
  let settings;
  if (role === "analysis") {
    const provider = $("#analysis-provider").value;
    settings = {
      ...saved,
      enabled: $("#analysis-enabled").checked,
      provider,
      model:
        provider === "api"
          ? $("#analysis-api-model").value.trim()
          : $("#analysis-local-model").value,
      url: $("#analysis-url").value.trim(),
      background: $("#analysis-background").checked,
      auto_apply: $("#analysis-auto").checked,
      threshold: Number($("#analysis-threshold").value) / 100,
      timeout_seconds: Number($("#analysis-timeout").value),
      parallelism: Number($("#analysis-parallelism").value),
    };
  } else {
    settings = {
      ...saved,
      provider: $("#categorization-provider").value,
      model: $("#categorization-local-model").value,
      http: {
        ...saved.http,
        url: $("#categorization-url").value.trim(),
        protocol: $("#categorization-protocol").value,
        model: $("#categorization-api-model").value.trim() || null,
        timeout_ms: Number($("#categorization-timeout").value),
        disclosure: "l2",
      },
    };
  }
  const key = $(`#${role}-key`).value.trim();
  settings.inference = Object.fromEntries(Object.entries({context_tokens:"context-tokens", max_output_tokens:"output-tokens", reserve_tokens:"reserve-tokens", batch_size:"batch-size"}).map(([key,field]) => [key,Number($(`#${role}-${field}`).value)]));
  settings.inference.response_format = $(`#${role}-response-format`).value;
  if (role === "analysis") settings.chunking = {
    discovery_mode: $("#analysis-discovery-mode").value,
    strategy: $("#analysis-chunk-strategy").value,
    ...Object.fromEntries(Object.entries({target_input_tokens:"target-input", context_hint_tokens:"hint-tokens", max_candidates:"max-candidates", max_fragments:"max-fragments"}).map(([key,field]) => [key,Number($(`#analysis-${field}`).value)])),
  };
  return {
    settings,
    ...(key ? { key } : {}),
    clear_key: $(`#${role}-clear-key`).checked,
  };
}
async function loadLearningSettings() {
  const [analysis, categorization] = await Promise.all([
    api("/api/v1/ui/analysis"),
    api("/api/v1/ui/categorization"),
  ]);
  LearningUI.analysis = analysis;
  LearningUI.categorization = categorization;
  renderLearningForms();
}
for (const role of ["analysis", "categorization"]) {
  for (const event of ["input", "change"])
    $(`#${role}-form`).addEventListener(event, () => {
      LearningUI.dirty.add(role);
      $(`#${role}-result`).textContent = tr("Есть несохранённые изменения");
      learningFormVisibility();
    });
  $(`#${role}-form`).addEventListener("submit", (event) => {
    event.preventDefault();
    saveLearningForm(role);
  });
  $(`#${role}-test`).addEventListener("click", () =>
    saveLearningForm(role, true),
  );
}
async function saveLearningForm(role, test = false) {
  if (LearningUI.busy.has(role) || !LearningUI[role]) return;
  const body = readLearningForm(role),
    output = $(`#${role}-result`),
    previous = LearningUI[role].settings;
  LearningUI.busy.add(role);
  learningFormVisibility();
  output.classList.remove("err");
  output.textContent = tr(test ? "Проверяем…" : "Применяем…");
  let saved = false;
  try {
    const result = await api(`/api/v1/ui/${role}${test ? "/test" : ""}`, {
      method: test ? "POST" : "PUT",
      body,
    });
    if (!test) {
      LearningUI[role] = result;
      saved = true;
      LearningUI.dirty.delete(role);
      renderLearningForms();
      if (role === "analysis" && (previous.enabled !== result.settings.enabled || (result.settings.enabled && !WORKER.data?.enabled)))
        await workerAction(result.settings.enabled ? "/start" : "/stop", {});
      await loadWorkerSummary();
    }
    const local = body.settings.provider === "local";
    output.textContent = test ? tr(local ? "Модель запущена и проверена на синтетическом примере." : "Подключение проверено")
      : tr("Настройки сохранены."); // Live readiness is shown by the widget and model cards.
    await loadLearningHistory();
  } catch (e) {
    output.textContent = saved ? tr("Настройки сохранены, но служба не выполнила действие: {{0}}", [learningError(e.message)]) : learningError(e.message);
    output.classList.add("err");
  } finally {
    LearningUI.busy.delete(role);
    learningFormVisibility();
  }
}

function learningWaitingReason() {
  const settings = LearningUI.analysis?.settings, worker = WORKER.data || {};
  if (!settings?.enabled || settings.provider === "none") return null;
  if (!worker.online) return tr("Служба анализа недоступна. Наличие файлов пока не проверено.");
  if (!worker.enabled) return tr("Анализ ожидает запуска обработки.");
  for (const [role, config] of [["analysis", settings], ["categorization", LearningUI.categorization?.settings]]) {
    if (config?.provider !== "local") continue;
    const issue = localModelIssue(config.model);
    if (issue) return tr(role === "analysis" ? "Анализ ожидает подготовки модели обнаружения: {{0}}. {{1}}"
      : "Анализ ожидает подготовки модели категоризации: {{0}}. {{1}}", [modelName(config.model), tr(issue)]);
  }
  return null;
}

function renderLearningWidget() {
  const history = LearningUI.history,
    settings = LearningUI.analysis?.settings,
    worker = WORKER.data || {};
  if (!history || !settings) return;
  const summary = history.summary,
    totals = summary.totals;
  const state =
    !settings.enabled || settings.provider === "none"
      ? tr("Анализ приостановлен")
      : learningWaitingReason() || (
        summary.running ? tr("Идёт анализ") : summary.queued ? tr("Ожидает обработки")
          : tr(settings.background ? "Ожидает новых данных" : "Автоматический сбор сообщений выключен. Можно отправлять документы вручную."));
  $("#learning-state").textContent = state;
  const toggle = $("#learning-toggle");
  toggle.textContent = tr(
    settings.enabled ? "Приостановить анализ" : "Возобновить анализ",
  );
  toggle.disabled =
    LearningUI.busy.has("toggle") || !LearningUI.analysis?.editable;
  $("#learning-metrics").replaceChildren(
    ...[
      ["В очереди", summary.queued],
      ["В работе", summary.running],
      ["Обработано", totals.processed],
      ["Извлечено", totals.extracted],
      ["Применено правил", totals.applied],
      ["На подтверждение", summary.pending],
      ["Ошибок обработки", totals.failed],
      ["Пропущено знакомых", totals.skipped_known],
      ["Вытеснено", totals.evicted],
    ].map(([label, value]) =>
      h(
        "div",
        { class: "learning-metric" },
        h("span", {}, tr(label)),
        h("strong", {}, number(value || 0)),
      ),
    ),
  );
  $("#learning-storage").textContent =
    tr("Очередь на диске: {{0}} из {{1}}", [
      bytesMiB(summary.queue_bytes),
      bytesMiB(summary.queue_limit_bytes),
    ]) +
    " · " +
    tr("Истёк срок: {{0}} · Не помещено: {{1}}", [
      number(totals.expired || 0),
      number(totals.skipped || 0),
    ]);
  $("#learning-memory").textContent =
    tr("ОЗУ воркера: {{0}} · модели: {{1}}", [
      bytesMiB(worker.resources?.worker_rss_bytes),
      bytesMiB(worker.resources?.model_rss_bytes),
    ]) +
    " · " +
    tr("Параллельность: {{0}}", [settings.parallelism]);
}
$("#learning-toggle").addEventListener("click", async () => {
  const settings = LearningUI.analysis?.settings;
  if (!settings || LearningUI.busy.has("toggle")) return;
  if (settings.provider === "none") {
    showTab("analysis");
    $("#analysis-provider").focus();
    toast(tr("Выберите и сохраните источник модели"));
    return;
  }
  LearningUI.busy.add("toggle");
  renderLearningWidget();
  try {
    LearningUI.analysis = await api("/api/v1/ui/analysis", {
      method: "PUT",
      body: { settings: { ...settings, enabled: !settings.enabled } },
    });
    if (!LearningUI.dirty.has("analysis")) renderLearningForms();
    await workerAction(
      LearningUI.analysis.settings.enabled ? "/start" : "/stop",
      {},
    );
    await loadLearningHistory();
  } catch (e) {
    fail(e);
  } finally {
    LearningUI.busy.delete("toggle");
    renderLearningWidget();
  }
});
async function learningAction(id, action, body = {}) {
  if (LearningUI.busy.has("action")) return;
  LearningUI.busy.add("action");
  try {
    await api(`/api/v1/ui/learning/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      body,
    });
    await loadLearningHistory(true);
    await loadInfo();
  } catch (e) {
    fail(e);
  } finally {
    LearningUI.busy.delete("action");
  }
}
async function loadLearningHistory(force = false) {
  LearningUI.history = await api("/api/v1/ui/learning");
  renderLearningWidget();
}
$("#learning-open-history").addEventListener("click", () => openReview("history"));
async function loadCategorization() {
  await Promise.all([
    loadLearningSettings(),
    loadWorkerSummary(),
    loadLearningHistory(),
  ]);
}
Object.assign(TABS, {
  categorization: loadCategorization,
  review: async () => {
    await Promise.all([loadJournal(), loadLearningHistory()]);
  },
});
setInterval(() => {
  if (
    !document.hidden &&
    ["#analysis", "#categorization", "#worker"].includes(
      location.hash,
    )
  )
    loadLearningHistory().catch(() => {});
}, 2500);
