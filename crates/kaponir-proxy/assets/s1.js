"use strict";

const LearningUI = {
  analysis: null,
  categorization: null,
  history: null,
  generation: {},
  providerDrafts: {},
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
  model_queue_timeout: "Служба была занята до конца проверки. Повторите после текущего задания.",
  local_probe_failed: "Служба не подтвердила готовность модели. Проверьте версию воркера и среду запуска.",
  scenario_requires_llm: "Для сценариев A–D включите локальную или OpenAI-совместимую категоризацию.",
  target_occurrence_uncertain: "Точное вхождение не удалось выделить однозначно.",
  scenario_mask: "Модель предлагает скрыть значение",
  scenario_review: "Требуется ручная проверка значения",
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

// Imported copies of the pinned GGUF get the same preset as the built-in model.
const studyArtifacts = {
  "35276d878e0d1205dd622466623651cacd738732f2495a9a38b51b9fd659123f": "kaponir-privacy-v4-epoch-2",
  "b1b3de114215d9507409a662a501a631095a479a419584e8a2ded6304b19b4f5": "lfm2.5-1.2b-instruct",
  "02a8b7e17487d326e46d68ce0ba24211e1b80a14c4cd0597fa73c1cd697f52ed": "lfm2.5-2.6b",
  "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4": "qwen3.5-4b",
  "91cb2811aebb47ac916aa619d27d4cdd7f4143106b7917accb8ead4cd7f5cbd9": "spark-x2.6-4b",
  "4923ec14f06b968b74d663e5949867d2d9c3bf13a20b8be1a9f9af39989b2bb0": "lfm2.5-8b-a1b",
  "740185b21d22ceb83a11c3aa62ad5842ef32c70f6096d756bbee85a1e4ec34b8": "gemma4-e2b-it-q4km",
  "85a896a047553e842f25297ee5b031d64ff30147d9c4af17b1e4b394cd1fab87": "gemma4-e4b-it-q4km",
};
// Search settings are from model-search-final.csv and its linked run plans.
// Classification uses the separately measured A–D settings for the same GGUF.
const baselineSearch = {context:4096, output:1600, timeout:300};
const searchProfiles = {
  "gemma4-e4b-qat": {top_k:64, reasoning_budget:384, thinking_source_cutoff:1024},
  "nanbeige42-3b": {top_k:20, completion_schema:true, log_temperature:0.2, log_target:2304, prompt_threads:4, maxFragments:64},
  "lfm2.5-1.2b-instruct": {temperature:0.1, top_k:50, repeat_penalty:1.05, log_target:2432},
  "lfm2.5-2.6b": {temperature:0.1, top_k:50, reasoning_budget:384, repeat_penalty:1.1, log_repeat_penalty:1, log_target:1536, log_hints:0, prompt_threads:4},
  "qwen3.5-4b": {repeat_penalty:1, presence_penalty:0, target:2432, prompt_threads:4},
  "gemma4-e4b-it-q4km": {temperature:1, top_k:64, reasoning_budget:2048},
  "lfm2.5-8b-a1b": {temperature:0.2, top_k:80, reasoning_budget:1024, repeat_penalty:1.05},
  "kaponir-privacy-v4-epoch-2": baselineSearch, "qwen3.5-2b": baselineSearch, "qwen3.5-0.8b": baselineSearch,
  "granite42-3b": {...baselineSearch, timeout:1200},
  "spark-x2.6-4b": {...baselineSearch, timeout:1200, prompt_threads:2, generation_threads:2},
  "minicpm5-2b": {...baselineSearch, timeout:600, format:"auto", limitsOnly:true},
  "gemma4-e2b-it-q4km": null,
};
const reviewProfiles = {
  "kaponir-privacy-v4-epoch-2": {timeout:600},
  "gemma4-e4b-qat": {temperature:1, top_k:64},
  "gemma4-e4b-it-q4km": {temperature:1, top_k:64},
  "gemma4-e2b-it-q4km": {temperature:1, top_k:64},
  "qwen3.5-0.8b": {timeout:600}, "qwen3.5-2b": {timeout:600}, "minicpm5-2b": {timeout:600},
  "nanbeige42-3b": {top_k:20, reasoning_budget:null, completion_schema:true},
  "granite42-3b": {temperature:1, reasoning_budget:384, low_effort:true},
  "lfm2.5-1.2b-instruct": {temperature:0.1, top_k:50, reasoning_budget:null, repeat_penalty:1.05},
  "lfm2.5-2.6b": {temperature:0.1, top_k:50, repeat_penalty:1.1},
  "lfm2.5-8b-a1b": {temperature:0.2, top_k:80, reasoning_budget:1024, repeat_penalty:1.05},
  "spark-x2.6-4b": {temperature:1, top_k:0, reasoning_effort:"low"},
  "qwen3.5-4b": {temperature:1, top_k:64},
};
const inferenceFields = {context_tokens:"context-tokens", max_output_tokens:"output-tokens",
  reserve_tokens:"reserve-tokens", batch_size:"batch-size", response_format:"response-format"};
const defaultInference = {context_tokens:4096, max_output_tokens:1600, reserve_tokens:64, batch_size:2, response_format:"auto"};

function studyModelId(role) {
  return measuredModelId(modelSelect(role).value);
}
function chosenLocalPreset(role, studyId = studyModelId(role)) {
  const search = role !== "categorization";
  const row = (search ? searchProfiles : reviewProfiles)[studyId];
  if (!row) return null;
  const {context=16384, output=4096, timeout=1200, format="grammar", target=0,
    maxFragments=256, limitsOnly=false, ...sampling} = row;
  const full = context === 16384;
  const generation = limitsOnly ? null : {
    temperature:0, top_k:40, top_p:0.95, min_p:0, seed:full ? 42 : null,
    parse_reasoning:full, reasoning_budget:search ? null : 2048, retry_output_tokens:full ? 8192 : null,
    ...(full ? {prompt_threads:2, generation_threads:2} : {}), ...(target ? {target} : {}), ...sampling,
  };
  return {inference:{context_tokens:context, max_output_tokens:output, reserve_tokens:64,
    batch_size:8, response_format:format, local_generation:generation},
    scenario:["qwen3.5-2b","minicpm5-2b"].includes(studyId) ? "B" : "D",
    timeout:search ? timeout : timeout * 1000, maxCandidates:24, target, maxFragments};

}
function writeInference(role, inference) {
  for (const [key, field] of Object.entries(inferenceFields))
    $(`#${role}-${field}`).value = inference?.[key] ?? defaultInference[key];
  LearningUI.generation[role] = inference?.local_generation ?? null;
}
function updateProfileNote(role, g = LearningUI.generation[role]) {
  const note = $(`#${role}-profile-note`);
  const id = studyModelId(role);
  const partial = role !== "categorization" && (id === "minicpm5-2b" || id === "gemma4-e2b-it-q4km");
  const preset = chosenLocalPreset(role);
  if (role !== "worker") {
    const fieldsMatch = preset && Object.entries(inferenceFields).every(([key, field]) =>
      String($("#"+role+"-"+field).value)===String(preset.inference[key]));
    const samplingMatch = preset && Object.keys({...g,...preset.inference.local_generation}).every(key=>(g?.[key]??null)===(preset.inference.local_generation?.[key]??null));
    const profileMatch = role==="categorization" ? $("#categorization-scenario").value === preset?.scenario :
      Number($("#analysis-target-input").value)===(preset?.target??0) && $("#analysis-chunk-strategy").value==="auto" &&
      Number($("#analysis-max-fragments").value)===(preset?.maxFragments??256) && Number($("#analysis-hint-tokens").value)===256 &&
      Number($("#analysis-max-candidates").value)===(preset?.maxCandidates??24);
    $("#" + role + "-preset").hidden = !preset || (fieldsMatch && samplingMatch && profileMatch && Number($("#"+role+"-timeout").value)===preset.timeout);
    const source = $("#" + role + "-study");
    source.hidden = !preset && !partial;
    source.href = id === "kaponir-privacy-v4-epoch-2"
      ? "https://github.com/miqonee/Kaponir/tree/main/research/privacy-training#readme"
      : "https://github.com/miqonee/Kaponir/blob/main/docs/experiments/model-search-final.csv";
  }
  note.hidden = !g && !partial && role === "worker";
  if (!g && !partial) note.textContent = tr("Параметры для этой модели можно задать вручную.");
  if (!g && partial) note.textContent = id === "minicpm5-2b"
    ? tr("В архиве поиска подтверждены только лимиты контекста и ответа.")
    : tr("Параметры исторического поиска не подтверждены. Используются обычные настройки.");

  if (g) note.textContent = tr("Параметры исследования · температура {{0}}, top_p {{1}}, top_k {{2}}, min_p {{3}}, seed {{4}}, бюджет рассуждений {{5}}.",
    [g.temperature, g.top_p, g.top_k, g.min_p, g.seed ?? "—", g.reasoning_budget ?? 0]);
  if (g && role === "analysis" && preset) note.textContent += " " + tr("Профиль для поиска по документам взят из таблицы исследования.");
  if (g?.thinking_source_cutoff) note.textContent += " " + tr("Рассуждения включаются до {{0}} токенов исходного текста.", [g.thinking_source_cutoff]);
}
function applySearchLimits(preset) {
  if (preset?.inference.local_generation) $("#analysis-discovery-mode").value = "typed";
  $("#analysis-target-input").value = preset?.target ?? 0;
  $("#analysis-hint-tokens").value = preset ? 256 : 0;
  $("#analysis-max-candidates").value = preset?.maxCandidates ?? 24;
  $("#analysis-max-fragments").value = preset?.maxFragments ?? 256;
  $("#analysis-chunk-strategy").value = "auto";
}
function applyLocalPreset(role) {
  if ($(`#${role}-provider`).value !== "local") return;
  const preset = chosenLocalPreset(role);
  writeInference(role, preset?.inference);
  $(`#${role}-timeout`).value = preset?.timeout ?? (role === "analysis" ? 180 : 30000);
  if (role === "analysis") {
    applySearchLimits(preset);
  } else {
    $("#categorization-scenario").value = preset?.scenario || "standard";
  }
  updateProfileNote(role);
}

function learningFormVisibility() {
  const a = $("#analysis-provider").value,
    c = $("#categorization-provider").value;
  $("#analysis-local-fields").hidden = a !== "local";
  $("#analysis-api-fields").hidden = a !== "api";
  $("#categorization-local-fields").hidden = c !== "local";
  $("#categorization-api-fields").hidden = c !== "s1";
  $("#categorization-none-note").hidden = c !== "none";
  $("#categorization-timeout-field").hidden = c === "none";
  $("#categorization-inference-settings").hidden = c === "none" || (c === "s1" && $("#categorization-protocol").value !== "openai");
  $("#categorization-scenario-field").hidden = $("#categorization-inference-settings").hidden;
  $("#analysis-timeout").max = a === "local" ? "1800" : "300";
  $("#categorization-timeout").max = c === "local" ? "1800000" : $("#categorization-protocol").value === "openai" ? "300000" : "60000";
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
  if (!$("#categorization-scenario-field").hidden) {
    renderScenarioStudy($("#categorization-scenario-study"), c === "local" ? studyModelId("categorization") : null, $("#categorization-scenario").value);
  }
  $("#analysis-threshold").disabled ||= !$("#analysis-auto").checked;
  if (typeof renderModelCards === "function") renderModelCards();
}
function renderLearningForms() {
  for (const role of ["analysis", "categorization"]) {
    const data = LearningUI[role];
    if (!data || LearningUI.dirty.has(role)) continue;
    const s = data.settings;
    writeInference(role, s.inference);
    LearningUI.providerDrafts[role] = {provider:s.provider};
    ensureModelChoice(canonicalModel(s.model), role);
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
      $("#categorization-scenario").value = s.scenario || "standard";
      $("#categorization-api-model").value = s.http.model || "";
      $("#categorization-url").value = s.http.url;
      $("#categorization-timeout").value = s.http.timeout_ms;
    }
    updateProfileNote(role);
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
      scenario: $("#categorization-scenario-field").hidden ? "standard" : $("#categorization-scenario").value,
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
  if (settings.provider === "local" && LearningUI.generation[role])
    settings.inference.local_generation = LearningUI.generation[role];
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
  for(const event of ["input","change"]) $("#"+role+"-form").addEventListener(event,()=>{
    if($("#"+role+"-provider").value==="local")updateProfileNote(role);
  });
  $(`#${role}-local-model`).addEventListener("change", () => applyLocalPreset(role));
  $(`#${role}-provider`).addEventListener("change", () => {
    const drafts = LearningUI.providerDrafts[role];
    if (!drafts) return;
    const provider = $(`#${role}-provider`).value;
    const fieldIds = Object.values(inferenceFields).map(field => `${role}-${field}`);
    if (role === "categorization") fieldIds.push("categorization-scenario");
    fieldIds.push(`${role}-timeout`);
    if (role === "analysis") fieldIds.push("analysis-target-input", "analysis-hint-tokens",
      "analysis-max-candidates", "analysis-max-fragments", "analysis-chunk-strategy");
    drafts[drafts.provider] = {values:Object.fromEntries(fieldIds.map(id => [id, $("#" + id).value])),
      generation:LearningUI.generation[role]};
    drafts.provider = provider;
    const previous = drafts[provider];
    if (previous) {
      for (const [id, value] of Object.entries(previous.values)) $("#" + id).value = value;
      LearningUI.generation[role] = previous.generation;
    } else if (provider === "local") {
      const select = modelSelect(role);
      const installed = [...select.options].filter(option => WORKER.data?.models?.[option.value]?.downloaded);
      const sibling = modelSelect(role === "analysis" ? "categorization" : "analysis").value;
      if (installed.length) select.value = installed.find(option => option.value === sibling)?.value || installed[0].value;
      applyLocalPreset(role);
    } else {
      if (role === "categorization") $("#categorization-scenario").value = "standard";
      writeInference(role, defaultInference);
      $(`#${role}-timeout`).value = role === "analysis" ? 180 : 30000;
      if (role === "analysis") applySearchLimits(null);
    }
    updateProfileNote(role);
  });
  $("#" + role + "-preset").addEventListener("click", () => {
    applyLocalPreset(role);
    $("#" + role + "-form").dispatchEvent(new Event("input"));
  });
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
  const local = body.settings.provider === "local";
  const limitMs = role === "analysis" ? body.settings.timeout_seconds * 1000 : body.settings.http.timeout_ms;
  const started = Date.now();
  const progress = () => {
    output.textContent = tr("Проверяем запуск модели… {{0}} с из {{1}} с", [Math.floor((Date.now() - started) / 1000), Math.ceil(limitMs / 1000)]);
  };
  let timer;
  if (test && local) {
    progress();
    timer = setInterval(progress, 1000);
  }
  let saved = false;
  try {
    const result = await api(`/api/v1/ui/${role}${test ? "/test" : ""}`, {
      method: test ? "POST" : "PUT",
      timeoutMs: test ? Math.max(45000, limitMs + 15000) : 45000,
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
    clearInterval(timer);
    output.textContent = test ? tr(local ? "Модель загружена. Токенизатор ответил." : "Подключение проверено")
      : tr("Настройки сохранены."); // Live readiness is shown by the widget and model cards.
    await loadLearningHistory();
  } catch (e) {
    output.textContent = saved ? tr("Настройки сохранены, но служба не выполнила действие: {{0}}", [learningError(e.message)]) : (e.name === "TimeoutError" || e.message === "model_timeout") ? tr("Проверка не уложилась в заданное время. Проверьте очередь и тайм-аут модели.") : learningError(e.message);
    output.classList.add("err");
  } finally {
    clearInterval(timer);
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

function jobDuration(ms) {
  if (ms == null) return "—";
  const [unit, divisor] = ms < 1000 ? ["millisecond", 1]
    : ms < 60000 ? ["second", 1000] : ["minute", 60000];
  return new Intl.NumberFormat(I18N.language, {
    style: "unit", unit, unitDisplay: "short", maximumFractionDigits: 1,
  }).format(ms / divisor);
}

function renderQueueActivity(summary) {
  const activity = summary.activity;
  $("#queue-waiting").textContent = number(summary.queued);
  $("#queue-running").textContent = number(summary.running);
  $("#queue-pending").textContent = number(summary.pending);
  $("#queue-completed").textContent = activity ? number(activity.completed_24h) : "—";
  $("#queue-speed").textContent = activity ? number(activity.hourly[23]) : "—";
  $("#queue-average").textContent = jobDuration(activity?.average_processing_ms);
  const since = summary.activity_since_ms;
  $("#queue-since").textContent = since && Date.now() - since < 86400000
    ? tr("Учёт с {{0}}", [time(since, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })]) : "";
  const chart = $("#queue-chart");
  const hourly = activity?.hourly || [];
  const peak = Math.max(1, ...hourly);
  chart.replaceChildren(...hourly.map((count, i) => h("span", {
    class: count ? "queue-bar filled" : "queue-bar",
    style: `--height:${100 * count / peak}%`,
    title: tr("{{0}}–{{1}} ч назад · заданий {{2}}", [24 - i, 23 - i, number(count)]),
  })));
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
  for (const el of [$("#learning-state"), $("#llm-stage-note")])
    if (el.textContent !== state) el.textContent = state;
  const toggle = $("#learning-toggle");
  toggle.textContent = tr(
    settings.enabled ? "Приостановить анализ" : "Возобновить анализ",
  );
  toggle.disabled =
    LearningUI.busy.has("toggle") || !LearningUI.analysis?.editable;
  renderQueueActivity(summary);
  $("#learning-totals").replaceChildren(
    ...[
      ["Обработано", totals.processed],
      ["Извлечено", totals.extracted],
      ["Применено правил", totals.applied],
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
  $("#queue-updated").textContent = tr("Обновлено {{0}}", [new Date().toLocaleTimeString(I18N.language)]);
  renderLearningWidget();
}
$("#queue-open-pending").addEventListener("click", () => showReview([], "pending", "learning"));
$("#learning-open-history").addEventListener("click", () => showReview([], "history", "learning"));
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
    loadLearningHistory().catch(() => {
      $("#queue-updated").textContent = tr("Не удалось обновить статистику");
    });
}, 2500);
