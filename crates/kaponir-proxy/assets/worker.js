"use strict";
const WORKER = { data: null, busy: false, timer: null, transition: null };
const workerStates = {
  queued: "В очереди",
  running: "Выполняется",
  paused: "Приостановлено",
  succeeded: "Готово",
  failed: "Ошибка",
  cancelled: "Отменено",
};
function workerStateLabel(data) {
  return tr(
    WORKER.transition ||
      {
        disabled: "Выключен",
        ready: "Готов",
        stopping: "Останавливается…",
        error: "Ошибка",
      }[data?.state] ||
      "Выключен",
  );
}
function renderWorkerModel() {
  const data = WORKER.data;
  if (!data?.configured) return;
  const model = data.models?.[$("#worker-model").value] || {};
  $("#worker-model-status").textContent =
    tr(model.downloaded ? "Веса загружены" : "Веса ещё не загружены") +
    " · " +
    tr(data.runtime_ready ? "Runtime готов" : "Runtime ещё не установлен");
}
function renderWorkerSummary() {
  const data = WORKER.data || {};
  $("#analysis-worker-state").textContent = workerStateLabel(data);
  const downloaded = Object.values(data.models || {}).some((m) => m.downloaded);
  $("#analysis-worker-summary").replaceChildren(
    ...[
      ["Модель", tr(downloaded ? "Веса загружены" : "Веса ещё не загружены")],
      [
        "Очередь",
        number(
          (data.jobs || []).filter((j) =>
            ["running", "queued"].includes(j.state),
          ).length,
        ),
      ],
    ].map(([k, v]) =>
      h(
        "div",
        { class: "worker-summary-row" },
        h("span", {}, tr(k)),
        h("strong", {}, v),
      ),
    ),
  );
  for (const id of ["worker-toggle", "analysis-worker-toggle"]) {
    const b = $("#" + id);
    b.textContent = WORKER.transition
      ? tr(WORKER.transition)
      : tr(data.enabled ? "Выключить воркер" : "Включить воркер");
    b.disabled =
      WORKER.busy ||
      data.state === "stopping" ||
      !data.can_manage ||
      !info.editable;
    b.title = !data.can_manage
      ? tr("Этот воркер не поддерживает управление из панели")
      : "";
  }
}
function renderWorker() {
  const data = WORKER.data || {};
  renderWorkerSummary();
  $("#worker-controls").hidden =
    !data.configured || data.online === false || !data.enabled;
  const stateNodes = [
    h("p", {}, h("strong", {}, workerStateLabel(data))),
    data.state === "error" && data.error
      ? h("p", { class: "err" }, tr(data.error))
      : null,
    !data.configured
      ? h(
          "p",
          { class: "hint" },
          tr(
            "Включите воркер для фонового анализа. Модель загружается отдельно.",
          ),
        )
      : null,
    data.configured && data.online !== false && !data.image_support
      ? h(
          "p",
          { class: "hint" },
          tr("Для изображений нужны Pillow и Tesseract, для PDF — pypdfium2."),
        )
      : null,
  ].filter(Boolean);
  $("#worker-state").replaceChildren(...stateNodes);
  $("#worker-root").textContent = tr("Каталог на машине worker: {{0}}", [
    data.input_root || "—",
  ]);
  renderWorkerModel();
  for (const id of ["worker-download", "worker-submit", "worker-scan-sessions"])
    $("#" + id).disabled = WORKER.busy || !data.enabled || !info.editable;
  const rows = (data.jobs || []).map((job) =>
    h(
      "tr",
      {},
      h("td", { class: "mono" }, job.id.slice(0, 8)),
      h(
        "td",
        {},
        job.kind === "download"
          ? tr("Загрузка модели") + " " + job.model
          : job.source,
        h(
          "div",
          { class: "hint" },
          job.kind === "scan"
            ? tr(
                ["qwen", "xiaomi"].includes(job.mode)
                  ? "Семантический поиск Qwen3 (CPU) + локальные детекторы"
                  : "Локальный поиск · быстрые детекторы",
              )
            : "",
        ),
      ),
      h(
        "td",
        {},
        tr(workerStates[job.state] || job.state),
        job.control ? h("small", {}, " · " + tr("Остановка запрошена")) : null,
      ),
      h(
        "td",
        {},
        job.kind === "download"
          ? job.state === "succeeded"
            ? tr("Проверено и готово")
            : Math.round(job.progress / 1048576) +
              " / " +
              Math.round(job.progress_total / 1048576) +
              " MiB"
          : job.completed + " / " + job.total,
        h("div", { class: "hint" }, job.current || ""),
        job.candidate_page
          ? h(
              "div",
              { class: "hint" },
              tr("Фрагмент текста: {{0}}", [job.candidate_page]),
            )
          : null,
        ...(job.errors || []).map((e) =>
          h(
            "div",
            { class: "hint" },
            (e.file || "") + " " + e.type + ": " + e.message,
          ),
        ),
        ...(job.warnings || []).map((w) =>
          h(
            "div",
            { class: "hint" },
            w.code === "ungrounded_candidates"
              ? tr("Не подтверждены текстом: {{0}}", [w.count])
              : "S1: " + w.s1_status,
          ),
        ),
      ),
      h(
        "td",
        {},
        ...(["queued", "running", "paused"].includes(job.state)
          ? [
              h(
                "button",
                {
                  type: "button",
                  class: "secondary",
                  disabled: WORKER.busy,
                  onclick: () =>
                    workerAction(
                      "/jobs/" +
                        job.id +
                        "/" +
                        (job.state === "paused" ? "resume" : "pause"),
                      {},
                    ),
                },
                tr(job.state === "paused" ? "Продолжить" : "Пауза"),
              ),
              h(
                "button",
                {
                  type: "button",
                  class: "secondary",
                  disabled: WORKER.busy,
                  onclick: () =>
                    workerAction("/jobs/" + job.id + "/cancel", {}),
                },
                tr("Отменить задание"),
              ),
            ]
          : []),
      ),
    ),
  );
  $("#worker-jobs").replaceChildren(
    rows.length
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
                ...["ID", "Источник", "Состояние", "Прогресс", "Действия"].map(
                  (s) => h("th", {}, tr(s)),
                ),
              ),
            ),
            h("tbody", {}, ...rows),
          ),
        )
      : h("p", {}, tr("Очередь пуста")),
  );
}
async function loadWorkerSummary() {
  try {
    WORKER.data = await api("/api/v1/ui/worker");
    renderWorkerSummary();
  } catch (e) {
    WORKER.data = {
      state: "error",
      online: false,
      can_manage: false,
      error: e.message,
    };
    renderWorkerSummary();
  }
}
async function loadWorker() {
  clearTimeout(WORKER.timer);
  try {
    WORKER.data = await api("/api/v1/ui/worker");
    renderWorker();
  } catch (e) {
    WORKER.data = {
      ...WORKER.data,
      state: "error",
      online: false,
      error: e.message,
    };
    renderWorker();
  }
  if (location.hash === "#worker")
    WORKER.timer = setTimeout(() => {
      if (location.hash === "#worker" && !document.hidden) loadWorker();
    }, 2500);
}
async function workerAction(path, body) {
  if (WORKER.busy) return;
  WORKER.busy = true;
  if (path === "/start" || path === "/stop")
    WORKER.transition = path === "/start" ? "Запускается…" : "Останавливается…";
  renderWorker();
  try {
    await api("/api/v1/ui/worker" + path, { method: "POST", body });
    await loadWorker();
  } catch (e) {
    WORKER.data = { ...WORKER.data, state: "error", error: e.message };
    fail(e);
  } finally {
    WORKER.busy = false;
    WORKER.transition = null;
    renderWorker();
  }
}
for (const id of ["worker-toggle", "analysis-worker-toggle"])
  $("#" + id).addEventListener("click", () =>
    workerAction(WORKER.data?.enabled ? "/stop" : "/start", {}),
  );
$("#worker-refresh").addEventListener("click", loadWorker);
$("#worker-model").addEventListener("change", renderWorkerModel);
$("#worker-download").addEventListener("click", () =>
  workerAction("/models/download", { model: $("#worker-model").value }),
);
$("#worker-submit").addEventListener("click", () =>
  workerAction("/jobs", {
    source: $("#worker-source").value.trim() || ".",
    model: $("#worker-model").value,
    use_s1: $("#worker-s1").checked,
    mode: $("#worker-mode").value,
  }),
);
$("#worker-scan-sessions").addEventListener("click", () => {
  if (WORKER.data?.enabled)
    workerAction("/jobs", {
      source: "@sessions",
      model: $("#worker-model").value,
      use_s1: $("#worker-s1").checked,
      mode: $("#worker-mode").value,
    });
});
Object.assign(TABS, { worker: loadWorker });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && location.hash === "#worker") loadWorker();
});

setInterval(() => {
  if (!document.hidden && location.hash === "#analysis" && !WORKER.busy)
    loadWorkerSummary();
}, 5000);
