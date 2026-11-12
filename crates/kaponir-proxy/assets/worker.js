"use strict";
const WORKER = { data: null, busy: false, transition: null, actionError: null, request: 0, cards: new Map() };
const workerStates = {
  queued: "В очереди", running: "Выполняется", paused: "Приостановлено",
  succeeded: "Готово", failed: "Ошибка", cancelled: "Отменено",
};
const installationStages = {
  model_download: "Скачивание весов", model_verify: "Проверка весов",
  runtime_download: "Скачивание среды запуска", runtime_verify: "Проверка среды запуска",
  runtime_install: "Подготовка среды запуска", ready: "Файлы подготовлены",
};
const installationErrors = {
  dns_error: "Не удалось найти сервер загрузки (DNS).",
  connection_failed: "Не удалось подключиться к серверу загрузки.",
  proxy_error: "Прокси-сервер не разрешил загрузку или недоступен.",
  tls_error: "Не удалось проверить защищённое соединение с сервером загрузки.",
  download_timeout: "Сервер загрузки не ответил вовремя.",
  http_error: "Сервер загрузки вернул ошибку HTTP.",
  disk_full: "Недостаточно места на диске машины анализа.",
  permission_denied: "Нет прав на чтение или запись файлов модели.",
  file_error: "Не удалось прочитать или записать файл модели.",
  size_mismatch: "Размер файла не совпадает с ожидаемым.",
  checksum_mismatch: "Контрольная сумма файла не совпадает. Файл повреждён или выбран другой вариант модели.",
  invalid_gguf: "Файл не является моделью GGUF. Проверьте выбранный файл и его источник.",
  runtime_missing: "Файл среды запуска llama-server не найден.",
  runtime_not_executable: "У llama-server нет разрешения на запуск.",
  runtime_unsupported: "Для этой платформы нужна отдельно установленная среда запуска.",
  runtime_install_failed: "Не удалось распаковать среду запуска.",
  installation_failed: "Не удалось завершить установку модели.",
};
const modelAliases = { q4: "qwen3-0.6b", q5: "qwen3-0.6b-q8" };
const canonicalModel = (model) => modelAliases[model] || model;
const builtinModelChoices = [
  ["qwen3-0.6b", "Qwen3-0.6B · Q4_K_M", 396705472],
  ["qwen3-0.6b-q8", "Qwen3-0.6B · Q8_0", 639446688],
  ["qwen3.5-0.8b", "Qwen3.5-0.8B · Q4_K_M", 532517120],
  ["qwen3.5-2b", "Qwen3.5-2B · Q4_K_M", 1280835840],
  ["minicpm5-2b", "MiniCPM5-2B · Q4_K_M", 1561318368],
  ["huihui-qwen3.5-2b-abliterated", "Huihui-Qwen3.5-2B-abliterated · Q4_K_M", 1270809024],
].map(([id,label,bytes])=>({id,label,bytes}));
function modelSelect(role) { return $(role === "worker" ? "#worker-model" : `#${role}-local-model`); }
function ensureModelChoice(model) {
  if (!model || !(builtinModelChoices.some((item)=>item.id===model) || /^hf-[a-f0-9]{24}$/.test(model))) return;
  for (const role of ["analysis","categorization","worker"]) {
    const select=modelSelect(role);
    if (![...select.options].some((option)=>option.value===model)) select.append(h("option",{value:model},WORKER.data?.models?.[model]?.label || model));
  }
}
function syncModelChoices() {
  const catalog=WORKER.data?.catalog || builtinModelChoices;
  for (const role of ["analysis","categorization","worker"]) {
    const select=modelSelect(role), current=select.value;
    const items=[...catalog];
    if (current && !items.some((item)=>item.id===current)) items.push({id:current,label:WORKER.data?.models?.[current]?.label || current});
    const signature=JSON.stringify(items.map((item)=>[item.id,item.label,item.bytes]));
    if (select.dataset.catalog===signature) continue;
    select.dataset.catalog=signature;
    select.replaceChildren(...items.map((item)=>h("option",{value:item.id},item.label+(item.bytes ? ` · ${formatModelBytes(item.bytes)}` : ""))));
    if (current) select.value=current;
  }
}
const formatModelBytes = (bytes) => `${(Number(bytes || 0) / 1048576).toFixed(1)} MiB`;
function modelName(model) {
  return [...$("#worker-model").options].find((o) => o.value === canonicalModel(model))?.textContent || model;
}
function workerStateLabel(data) {
  return tr(WORKER.transition || {
    disabled: data?.online ? "Обработка приостановлена" : "Служба остановлена",
    ready: "Служба запущена", stopping: "Останавливается…", error: "Служба недоступна",
  }[data?.state] || "Проверяем службу…");
}
function installationErrorMessage(error) {
  if (error.code) return tr(installationErrors[error.code] || installationErrors.installation_failed);
  return error.message === "worker operation failed"
    ? tr("Причина старого сбоя не сохранена. Повторите загрузку для получения диагностики.")
    : error.message || tr(installationErrors.installation_failed);
}
function installationRecovery(error) {
  if (["dns_error", "connection_failed", "proxy_error", "download_timeout", "http_error"].includes(error.code))
    return tr("Проверьте доступ в интернет и настройки прокси на машине анализа или установите файлы без интернета.");
  if (error.code === "tls_error")
    return tr("Проверьте системное время и доверенные сертификаты на машине анализа или перенесите файлы вручную.");
  if (error.code === "disk_full") return tr("Освободите место в каталоге моделей и повторите установку.");
  if (["permission_denied", "runtime_not_executable", "file_error"].includes(error.code))
    return tr("Проверьте права службы анализа на каталог моделей и файл llama-server.");
  return tr("Повторите установку или откройте инструкцию «Установить без интернета».");
}
function installationErrorNode(error) {
  const technical = [["Код", error.code], ["Тип", error.type], ["Сервер", error.host],
    ["HTTP", error.http_status], ["Этап", error.stage && tr(installationStages[error.stage] || error.stage)],
    ["Код ОС", error.errno]].filter(([, value]) => value != null);
  return h("div", { class: "installation-error" },
    h("p", { class: "err" }, installationErrorMessage(error)),
    h("p", { class: "hint" }, installationRecovery(error)),
    h("details", {}, h("summary", {}, tr("Технические сведения")),
      h("dl", { class: "model-metadata" }, ...technical.flatMap(([label, value]) =>
        [h("dt", {}, tr(label)), h("dd", {}, String(value))]))));
}
function modelDownloadJob(model) {
  return WORKER.data?.jobs?.find((job) => job.kind === "download" && canonicalModel(job.model) === canonicalModel(model));
}
function activeModelDownload(model) {
  return WORKER.data?.jobs?.find((job) => job.kind === "download" && canonicalModel(job.model) === canonicalModel(model)
    && ["queued", "running", "paused"].includes(job.state));
}
function localModelIssue(model) {
  const data = WORKER.data, weights = data?.models?.[model];
  if (!data?.online) return "Служба анализа недоступна. Наличие файлов пока не проверено.";
  if (!data.model_verification) return "Обновите воркер для проверки целостности файлов и инструкций установки.";
  if (!weights) return "Выбранной модели нет в каталоге службы. Добавьте её по HF-ссылке или обновите службу.";
  if (weights.state === "missing") return "Модель не установлена";
  if (weights.state === "checking") return "Проверяем контрольную сумму модели…";
  if (weights.state === "invalid" || weights.state === "unreadable") return "Файл модели не прошёл проверку";
  if (!weights.downloaded) return "Наличие файлов пока не проверено";
  if (!data.runtime_ready) return "Веса проверены. Требуется среда запуска llama-server.";
  return null;
}
function createModelCard(role) {
  const root = $(`#${role}-model-install`),
    status = h("p", { class: "model-state", role: "status", "aria-live": "polite" }),
    file = h("p", { class: "hint mono model-file" }),
    runtime = h("p", { class: "hint" }),
    stage = h("p", { class: "hint" }),
    progress = h("progress", { "aria-label": tr("Прогресс установки"), "data-i18n-aria-label": "Прогресс установки" }),
    error = h("div"), hint = h("p", { class: "hint" }), offlineBody = h("div"),
    download = h("button", { type: "button", class: "secondary", id: `${role}-download`,
      onclick: () => {
        const model = selectedModel(role), active = activeModelDownload(model);
        runWorkerAction(active?.state === "paused" ? `/jobs/${active.id}/resume` : "/models/download", { model });
      } }, tr("Скачать на сервер")),
    verify = h("button", { type: "button", class: "secondary", id: `${role}-verify`,
      "data-i18n": "Проверить файлы", onclick: () => runWorkerAction("/models/verify", { model: selectedModel(role) }) }, tr("Проверить файлы")),
    start = h("button", { type: "button", class: "primary", id: `${role}-model-start`,
      "data-i18n": "Запустить обработку", onclick: () => runWorkerAction("/start", {}) }, tr("Запустить обработку"));
  root.replaceChildren(status, file, runtime, stage, progress, error,
    h("div", { class: "form-actions" }, download, verify, start), hint,
    h("details", { class: "model-offline" }, h("summary", { "data-i18n": "Установить без интернета" }, tr("Установить без интернета")), offlineBody));
  const card = { root, status, file, runtime, stage, progress, error, hint, offlineBody, download, verify, start };
  WORKER.cards.set(role, card);
  return card;
}
function selectedModel(role) {
  return $(role === "worker" ? "#worker-model" : `#${role}-local-model`).value;
}
function downloadLink(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:" && !parsed.username && !parsed.password)
      return h("a", { href: parsed.href, target: "_blank", rel: "noopener noreferrer" }, parsed.href);
  } catch {}
  return h("span", {}, "—");
}
function offlineInstructions(model, runtime) {
  if (!model?.path || !model?.sha256 || !runtime?.path)
    return [h("p", { class: "hint" }, tr("Запустите службу анализа, чтобы получить точные пути на её машине. Если служба устарела, обновите воркер."))];
  const metadata = (items) => h("dl", { class: "model-metadata" }, ...items.flatMap(([key, value]) =>
    [h("dt", {}, tr(key)), h("dd", {}, value)]));
  const nodes = [
    h("p", { class: "hint" }, tr("Скачайте файлы на машине с интернетом и перенесите их на машину службы анализа. Пути ниже относятся к этой машине, а не к браузеру.")),
    metadata([["Файл модели", model.file], ["Размер", `${formatModelBytes(model.bytes)} (${model.bytes} ${tr("байт")})`],
      ["Источник", downloadLink(model.url)], ["Положить в", h("code", {}, model.path)], ["SHA-256", h("code", {}, model.sha256)]]),
    h("p", { class: "hint" }, tr("Среда запуска llama-server")),
  ];
  if (runtime.automatic_install) {
    const archive = runtime.archive;
    nodes.push(metadata([["Архив", archive.file], ["Источник", downloadLink(archive.url)],
      ["SHA-256", h("code", {}, archive.sha256)], ["Распаковать в", h("code", {}, archive.extract_to)],
      ["Ожидаемый исполняемый файл", h("code", {}, runtime.path)]]),
      h("p", { class: "hint" }, tr("Сохраните структуру каталогов архива и все библиотеки рядом с llama-server. Файлу llama-server нужны права на запуск.")));
  } else {
    nodes.push(metadata([["Ожидаемый исполняемый файл", h("code", {}, runtime.path)]]),
      h("p", { class: "hint" }, tr("Установите совместимую сборку llama-server для этой платформы. Путь задаётся в worker.runtime; после изменения конфигурации перезапустите службу.")));
  }
  nodes.push(h("p", { class: "hint" }, tr("В Docker пути относятся к контейнеру worker: файлы должны попасть в volume моделей, смонтированный в /models, а не в файловую систему прокси.")),
    h("p", { class: "hint" }, tr("После переноса нажмите «Проверить файлы». Эта проверка не использует интернет и не запускает модель. Затем проверьте запуск на синтетическом примере.")));
  return nodes;
}
function renderModelCards() {
  const data = WORKER.data || {};
  for (const button of document.querySelectorAll("[data-add-hf]")) button.disabled=!info.editable || WORKER.busy;
  for (const role of ["analysis", "categorization", "worker"]) {
    const card = WORKER.cards.get(role) || createModelCard(role), model = selectedModel(role),
      weights = data.models?.[model], job = activeModelDownload(model) || modelDownloadJob(model),
      active = job && ["queued", "running", "paused"].includes(job.state),
      editable = role === "worker" ? info?.editable : LearningUI[role]?.editable,
      busy = WORKER.busy || LearningUI.busy.has(role), issue = localModelIssue(model), ready = !issue;
    card.status.textContent = tr(active ? workerStates[job.state] : issue || "Файлы проверены и готовы к запуску");
    card.status.classList.toggle("err", ["invalid", "unreadable"].includes(weights?.state));
    card.file.textContent = weights?.file ? `${weights.file} · ${formatModelBytes(weights.bytes)}` : modelName(model);
    card.runtime.textContent = tr(data.online
      ? data.runtime_ready ? "Среда запуска готова" : "Среда запуска ещё не готова"
      : "Проверка возможна после запуска службы анализа.");
    card.stage.hidden = !active;
    card.stage.textContent = active ? tr(installationStages[job.stage] || "Ожидает начала установки") : "";
    card.progress.hidden = !active || job.state !== "running";
    card.progress.max = job?.progress_total || 1;
    if (job?.progress_total && ["model_download", "runtime_download"].includes(job.stage)) {
      card.progress.value = job.progress || 0;
      card.stage.textContent += ` · ${formatModelBytes(job.progress)} / ${formatModelBytes(job.progress_total)}`;
    } else card.progress.removeAttribute("value");
    const error = !active && !ready ? (weights?.error ? { code: weights.error, stage: "model_verify" }
      : data.runtime?.state === "not_executable" ? { code: "runtime_not_executable", stage: "runtime_install" }
        : data.runtime?.state === "unreadable" ? { code: "permission_denied", stage: "runtime_install" }
        : job?.state === "failed" ? job.errors?.[0] : null) : null,
      errorKey = JSON.stringify([error, I18N.language]);
    if (card.errorKey !== errorKey) {
      card.error.replaceChildren(...(error ? [installationErrorNode(error)] : []));
      card.errorKey = errorKey;
    }
    card.download.textContent = tr(job?.state === "paused" ? "Продолжить установку" : error ? "Повторить установку" : "Скачать на сервер");
    card.download.hidden = ready;
    card.download.disabled = !editable || busy || !data.online || !data.enabled || !weights || (active && job.state !== "paused");
    card.verify.disabled = !editable || busy || !data.online || !data.model_verification || !weights || weights.state === "checking" || active;
    if (role !== "worker" && $(`#${role}-provider`).value === "local")
      $(`#${role}-test`).disabled = !editable || busy || !data.enabled || !ready;
    card.start.hidden = data.online && data.enabled;
    card.start.disabled = !editable || busy || !data.can_manage || data.state === "stopping";
    card.hint.textContent = tr(ready
      ? "Проверка файлов завершена. Работу модели проверяет кнопка «Проверить запуск» на синтетическом примере."
      : !data.online || !data.enabled
        ? "Для скачивания запустите обработку. Это также продолжит задания очереди; модели сами не скачиваются."
        : "Скачивание начнётся только по кнопке. Для закрытого контура откройте инструкцию ниже.");
    const metadataKey = JSON.stringify([model, weights?.path, weights?.sha256, data.runtime, I18N.language]);
    if (card.metadataKey !== metadataKey) {
      card.offlineBody.replaceChildren(...offlineInstructions(weights, data.runtime));
      card.metadataKey = metadataKey;
    }
  }
}
function renderWorkerSummary() {
  const data = WORKER.data || {};
  $("#analysis-worker-state").textContent = workerStateLabel(data);
  if (typeof renderLearningWidget === "function") renderLearningWidget();
  const rows = [["Модель обнаружения", LearningUI.analysis?.settings], ["Модель категоризации", LearningUI.categorization?.settings]];
  $("#analysis-worker-summary").replaceChildren(...rows.map(([label, settings]) =>
    h("div", { class: "worker-summary-row" }, h("span", {}, tr(label)),
      h("strong", {}, settings?.provider === "local" ? `${modelName(settings.model)} · ${tr(localModelIssue(settings.model) || "Файлы готовы")}`
        : settings?.provider === "none" ? tr("Выключено") : settings ? tr("Модель по API") : "—"))));
  for (const id of ["worker-toggle", "analysis-worker-toggle"]) {
    const button = $("#" + id);
    button.textContent = WORKER.transition ? tr(WORKER.transition) : tr(data.enabled ? "Остановить обработку" : "Запустить обработку");
    button.disabled = WORKER.busy || data.state === "stopping" || !data.can_manage || !info.editable;
    button.title = !data.can_manage ? tr("Этот воркер не поддерживает управление из панели") : "";
  }
  renderModelCards();
}
function jobProgress(job) {
  if (job.kind !== "download") return job.state === "succeeded" && !job.total
    ? tr("Нет данных для анализа") : `${job.completed} / ${job.total}`;
  if (job.state === "succeeded") return tr("Файлы подготовлены");
  const stage = tr(installationStages[job.stage] || "Установка модели");
  return job.progress_total && ["model_download", "runtime_download"].includes(job.stage)
    ? `${stage} · ${formatModelBytes(job.progress)} / ${formatModelBytes(job.progress_total)}` : stage;
}
function renderWorker() {
  const data = WORKER.data || {};
  renderWorkerSummary();
  $("#worker-controls").hidden = false;
  $("#worker-state").replaceChildren(h("p", {}, h("strong", {}, workerStateLabel(data))),
    h("p", { class: "hint" }, tr("Служба обрабатывает очередь и запускает локальные модели. Её запуск не скачивает модели и не включает автоматический сбор сообщений.")),
    ...(data.state === "error" ? [h("p", { class: "err" }, tr("Служба анализа недоступна. Проверьте процесс и настройки подключения."))] : []),
    ...(WORKER.actionError ? [h("p", { class: "err" }, WORKER.actionError)] : []));
  $("#worker-root").textContent = tr("Каталог на машине worker: {{0}}", [data.input_root || "—"]);
  for (const id of ["worker-submit", "worker-scan-sessions"])
    $("#" + id).disabled = WORKER.busy || !data.online || !data.enabled || !info.editable;
  const mode = $("#worker-mode").value;
  $("#worker-s1-field").hidden = mode === "configured";
  $("#worker-s1-help").hidden = mode === "configured";
  $("#worker-mode-help").textContent = tr(mode === "configured"
    ? "Используются сохранённые настройки обнаружения и категоризации. Выбор модели в карточке установки не меняет эти настройки."
    : mode === "qwen" ? "Используется модель, выбранная в карточке установки. Быстрые детекторы проверяют текст дополнительно."
      : "Используются быстрые детекторы. Локальная модель для этого режима не нужна.");
  const openDetails = new Set([...$("#worker-jobs").querySelectorAll("details[open][data-job-id]")].map((node) => node.dataset.jobId));
  const rows = (data.jobs || []).map((job) => {
    const buttons = [], terminal = !["queued", "running", "paused"].includes(job.state);
    if (!terminal) {
      buttons.push(h("button", { type: "button", class: "secondary", disabled: WORKER.busy || !info.editable,
        onclick: () => runWorkerAction(`/jobs/${job.id}/${job.state === "paused" ? "resume" : "pause"}`, {}) },
        tr(job.state === "paused" ? "Продолжить" : "Пауза")),
      h("button", { type: "button", class: "secondary", disabled: WORKER.busy || !info.editable,
        onclick: () => runWorkerAction(`/jobs/${job.id}/cancel`, {}) }, tr("Отменить задание")));
    } else if (job.kind === "download" && ["failed", "cancelled"].includes(job.state)) {
      buttons.push(h("button", { type: "button", class: "secondary", disabled: WORKER.busy || !data.enabled || !data.online || !info.editable,
        onclick: () => runWorkerAction("/models/download", { model: job.model }) }, tr("Повторить установку")));
    }
    const errors = (job.errors || []).map((error) => job.kind === "download"
      ? installationErrorNode(error) : h("p", { class: "hint" }, `${error.file || ""} ${error.type}: ${error.message}`));
    for (const node of errors) {
      const details = node.querySelector("details");
      if (details) { details.dataset.jobId = job.id; details.open = openDetails.has(job.id); }
    }
    return h("tr", {}, h("td", { class: "mono" }, job.id.slice(0, 8)),
      h("td", {}, job.kind === "download" ? `${tr("Установка модели")} · ${modelName(job.model)}` : job.source,
        h("div", { class: "hint" }, job.kind !== "scan" ? "" : tr(job.mode === "configured" ? "Настроенный фоновый анализ"
          : ["qwen", "xiaomi"].includes(job.mode) ? "Поиск локальной моделью (CPU) + быстрые детекторы" : "Локальный поиск · быстрые детекторы")),
        job.kind==="scan" && ["qwen","xiaomi"].includes(job.mode) ? h("small",{class:"hint"},modelName(job.model)) : null),
      h("td", {}, tr(workerStates[job.state] || job.state), job.control ? h("small", {}, " · " + tr("Остановка запрошена")) : null),
      h("td", {}, jobProgress(job), h("div", { class: "hint mono" }, job.current || ""), ...errors,
        job.candidate_page ? h("div", { class: "hint" }, tr("Фрагмент текста: {{0}}", [job.candidate_page])) : null,
        ...(job.warnings || []).map((warning) => h("div", { class: "hint" }, warning.code === "ungrounded_candidates"
          ? tr("Не подтверждены текстом: {{0}}", [warning.count]) : "S1: " + warning.s1_status))),
      h("td", {}, ...buttons));
  });
  $("#worker-jobs").replaceChildren(rows.length ? h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["ID", "Источник", "Состояние", "Прогресс", "Действия"].map((s) => h("th", {}, tr(s))))),
    h("tbody", {}, ...rows))) : h("p", {}, tr("Очередь пуста")));
}
async function loadWorkerSummary() {
  const request = ++WORKER.request;
  try {
    const data = await api("/api/v1/ui/worker");
    if (request !== WORKER.request) return;
    WORKER.data = data;
    syncModelChoices();
  } catch (error) {
    if (request !== WORKER.request) return;
    WORKER.data = { state: "error", online: false, can_manage: false, error: error.message };
  }
  if (location.hash === "#worker") renderWorker();
  else renderWorkerSummary();
}
const loadWorker = loadWorkerSummary;
async function workerAction(path, body) {
  if (WORKER.busy) throw new Error(tr("Дождитесь завершения текущего действия службы."));
  WORKER.busy = true;
  WORKER.actionError = null;
  if (path === "/start" || path === "/stop") WORKER.transition = path === "/start" ? "Запускается…" : "Останавливается…";
  renderWorker();
  try {
    const result = await api("/api/v1/ui/worker" + path, { method: "POST", body });
    await loadWorkerSummary();
    return result;
  } catch (error) {
    WORKER.actionError = error.message;
    throw error;
  } finally {
    WORKER.busy = false;
    WORKER.transition = null;
    renderWorker();
  }
}
function runWorkerAction(path, body) { return workerAction(path, body).catch(fail); }
for (const id of ["worker-toggle", "analysis-worker-toggle"])
  $("#" + id).addEventListener("click", () => runWorkerAction(WORKER.data?.enabled ? "/stop" : "/start", {}));
$("#worker-refresh").addEventListener("click", loadWorker);
for (const role of ["analysis", "categorization", "worker"])
  $(role === "worker" ? "#worker-model" : `#${role}-local-model`).addEventListener("change", () => {
    renderModelCards(); loadWorkerSummary();
  });
$("#worker-mode").addEventListener("change", renderWorker);
function submitWorkerSource(source) {
  const mode = $("#worker-mode").value;
  return runWorkerAction("/jobs", { source, model: $("#worker-model").value,
    use_s1: mode !== "configured" && $("#worker-s1").checked, mode });
}
$("#worker-submit").addEventListener("click", () => submitWorkerSource($("#worker-source").value.trim() || "."));
$("#worker-scan-sessions").addEventListener("click", () => submitWorkerSource("@sessions"));
Object.assign(TABS, { worker: loadWorker });
const workerTabs = ["#analysis", "#categorization", "#worker"];
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && workerTabs.includes(location.hash)) loadWorkerSummary();
});
setInterval(() => {
  if (!document.hidden && workerTabs.includes(location.hash) && !WORKER.busy) loadWorkerSummary();
}, 2500);
// Keep forms inert until their saved settings have arrived; early edits must not mask defaults.
learningFormVisibility();

const hfModelErrors = {
  hf_invalid_url: "Вставьте HTTPS-ссылку на репозиторий или файл модели на huggingface.co.",
  hf_not_found: "Репозиторий, ревизия или файл не найдены. Проверьте ссылку.",
  hf_access_denied: "Репозиторий требует доступа. Для установки по ссылке нужен общедоступный GGUF.",
  hf_unavailable: "Hugging Face недоступен с машины анализа. Проверьте интернет или прокси и повторите.",
  hf_no_gguf: "В репозитории нет подходящего GGUF. Найдите GGUF-версию модели; Safetensors, проекторы и наборы частей здесь не устанавливаются.",
  hf_file_not_supported: "По ссылке выбран неподдерживаемый файл. Нужен один основной файл GGUF, без mmproj и разделения на части.",
  hf_invalid_metadata: "Hugging Face не вернул размер и контрольную сумму файла. Установка не начата.",
  hf_metadata_too_large: "Каталог репозитория слишком большой. Используйте ссылку на другой репозиторий GGUF.",
  hf_catalog_full: "Достигнут лимит: 32 собственные модели в каталоге службы анализа.",
};
function hfModelError(error) { const code=error.messageKey || error.message; return tr(hfModelErrors[code] || code); }
let hfModelSession=0;
function openHfModel(role, initialUrl="") {
  const session=++hfModelSession, dialog=$("#hf-model-dialog");
  let resolved=null, busy=false;
  const source=h("input",{type:"url",required:true,id:"hf-model-url",placeholder:"https://huggingface.co/author/model-GGUF",value:initialUrl}),
    output=h("div",{id:"hf-model-result",role:"status","aria-live":"polite"}),
    find=h("button",{class:"secondary",type:"submit",id:"hf-model-resolve"},tr("Проверить ссылку")),
    add=h("button",{class:"primary",type:"button",id:"hf-model-add",disabled:true},tr("Добавить модель"));
  const validSession=()=>session===hfModelSession && dialog.open;
  const setBusy=(value)=>{busy=value;source.disabled=value;find.disabled=value;add.disabled=value || !resolved;const files=$("#hf-model-file");if(files)files.disabled=value;};
  source.addEventListener("input",()=>{resolved=null;add.disabled=true;output.replaceChildren();});
  const form=h("form",{onsubmit:async(event)=>{
    event.preventDefault();if(busy)return;setBusy(true);resolved=null;output.replaceChildren(h("p",{class:"hint"},tr("Проверяем файлы на Hugging Face…")));
    try {
      const result=await api("/api/v1/ui/worker/models/resolve",{method:"POST",body:{url:source.value.trim()}});
      if(!validSession())return;resolved=result;
      const files=h("select",{id:"hf-model-file"},...result.files.map((file)=>h("option",{value:file.file,selected:file.file===result.selected},`${file.file} · ${formatModelBytes(file.bytes)}`))),
        details=h("div",{class:"model-offline-body"});
      const renderDetails=()=>{
        const file=result.files.find((item)=>item.file===files.value);
        details.replaceChildren(h("dl",{class:"model-metadata"},h("dt",{},tr("Ревизия")),h("dd",{class:"mono"},result.revision),
          h("dt",{},"SHA-256"),h("dd",{class:"mono"},file.sha256)),
          h("a",{href:file.url,target:"_blank",rel:"noopener noreferrer"},tr("Источник файла")));
      };
      files.addEventListener("change",renderDetails);renderDetails();
      output.replaceChildren(h("p",{},result.repo),h("label",{class:"field"},h("span",{},tr("Файл GGUF")),files),details,
        h("p",{class:"hint"},tr("Ревизия и контрольная сумма закрепляются. Добавление в список не скачивает веса и не меняет сохранённые настройки анализа.")));
    } catch(error) { if(validSession())output.replaceChildren(h("p",{class:"err"},hfModelError(error))); }
    finally {if(validSession())setBusy(false);}
  }},h("label",{class:"field"},h("span",{},tr("Ссылка Hugging Face")),source),find);
  add.addEventListener("click",async()=>{
    if(busy || !resolved)return;setBusy(true);
    const file=resolved.files.find((item)=>item.file===$("#hf-model-file").value);
    try {
      const result=await workerAction("/models/register",{url:file.url});
      if(!validSession())return;
      ensureModelChoice(result.model);const select=modelSelect(role);select.value=result.model;
      select.dispatchEvent(new Event("input",{bubbles:true}));select.dispatchEvent(new Event("change",{bubbles:true}));
      dialog.close();select.focus();renderModelCards();
      toast(tr("Модель добавлена. Скачайте веса в карточке модели и сохраните настройки выбранного этапа."));
    } catch(error){if(validSession())output.append(h("p",{class:"err"},hfModelError(error)));}
    finally {if(validSession())setBusy(false);}
  });
  const available=WORKER.data?.online && WORKER.data?.hf_import && info.editable;
  find.disabled=!available;
  dialog.replaceChildren(h("div",{class:"review-confirm-body"},h("h2",{id:"hf-model-title"},tr("Своя модель Hugging Face")),
    h("p",{class:"hint"},tr("Вставьте ссылку на общедоступный репозиторий GGUF или конкретный файл. Для текстового анализа нужен основной GGUF; vision-проектор не загружается.")),
    !available?h("div",{class:"inline-notice"},tr(!WORKER.data?.online ? "Сначала запустите службу анализа для проверки ссылки." : "Обновите службу анализа для добавления моделей по ссылке."),
      !WORKER.data?.online&&WORKER.data?.can_manage?h("button",{type:"button",class:"secondary",onclick:async()=>{try{await workerAction("/start",{});openHfModel(role,source.value);}catch(error){fail(error);}}},tr("Запустить обработку")):null):null,
    form,output,h("div",{class:"form-actions"},add,h("button",{class:"secondary",type:"button",onclick:()=>dialog.close()},tr("Отмена")))));
  if(!dialog.open)dialog.showModal();source.focus();
}
for(const button of document.querySelectorAll("[data-add-hf]")) button.addEventListener("click",()=>openHfModel(button.dataset.addHf));
syncModelChoices();
