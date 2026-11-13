"use strict";
const ReviewUI = { data: null, view: "pending", selected: new Map(), drafts: new Map(), offset: 0, busy: false,
  request: 0, groups: false, jobs: new Map(), modelSource: "", selectionKey: "", keys: new URLSearchParams(location.search).get("review_keys") || "" };
const reviewReasons = {
  numeric_type_corrected: "Числовой формат исправлен на финансовое значение. Исходный тип модели сохранён в подробностях. Требуется подтверждение.",
  type_format_conflict: "Предложенный тип противоречит формату значения. Автоматическое применение запрещено.",
  type_rechecked: "Исходный тип противоречил формату значения и был перепроверен категоризатором.",
  protected_credential: "Защищённые учётные данные не передаются модели для пересмотра.",
  review_inconclusive: "Модели недостаточно данных. Выберите решение вручную или повторите пересмотр.",
  review_stale: "Рекомендация устарела. Пересмотрите запись заново.",
  interrupted_review: "Пересмотр прерван перезапуском. Его можно запустить заново.",
  review_busy: "Уже выполняются задания пересмотра. Дождитесь их завершения.",
  select_up_to_64: "Выберите от 1 до 64 записей.",
  entry_not_found: "Запись больше недоступна. Обновите очередь.",
  entry_not_pending: "По записи уже принято решение. Обновите очередь.",
  event_not_found: "Подробности события больше не хранятся.",
  job_not_found: "Задание пересмотра больше не хранится.",
  invalid_action: "Действие недоступно для этого состояния записи.",
  invalid_kind: "Выберите допустимый тип значения.",
  invalid_scope: "Выберите контур правила.",
};
function reviewError(code) { return tr(reviewReasons[code] || learningErrors[code] || code); }
function reviewState(item) {
  return tr(({ open: "На проверку", pending: "На проверку", masked: "Создано правило маскирования", kept: "Создано исключение",
    dismissed: "Закрыто без правила", rejected: "Находка отклонена", rolled_back: "Правило отменено", active: "Правило действует" })[item.state] || learningStates[item.state] || item.state);
}
const reviewPercent = (score) => score == null ? tr("Не сохранено") : `${(score * 100).toFixed(1)}%`;
function categoryStage(item) {
  if (item.categorization_state) return item.categorization_state;
  if (item.reason === "categorization_disabled") return "disabled";
  return item.classification_confidence != null ? "succeeded" : "unknown";
}
function stageText(item) {
  const stage = categoryStage(item);
  return tr(({ disabled: "Категоризация выключена — тип не проверен", not_run: "Категоризация ещё не выполнялась",
    running: "Категоризация выполняется", failed: "Категоризация завершилась ошибкой", unknown: "Состояние категоризации не сохранено старой версией" })[stage]
    || "Тип проверен категоризацией");
}
function detectorReason(item) {
  const sources = (item.evidence || []).map((e) => e.source);
  if (sources.includes("private-path-root")) return tr("Неизвестная часть пути в частном каталоге. Название проекта не подтверждено.");
  if (sources.includes("name:pair")) return tr("Слова похожи на сочетание имени и фамилии. Детектор не подтвердил, что это человек.");
  return tr("Тип предложен детектором по признакам текста. Приватность значения нужно подтвердить по контексту.");
}
function showReview(keys = [], view = "pending") {
  ReviewUI.keys = keys.join(","); ReviewUI.offset = 0; ReviewUI.view = view; ReviewUI.selected.clear();
  const url = new URL(location.href);
  if (keys.length) url.searchParams.set("review_keys", ReviewUI.keys); else url.searchParams.delete("review_keys");
  url.hash = "review"; history.replaceState(null, "", url); showTab("review");
}
async function queueEventReview(eventId,index,dialog) {
  try { const result=await api("/api/v1/ui/review/from-event",{method:"POST",body:{event_id:eventId,indexes:[index]}});dialog.close();showReview(result.keys); }
  catch(error){fail(new Error(reviewError(error.messageKey||error.message)));}
}
function openReview(view = "pending") { ReviewUI.view = view; ReviewUI.offset = 0; ReviewUI.selected.clear(); showTab("review"); }
function reviewDraft(item) {
  if (!ReviewUI.drafts.has(item.key)) ReviewUI.drafts.set(item.key, { kind: item.kind, scope: item.scope || item.recommended_scope || defaultScopeForKind(item.kind), mode: "auto", scopeChanged: false });
  return ReviewUI.drafts.get(item.key);
}
function selectReviewItem(item, checked) {
  if (checked && ReviewUI.selected.size >= 64) { toast(tr("Выберите от 1 до 64 записей."), true); return false; }
  if (checked) ReviewUI.selected.set(item.key, item); else ReviewUI.selected.delete(item.key);
  renderReviewSelection(); return checked;
}
function reviewCard(item) {
  const pending = ["open", "pending"].includes(item.state), rec = item.recommendation,
    valid = rec?.state === "succeeded", draft = reviewDraft(item), ro = !ReviewUI.data.editable || ReviewUI.busy;
  const article = h("article", { class: "panel review-card", "data-review-key": item.key, "data-learning-id": item.origin === "learning" ? item.id : null });
  const heading = h("div", { class: "review-card-heading" },
    pending ? h("input", { type: "checkbox", checked: ReviewUI.selected.has(item.key), disabled: ro,
      "aria-label": tr("Выбрать значение: {{0}}", [item.value]), onchange: (event) => { event.target.checked = selectReviewItem(item, event.target.checked); } }) : null,
    h("code", { class: "review-value" }, item.value), h("span", { class: "tag" }, tr(item.origin === "detector" ? "Быстрый детектор" : "LLM-находка")),
    h("span", { class: "status-pill" }, reviewState(item)));
  const type = h("p", { class: "review-type" }, tr(item.origin === "learning" && categoryStage(item) !== "succeeded" ? "Предложенный тип: {{0}}" : "Тип: {{0}}", [kindLabel(item.kind)]));
  const current = item.origin === "detector" ? h("p", { class: "review-current" },
    tr(item.applied === "mask" ? "В этом запросе значение замаскировано по политике." : "В этом запросе значение осталось в тексте."),
    h("span", { class: "hint" }, " " + tr("Оценка детектора: {{0}}", [reviewPercent(item.probability)])),
    helpButton("Оценка детектора — результат его признаков, а не измеренная вероятность правильности. Пересмотр моделью хранится отдельно."))
    : h("p", { class: "review-current" }, tr("Оценка обнаружения: {{0}}", [reviewPercent(item.extraction_confidence)]),
      h("span", { class: "hint" }, " · " + stageText(item)));
  const reason = item.origin === "detector" ? detectorReason(item) : item.validation_reason ? reviewError(item.validation_reason)
    : item.reason && item.reason !== "categorization_disabled" ? reviewError(item.reason) : "";
  const evidence = h("details", { class: "review-evidence" }, h("summary", {}, tr("Контекст и основания")),
    h("blockquote", {}, item.context || tr("Контекст не сохранён")),
    h("p", { class: "hint" }, item.origin === "detector" ? item.recognizer : item.source),
    h("p", { class: "hint" }, item.origin === "learning" ? tr("Модель обнаружения: {{0}}", [modelName(item.model) || tr("Не сохранено")])
      : (item.evidence || []).map((e) => `${e.source}: ${e.logit.toFixed(2)}`).join(" · ")),
    item.origin === "learning" ? h("p", { class: "hint" }, tr("Категоризация: {{0}} · модель: {{1}}", [
      categoryStage(item) === "succeeded" ? reviewPercent(item.classification_confidence) : stageText(item),
      modelName(item.classifier) || tr(categoryStage(item) === "disabled" ? "Не назначена" : "Не сохранено")])) : null,
    item.proposed_kind && item.proposed_kind !== item.kind ? h("p", { class: "hint" }, tr("Исходный тип модели: {{0}}", [kindLabel(item.proposed_kind)])) : null,
    item.classification_proposed_kind && item.classification_proposed_kind !== item.kind ? h("p", { class: "hint" }, tr("Тип категоризатора до проверки формата: {{0}}", [kindLabel(item.classification_proposed_kind)])) : null,
    item.validation_reason ? h("p", { class: "hint" }, reviewError(item.validation_reason)) : null);
  article.append(heading, type, current);
  if (reason) article.append(h("p", { class: "hint" }, reason));
  if (valid) article.append(h("div", { class: "review-recommendation" },
    h("strong", {}, tr(rec.action === "mask" ? "Модель предлагает маскировать" : "Модель предлагает не маскировать")),
    h("p", {}, `${kindLabel(rec.kind)} · ${scopeLabel(rec.scope)} · ${tr("Оценка пересмотра")}: ${reviewPercent(rec.confidence)}`),
    h("p", {}, rec.reason === "configured_categorizer" ? tr("Рекомендация настроенного категоризатора") : rec.reason),
    h("small", { class: "hint" }, modelName(rec.model))));
  else if (rec?.state === "needs_review") article.append(h("div", { class: "review-recommendation inconclusive" },
    h("strong", {}, tr("Модели недостаточно данных для решения")),
    h("p", {}, rec.reason),
    h("small", { class: "hint" }, tr("Проверьте контекст и выберите решение вручную или повторите пересмотр.")),
    h("p", { class: "hint" }, modelName(rec.model))));
  else if (rec) article.append(h("p", { class: "err" }, rec.state === "stale" ? reviewError("review_stale") : reviewError(rec.error)));
  article.append(evidence);
  const controls = h("div", { class: "review-card-actions" });
  if (pending) {
    const mode = h("select", { "aria-label": tr("Способ выбора типа и контура"), disabled: ro },
      h("option", { value: "auto", disabled: !valid }, valid ? tr("Авто по рекомендации") : tr("Авто: сначала пересмотрите моделью")),
      h("option", { value: "manual" }, tr("Вручную")));
    mode.value = valid && draft.mode === "auto" ? "auto" : "manual";
    const kind = makeKindSelect(mode.value === "auto" ? rec.kind : draft.kind, ro || mode.value === "auto"),
      scope = makeScopeSelect(mode.value === "auto" ? rec.scope : draft.scope, ro || mode.value === "auto");
    mode.addEventListener("change", () => { draft.mode = mode.value; renderReview(true); });
    kind.addEventListener("change", () => { draft.kind = kind.value; draft.mode = "manual"; if (!draft.scopeChanged) { draft.scope = defaultScopeForKind(kind.value); scope.value = draft.scope; } });
    scope.addEventListener("change", () => { draft.scope = scope.value; draft.scopeChanged = true; draft.mode = "manual"; });
    controls.append(mode, kind, scope,
      h("button", { class: "primary", disabled: ro, onclick: () => confirmReview([item], mode.value === "auto" ? "recommendation" : "mask") },
        tr(mode.value === "auto" ? "Применить рекомендацию" : "Создать правило маскирования")),
      h("button", { class: "secondary", disabled: ro || item.protected, onclick: () => requestReview([item]) }, tr("Пересмотреть моделью")));
    if (item.origin === "detector") controls.append(
      h("button", { class: "ghost", disabled: ro, onclick: () => confirmReview([item], "keep") }, tr("Создать исключение")),
      h("button", { class: "ghost", disabled: ro, onclick: () => confirmReview([item], "dismiss") }, tr("Закрыть без правила")));
    else controls.append(h("button", { class: "ghost", disabled: ro, onclick: () => confirmReview([item], "reject") }, tr("Отклонить находку")));
    if (item.protected) article.append(h("p", { class: "hint" }, reviewError("protected_credential")));
  } else {
    if (item.state === "dismissed") controls.append(h("button", { class: "secondary", disabled: ro, onclick: () => applyReview([{ item, action: "reopen", kind: item.kind, scope: item.recommended_scope }]) }, tr("Вернуть на проверку")));
    if (item.state === "active" || item.decision?.rule_id && ["masked", "kept"].includes(item.state)) controls.append(
      h("button", { class: "secondary", disabled: ro, onclick: () => confirmReview([item], "rollback") }, tr("Отменить правило")),
      h("button", { class: "ghost", onclick: () => { showTab("rules"); } }, tr("Открыть правила")));
  }
  article.append(controls); return article;
}
function scopeLabel(scope) { return tr({ personal: "Личные", team: "Команда", global: "Корпоративные" }[scope] || scope); }
function inferenceDetails(job) {
  if (!job.inference?.length) return null;
  return h("details",{class:"inference-details"},h("summary",{},tr("Диагностика модели"),` · ${job.inference.length}`),
    h("p",{class:"hint"},tr("Расход токенов и причины завершения обращений. Обрывы, после которых фрагмент проверен частями, остаются в журнале.")),
    h("div",{class:"table-wrap"},h("table",{},h("thead",{},h("tr",{},...["Обращение","Вход","Ответ","Бюджет контекста","Завершение"].map(key=>h("th",{},tr(key))))),
      h("tbody",{},...job.inference.map((trace,index)=>h("tr",{},h("td",{},String(index+1)),h("td",{},trace.prompt_tokens??"—"),
        h("td",{},`${trace.completion_tokens??"—"} / ${trace.max_output_tokens??"—"}`),h("td",{},trace.context_tokens??"—"),
        h("td",{},trace.finish_reason||"—",trace.finish_reason==="length"?" · "+tr(trace.token_count_method==="runtime_tokenizer"&&(trace.total_tokens??0)>=(trace.context_tokens??Infinity)?"Исчерпан контекст":(trace.completion_tokens??0)>=(trace.max_output_tokens??Infinity)?"Достигнут лимит ответа":"Провайдер остановил ответ по лимиту"):"",trace.http_status>=400?` · HTTP ${trace.http_status}`:"",trace.runtime_exit_code!=null?` · exit ${trace.runtime_exit_code}`:"",trace.error?" · "+learningError(trace.error):"",trace.structured_output?h("div",{class:"hint"},tr(trace.structured_output==="json_prompt"?"JSON по инструкции":"Ограничение JSON Schema")):null)))))));
}
function renderReview(force = false) {
  const data = ReviewUI.data;
  if (!data) return;
  for (const button of document.querySelectorAll("[data-review-view]")) button.setAttribute("aria-pressed", String(button.dataset.reviewView === ReviewUI.view));
  // Polling must not replace controls while someone edits or reads evidence.
  if (!force && (document.activeElement?.closest(".review-card") || $("#review-list details[open]"))) { renderReviewSelection(); return; }
  const nodes = [];
  if (ReviewUI.keys) nodes.push(h("div", { class: "inline-notice" }, tr("Показаны решения выбранного события."),
    h("button", { class: "text-button", onclick: () => showReview() }, tr("Показать все"))));
  if (!data.items.length && (ReviewUI.view === "pending" || !ReviewUI.learning?.jobs?.length)) nodes.push(h("div", { class: "panel empty-state" }, h("strong", {}, tr(ReviewUI.view === "pending" ? "Нет решений для проверки" : "История пока пуста"))));
  if (ReviewUI.groups) {
    const groups = new Map();
    for (const item of data.items) { const key = item.kind + " · " + (item.shape || item.origin); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(item); }
    for (const [key, members] of groups) nodes.push(h("details", { class: "review-group", open: true },
      h("summary", {}, `${kindLabel(members[0].kind)} · ${members.length}`, " · ", key), ...members.map(reviewCard)));
  } else nodes.push(...data.items.map(reviewCard));
  if (ReviewUI.view === "history" && ReviewUI.learning?.jobs?.length) nodes.push(h("section",{class:"panel review-card"},h("h2",{},tr("Задания анализа")),
    ...ReviewUI.learning.jobs.map((job)=>h("article",{class:"learning-job"},h("div",{},h("strong",{},job.name),h("p",{class:"hint"},tr(learningStates[job.state]||job.state),job.model?" · "+job.model:"",job.error?" · "+reviewError(job.error):""),inferenceDetails(job)),
      job.retryable?h("button",{class:"secondary",disabled:ReviewUI.busy||!data.editable,onclick:async()=>{await learningAction(job.id,"retry");await loadReview(true);}},tr("Повторить")):null,
      job.state === "succeeded" && ReviewUI.learning.entries.some((entry)=>entry.job_id===job.id&&entry.state==="active")?h("button",{class:"ghost",disabled:ReviewUI.busy||!data.editable,onclick:async()=>{await learningAction(job.id,"rollback-job");await loadReview(true);}},tr("Отменить изменения задания")):null))));
  $("#review-list").replaceChildren(...nodes);
  $("#review-pagination").replaceChildren(h("button", { class: "secondary", disabled: !ReviewUI.offset, onclick: () => { ReviewUI.offset = Math.max(0, ReviewUI.offset - data.limit); loadReview(true); } }, tr("Назад")),
    h("span", { class: "hint" }, tr("{{0}}–{{1}} из {{2}}", [data.total ? ReviewUI.offset + 1 : 0, Math.min(ReviewUI.offset + data.items.length, data.total), data.total])),
    h("button", { class: "secondary", disabled: ReviewUI.offset + data.limit >= data.total, onclick: () => { ReviewUI.offset += data.limit; loadReview(true); } }, tr("Далее")));
  $("#review-select-page").checked = data.items.some((i) => ["open", "pending"].includes(i.state)) && data.items.filter((i) => ["open", "pending"].includes(i.state)).every((i) => ReviewUI.selected.has(i.key));
  renderReviewSelection();
}
function renderReviewSelection() {
  const panel = $("#review-selection"), selected = [...ReviewUI.selected.values()]; panel.hidden = !selected.length;
  if (!selected.length) { ReviewUI.selectionKey = ""; return; }
  const signature = JSON.stringify([selected.map((i) => [i.key,i.recommendation?.state]),ReviewUI.busy,ReviewUI.data?.editable,I18N.language]);
  if (ReviewUI.selectionKey === signature) return; ReviewUI.selectionKey = signature;
  panel.replaceChildren(h("strong", {}, tr("Выбрано: {{0}}", [selected.length])),
    h("label", { class: "field" }, h("span", {}, tr("Модель пересмотра")), h("select", { id: "review-model-source", disabled: ReviewUI.busy,
      onchange: (event) => {ReviewUI.modelSource=event.target.value;} },
      ...(ReviewUI.data?.sources || []).filter((s) => s.provider !== "none").map((s) => h("option", { value: s.role, selected: s.role === ReviewUI.modelSource }, `${tr(s.role === "categorization" ? "Категоризация" : "Обнаружение")} · ${modelName(s.model)}`)))),
    h("button", { class: "secondary", disabled: ReviewUI.busy || selected.some((i) => i.protected) || !ReviewUI.data.sources.some((s) => s.provider !== "none"), onclick: () => requestReview(selected) }, tr("Пересмотреть моделью")),
    h("button", { class: "primary", disabled: ReviewUI.busy || selected.some((i) => i.recommendation?.state !== "succeeded"), onclick: () => confirmReview(selected, "recommendation") }, tr("Применить рекомендации")),
    h("button", { class: "secondary", disabled: ReviewUI.busy, onclick: () => confirmReview(selected, "mask") }, tr("Создать правила")),
    selected.every((i) => i.origin === "detector") ? h("button", { class: "ghost", disabled: ReviewUI.busy, onclick: () => confirmReview(selected, "dismiss") }, tr("Закрыть без правила")) : null,
    selected.every((i) => i.origin === "learning") ? h("button", { class: "ghost", disabled: ReviewUI.busy, onclick: () => confirmReview(selected, "reject") }, tr("Отклонить находки")) : null,
    h("button", { class: "text-button", onclick: () => { ReviewUI.selected.clear(); renderReview(true); } }, tr("Снять выбор")));
}
function confirmReview(items, operation) {
  const dialog = $("#review-confirm"), entries = items.map((item) => {
    const draft = reviewDraft(item), rec = item.recommendation;
    return { item, action: operation === "recommendation" ? rec.action : operation,
      use_recommendation: operation === "recommendation", kind: operation === "recommendation" ? rec.kind : draft.kind,
      scope: operation === "recommendation" ? rec.scope : draft.scope };
  });
  const origin = document.activeElement;
  const effect = (entry) => tr(entry.action === "dismiss" ? "Закрывает запись, но не меняет маскирование и не создаёт правила. Повторные появления остаются в закрытой записи."
    : entry.action === "reject" || entry.item.origin === "learning" && entry.action === "keep" ? "Не создаёт KEEP-правило. Запоминает отклонение находки для фонового обучения."
      : entry.action === "keep" ? "Создаёт точное KEEP-исключение для будущих запросов. Обязательные защитные детекторы сохраняют приоритет."
        : entry.action === "rollback" ? "Отключает созданное здесь правило. Токены старых ответов сохраняются."
          : "Создаёт точное MASK-правило для будущих запросов. Исходное решение события не изменяется.");
  dialog.replaceChildren(h("div", { class: "review-confirm-body" }, h("h2", {}, tr("Проверить изменения")),
    h("p", { class: "hint" }, tr("Записей: {{0}}", [entries.length])),
    h("div", { class: "review-confirm-list" }, ...entries.map((entry) => h("div", {}, h("code", {}, entry.item.value),
      h("p", {}, `${kindLabel(entry.kind)} · ${scopeLabel(entry.scope)}`), h("p", { class: "hint" }, effect(entry))))),
    h("div", { class: "form-actions" }, h("button", { class: "primary", id: "review-confirm-apply", onclick: () => { dialog.close(); origin?.focus(); applyReview(entries); } }, tr("Применить изменения")),
      h("button", { class: "secondary", onclick: () => { dialog.close(); origin?.focus(); } }, tr("Отмена")))));
  dialog.showModal();
}
async function applyReview(entries) {
  if (ReviewUI.busy) return; ReviewUI.busy = true;
  const failures = []; let applied = 0; renderReview(true);
  for (const entry of entries) {
    const item = entry.item;
    try {
      await api(`/api/v1/ui/review/${item.origin}/${encodeURIComponent(item.id)}/${entry.use_recommendation ? "apply" : entry.action}`, {
        method: "POST", body: { kind: entry.kind, scope: entry.scope, use_recommendation: !!entry.use_recommendation } });
      applied++; ReviewUI.selected.delete(item.key); ReviewUI.drafts.delete(item.key);
    } catch (error) { failures.push(`${item.value}: ${reviewError(error.messageKey || error.message)}`); }
  }
  ReviewUI.busy = false;
  $("#review-result").textContent = tr("Применено: {{0}}", [applied]) + (failures.length ? " · " + failures.join("; ") : "");
  $("#review-result").classList.toggle("err", !!failures.length);
  await Promise.all([loadReview(true), loadInfo()]);
}
async function requestReview(items) {
  if (ReviewUI.busy) return;
  const sources = (ReviewUI.data?.sources || []).filter((s) => s.provider !== "none");
  if (!sources.length) { showTab("analysis"); toast(tr("Выберите и сохраните источник модели"), true); return; }
  const chosen = $("#review-model-source")?.value || sources[0].role, source = sources.find((s) => s.role === chosen) || sources[0], dialog = $("#review-confirm");
  const unavailable = source.provider === "local" && (localModelIssue(source.model) || (!WORKER.data?.enabled && "Анализ ожидает запуска обработки."));
  if (unavailable) {
    dialog.replaceChildren(h("div",{class:"review-confirm-body"},h("h2",{},tr("Подготовить модель пересмотра")),h("p",{},source.model),h("p",{class:"hint"},tr(unavailable)),
      h("div",{class:"form-actions"},!WORKER.data?.enabled&&WORKER.data?.can_manage?h("button",{class:"primary",onclick:async()=>{try{await workerAction("/start",{});dialog.close();requestReview(items);}catch(error){fail(error);}}},tr("Запустить обработку")):null,
        h("button",{class:"secondary",onclick:()=>{dialog.close();showTab(source.role === "analysis" ? "analysis" : "categorization");}},tr("Настроить модель")),h("button",{class:"ghost",onclick:()=>dialog.close()},tr("Отмена")))));
    dialog.showModal();return;
  }
  dialog.replaceChildren(h("div", { class: "review-confirm-body" }, h("h2", {}, tr("Пересмотреть моделью")),
    h("p", {}, `${modelName(source.model)} · ${source.provider === "local" ? tr("Локальная модель") : tr("Модель по API")}`),
    h("p", { class: "hint" }, tr("Модель получит выбранные значения и короткий контекст. Пересмотр не создаёт правил. Другие обнаруженные секреты в контексте скрыты.")),
    h("p", {}, tr("Записей: {{0}}", [items.length])),
    h("div", { class: "form-actions" }, h("button", { class: "primary", id: "review-confirm-run", onclick: async () => {
      dialog.close(); ReviewUI.busy = true; renderReview(true);
      try {
        const job = await api("/api/v1/ui/review/jobs", { method: "POST", body: { keys: items.map((i) => i.key), model_source: source.role } });
        ReviewUI.jobs.set(job.id, job); $("#review-result").textContent = tr("Пересмотр поставлен в очередь. Правила не изменены.");
      } catch (error) { $("#review-result").textContent = reviewError(error.messageKey || error.message); $("#review-result").classList.add("err"); }
      finally { ReviewUI.busy = false; await loadReview(true); }
    } }, tr("Начать пересмотр")), h("button", { class: "secondary", onclick: () => dialog.close() }, tr("Отмена")))));
  dialog.showModal();
}
async function loadReview(force = false) {
  const request = ++ReviewUI.request, params = new URLSearchParams({ status: ReviewUI.view, source: $("#review-source").value,
    sort: $("#review-sort").value, q: $("#review-query").value, kind: $("#review-kind").value, action: $("#review-action").value, offset: ReviewUI.offset, limit: 50 });
  if (ReviewUI.keys) params.set("keys", ReviewUI.keys);
  try {
    const [data,learning] = await Promise.all([api("/api/v1/ui/review?" + params),ReviewUI.view==="history"?api("/api/v1/ui/learning"):Promise.resolve(null)]);
    if (request !== ReviewUI.request) return; ReviewUI.data = data;
    $("#review-badge").textContent = data.pending || ""; $("#review-badge").hidden = !data.pending;
    if (learning) ReviewUI.learning=learning;
    for (const item of data.items) if (ReviewUI.selected.has(item.key)) ReviewUI.selected.set(item.key, item);
    for (const job of data.jobs) ReviewUI.jobs.set(job.id, job);
    for (const [id] of ReviewUI.jobs) {
      const job = await api("/api/v1/ui/review/jobs/" + id);
      if (["failed", "succeeded"].includes(job.state)) {
        ReviewUI.jobs.delete(id); $("#review-result").textContent = job.error ? reviewError(job.error) : tr("Пересмотр завершён. Проверьте рекомендации перед применением.");
        $("#review-result").classList.toggle("err", !!job.error);
      } else { $("#review-result").textContent = tr("Пересмотр: {{0}} из {{1}}", [job.completed, job.targets.length]); }
    }
    renderReview(force);
  } catch (error) { $("#review-result").textContent = reviewError(error.messageKey || error.message); $("#review-result").classList.add("err"); }
}
$("#review-kind").replaceChildren(h("option", { value: "", "data-i18n": "Все типы" }, tr("Все типы")), ...STANDARD_KINDS.map((kind) => h("option", { value: kind }, kindLabel(kind))));
for (const button of document.querySelectorAll("[data-review-view]")) button.addEventListener("click", () => {
  ReviewUI.view = button.dataset.reviewView; ReviewUI.offset = 0; ReviewUI.selected.clear(); loadReview(true);
});
for (const id of ["review-source", "review-sort", "review-kind", "review-action"]) $("#" + id).addEventListener("change", () => { ReviewUI.offset = 0; ReviewUI.selected.clear(); loadReview(true); });
$("#review-query").addEventListener("input", debounce(() => { ReviewUI.offset = 0; ReviewUI.selected.clear(); loadReview(true); }, 250));
$("#review-refresh").addEventListener("click", () => loadReview(true));
$("#review-select-page").addEventListener("change", (event) => {
  for (const item of ReviewUI.data?.items || []) if (["open", "pending"].includes(item.state)) selectReviewItem(item, event.target.checked);
  renderReview(true);
});
$("#review-groups").addEventListener("click", (event) => { ReviewUI.groups = !ReviewUI.groups; event.currentTarget.setAttribute("aria-pressed", String(ReviewUI.groups)); renderReview(true); });
Object.assign(TABS, { review: async () => { await Promise.all([loadLearningSettings(), loadWorkerSummary(), loadReview(true)]); } });
setInterval(() => { if (!document.hidden && location.hash === "#review" && !ReviewUI.busy && !$("#review-confirm").open) loadReview(); }, 2500);
