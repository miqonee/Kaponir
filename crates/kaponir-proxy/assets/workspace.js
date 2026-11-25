"use strict";

const desk = {
  jobs: [],
  offset: 0,
  sequence: 0,
  timer: null,
  datasetStatus: "draft",
  datasetOffset: 0,
  collection: null,
  edit: null,
};
ReviewUI.excluded = new Set();
function workButton(label, action, cls = "text-button", disabled = false) {
  return h(
    "button",
    {
      type: "button",
      class: cls,
      disabled,
      onclick: async (e) => {
        const button = e.currentTarget;
        button.disabled = true;
        try {
          await action();
        } catch (error) {
          fail(error);
        } finally {
          button.disabled = disabled;
        }
      },
    },
    tr(label),
  );
}
function requestKey() {
  return Array.from(crypto.getRandomValues(new Uint32Array(4)), (v) =>
    v.toString(16).padStart(8, "0"),
  ).join("");
}
async function postOnce(path, body) {
  const key = "kaponir-pending:" + path,
    signature = JSON.stringify(body);
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem(key));
  } catch {}
  const id = saved?.signature === signature ? saved.id : requestKey();
  sessionStorage.setItem(key, JSON.stringify({ signature, id }));
  const reply = await api(path, { method: "POST", body: { ...body, request_id: id } });
  sessionStorage.removeItem(key);
  return reply;
}
function rememberFindings(id) {
  const state = {
    filters: reviewFilters(),
    scope: ReviewUI.filterSet,
    chosen: [...ReviewUI.selected.keys()],
    snapshot: ReviewUI.snapshot,
    excluded: [...ReviewUI.excluded],
    offset: ReviewUI.offset,
    scroll: scrollY,
    focus: document.activeElement?.id,
  };
  sessionStorage.setItem("kaponir-return-findings:" + id, JSON.stringify(state));
}
async function returnFindings(id) {
  let state;
  try {
    state = JSON.parse(sessionStorage.getItem("kaponir-return-findings:" + id));
  } catch {}
  if (!state) {
    await openJobResults(id, "");
    return;
  }
  const f = state.filters;
  for (const [id, key] of [
    ["review-source", "source"],
    ["review-sort", "sort"],
    ["review-query", "q"],
    ["review-kind", "kind"],
    ["review-action", "action"],
  ])
    $("#" + id).value = f[key] || "";
  clearPicking();
  Object.assign(ReviewUI, {
    view: f.status,
    filterSet: state.scope,
    browseSet: f.snapshot,
    keys: f.keys || "",
    jobId: f.job_id,
    outcome: f.outcome,
    learningJob: f.learning_job,
    snapshot: state.snapshot,
    excluded: new Set(state.excluded),
    offset: state.offset,
    detailKey: null,
  });
  for (const key of state.chosen) ReviewUI.selected.set(key, { key });
  showTab("review");
  await loadReview();
  window.scrollTo(0, state.scroll);
  if (state.focus) document.getElementById(state.focus)?.focus({ preventScroll: true });
}
function jobRoute(job) {
  return "jobs/" + job.family + "/" + encodeURIComponent(job.id);
}
const stageNames = {
  queued: "В очереди",
  running: "В работе", processing: "В работе", scanning: "В работе", downloading: "В работе", interrupted: "Прервано",
  succeeded: "Завершено",
  completed: "Завершено",
  failed: "Ошибка",
  paused: "Приостановлено",
  cancelled: "Отменено",
  expired: "Срок хранения истёк",
  evicted: "Удалено из очереди",
};
function stage(job) {
  return tr(stageNames[job.state] || learningStates[job.state] || job.state);
}
const workingStates = new Set(["running", "processing", "scanning", "downloading"]);
function jobGroup(job) {
  if (workingStates.has(job.state)) return "running";
  return ["queued", "paused"].includes(job.state) ? job.state : "history";
}
function jobTitle(job) {
  if (job.family === "review") return job.total != null ? tr("Проверка находок · {{0}}", [number(job.total)]) : tr("Проверка находок");
  if (job.family === "learning" && job.kind === "turn")
    return job.event_id != null ? tr("Анализ запроса №{{0}}", [job.event_id]) : tr("Анализ запроса");
  if (job.kind === "session") return tr("Анализ переписки · {{0}}",[job.session_id?.slice(0,8) || job.event_id || job.name]);
  if (job.kind === "download") return tr("Установка модели");
  if (job.kind === "scan") return tr(job.name === "@sessions" ? "Поиск в сохранённых сессиях" : "Поиск значений в файлах");
  return job.name || tr(job.family === "learning" ? "Анализ документа" : "Обработка файлов");
}
function jobModel(job) {
  const id = (job.state === "running" && job.progress?.model) || job.source?.model || job.model;
  return (WORKER.data?.catalog || builtinModelChoices).find(model => model.id === id)?.label || id;
}
function jobContext(job) {
  const parts = [];
  if (job.session_id && job.session_id !== "request-" + job.event_id)
    parts.push(tr("Сессия {{0}}", [job.session_id.slice(0, 8)]));
  if (job.family === "learning" && job.kind === "document") parts.push(tr("Документ"));
  if (job.kind === "scan" && job.name && job.name !== "@sessions") parts.push(job.name);
  const model = jobModel(job);
  if (model) parts.push(model);
  return parts.join(" · ");
}
const queueReasons = {
  analysis_paused: "Фоновый анализ приостановлен в настройках LLM.",
  worker_offline: "Ожидает подключения службы обработки.",
  worker_paused: "Служба обработки приостановлена.",
  detection_model: "Ожидает готовности модели обнаружения и среды запуска.",
  categorization_model: "Ожидает готовности модели категоризации и среды запуска.",
};
function jobActivity(job) {
  const group = jobGroup(job), pr = job.progress;
  const count = (label, unit, done, total) => ({
    label: tr(label), text: tr(unit, [number(done),number(total)]), done, total,
  });
  if (group === "queued") {
    const wait = job.queue_wait;
    if (wait?.reason === "capacity") return {label:tr("Ждёт свободного обработчика. Лимит параллельных заданий — {{0}}.", [number(wait.limit)])};
    return {label:tr(queueReasons[wait?.reason] || "Ожидает запуска")};
  }
  if (group === "paused") return {label: tr("Обработка приостановлена")};
  if (group === "history") {
    let result;
    if (job.family === "learning" && job.extracted != null && ["succeeded","failed"].includes(job.state))
      result = tr("Сохранено находок · {{0}}", [number(job.extracted)]);
    else if (job.family === "review" && job.total > 0)
      result = tr("Обработано {{0}} из {{1}}", [number(job.processed ?? job.completed ?? 0), number(job.total)]);
    else if (job.kind === "scan" && job.state === "succeeded" && job.total === 0)
      result = tr("Нет данных для анализа");
    else if (job.kind === "download") {
      if (job.state === "succeeded") result = tr("Файлы подготовлены");
    } else if (job.family === "file" && job.total > 0)
      result = tr(job.name === "@sessions" ? "Обработано документов · {{0}} из {{1}}" : "Обработано файлов · {{0}} из {{1}}",
        [number(job.completed ?? 0), number(job.total)]);
    const error = job.error_code ? installationErrorMessage({code:job.error_code}) : job.error ? reviewError(job.error) : null;
    return error ? {label:error, text:result} : {label:result};
  }
  if (job.family === "review") {
    if (job.total > 0) return count("Проверка моделью", "Обработано {{0}} из {{1}}", job.processed ?? job.completed ?? 0, job.total);
    return {label:tr("Подготовка выборки")};
  }
  if (job.family === "learning") {
    if (pr?.stage === "detecting") return count("Поиск значений", "Фрагменты · {{0}} из {{1}}", pr.completed, pr.total);
    if (pr?.stage === "categorizing") return count("Категоризация находок", "Значения · {{0}} из {{1}}", pr.completed, pr.total);
    if (pr?.stage === "saving") return {label:tr("Сохранение результатов"), text:tr("Найдено кандидатов · {{0}}", [number(pr.found)])};
    return {label:tr(pr?.stage === "preparing" ? "Подготовка текста" : "Идёт фоновый анализ"), indeterminate:true};
  }
  if (job.kind === "download") {
    const label = tr(installationStages[job.stage] || "Установка модели");
    if (job.total > 0 && ["model_download","runtime_download"].includes(job.stage))
      return {label, text:formatModelBytes(job.completed ?? 0) + " / " + formatModelBytes(job.total), done:job.completed ?? 0, total:job.total};
    return {label, indeterminate:true};
  }
  if (job.total > 0) {
    const activity = count("Поиск значений", job.name === "@sessions" ? "Обработано документов · {{0}} из {{1}}" :
      "Обработано файлов · {{0}} из {{1}}", job.completed ?? 0, job.total);
    if (job.current_file) activity.label = tr("Поиск в файле «{{0}}»", [job.current_file]);
    return activity;
  }
  return {label:tr("Подготовка источников"), indeterminate:true};
}
function inferenceLine(job) {
  const live=job.inference_activity;
  const calls=live?.calls || [];
  const last=live?.last || job.last_call;
  const rows=calls.length ? calls : last ? [last] : [];
  if (!rows.length) return null;
  const phases={waiting_slot:"Ожидает слот API",waiting:"Ожидает ответа модели",preparing:"Подготовка входа модели",generating:"Модель генерирует ответ"};
  return h("div",{class:"job-meter"},rows.map(call=>{
    const parts=[tr(calls.length ? (phases[call.phase] || "В работе") : "Последний ответ модели")];
    if (Number.isFinite(call.elapsed_ms)) parts.push(tr("{{0}} с",[number(Math.floor(call.elapsed_ms/1000))]));
    if (Number.isFinite(call.completion_tokens)) parts.push(tr("Выдано токенов · {{0}}",[number(call.completion_tokens)]));
    if (Number.isFinite(call.tokens_per_second)) parts.push(tr("{{0}} ток/с",[number(Math.round(call.tokens_per_second*10)/10)]));
    else if (Number.isFinite(call.average_tokens_per_second)) parts.push(tr("В среднем {{0}} ток/с с отправки",[number(Math.round(call.average_tokens_per_second*10)/10)]));
    else {
      if (call.characters>0) parts.push(tr("Получено знаков · {{0}}",[number(call.characters)]));
      if (call.phase !== "waiting_slot") parts.push(tr(calls.length ? "Скорость пока недоступна" : "Модель не сообщила скорость"));
    }
    return h("span",{},parts.join(" · "));
  }));
}
function activityBlock(job) {
  const {label, text, done, total, indeterminate} = jobActivity(job);
  if (!label) return null;
  return h("div", {class:"job-activity"},
    h("div", {class:"job-activity-line"}, h("span", {class:"job-phase"}, label), text ? h("span", {class:"job-counter"}, text) : null),
    total > 0 || indeterminate ? h("progress", {max:total || undefined, value:total > 0 ? done : undefined,
      "aria-label":jobTitle(job) + " · " + label, "aria-valuetext":text}) : null, inferenceLine(job));
}
function jobBadge(job) {
  return h("span", {class:"job-state", "data-state":job.state}, stage(job));
}
function jobRow(job, preview = false, sharedWait = false) {
  const route = jobRoute(job), group = jobGroup(job), context = jobContext(job);
  const link = h("a", {class:"job-link " + (preview ? "job-preview-row" : "job-name"), href:"#"+route,
    "data-job-group":group, onclick:event=>{if (followLocalLink(event)) showTab(route);}},
    h("div", {class:"job-row-head"}, h("strong", {}, jobTitle(job)), (!preview && !sharedWait) || group === "history" ? jobBadge(job) : null),
    context ? h("span", {class:"job-meta"}, context) : null,
    sharedWait ? null : activityBlock(job));
  return preview ? link : h("article",{class:"job-history-row"},link);
}
const jobGroups = [["running","В работе"],["queued","В очереди"],["paused","На паузе"],["history","История"]];
function groupedJobs(rows, data, preview = false) {
  return jobGroups.flatMap(([group, title]) => {
    const part = rows.filter(job => jobGroup(job) === group);
    const total = group === "queued" ? data.queued : group === "running" ? data.active - data.queued : null;
    if (!part.length && !(preview && total)) return [];
    const sharedWait = group === "queued" && part.length > 1 && part.every(j => j.queue_wait?.reason === part[0].queue_wait?.reason);
    return [h("section", {class:"job-group", "data-group":group},
      h(preview ? "h3" : "h2", {class:"job-group-title"}, tr(title), total != null ? h("span", {}, number(total)) : null),
      sharedWait ? h("p", {class:"job-wait"}, jobActivity(part[0]).label) : null,
      part.map(job => jobRow(job,preview,sharedWait)),
      preview && total > part.length ? h("p", {class:"job-more"}, tr("Показано {{0}} из {{1}}", [number(part.length),number(total)])) : null)];
  });
}
function renderJobIndicator(data) {
  const trigger = $("#job-indicator"), preview = $("#job-preview-list");
  const rows = data.preview || data.jobs.slice(0, 4);
  const running = data.active - data.queued, counts = [];
  if (running) counts.push(tr("В работе · {{0}}", [number(running)]));
  if (data.queued) counts.push(tr("В очереди · {{0}}", [number(data.queued)]));
  const label = [tr("Задания"), ...counts].join(" · ");
  trigger.title = trigger.querySelector("span").textContent = label;
  trigger.dataset.compact = [tr("Задания"), data.active || null].filter(Boolean).join(" · ");
  trigger.classList.toggle("working", !!data.active);
  const signature = JSON.stringify([rows, counts, I18N.language]);
  const sync = $("#job-preview-sync");
  sync.hidden = false;
  sync.textContent = tr("Очередь обновлена в {{0}}", [time(data.updated)]);
  if (desk.previewSignature === signature) return;
  desk.previewSignature = signature;
  const focused = preview.contains(document.activeElement) ? document.activeElement : null;
  preview.replaceChildren(...(!data.active ? [h("p",{class:"job-idle"},tr("Активных заданий нет"))] : []), ...groupedJobs(rows, data, true));
  if (focused) ([...preview.querySelectorAll("a")].find(a=>a.getAttribute("href")===focused.getAttribute("href")) ||
    $("#job-preview .panel-title a")).focus({preventScroll:true});
}
async function openJobResults(id, outcome) {
  clearPicking();
  ReviewUI.browseSet = null;
  ReviewUI.keys = "";
  ReviewUI.filterSet = null;
  ReviewUI.jobId = id;
  ReviewUI.learningJob = null;
  ReviewUI.outcome = outcome;
  ReviewUI.view = "all";
  ReviewUI.offset = 0;
  $("#review-source").value = "all";
  $("#review-query").value = "";
  $("#review-action").value = "";
  $("#review-kind").value = "";
  showTab("review");
}
function renderJob(job, family) {
  job = { ...job, family };
  const n = job.total ?? job.targets?.length ?? 0,
    completed = job.processed ?? job.completed ?? 0,
    active = job.active || 0,
    error = job.error;
  const nodes = [workButton("Все задания", () => showTab("jobs"), "text-button job-back")];
  const context = jobContext(job);
  const source = family === "review" ? [job.source?.connection, job.source?.provider ? tr(job.source.provider === "local" ? "Локальная модель" : "Модель по API") : null] : [];
  const scenario = job.source?.scenario || job.scenario;
  if (scenario) source.push(tr(scenarioLabels[scenario] || scenario));
  const card = h("article", {class:"panel job-progress job-detail"},
    h("header", {class:"job-detail-head"}, h("h2", {}, jobTitle(job)), jobBadge(job)),
    context || source.some(Boolean) ? h("p", {class:"job-meta"}, [context, ...source].filter(Boolean).join(" · ")) : null,
    activityBlock(job));
  if (family === "review") {
    if (["running", "queued"].includes(job.state))
      card.append(h("p", {}, tr("Принято {{0}} · ждут {{1}} · в работе {{2}}", [
        number(n), number(Math.max(0, n - completed - active)), number(active),
      ])));
    if (job.skipped)
      card.append(
        h("p", { class: "hint" }, tr("Остались локально или уже разобраны · {{0}}", [job.skipped])),
      );
    if (job.state === "running" && !completed)
      card.append(h("p", {}, tr("Ожидаем первый ответ модели")));
    const actions = h("div", { class: "form-actions" });
    for (const [key, label, count] of [
      ["succeeded", "Рекомендации", completed - (job.manual || 0) - (job.failed || 0)],
      ["needs_review", "Вручную", job.manual],
      ["failed", "Ошибки", job.failed],
    ]) {
      if (count)
        actions.append(
          workButton(tr(label) + " · " + count, () => openJobResults(job.id, key), "secondary"),
        );
    }
    if (
      job.source?.provider === "local" &&
      /worker|model_not_ready|model_connection_failed|runtime/.test(error || "")
    ) {
      actions.append(
        workButton("Подготовить модель", () => prepareModel(job.source.model, jobRoute(job))),
      );
    }
    if (job.state === "failed")
      actions.append(
        workButton(
          "Повторить незавершённые и ошибки",
          async () => {
            const next = await postOnce("/api/v1/ui/review/jobs/" + job.id + "/retry", {});
            showTab("jobs/review/" + next.id);
          },
          "primary",
          !info.editable,
        ),
      );
    if (job.source?.provider === "api") {
      if (desk.apiActive != null && job.state === "running")
        card.append(h("p", { class: "hint" }, tr("Активные запросы") + " · " + desk.apiActive));
      if (job.last_packet_seconds != null)
        card.append(
          h(
            "p",
            { class: "hint" },
            tr("Последний ответ модели · {{0}} с", [job.last_packet_seconds.toFixed(1)]),
          ),
        );
    } else if (job.source?.provider === "local" && job.state === "running" && desk.workerReadings) {
      card.append(viewHardware(desk.workerReadings));
    }
    card.append(actions);
    nodes.push(
      card,
      workButton("Вернуться к выборке", () => returnFindings(job.id)),
    );
  } else {
    if (family === "learning") {
      if (job.state === "running" && job.progress?.stage === "detecting")
        card.append(h("p", {class:"hint"}, tr("Найдено кандидатов · {{0}}", [number(job.progress.found)])));
      const actions = h("div", {class:"form-actions"});
      if (job.event_id != null)
        actions.append(workButton(tr("Открыть запрос №{{0}}", [job.event_id]), () => openInspector(job.event_id), "secondary"));
      else if (job.kind === "session" && job.session_id)
        actions.append(workButton("Открыть переписку",async()=>{await loadDashboard();openSession({session_id:job.session_id});},"secondary"));
      if (job.extracted > 0 && ["failed","succeeded"].includes(job.state))
        actions.append(workButton("Открыть находки этого запуска", () => openLearningResults(job.id), "secondary"));
      if (job.retryable) actions.append(workButton("Повторить", () => learningAction(job.id, "retry")));
      if (job.state === "queued" && job.queue_wait?.reason)
        actions.append(job.queue_wait.reason === "capacity"
          ? workButton("Настройки параллельности", async () => {
              await showTab("analysis");
              if (location.hash === "#analysis") $("#analysis-parallelism").focus();
            })
          : workButton("Настройки LLM", () => showTab("settings")));
      if (actions.childElementCount) card.append(actions);
      const trace = inferenceDetails(job);
      if (trace) card.append(trace);
    } else {
      card.append(workButton("Файловая служба", () => showTab("worker"), "secondary"));
    }
    nodes.push(card);
  }
  if (error && jobGroup(job) !== "history") card.append(h("p", {class:"err"}, reviewError(error)));
  const stamps = [["Поставлено в очередь",job.created],["Начало работы",job.started],["Последнее изменение",job.updated]]
    .filter(([,value]) => value > 0);
  if (stamps.length) card.append(h("dl", {class:"job-times"}, stamps.map(([key,value]) => h("div", {},
    h("dt", {}, tr(key)), h("dd", {}, h("time", {datetime:new Date(value).toISOString()}, time(value,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"})))))));
  return nodes;
}
async function openLearningResults(id) {
  clearPicking();
  Object.assign(ReviewUI, {
    browseSet: null,
    keys: "",
    filterSet: null,
    jobId: null,
    learningJob: id,
    view: "all",
    offset: 0,
  });
  $("#review-query").value = "";
  $("#review-source").value = "all";
  $("#review-action").value = "";
  $("#review-kind").value = "";
  showTab("review");
}
async function refreshJobs() {
  clearTimeout(desk.timer);
  const seq = ++desk.sequence,
    route = location.hash.slice(1),
    [section, family, encoded] = route.split("/");
  const id = encoded ? decodeURIComponent(encoded) : null;
  try {
    const params = new URLSearchParams({
      limit: section === "jobs" ? 100 : 1,
      offset: section === "jobs" && !family ? desk.offset : 0,
    });
    const [data, detail] = await Promise.all([
      api("/api/v1/ui/jobs?" + params),
      family === "review" && id
        ? api("/api/v1/ui/review/jobs/" + id + "?brief=true")
        : family === "learning" && id
          ? api("/api/v1/ui/jobs?" + new URLSearchParams({ family, id })).then((data) => data.jobs[0])
          : Promise.resolve(null),
    ]);
    if (seq !== desk.sequence) return;
    desk.jobs = data.jobs;
    desk.lastUpdate = data.updated;
    desk.apiActive = data.api_active;
    if (detail?.source?.provider === "local" && detail.state === "running") {
      desk.workerReadings = await api("/api/v1/ui/worker").catch(() => null);
      if (seq !== desk.sequence) return;
    }
    renderJobIndicator(data);
    if (section === "review" && location.hash === "#review" && ReviewUI.data && !ReviewUI.busy &&
        $("#review-list").getAttribute("aria-busy") !== "true" && !document.querySelector("#tab-review dialog[open]")) {
      const activity = data.review_activity || (data.preview || []).filter(job => job.family === "review").map(job => [job.id,job.state,job.completed]);
      const signature = JSON.stringify(activity), previous = desk.reviewProgress;
      desk.reviewProgress = signature;
      if (previous !== signature && (previous != null || activity.length)) void loadReview({background:true});
    }
    const first = (data.preview || data.jobs)[0];
    const home = $("#home-job");
    home.hidden = !first;
    const homeSignature = JSON.stringify([first, I18N.language]);
    if (first && desk.homeSignature !== homeSignature) {
      const focused = home.contains(document.activeElement);
      home.replaceChildren(h("h2", {}, tr(data.active ? "Текущее задание" : "Последнее задание")), jobRow(first));
      if (focused) home.querySelector("a")?.focus({preventScroll:true});
    }
    desk.homeSignature = homeSignature;
    if (route === location.hash.slice(1) && section === "settings") void refreshWorkerReadings();
    if (route !== location.hash.slice(1) || section !== "jobs") return;
    const host = $("#jobs-content");
    host.setAttribute("aria-busy", "false");
    let nodes, selected;
    if (family && id) {
      selected = detail || data.jobs.find((j) => j.id === id && j.family === family);
      if (!selected && family !== "review") {
        const extra = await api("/api/v1/ui/jobs?" + new URLSearchParams({ family, id }));
        if (seq !== desk.sequence || route !== location.hash.slice(1)) return;
        selected = extra.jobs[0];
      }
      if (selected && data.model_activity?.[id]) selected={...selected,inference_activity:data.model_activity[id]};
      const unavailable = family === "file" && !data.worker_online;
      if (selected && family === "file") desk.lastFileJob = selected;
      if (!selected && unavailable && desk.lastFileJob?.id === id) selected = desk.lastFileJob;
      nodes = selected
        ? renderJob(selected, family)
        : [
            h(
              "p",
              { class: "empty" },
              tr(unavailable ? "Файловая служба недоступна" : "Задание больше не хранится"),
            ),
            workButton("Все задания", () => showTab("jobs")),
          ];
      if (selected && unavailable)
        nodes.push(h("p", { class: "inline-notice" }, tr("Файловая служба недоступна")));
    } else {
      if (desk.offset && !data.jobs.length) {
        desk.offset = Math.max(0, Math.floor((data.total - 1) / 100) * 100);
        return refreshJobs();
      }
      nodes = groupedJobs(data.jobs, data);
      if (!nodes.length)
        nodes.push(
          h("div", { class: "empty" }, tr("Очередь пуста. Запустите проверку из находок.")),
          workButton("Открыть находки", () => showReview()),
        );
      if (desk.offset || desk.offset + data.jobs.length < data.total)
        nodes.push(
          h(
            "div",
            { class: "form-actions" },
            workButton(
              "Назад",
              () => {
                desk.offset -= 100;
                return refreshJobs();
              },
              "secondary",
              !desk.offset,
            ),
            h("span",{class:"hint"},tr("{{0}}–{{1}} из {{2}}",[number(desk.offset + 1),number(desk.offset + data.jobs.length),number(data.total)])),
            workButton(
              "Далее",
              () => {
                desk.offset += 100;
                return refreshJobs();
              },
              "secondary",
              desk.offset + 100 >= data.total,
            ),
          ),
        );
    }
    const sync = $("#jobs-sync");
    sync.hidden = false;
    sync.textContent = tr("Очередь обновлена в {{0}}", [time(data.updated)]);
    const state = selected ? [selected.state, selected.state === "running" ? selected.progress?.stage : null] : [data.active - data.queued, data.queued];
    const stateKey = JSON.stringify(state);
    if (route === desk.jobRoute && desk.lastJobState !== stateKey) {
      $("#jobs-announcement").textContent = selected ? [jobTitle(selected),stage(selected),jobActivity(selected).label].filter(Boolean).join(" · ") :
        tr("В работе {{0}}, в очереди {{1}}", state);
    } else if (route !== desk.jobRoute) $("#jobs-announcement").textContent = "";
    desk.lastJobState = stateKey;
    const signature = JSON.stringify([route, family ? selected : data.jobs, family ? null : [data.total,data.active,data.queued,desk.offset],
      data.worker_online, family === "review" && selected?.state === "running" ? [data.api_active,desk.workerReadings?.resources] : null, I18N.language]);
    if (desk.jobSignature !== signature || !host.children.length) {
      const same = route === desk.jobRoute, focus = same && host.contains(document.activeElement) ? document.activeElement : null;
      const open = same && host.querySelector(".inference-details")?.open;
      host.replaceChildren(...nodes);
      if (open && host.querySelector(".inference-details")) host.querySelector(".inference-details").open = true;
      if (focus) {
        const target = [...host.querySelectorAll("a,button,summary")].find(el => focus.matches("a") ?
          el.getAttribute("href") === focus.getAttribute("href") : el.tagName === focus.tagName && el.textContent === focus.textContent);
        (target || $("#page-title")).focus({preventScroll:true});
      }
      desk.jobSignature = signature;
    }
    desk.jobRoute = route;
    host.querySelector(".connection-notice")?.remove();
  } catch (e) {
    if (seq !== desk.sequence) return;
    const trigger = $("#job-indicator");
    trigger.title = trigger.querySelector("span").textContent = tr("Задания") + " · " + tr("Статус недоступен");
    trigger.dataset.compact = tr("Задания") + " · ?";
    trigger.classList.remove("working");
    $("#job-preview-sync").hidden = true;
    $("#jobs-sync").hidden = true;
    const preview = $("#job-preview-list");
    if (!preview.querySelector(".connection-notice")) {
      const focused = preview.contains(document.activeElement);
      const retry = workButton("Повторить", refreshJobs);
      preview.replaceChildren(h("p", {class:"connection-notice hint"}, tr("Статус недоступен")), retry);
      if (focused) retry.focus({preventScroll:true});
    }
    desk.previewSignature = null;
    if (section === "jobs" && seq === desk.sequence) {
      const host = $("#jobs-content");
      host.setAttribute("aria-busy", "false");
      if (desk.jobRoute !== route) { host.replaceChildren(); desk.jobRoute = route; }
      if (!host.querySelector(".connection-notice"))
        host.prepend(h("div", {class:"connection-notice inline-notice", role:"status"},
          h("span", {}, tr("Статус недоступен"), desk.lastUpdate ? " · " + tr("Обновлено · {{0}}", [time(desk.lastUpdate)]) : ""),
          workButton("Повторить", refreshJobs)));
    }
    desk.jobSignature = null;
  } finally {
    if (seq === desk.sequence)
      desk.timer = setTimeout(() => {
        if (!document.hidden) refreshJobs();
      }, 2000);
  }
}
TABS.jobs = refreshJobs;
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshJobs();
});
$("#job-indicator").addEventListener("click", () => {
  if (!("showPopover" in HTMLElement.prototype)) showTab("jobs");
});
$("#discover-findings").onclick = async () => {
  const keys = D.report?.finding_keys || [];
  if (!keys.length) return;
  const snap = await api("/api/v1/ui/review/selection", {
    method: "POST",
    body: { keys: keys.join(","), status: "all" },
  });
  openFindingSet(snap);
};
async function openRuleFindings(id, scope) {
  const snap = await api("/api/v1/ui/review/selection", {
    method: "POST",
    body: { status: "all", rule_id: id, rule_scope: scope },
  });
  openFindingSet(snap);
}
function openFindingSet(snap) {
  clearPicking();
  Object.assign(ReviewUI, {
    browseSet: null,
    keys: "",
    jobId: null,
    learningJob: null,
    filterSet: snap.snapshot,
    offset: 0,
    view: "all",
    detailKey: null,
  });
  $("#review-query").value = "";
  $("#review-source").value = "all";
  $("#review-action").value = "";
  $("#review-kind").value = "";
  showTab("review");
}
async function collectFindings(selection) {
  const receipt = await postOnce("/api/v1/ui/dataset/collect", selection);
  const status = $("#review-result");
  status.replaceChildren(
    h("strong", {}, tr("Добавлено {{0}} · уже были {{1}}", [receipt.added, receipt.existing])),
    workButton("Открыть добавленные примеры", () => {
      desk.collection = receipt.id;
      desk.datasetStatus = "all";
      desk.datasetOffset = 0;
      showTab("dataset");
    }),
  );
  if (receipt.new_occurrences > receipt.added)
    status.append(h("span", {class:"hint"}, tr("Новых вхождений · {{0}}", [number(receipt.new_occurrences)])));
  if (receipt.added || receipt.new_occurrences)
    status.append(
      workButton("Отменить добавление", async () => {
        const r = await api("/api/v1/ui/dataset/collections/" + receipt.id + "/undo", {
          method: "POST",
          body: {},
        });
        status.textContent = r.removed_occurrences > r.removed
          ? tr("Убрано значений · {{0}}, вхождений · {{1}}", [number(r.removed), number(r.removed_occurrences)])
          : tr("Отменено · {{0}}", [r.removed]);
        await loadReview();
      }),
    );
  await loadReview();
}
function datasetQuery(extra = {}) {
  const q = { status: desk.datasetStatus, offset: desk.datasetOffset, limit: 100, ...extra };
  if (desk.collection) q.collection = desk.collection;
  return new URLSearchParams(q);
}
function downloadRows(rows, name) {
  const url = URL.createObjectURL(
    new Blob(
      rows.map((row) => JSON.stringify(row) + "\n"),
      { type: "application/x-ndjson" },
    ),
  );
  h("a", { href: url, download: name }).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function exportRows(status) {
  const data = await api("/api/v1/ui/dataset?" + datasetQuery({ status, export: true }));
  const rows =
    status === "checked"
      ? data.items.map(({ id, group, source, language, format, text, spans }) => ({
          id,
          group,
          source,
          language,
          format,
          text,
          spans,
        }))
      : data.items;
  downloadRows(rows, status === "checked" ? "kaponir-training.jsonl" : "kaponir-drafts.jsonl");
}
function datasetSource(row) {
  switch (row.source) {
    case "detector": return tr("Быстрый детектор");
    case "learning": return tr("Фоновый анализ");
    case "synthetic-curated": return tr("Примеры из этого браузера");
    default: return row.source;
  }
}
function showExampleOccurrences(row) {
  const dialog = h("dialog", {class:"review-confirm", "aria-labelledby":"occurrence-title"});
  dialog.addEventListener("close", () => dialog.remove(), {once:true});
  document.body.append(dialog);
  const excerpts = row.occurrences.map(source => {
    const span = source.spans.find(mark => mark.value === row.value);
    return h("li", {}, h("strong", {}, datasetSource(source)),
      h("blockquote", {class:"candidate-fragment"}, candidateExcerpt(source.text, row.value, span && [span.start_byte,span.end_byte])));
  });
  dialog.replaceChildren(h("div", {class:"review-confirm-body"},
    h("h2", {id:"occurrence-title"}, tr("Вхождения значения")), h("code", {class:"decision-value"}, row.value),
    h("ul", {class:"occurrence-excerpts"}, excerpts), workButton("Закрыть", () => dialog.close(), "secondary")));
  dialog.showModal();
}
function editExample(row, index = 0, drafts = 0) {
  desk.edit = row.id;
  const spans = row.spans.map((s) => ({ ...s }));
  const excerpt = h("textarea", { readonly: true, rows: 5, "aria-label": tr("Исходный фрагмент") });
  excerpt.value = row.text;
  const kind = h(
    "select",
    { "aria-label": tr("Тип значения") },
    ...[...new Set([...STANDARD_KINDS, ...row.spans.map((s) => s.kind), "ADDRESS", "ID"])].map((k) =>
      h("option", { value: k }, kindLabel(k)),
    ),
  );
  const action = h(
    "select",
    { "aria-label": tr("Действие") },
    ...["mask", "keep", "review"].map((v, i) =>
      h("option", { value: v }, tr(["Скрыть", "Оставить открытым", "Требует проверки"][i])),
    ),
  );
  const marks = h("div", { class: "dataset-marks" }),
    encoder = new TextEncoder();
  const preview = h("div", {class:"marked-source"});
  const sourceInput = h("label", {class:"field", hidden:true}, tr("Исходный фрагмент"), excerpt);
  const fields = h("div", {hidden:true}, h("div", {class:"form-actions"},
    h("label", {class:"field"}, tr("Тип значения"), kind), h("label", {class:"field"}, tr("Действие"), action)),
    h("p", {class:"hint"}, tr("Выделите точное значение в тексте и запишите отметку.")),
    workButton("Записать выделение", mark), workButton("Отмена", () => { selected=null; modified=false; showMarks(); editRange(false); }));
  let selected = null, modified = false, saving = false;
  function editRange(open) {
    sourceInput.hidden = fields.hidden = !open;
    preview.hidden = open;
  }
  for (const control of [kind, action])
    control.onchange = () => {
      modified = true;
    };
  excerpt.onselect = () => {
    if (selected !== null) modified = true;
  };
  function selectSpan(i) {
    const span = spans[i], pos = byteMap(row.text);
    selected = i;
    kind.value = span.kind;
    action.value = span.action;
    editRange(true);
    excerpt.focus();
    excerpt.setSelectionRange(pos(span.start_byte), pos(span.end_byte));
    modified = false;
    for (const button of marks.querySelectorAll(".mark-choice")) button.setAttribute("aria-pressed", String(Number(button.dataset.index) === i));
  }
  function showMarks() {
    const offset = byteMap(row.text), text = [];
    let cursor = 0;
    for (const {span,i} of spans.map((span,i)=>({span,i})).sort((a,b)=>a.span.start_byte-b.span.start_byte)) {
      const start=offset(span.start_byte), end=offset(span.end_byte);
      text.push(row.text.slice(cursor,start), h("mark", {class:"candidate-match"},
        h("button", {type:"button", onclick:()=>selectSpan(i)}, row.text.slice(start,end))));
      cursor=end;
    }
    preview.replaceChildren(...text,row.text.slice(cursor));
    marks.replaceChildren(
      ...spans.map((s, i) =>
        h(
          "div",
          { class: "work-row" },
          h("button", {class:"mark-choice", type:"button", "data-index":i, "aria-pressed":String(selected===i), onclick:()=>selectSpan(i)},
            h("strong", {}, s.value), h("small", {}, kindLabel(s.kind) + " · " + tr({mask:"Скрыть",keep:"Оставить открытым",review:"Требует проверки"}[s.action]))),
          workButton("Убрать отметку", () => {
            spans.splice(i, 1);
            selected = null; modified = false; editRange(false);
            showMarks();
          }),
        ),
      ),
    );
  }
  function mark() {
    const [lo, hi] = [excerpt.selectionStart, excerpt.selectionEnd],
      value = excerpt.value.slice(lo, hi);
    if (!value.trim()) throw new Error(tr("Сначала выделите значение в тексте"));
    const start = encoder.encode(row.text.slice(0, lo)).length,
      end = start + encoder.encode(value).length;
    const index = selected ?? spans.findIndex((s) => s.start_byte === start && s.end_byte === end);
    if (spans.some((s, i) => i !== index && s.start_byte < end && s.end_byte > start))
      throw new Error(tr("Выделения пересекаются"));
    const span = { value, kind: kind.value, action: action.value, start_byte: start, end_byte: end };
    if (index < 0) spans.push(span);
    else spans[index] = span;
    selected = null;
    modified = false;
    showMarks();
    editRange(false);
  }
  async function save(status, advance = false) {
    if (saving) return;
    if (!fields.hidden && (modified || (selected === null && excerpt.selectionEnd > excerpt.selectionStart))) mark();
    saving = true;
    editor.disabled = true;
    try {
      await api("/api/v1/ui/dataset/import", {method:"POST", body:{items:[{...row, spans, status}]}});
      if (!editor.isConnected || location.hash !== "#dataset") return;
      desk.edit = null;
      if (advance && desk.datasetStatus !== "draft") {
        desk.datasetStatus = "draft";
        desk.datasetOffset = 0;
        index = 0;
      }
      $("#dataset-feedback").textContent = tr(status === "checked" ? "Пример сохранён как проверенный." : "Черновик сохранён.");
      await loadVaultDataset(advance ? index : null);
    } finally {
      saving = false;
      editor.disabled = false;
    }
  }
  showMarks();
  const host = $("#vault-dataset");
  const editor = h("fieldset", {class:"dataset-editor"}, h("legend", {class:"sr-only"}, tr("Разметка примера")),
    h("div", {class:"dataset-editor-nav"}, workButton("К примерам", () => {
      desk.edit = null;
      return loadVaultDataset();
    }), h("span", {class:"hint"}, tr("Осталось черновиков · {{0}}", [number(drafts)]))),
    h("div", {class:"dataset-editor-grid"},
      h("section", {class:"panel dataset-source"}, h("h2", {tabindex:-1}, row.value || tr("Пример для датасета")),
        h("p", {class:"hint"}, datasetSource(row)), preview, sourceInput,
        row.occurrences ? workButton(tr("Показать вхождения · {{0}}", [number(row.occurrences.length)]), () => showExampleOccurrences(row), "secondary") : null,
        row.finding ? workButton("Открыть исходную находку", () => showReview([row.finding], "all")) : null),
      h("section", {class:"panel dataset-annotations"}, h("h2", {}, tr("Разметка примера")), marks, fields,
      workButton("Новая отметка", () => {
        selected = null;
        modified = false;
        showMarks();
        editRange(true);
        excerpt.focus();
        excerpt.setSelectionRange(0,0);
      }))),
    h(
      "div",
      { class: "form-actions" },
      workButton("Сохранить черновик", () => save("draft"), "secondary", !info.editable),
      workButton("Сохранить проверенным", () => save("checked"), row.status === "checked" ? "primary" : "secondary", !info.editable),
      row.status !== "checked" ? workButton("Сохранить и продолжить проверку", () => save("checked",true), "primary", !info.editable) : null,
    ),
  );
  host.replaceChildren(editor);
  editor.querySelector("h2").focus();
}
function openFindingExample(id) {
  desk.openExample = id;
  desk.datasetStatus = "all";
  desk.collection = null;
  desk.datasetOffset = 0;
  showTab("dataset");
}
async function loadVaultDataset(continueAt = null, example = null) {
  const seq = (desk.datasetRequest = (desk.datasetRequest || 0) + 1);
  let data;
  try {
    data = await api("/api/v1/ui/dataset?" + datasetQuery(example ? {example} : {}));
  } catch (error) {
    if (seq === desk.datasetRequest && !desk.edit && location.hash === "#dataset")
      $("#vault-dataset").replaceChildren(h("div", {class:"empty-state"}, h("p", {role:"alert"}, error.message),
        workButton("Повторить", () => loadVaultDataset(continueAt, example))));
    return;
  }
  if (seq !== desk.datasetRequest || desk.edit || location.hash !== "#dataset") return;
  if (example) {
    if (data.items.length) return editExample(data.items[0], 0, data.drafts);
    $("#dataset-feedback").textContent = tr("Этого примера больше нет в датасете.");
    return loadVaultDataset();
  }
  if (desk.datasetOffset && !data.items.length) {
    desk.datasetOffset = Math.max(0, Math.floor((data.total-1)/100)*100);
    return loadVaultDataset(continueAt === null ? null : 0);
  }
  if (continueAt !== null && data.items.length) {
    const i = continueAt < data.items.length ? continueAt : 0;
    editExample(data.items[i], i, data.drafts);
    return;
  }
  if (continueAt !== null && !data.drafts) {
    $("#dataset-feedback").textContent = tr("Все черновики этой выборки проверены. Можно выгрузить примеры для обучения.");
    desk.datasetStatus = "checked";
    desk.datasetOffset = 0;
    return loadVaultDataset();
  }
  const host = $("#vault-dataset");
  const filter = h(
    "div", {class:"seg dataset-filter", role:"group", "aria-label":tr("Состояние примеров")},
    ...[
      ["draft", "Черновики", data.drafts],
      ["checked", "Проверенные", data.checked],
      ["all", "Все примеры", data.checked + data.drafts],
    ].map(([value, label, n]) =>
      h("button", {type:"button","aria-pressed":String(desk.datasetStatus === value), onclick:() => {
        if (desk.datasetStatus === value) return;
        desk.datasetStatus = value; desk.datasetOffset = 0;
        loadVaultDataset().catch(fail);
      }}, tr(label) + " · " + n),
    ),
  );
  const file = h("input", {
    type: "file",
    accept: ".jsonl,.ndjson",
    "aria-label": tr("Загрузить исправления"),
  });
  file.onchange = async () => {
    try {
      if (!file.files.length) return;
      const rows = (await file.files[0].text())
        .split(/\r?\n/)
        .filter((s) => s.trim())
        .map((s) => JSON.parse(s));
      const r = await api("/api/v1/ui/dataset/import", { method: "POST", body: { items: rows } });
      toast(tr("Сохранено · {{0}}", [r.updated]));
      await loadVaultDataset();
    } catch (e) {
      fail(e);
    }
  };
  const tools = h(
    "div",
    { class: "form-actions" },
    filter,
    data.drafts ? workButton(tr("Начать проверку · {{0}}", [number(data.drafts)]), () => {
      desk.datasetStatus = "draft"; desk.datasetOffset = 0;
      return loadVaultDataset(0);
    }, "primary") : null,
    workButton("Выгрузить черновики", () => exportRows("draft"), "secondary", !data.drafts),
    workButton("Выгрузить для обучения", () => exportRows("checked"), "secondary", !data.checked),
  );
  const content = [
    tools,
    h(
      "details",
      { class: "advanced-settings" },
      h("summary", {}, tr("Загрузить исправления JSONL")),
      file,
    ),
  ];
  if (desk.collection)
    content.push(
      workButton("Весь датасет", () => {
        desk.collection = null;
        desk.datasetOffset = 0;
        return loadVaultDataset();
      }),
    );
  if (!data.items.length)
    content.push(
      h("div", { class: "empty-state" }, h("strong", {}, tr("В этой выборке пока нет примеров")),
        h("p", {}, tr(desk.datasetStatus === "checked" && data.drafts ? "Откройте черновик и проверьте его разметку." : "В находках можно добавить в датасет отдельные значения или сразу всю выборку.")),
        desk.datasetStatus === "checked" && data.drafts ? workButton("Открыть черновики", () => {
          desk.datasetStatus="draft"; desk.datasetOffset=0; return loadVaultDataset();
        }) : workButton("Открыть находки", () => showReview())),
    );
  else content.push(h("table", {class:"dataset-table"},
    h("thead", {}, h("tr", {}, ["Значение","Состояние","Источник"].map(label => h("th", {scope:"col"}, tr(label))))),
    h("tbody", {}, data.items.map((row, index) => h("tr", {},
      h("td", {}, workButton(row.value || row.text.slice(0, 70), () => editExample(row, index, data.drafts), "finding-value")),
      h("td", {}, h("span", {class:"dataset-state", "data-state":row.status}, tr(row.status==="checked" ? "Проверено" : "Черновик"))),
      h("td", {}, datasetSource(row), row.occurrences ? h("small", {class:"hint"}, tr("Вхождений · {{0}}", [number(row.occurrences.length)])) : null))))));
  if (desk.datasetOffset || desk.datasetOffset + 100 < data.total)
    content.push(
      h(
        "div",
        { class: "form-actions" },
        workButton(
          "Назад",
          () => {
            desk.datasetOffset -= 100;
            return loadVaultDataset();
          },
          "secondary",
          !desk.datasetOffset,
        ),
        workButton(
          "Далее",
          () => {
            desk.datasetOffset += 100;
            return loadVaultDataset();
          },
          "secondary",
          desk.datasetOffset + 100 >= data.total,
        ),
      ),
    );
  const focusedFilter = host.querySelector(".dataset-filter")?.contains(document.activeElement);
  host.replaceChildren(...content);
  if (focusedFilter) filter.querySelector('[aria-pressed="true"]')?.focus({preventScroll:true});
  refreshFilePickers();
}
$("#dataset-migrate").onclick = async () => {
  try {
    const items = await datasetOperation("readonly", (store, done) => {
      const req = store.getAll();
      req.onsuccess = () => done(req.result);
    });
    const result = await api("/api/v1/ui/dataset/import", {
      method: "POST",
      body: { legacy: true, items },
    });
    desk.datasetStatus = "checked";
    desk.collection = null;
    desk.edit = null;
    await loadVaultDataset();
    toast(tr("Сохранено · {{0}}", [result.updated]));
  } catch (e) {
    fail(e);
  }
};
TABS.dataset = async () => {
  const example = desk.openExample;
  desk.openExample = null;
  desk.edit = null;
  $("#dataset-feedback").textContent = "";
  await loadVaultDataset(null, example);
};
refreshJobs();

function viewHardware(worker) {
  const hw = worker.hardware || {}, res = worker.resources || {};
  const field = (label, value) => value == null || value === "" ? null : h("div", {}, h("dt", {}, tr(label)), h("dd", {}, value));
  const mib = value => value == null ? null : bytesMiB(value);
  const main = h("dl", {class:"worker-readings"},
    field("Доступная память", mib(res.available_bytes)), field("Процессоры воркера", hw.host_logical_cpus),
    field("CPU процесса модели", res.model_cpu_cores == null ? null : res.model_cpu_cores + " CPU"),
    field("Токенов в секунду", res.tokens_per_second));
  const rest = h("dl", {class:"worker-readings"},
    field("Память машины", mib(hw.host_ram_bytes)), field("Память модели", mib(res.model_rss_bytes)),
    field("Пиковая память модели", mib(res.model_peak_rss_bytes)),
    field("Лимит памяти", hw.cgroup_memory_limit_status === "unlimited" ? tr("Без ограничения") : mib(hw.effective_cgroup_memory_limit_bytes)),
    field("Лимит CPU", hw.effective_cgroup_cpu_quota_cores ?? (hw.cgroup_cpu_limit_status === "unlimited" ? tr("Без ограничения") : null)),
    field("Потоки модели", res.active_threads?.join(" / ") || worker.threads),
    field("Активные запросы", worker.evaluations), field("Ожидание запроса, с", res.wait_seconds));
  return h("div", {class:"hardware-readout"}, main.childElementCount ? main :
    h("p", {class:"hint"}, tr(worker.online ? "Служба пока не передала показатели нагрузки." : "Показатели появятся после подключения службы анализа.")),
    rest.childElementCount ? h("details", {}, h("summary", {}, tr("Все показатели и лимиты")), rest) : null);
}
function workerModelState(worker) {
  return JSON.stringify([!!worker.enabled, !!worker.online, worker.calibration?.state, worker.calibration?.model,
    worker.catalog?.map(model => model.id), Object.entries(worker.models || {}).map(([id, files]) => [id, !!files.downloaded, files.sha256])]);
}
async function loadWorkspaceModels() {
  const request = desk.modelRequest = (desk.modelRequest || 0) + 1;
  const box = $("#workspace-models");
  let cfg, worker;
  try {
    [cfg, worker] = await Promise.all([api("/api/v1/ui/workspace/model"), api("/api/v1/ui/worker").catch(() => ({online:false}))]);
  } catch (error) {
    if (location.hash === "#settings" && request === desk.modelRequest)
      box.replaceChildren(h("section", {class:"panel model-setup"}, h("p", {role:"alert"}, error.message),
        workButton("Повторить", loadWorkspaceModels)));
    return;
  }
  if (location.hash !== "#settings" || request !== desk.modelRequest) return;
  desk.workerModelState = workerModelState(worker);
  const models = worker.models || {},
    hw = worker.hardware || {};
  const budget = [
    hw.host_ram_bytes,
    hw.effective_cgroup_memory_limit_bytes,
    worker.resources?.available_bytes == null
      ? null
      : worker.resources.available_bytes + (worker.resources.model_rss_bytes || 0),
  ]
    .filter((v) => v > 0)
    .sort((a, b) => a - b)[0];
  const catalog = worker.catalog || Object.entries(models)
    .filter(([, files]) => files.downloaded)
    .map(([id, files]) => builtinModelChoices.find(model => model.id === id) ||
      {id, label: files.label || id, bytes: files.bytes});
  const fits = (m) => budget && m.bytes > 0 && m.bytes * 1.3 + 512 * 1024 ** 2 < budget * 0.8;
  const candidates = catalog.filter(
    (m) =>
      fits(m) &&
      reviewProfiles[studyArtifacts[models[m.id]?.sha256] || m.id] &&
      searchProfiles[studyArtifacts[models[m.id]?.sha256] || m.id],
  );
  candidates.sort(
    (a, b) =>
      Number(!!models[b.id]?.downloaded) - Number(!!models[a.id]?.downloaded) || a.bytes - b.bytes,
  );
  const activeName = source => source && source.provider !== "none" ? modelName(source.model) || source.connection : tr("Отключено");
  const pick = h(
    "select",
    { "aria-label": tr("Модель поиска и проверки") },
    h("option", { value: "" }, tr("Выберите локальную модель")),
    ...catalog.map((m) => h("option", { value: m.id }, m.label || m.id)),
  );
  const preferred = desk.modelPick || [cfg.categorization, cfg.analysis].find(source => source?.provider === "local")?.model || candidates[0]?.id;
  if (catalog.some((m) => m.id === preferred)) pick.value = preferred;
  const save = async (body) => {
    if (!await saveModelSettings("/api/v1/ui/workspace/model", body)) return;
    await loadWorkspaceModels();
    const status = $("#workspace-save-state");
    if (location.hash === "#settings" && status) status.textContent = tr("Настройки сохранены");
  };
  const usingCategorizationModel = () => cfg.categorization?.provider === "local" && cfg.categorization.model === pick.value;
  const usingPickedModel = () => usingCategorizationModel() && cfg.analysis?.provider === "local" && cfg.analysis.model === pick.value;
  const activeScenario = () => cfg.categorization?.scenario || "standard";
  const scenario = h("select", {id: "workspace-scenario"}, scenarioOptions("standard"));
  const scenarioField = h("label", {class: "field"}, tr("Сценарий проверки"), scenario);
  const studyBox = h("div", {id: "workspace-scenario-study", class: "scenario-study"});
  function resetScenario() {
    scenario.value = desk.scenarioDraft?.model === pick.value ? desk.scenarioDraft.scenario :
      usingCategorizationModel() ? activeScenario() : chosenLocalPreset("categorization", measuredModelId(pick.value, models))?.scenario || "standard";
  }
  resetScenario();
  const modelControls = h(
    "div",
    { class: "model-choice-fields" },
    h("label", {class: "field"}, tr("Локальная модель"), pick), scenarioField,
    workButton(
      "Использовать для поиска и проверки",
      async () => {
        if (usingCategorizationModel() && scenario.value !== activeScenario()) {
          await save({scenario: scenario.value});
          desk.scenarioDraft = null;
          return;
        }
        if (!models[pick.value]?.downloaded) {
          await prepareModel(pick.value, "settings");
          return;
        }
        const study = measuredModelId(pick.value, models);
        const search = chosenLocalPreset("analysis", study),
          review = chosenLocalPreset("categorization", study);
        await save({
          model: pick.value,
          analysis_inference: search?.inference,
          categorization_inference: review?.inference,
          analysis_timeout: search?.timeout,
          categorization_timeout_ms: review?.timeout,
          scenario: scenario.value,
          chunking: search
            ? {
                discovery_mode: "typed",
                strategy: "auto",
                target_input_tokens: search.target ?? 0,
                context_hint_tokens: 256,
                max_candidates: search.maxCandidates,
                max_fragments: search.maxFragments,
              }
            : undefined,
        });
        desk.scenarioDraft = null;
      },
      "primary",
      !info.editable || !catalog.length,
    ),
  );
  if (
    cfg.analysis?.provider === "api" &&
    (cfg.categorization?.model !== cfg.analysis.model ||
      cfg.categorization?.connection_id !== cfg.analysis.connection_id)
  ) {
    modelControls.append(
      workButton(
        "Использовать API-модель поиска для проверки",
        () => save({ use_analysis_api: true }),
        "secondary",
        !info.editable,
      ),
    );
  }
  const slot = h("input", {
    type: "number",
    min: 1,
    max: 10,
    required:true, value: desk.slotDraft ?? cfg.slots,
    "aria-label": tr("Параллельные запросы"),
  });
  const saveSlots = h("button", {type:"button", class:"secondary"}, tr("Сохранить"));
  const syncSlots = () => { saveSlots.disabled = !info.editable || !slot.validity.valid || Number(slot.value) === cfg.slots; };
  slot.oninput = () => { desk.slotDraft = slot.value; syncSlots(); };
  saveSlots.onclick = async () => {
    if (!slot.reportValidity()) return;
    saveSlots.disabled = true;
    const value = Number(slot.value);
    try {
      await save({slots:value});
      if (Number(desk.slotDraft) === value) desk.slotDraft = null;
    } catch (error) { fail(error); }
    finally { syncSlots(); }
  };
  syncSlots();
  const slots = h(
    "div",
    { class: "form-actions worker-slot-controls" },
    h("label", { class: "field" }, tr("Параллельные запросы"), slot),
    saveSlots,
  );
  if (cfg.previous_slots != null && cfg.previous_slots !== cfg.slots)
    slots.append(
      workButton(tr("Вернуть {{0}} слотов", [cfg.previous_slots]), () =>
        save({ slots: cfg.previous_slots }),
      ),
    );
  const cal = worker.calibration,
    calibration = h("div", { id: "cpu-calibration", class: "form-actions" });
  let applySlots;
  if (cal) {
    calibration.append(
      workButton(
        "Проверить производительность",
        async () => {
          await api("/api/v1/ui/worker/calibrate", { method: "POST", body: { model: pick.value } });
          await loadWorkspaceModels();
        },
        "secondary",
        !info.editable || !worker.enabled || cal.state === "running" || !catalog.length,
      ),
    );
    calibration.append(
      h(
        "span",
        { class: "hint" },
        tr(
          {
            running: "Идёт замер на синтетическом тексте",
            failed: "Замер завершился ошибкой",
            insufficient_resources: "Недостаточно ресурсов для замера",
            succeeded: "Замер завершён",
          }[cal.state] || "",
        ),
      ),
    );
    if (cal.model) calibration.append(h("strong", {}, modelName(cal.model)));
    for (const trial of cal.trials || [])
      calibration.append(
        h("small", {}, tr("Слоты {{0}} · {{1}} запросов/с", [trial.slots, trial.requests_per_second])),
      );
    if (cal.state === "succeeded" && cal.recommended_slots && cal.recommended_slots !== cfg.slots) {
      applySlots = workButton(tr("Применить {{0}} слотов", [cal.recommended_slots]), () => save({slots:cal.recommended_slots}), "secondary", !info.editable);
      calibration.append(applySlots);
    }
  }
  function updateChoice() {
    const button = modelControls.querySelector("button");
    const installed = !!models[pick.value]?.downloaded;
    button.disabled = !info.editable || !pick.value;
    scenarioField.hidden = !pick.value;
    scenario.disabled = !info.editable;
    button.hidden = !pick.value || (installed && usingPickedModel() && scenario.value === activeScenario());
    button.textContent = tr(usingCategorizationModel() && scenario.value !== activeScenario() ? "Сохранить сценарий" :
      !pick.value || installed ? "Использовать для поиска и проверки" : "Подготовить модель");
    studyBox.hidden = !pick.value;
    if (pick.value) renderScenarioStudy(studyBox, measuredModelId(pick.value, models), scenario.value, scenario);
    if (applySlots) applySlots.hidden = cal.model !== pick.value || !installed;
    const measure = calibration.querySelector("button");
    if (measure)
      measure.disabled = !info.editable || !worker.enabled || !installed || cal.state === "running";
  }
  pick.onchange = () => {
    desk.modelPick = pick.value;
    desk.scenarioDraft = null;
    resetScenario();
    updateChoice();
  };
  scenario.onchange = () => {
    desk.scenarioDraft = {model: pick.value, scenario: scenario.value};
    updateChoice();
  };
  updateChoice();
  const shared = cfg.analysis?.model && cfg.analysis.model === cfg.categorization?.model &&
    cfg.analysis.provider === cfg.categorization.provider && cfg.analysis.connection_id === cfg.categorization.connection_id;
  box.replaceChildren(
    h("section", {id: "workspace-model-choice", class:"panel model-setup", "aria-labelledby": "workspace-model-title"},
      h("h2", {id: "workspace-model-title"}, tr("Модель поиска и проверки")),
      h("p", {class:"model-assignment"}, shared ? tr("Для поиска и проверки используется {{0}}.", [activeName(cfg.analysis)]) :
        tr("Поиск · {{0}}", [activeName(cfg.analysis)]) + "  ·  " + tr("Категоризация · {{0}}", [activeName(cfg.categorization)])),
      modelControls, candidates.length ? h("p", {class:"hint"}, tr("Подходит по памяти и проверен на задачах поиска и категоризации · {{0}}", [candidates[0].label || candidates[0].id])) : null,
      !catalog.length ? h("p", {class:"hint"}, tr("Чтобы выбрать локальную модель, откройте службу моделей. Для внешней модели укажите API в настройках обнаружения.")) : null,
      studyBox, h("div", {class:"model-paths"},
        h("a", {href:"#analysis"}, tr("Источник модели и API")), h("a", {href:"#worker"}, tr("Открыть службу моделей"))),
      h("p", {id:"workspace-save-state", role:"status", class:"form-status"})),
    h("section", {class:"panel hardware-panel"}, h("h2", {}, tr("Производительность воркера")),
      viewHardware(worker), h("p", {id:"resource-error", role:"status", class:"form-status"}),
      h("h3", {}, tr("Параллельная обработка")), slots, calibration),
  );
}
const basicSettings = TABS.settings;
TABS.settings = async () => {
  basicSettings();
  await loadWorkspaceModels();
};

async function refreshWorkerReadings() {
  const readings = $("#workspace-models .hardware-readout");
  if (!readings || desk.readingsPending) return;
  desk.readingsPending = true;
  try {
    const worker = await api("/api/v1/ui/worker");
    if (location.hash !== "#settings" || !readings.isConnected) return;
    if (desk.workerModelState !== workerModelState(worker)) {
      await loadWorkspaceModels();
      return;
    }
    $("#resource-error").textContent = "";
    const updated = viewHardware(worker);
    const details = updated.querySelector("details");
    if (details) details.open = !!readings.querySelector("details[open]");
    const focus = readings.contains(document.activeElement);
    readings.replaceWith(updated);
    if (focus) updated.querySelector("summary")?.focus({preventScroll:true});
  } catch {
    if (location.hash === "#settings" && readings.isConnected)
      $("#resource-error").textContent = tr("Не удалось обновить показатели воркера. Повторяем подключение.");
  } finally { desk.readingsPending = false; }
}

$("#discover-collect").onclick = async () => {
  try {
    const receipt = await postOnce("/api/v1/ui/dataset/collect", { keys: D.report.finding_keys });
    $("#d-source-summary").replaceChildren(
      h("strong", {}, tr("Добавлено {{0}} · уже были {{1}}", [receipt.added, receipt.existing])),
      workButton("Открыть добавленные примеры", () => {
        desk.collection = receipt.id;
        desk.datasetStatus = "all";
        desk.datasetOffset = 0;
        showTab("dataset");
      }),
      receipt.added
        ? workButton("Отменить добавление", async () => {
            const r = await api("/api/v1/ui/dataset/collections/" + receipt.id + "/undo", {
              method: "POST",
              body: {},
            });
            $("#d-source-summary").textContent = tr("Отменено · {{0}}", [r.removed]);
          })
        : "",
    );
  } catch (e) {
    fail(e);
  }
};

async function prepareModel(model, origin) {
  showTab("worker");
  await loadWorkerSummary();
  if (model) {
    const select = $("#worker-model");
    if ([...select.options].some((option) => option.value === canonicalModel(model))) {
      select.value = canonicalModel(model);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
  const area = $("#tab-worker");
  area.querySelector(".return-to-work")?.remove();
  area.prepend(workButton("Вернуться к задаче", () => showTab(origin), "secondary return-to-work"));
}
