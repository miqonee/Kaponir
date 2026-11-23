"use strict";
const ReviewUI = { data: null, view: "pending", selected: new Map(), drafts: new Map(), offset: 0, busy: false,
  request: 0, groups: false, jobs: new Map(), modelSource: "", selectionKey: "", keys: new URLSearchParams(location.search).get("review_keys") || "" };
const reviewReasons = {
  numeric_type_corrected: "Числовой формат исправлен на финансовое значение. Исходный тип модели сохранён в подробностях. Требуется подтверждение.",
  type_format_conflict: "Предложенный тип противоречит формату значения. Автоматическое применение запрещено.",
  type_rechecked: "Исходный тип противоречил формату значения и был перепроверен категоризатором.",
  protected_credential: "Защищённые учётные данные не передаются модели для пересмотра.",
  review_inconclusive: "Модели недостаточно данных. Выберите решение вручную или повторите пересмотр.",
  review_partial_failure: "Часть ответов модели некорректна. Остальные рекомендации сохранены; записи с ошибками можно пересмотреть отдельно.",
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
    dismissed: "Закрыто без правила", rejected: "Находка отклонена", rolled_back: "Правило отменено", active: "Правило действует", covered: "Правило действует" })[item.state] || learningStates[item.state] || item.state);
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
    running: "Категоризация выполняется", verdict_only: "Тип остаётся на ручной проверке", failed: "Категоризация завершилась ошибкой", unknown: "Состояние категоризации не сохранено старой версией" })[stage]
    || "Тип проверен категоризацией");
}
function detectorReason(item) {
  const sources = (item.evidence || []).map((e) => e.source);
  if (sources.includes("private-path-root")) return tr("Неизвестная часть пути в частном каталоге. Название проекта не подтверждено.");
  if (sources.includes("name:pair")) return tr("Слова похожи на сочетание имени и фамилии. Детектор не подтвердил, что это человек.");
  return tr("Тип предложен детектором по признакам текста. Приватность значения нужно подтвердить по контексту.");
}
function showReview(keys = [], view = "pending", source = "all") {
  clearPicking(); ReviewUI.filterSet=null; ReviewUI.browseSet=null; ReviewUI.jobId=null; ReviewUI.learningJob=null; ReviewUI.detailKey=null;
  $("#review-result").replaceChildren();
  for(const [id,value] of [["review-source",source],["review-query",""],["review-action",""],["review-kind",""]]) $("#"+id).value=value;
  ReviewUI.keys = keys.join(","); ReviewUI.offset = 0; ReviewUI.view = view; ReviewUI.selected.clear();
  const url = new URL(location.href);
  if (keys.length) url.searchParams.set("review_keys", ReviewUI.keys); else url.searchParams.delete("review_keys");
  url.hash = "review";
  if(url.href!==location.href)history.pushState(null, "", url);
  showTab("review");
}
async function queueEventReview(eventId,index,dialog) {
  try { const result=await api("/api/v1/ui/review/from-event",{method:"POST",body:{event_id:eventId,indexes:[index]}});dialog.close();showReview(result.keys); }
  catch(error){fail(new Error(reviewError(error.messageKey||error.message)));}
}
function reviewDraft(item) {
  if (!ReviewUI.drafts.has(item.key)) ReviewUI.drafts.set(item.key, { kind: item.kind, scope: item.scope || item.recommended_scope || defaultScopeForKind(item.kind), scopeChanged: false });
  return ReviewUI.drafts.get(item.key);
}
function selectReviewItem(item, checked) {

  if (ReviewUI.snapshot) {
    if (checked) ReviewUI.excluded.delete(item.key); else ReviewUI.excluded.add(item.key);
  } else if (checked) ReviewUI.selected.set(item.key, item); else ReviewUI.selected.delete(item.key);
  if(ReviewUI.snapshot && pickedCount()===0)clearPicking();
  renderReviewSelection(); return checked;
}
function reviewCard(item) {
  const pending = pendingFinding(item), rec = item.recommendation,
    valid = rec?.state === "succeeded", draft = reviewDraft(item), ro = !ReviewUI.data.editable || ReviewUI.busy;
  const article = h("article", { class: "panel review-card", "data-review-key": item.key, "data-learning-id": item.origin === "learning" ? item.id : null });
  const heading = h("div", { class: "review-card-heading" },
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
  const excerpt = h("figure", { class: "finding-fragment" }, h("figcaption", {}, tr("Фрагмент")),
    h("blockquote", { class: "candidate-fragment" }, candidateExcerpt(item.context, item.value, item.context_span)));
  const evidence = h("details", { class: "review-evidence" }, h("summary", {}, tr("Почему значение попало в находки")),
    item.origin === "detector" ? h("p", {}, reason) : null,
    h("p", { class: "hint" }, item.origin === "detector" ? item.recognizer : item.source),
    h("p", { class: "hint" }, item.origin === "learning" ? tr("Модель обнаружения: {{0}}", [modelName(item.model) || tr("Не сохранено")])
      : (item.evidence || []).map((e) => `${e.source}: ${e.logit.toFixed(2)}`).join(" · ")),
    item.origin === "learning" ? h("p", { class: "hint" }, tr("Категоризация: {{0}} · модель: {{1}}", [
      categoryStage(item) === "succeeded" ? reviewPercent(item.classification_confidence) : stageText(item),
      modelName(item.classifier) || tr(categoryStage(item) === "disabled" ? "Не назначена" : "Не сохранено")])) : null,
    item.proposed_kind && item.proposed_kind !== item.kind ? h("p", { class: "hint" }, tr("Исходный тип модели: {{0}}", [kindLabel(item.proposed_kind)])) : null,
    item.classification_proposed_kind && item.classification_proposed_kind !== item.kind ? h("p", { class: "hint" }, tr("Тип категоризатора до проверки формата: {{0}}", [kindLabel(item.classification_proposed_kind)])) : null,
    item.validation_reason ? h("p", { class: "hint" }, reviewError(item.validation_reason)) : null);
  article.append(heading, excerpt, type, current);
  if (reason && item.origin !== "detector") article.append(h("p", { class: "hint" }, reason));
  if (item.scenario_result) article.append(h("p", {}, learningErrors[item.scenario_result.reason] ? learningError(item.scenario_result.reason) : item.scenario_result.reason));
  if (valid) article.append(h("div", { class: "review-recommendation" },
    h("strong", {}, tr(rec.action === "mask" ? "Модель предлагает маскировать" : "Модель предлагает не маскировать")),
    rec.verdict_only ? h("p", {class:"hint"}, tr("Тип и область из находки · {{0}} · {{1}}",[kindLabel(rec.kind),scopeLabel(rec.scope)])) : h("p", {}, `${kindLabel(rec.kind)} · ${scopeLabel(rec.scope)} · ${tr("Оценка пересмотра")}: ${reviewPercent(rec.confidence)}`),
    h("p", {}, rec.reason === "configured_categorizer" ? tr("Рекомендация настроенного категоризатора") : rec.reason),
    h("small", { class: "hint" }, modelName(rec.model))));
  else if (rec?.state === "needs_review") article.append(h("div", { class: "review-recommendation inconclusive" },
    h("strong", {}, tr("Модели недостаточно данных для решения")),
    h("p", {}, learningErrors[rec.reason] ? learningError(rec.reason) : rec.reason),
    h("small", { class: "hint" }, tr("Проверьте контекст и выберите решение вручную или повторите пересмотр.")),
    h("p", { class: "hint" }, modelName(rec.model))));
  else if (rec) article.append(h("p", { class: "err" }, rec.state === "stale" ? reviewError("review_stale") : reviewError(rec.error)));
  if(item.event_id) evidence.append(workButton(tr("Открыть запрос {{0}}",[item.event_id]),()=>openInspector(item.event_id,"review")));
  if(item.job_id && !item.job_id.startsWith("document/")) evidence.append(workButton("Открыть задание",()=>showTab("jobs/learning/"+encodeURIComponent(item.job_id))));
  const controls = h("div", { class: "review-card-actions" });
  if (pending) {
    const kind = makeKindSelect(draft.kind, ro), scope = makeScopeSelect(draft.scope, ro);
    kind.addEventListener("change", () => {
      draft.kind = kind.value;
      if (!draft.scopeChanged) { draft.scope = defaultScopeForKind(kind.value); scope.value = draft.scope; }
    });
    scope.addEventListener("change", () => { draft.scope = scope.value; draft.scopeChanged = true; });
    const manual = workButton("Скрыть без ИИ", () => confirmReview([item], "mask"), "primary", ro);
    excerpt.after(h("div", { class: "finding-manual" }, manual,
      h("details", {}, h("summary", {}, tr("Тип и область правила")), kind, scope)));
    if (valid) controls.append(workButton("Применить рекомендацию", () => confirmReview([item], "recommendation"), "secondary", ro));
    if (ReviewUI.data.sources.some(s => s.provider !== "none")) {
      controls.append(workButton("Пересмотреть моделью", () => requestReview([item]), "secondary", ro || item.protected));
    }
    if (item.origin === "detector") controls.append(
      h("button", { class: "ghost", disabled: ro, onclick: () => confirmReview([item], "keep") }, tr("Создать исключение")),
      h("button", { class: "ghost", disabled: ro, onclick: () => confirmReview([item], "dismiss") }, tr("Закрыть без правила")));
    else controls.append(h("button", { class: "ghost", disabled: ro, onclick: () => confirmReview([item], "reject") }, tr("Отклонить находку")));
    if (item.protected) article.append(h("p", { class: "hint" }, reviewError("protected_credential")));
  } else {
    if(item.state==="covered") controls.append(workButton("Открыть правило",()=>openFindingRule(item)));
    if (item.state === "dismissed") controls.append(h("button", { class: "secondary", disabled: ro, onclick: () => applyReview([{ item, action: "reopen", kind: item.kind, scope: item.recommended_scope }]) }, tr("Вернуть на проверку")));
    if (item.state === "active" || item.decision?.rule_id && ["masked", "kept"].includes(item.state)) controls.append(
      h("button", { class: "secondary", disabled: ro, onclick: () => confirmReview([item], "rollback") }, tr("Отменить правило")),
      h("button", { class: "ghost", onclick: () => openFindingRule(item) }, tr("Открыть правило")));
  }
  controls.append(h('button', {
    class:'secondary', disabled:ro, onclick:() => collectFindings({keys:[item.key]}),
  }, tr('В датасет')));
  article.append(evidence, controls); return article;
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
function reviewFilters() {
  const query = {status: ReviewUI.view, source: $("#review-source").value, sort: $("#review-sort").value,
    q: $("#review-query").value, kind: $("#review-kind").value, action: $("#review-action").value};
  if (ReviewUI.learningJob) query.learning_job=ReviewUI.learningJob;
  if (ReviewUI.keys) query.keys = ReviewUI.keys;
  if (ReviewUI.filterSet || ReviewUI.browseSet) query.snapshot = ReviewUI.filterSet || ReviewUI.browseSet;
  if (ReviewUI.jobId) { query.job_id = ReviewUI.jobId; query.outcome = ReviewUI.outcome; }
  return query;
}
function clearPicking() {
  ReviewUI.snapshot = null; ReviewUI.excluded = new Set(); ReviewUI.selected.clear();
}
function picked(item) {return ReviewUI.snapshot ? !ReviewUI.excluded.has(item.key) : ReviewUI.selected.has(item.key);}
function pickedCount() {return ReviewUI.snapshot ? ReviewUI.snapshot.total - ReviewUI.excluded.size : ReviewUI.selected.size;}
async function selectedPayload(items) {
  if (items) return {keys:items.map(i=>i.key)};
  if (ReviewUI.snapshot) return {snapshot:ReviewUI.snapshot.snapshot,excluded:[...ReviewUI.excluded]};
  if (ReviewUI.selected.size) return {keys:[...ReviewUI.selected.keys()]};
  if(ReviewUI.data?.snapshot)return {snapshot:ReviewUI.data.snapshot};
  const snapshot = await api("/api/v1/ui/review/selection",{method:"POST",body:reviewFilters()});
  return {snapshot:snapshot.snapshot};
}
function findingSource(item) {
  return item.source || (item.session ? tr("Сессия · {{0}}",[item.session]) : null) || tr(item.origin === "detector" ? "Быстрый детектор" : "Фоновый анализ");
}
function findingStatus(item) {
  if (!["pending","open"].includes(item.state)) return reviewState(item);
  return tr(({succeeded:"Рекомендация",needs_review:"Нужна проверка",failed:"Ошибка",stale:"Рекомендация устарела"})[item.recommendation?.state] || "На проверку");
}
async function openFindingRule(item) {
  const rule=item.document_rule;
  const id=rule ? (rule.id || "index-"+rule.index) : item.decision?.rule_id || item.rule_id;
  const scope=rule?.scope || item.decision?.scope || item.scope;
  showTab("rules");
  showRulesView("list");
  await loadRules();
  const row=document.getElementById("rule-"+scope+"-"+id);
  if(row){row.focus();row.scrollIntoView({block:"center"});}
}
function showFinding(item) {
  const detail=$("#finding-detail");
  ReviewUI.detailKey=item?.key;
  detail.hidden=!item;
  detail.parentElement.classList.toggle("with-detail",!!item);
  if (!item) { detail.replaceChildren(); return; }
  const card=reviewCard(item);
  if(item.job_recommendation && item.job_recommendation.job_id!==item.recommendation?.job_id){
    card.prepend(h("p",{class:"inline-notice"},tr("Решение в этом задании")," · ",item.job_recommendation.reason||reviewError(item.job_recommendation.error)));
  }
  const close=workButton("Назад к списку",()=>{showFinding(null);document.getElementById("finding-"+item.key)?.focus();});
  detail.replaceChildren(close,card);
  close.focus({preventScroll:true});
  if(matchMedia("(max-width: 800px)").matches)detail.scrollIntoView({block:"start"});
}
function findingRow(item) {
  const checkbox=h("input",{type:"checkbox",checked:picked(item),"aria-label":tr("Выбрать значение: {{0}}",[item.value]),
    onchange:event=>{selectReviewItem(item,event.target.checked);row.classList.toggle("picked",picked(item));syncPageCheck();}});
  const record=h("button",{class:"finding-value",id:"finding-"+item.key,onclick:()=>showFinding(item)},item.value);
  const row=h("tr",{class:picked(item)?"picked":""},h("td",{},h("label",{class:"row-check"},checkbox)),
    h("td",{},record,h("small",{class:"mobile-source"},findingSource(item))),
    h("td",{class:"source-cell"},findingSource(item)),h("td",{},kindLabel(item.kind)),
    h("td",{},findingStatus(item),item.in_dataset?h("small",{class:"hint"},tr("В датасете")):null));
  return row;
}
function syncPageCheck() {
  const current=ReviewUI.data?.items || [], count=current.filter(picked).length;
  $("#review-select-page").checked=!!count && count===current.length;
  $("#review-select-page").indeterminate=count>0 && count<current.length;
}
function renderReview() {
  const data=ReviewUI.data;
  if (!data) return;
  for(const button of document.querySelectorAll("[data-review-view]"))button.setAttribute("aria-pressed",String(button.dataset.reviewView===ReviewUI.view));
  const nodes=[];
  if (ReviewUI.keys || ReviewUI.filterSet || ReviewUI.jobId || ReviewUI.learningJob) nodes.push(h("div",{class:"inline-notice"},tr("Результаты выбранного запуска"),
    workButton("Все находки",()=>showReview())));
  function table(items) {
    return h("table",{class:"findings-table"},h("thead",{},h("tr",{},...["","Значение","Источник","Тип","Состояние"].map((title,i)=>h("th",{class:i===2?"source-cell":""},tr(title))))),h("tbody",{},items.map(findingRow)));
  }
  if(!data.items.length)nodes.push(h("div",{class:"empty"},tr("Находок по этим условиям нет")));
  else if(ReviewUI.groups) {
    for(const kind of new Set(data.items.map(i=>i.kind)))nodes.push(h("details",{open:true,class:"review-group"},h("summary",{},kindLabel(kind)),table(data.items.filter(i=>i.kind===kind))));
  } else nodes.push(table(data.items));
  $("#review-list").replaceChildren(...nodes);
  $("#review-pagination").replaceChildren(workButton("Назад",()=>{ReviewUI.offset-=data.limit;loadReview(true);},"secondary",!ReviewUI.offset),
    h("span",{class:"hint"},tr("{{0}}–{{1}} из {{2}}",[data.total?data.offset+1:0,data.offset+data.items.length,data.total])),
    workButton("Далее",()=>{ReviewUI.offset+=data.limit;loadReview(true);},"secondary",data.offset+data.limit>=data.total));
  if(ReviewUI.detailKey) {
    const item=data.items.find(i=>i.key===ReviewUI.detailKey);
    if(item && !$("#finding-detail").contains(document.activeElement))showFinding(item);
    else if(!item)showFinding(null);
  }
  syncPageCheck();renderReviewSelection();
}
function renderReviewSelection() {
  const data=ReviewUI.data, panel=$("#review-selection");
  if(!data)return;
  panel.hidden=!data.total;
  const count=pickedCount() || data.total, busy=ReviewUI.busy || !info.editable;
  const sources=(data.sources||[]).filter(s=>s.provider!=="none");
  const choices=sources.filter((s,index)=>sources.findIndex(other=>other.model===s.model && other.connection_id===s.connection_id)===index);
  const source=choices.find(s=>s.role===ReviewUI.modelSource)||choices[0];
  if(source)ReviewUI.modelSource=source.role;
  const model=choices.length>1?h("select",{id:"review-model-source","aria-label":tr("Модель пересмотра"),onchange:e=>{ReviewUI.modelSource=e.target.value;ReviewUI.scenario="";renderReviewSelection();}},
    ...choices.map(s=>h("option",{value:s.role,selected:s===source},modelName(s.model)))):source?h("strong",{},modelName(source.model)):null;
  const study = panel.querySelector(".review-study") || h("div", {class: "scenario-study review-study"});
  const showStudy = () => renderScenarioStudy(study, source.provider === "local" ? measuredModelId(source.model) : null,
    ReviewUI.scenario || source.scenario || "standard");
  const mode = source?.supports_scenarios ? h("select", {"aria-label": tr("Способ проверки для запуска"), onchange: e => {
    ReviewUI.scenario = e.target.value; showStudy();
  }}, scenarioOptions(ReviewUI.scenario || source.scenario || "standard")) : null;
  const controls = h("div", {class: "form-actions"});
  const list = [...ReviewUI.selected.values()];
  if (ReviewUI.view !== "history") {
    const manualCount = ReviewUI.snapshot ? pickedCount() : list.filter(pendingFinding).length;
    const label = manualCount && (!ReviewUI.snapshot || ReviewUI.view === "pending")
      ? tr("Скрыть без ИИ · {{0}}", [manualCount]) : tr("Скрыть без ИИ");
    controls.append(workButton(label, maskSelectedFindings, manualCount || !source ? "primary" : "secondary", busy || !manualCount));
    if (source) controls.append(workButton(tr(pickedCount() ? "Проверить моделью · {{0}}" : "Проверить моделью все {{0}}", [count]),
      () => requestReview(), manualCount ? "secondary" : "primary", busy));
  }
  controls.append(workButton(tr(pickedCount()?"В датасет · {{0}}":"В датасет все {{0}}",[count]),async()=>collectFindings(await selectedPayload()),"secondary",busy));
  if(!ReviewUI.snapshot && data.total>pickedCount())controls.append(workButton(tr("Выбрать все {{0}} по фильтру",[data.total]),async()=>{
    const request = ReviewUI.request;
    const snap = await api("/api/v1/ui/review/selection", {method:"POST", body:reviewFilters()});
    if (request !== ReviewUI.request) return;
    clearPicking();ReviewUI.snapshot=snap;renderReview();
  }));
  if(pickedCount())controls.append(workButton("Снять выбор",()=>{clearPicking();renderReview();}));
  if(list.length && list.every(i=>i.recommendation?.state==="succeeded"))controls.append(workButton("Применить рекомендации",()=>confirmReview(list,"recommendation"),"secondary",busy));
  if(ReviewUI.jobId && ReviewUI.outcome==="succeeded" && (!list.length || ReviewUI.snapshot))controls.append(workButton("Применить рекомендации",async()=>{
    const filtered=[];const scope=await selectedPayload();
    for(let offset=0;offset<data.total;offset+=250){
      const page=await api("/api/v1/ui/review?"+new URLSearchParams({...reviewFilters(),snapshot:scope.snapshot,limit:250,offset}));
      filtered.push(...page.items.filter(i=>!ReviewUI.excluded.has(i.key)&&i.recommendation?.state==="succeeded"&&i.recommendation.job_id===ReviewUI.jobId));
    }
    if(filtered.length)confirmReview(filtered,"recommendation");
  },"secondary",busy));
  if (!source) controls.append(workButton("Выбрать модель", () => showTab("settings")));
  panel.replaceChildren(h("div", {class: "bulk-actions"},
    h("strong", {}, tr(pickedCount() ? "Выбрано: {{0}}" : "Найдено · {{0}}", [count])), controls));
  if (source) panel.append(h("div", {class: "finding-model"}, model,
    h("small", {class: "hint"}, source.connection || tr(source.provider === "local" ? "Локальная модель" : "Модель по API")), mode));
  if (mode) { panel.append(study); showStudy(); }
}
function pendingFinding(item) {
  return item.state === "open" || item.state === "pending";
}
async function maskSelectedFindings() {
  const snapshot = ReviewUI.snapshot;
  let items = [...ReviewUI.selected.values()].filter(pendingFinding);
  if (snapshot) {
    const request = ReviewUI.request, excluded = new Set(ReviewUI.excluded);
    items = [];
    for (let offset = 0; offset < snapshot.total; offset += 250) {
      const params = new URLSearchParams({snapshot: snapshot.snapshot, status: "all", offset, limit: 250});
      const page = await api("/api/v1/ui/review?" + params);
      if (location.hash !== "#review" || request !== ReviewUI.request || snapshot !== ReviewUI.snapshot) return;
      items.push(...page.items.filter(item => pendingFinding(item) && !excluded.has(item.key)));
      if (offset + page.items.length >= page.total) break;
    }
  }
  if (items.length) confirmReview(items, "mask");
  else $("#review-result").textContent = tr("В выборке больше нет находок на проверку.");
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
  dialog.replaceChildren(h("div", { class: "review-confirm-body" }, h("h2", { id: "review-confirm-title" }, tr("Проверить изменения")),
    h("p", { class: "hint" }, tr("Записей: {{0}}", [entries.length])),
    operation === "mask" ? h("p", {}, tr("Выбранные значения будут скрываться в будущих запросах.")) : null,
    h("div", { class: "review-confirm-list" }, ...entries.map((entry) => h("div", {}, h("code", {}, entry.item.value),
      h("p", {}, `${kindLabel(entry.kind)} · ${scopeLabel(entry.scope)}`), operation === "mask" ? null : h("p", { class: "hint" }, effect(entry))))),
    h("div", { class: "form-actions" }, h("button", { class: "primary", id: "review-confirm-apply", onclick: () => { dialog.close(); origin?.focus(); applyReview(entries); } }, tr("Применить изменения")),
      h("button", { class: "secondary", onclick: () => { dialog.close(); origin?.focus(); } }, tr("Отмена")))));
  dialog.showModal();
}
async function applyReview(entries) {
  if (ReviewUI.busy) return; ReviewUI.busy = true;
  const failures = [], snapshot = ReviewUI.snapshot;
  let applied = 0; renderReview(true);
  for (const entry of entries) {
    const item = entry.item;
    try {
      await api(`/api/v1/ui/review/${item.origin}/${encodeURIComponent(item.id)}/${entry.use_recommendation ? "apply" : entry.action}`, {
        method: "POST", body: { kind: entry.kind, scope: entry.scope, use_recommendation: !!entry.use_recommendation, expected_job:entry.use_recommendation ? item.recommendation.job_id : undefined } });
      applied++; ReviewUI.selected.delete(item.key); ReviewUI.drafts.delete(item.key);
      if (snapshot && snapshot === ReviewUI.snapshot) ReviewUI.excluded.add(item.key);
    } catch (error) { failures.push(`${item.value}: ${reviewError(error.messageKey || error.message)}`); }
    if (entries.length > 1) $("#review-result").textContent = tr("Обработано {{0}} из {{1}}", [applied + failures.length, entries.length]);
  }
  if (snapshot && snapshot === ReviewUI.snapshot && pickedCount() === 0) clearPicking();
  ReviewUI.busy = false;
  $("#review-result").textContent = tr("Применено: {{0}}", [applied]) + (failures.length ? " · " + failures.join("; ") : "");
  $("#review-result").classList.toggle("err", !!failures.length);
  await Promise.all([loadReview(true), loadInfo()]);
}
async function requestReview(items) {
  if(ReviewUI.busy)return;
  if(!ReviewUI.data?.sources.some(s=>s.provider!=="none")){showTab("settings");return;}
  ReviewUI.busy=true;renderReviewSelection();
  try {
    const selection=await selectedPayload(items);
    const body={...selection,model_source:ReviewUI.modelSource||"auto",scenario:ReviewUI.scenario||undefined};
    const job=await postOnce("/api/v1/ui/review/jobs",body);
    rememberFindings(job.id);showTab("jobs/review/"+job.id);
  } catch(e){$("#review-result").textContent=reviewError(e.messageKey||e.message);}
  finally{ReviewUI.busy=false;renderReviewSelection();}
}
function lockReviewResults(locked) {
  for (const id of ["review-pagination", "review-list", "review-selection"]) $("#" + id).inert = locked;
  $("#review-select-page").disabled = locked;
}
async function loadReview() {
  clearTimeout(ReviewUI.filterTimer);
  const request=++ReviewUI.request;
  lockReviewResults(true);
  $("#review-list").setAttribute("aria-busy", "true");
  try {
    const params=new URLSearchParams({...reviewFilters(),stable:true,offset:ReviewUI.offset,limit:$("#review-size").value});
    const data=await api("/api/v1/ui/review?"+params);
    if(request!==ReviewUI.request)return;
    ReviewUI.data=data;ReviewUI.offset=data.offset;ReviewUI.browseSet=data.snapshot;
    for(const item of data.items)if(ReviewUI.selected.has(item.key))ReviewUI.selected.set(item.key,item);
    $("#review-badge").textContent=data.pending||"";$("#review-badge").hidden=!data.pending;
    renderReview();
    lockReviewResults(false);
  }catch(e){if(request===ReviewUI.request)$("#review-result").textContent=reviewError(e.messageKey||e.message);}
  finally{if(request===ReviewUI.request)$("#review-list").setAttribute("aria-busy", "false");}
}
$("#review-kind").replaceChildren(h("option",{value:""},tr("Все типы")),...STANDARD_KINDS.map(k=>h("option",{value:k},kindLabel(k))));
function changedFilter(event) {
  ReviewUI.browseSet=null;clearPicking();ReviewUI.offset=0;showFinding(null);
  if (event?.type !== "input") return loadReview();
  clearTimeout(ReviewUI.filterTimer);
  ++ReviewUI.request;
  lockReviewResults(true);
  $("#review-list").setAttribute("aria-busy", "true");
  ReviewUI.filterTimer = setTimeout(loadReview, 250);
}
for(const b of document.querySelectorAll("[data-review-view]")) b.onclick=()=>{ReviewUI.view=b.dataset.reviewView;changedFilter();};
for(const id of ["review-source","review-sort","review-kind","review-action"])$("#"+id).onchange=changedFilter;
$("#review-query").oninput=changedFilter;
$("#review-refresh").onclick=()=>{ReviewUI.browseSet=null;loadReview();};
$("#review-size").value=localStorage.getItem("kaponir-page-size")||"100";
$("#review-size").onchange=e=>{localStorage.setItem("kaponir-page-size",e.target.value);ReviewUI.offset=0;loadReview();};
$("#review-select-page").onchange=event=>{
  for(const item of ReviewUI.data?.items||[]) {
    if(ReviewUI.snapshot){if(event.target.checked)ReviewUI.excluded.delete(item.key);else ReviewUI.excluded.add(item.key);}
    else if(event.target.checked)ReviewUI.selected.set(item.key,item);else ReviewUI.selected.delete(item.key);
  }
  if(ReviewUI.snapshot && pickedCount()===0)clearPicking();
  renderReview();
};
$("#review-groups").onclick=e=>{ReviewUI.groups=!ReviewUI.groups;e.currentTarget.setAttribute("aria-pressed",String(ReviewUI.groups));renderReview();};
TABS.review=loadReview;
