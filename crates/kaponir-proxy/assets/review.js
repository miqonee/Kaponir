"use strict";
const ReviewUI = { data: null, view: "pending", selected: new Map(), drafts: new Map(), offset: 0, busy: false,
  request: 0, selectionVersion: 0, groups: false, jobs: new Map(), modelSource: "", selectionKey: "", keys: new URLSearchParams(location.search).get("review_keys") || "" };
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
  ++ReviewUI.selectionVersion;

  if (ReviewUI.snapshot) {
    if (checked) ReviewUI.excluded.delete(item.key); else ReviewUI.excluded.add(item.key);
  } else if (checked) ReviewUI.selected.set(item.key, item); else ReviewUI.selected.delete(item.key);
  if(ReviewUI.snapshot && pickedCount()===0)clearPicking();
  renderReviewSelection(); return checked;
}
function findingOrigin(item) {
  if (item.origin === "detector") return tr("Быстрый детектор");
  if (item.job_id?.startsWith("document/")) return tr("Поиск по документам и сессиям");
  return tr("Модель обнаружения");
}
function activeReview(item) {
  return ReviewUI.activeJobs?.get(item.key);
}
function decisionStatus(item) {
  if (!pendingFinding(item)) return reviewState(item);
  const job = activeReview(item);
  if (job) return tr(job.state === "queued" ? "В очереди на проверку ИИ" : "Идёт проверка ИИ");
  if (item.proposal?.declined) return tr("Предложение отклонено");
  if (item.proposal) return tr(item.proposal.action === "mask" ? "ИИ предлагает скрыть" : "ИИ предлагает оставить");
  const rec = item.recommendation;
  if (rec?.state === "stale") return tr("Предложение устарело");
  if (rec?.state === "failed" || categoryStage(item) === "failed") return tr("Ошибка проверки ИИ");
  if (rec?.state === "needs_review" && rec.reason === "configured_categorizer") return tr("Нет решения о маскировании");
  if (rec?.state === "needs_review" || item.scenario_result?.verdict === "review") return tr("ИИ просит решить вручную");
  if (categoryStage(item) === "succeeded") return tr("ИИ проверил только тип");
  if (item.origin === "learning" && item.model) return tr("ИИ нашёл значение");
  return tr("Без проверки ИИ");
}
function decisionEffect(item, action) {
  if (action === "mask") return tr("В будущих запросах это значение будет заменяться токеном.");
  if (action === "keep" && item.origin === "detector")
    return tr("Будет создано точное исключение для будущих запросов. Обязательная защита секретов сохранится.");
  if (action === "reject" || action === "keep")
    return tr("Фоновый поиск запомнит отклонение этого значения. Правила маскирования сохранятся.");
  if (action === "dismiss") return tr("Находка переместится в историю. Её можно вернуть на проверку.");
  return tr("Созданное правило отключится. Сохранённые токены останутся доступны.");
}
function reviewCard(item) {
  const pending = pendingFinding(item), proposal = item.proposal, rec = item.recommendation;
  const ro = !ReviewUI.data.editable, currentJob = activeReview(item);
  const hasProposal = pending && proposal && !proposal.declined && !currentJob;
  const canAsk = pending && !item.protected && ReviewUI.data.sources.some(source => source.provider !== "none");
  const article = h("article", {class:"panel review-card", "data-review-key":item.key,
    "data-learning-id":item.origin === "learning" ? item.id : null});
  const heading = h("header", {class:"decision-heading"},
    h("div", {class:"finding-meta"}, findingOrigin(item), h("span", {}, kindLabel(item.kind))),
    h("h2", {class:"review-value", tabindex:-1}, item.value));
  const verdict = h("section", {class:"decision-summary", "data-tone":hasProposal ? "proposal" : "neutral"},
    h("h3", {}, decisionStatus(item)));
  const primary = h("div", {class:"decision-actions"});
  if (hasProposal) {
    const why = proposal.reason;
    verdict.append(h("p", {}, decisionEffect(item, proposal.action)));
    if (why && why !== "configured_categorizer") verdict.append(h("p", {class:"decision-reason"}, learningError(why)));
    verdict.append(h("p", {class:"hint"}, [modelName(proposal.model), kindLabel(proposal.kind), scopeLabel(proposal.scope)].filter(Boolean).join(" · ")));
    primary.append(workButton("Принять предложение", () => decideProposal(item, true), "primary", ro),
      workButton("Отклонить предложение", () => decideProposal(item, false), "secondary", ro));
  } else if (pending) {
    let note;
    if (currentJob) {
      note = tr("Результат появится здесь после ответа модели.");
      primary.append(workButton("Открыть задание", () => showTab("jobs/review/"+encodeURIComponent(currentJob.id)), "secondary"));
    } else if (proposal?.declined) note = tr("Правила не менялись. Можно принять ручное решение или запросить новое предложение.");
    else if (rec?.state === "failed" || rec?.state === "stale") note = reviewError(rec.error || "review_stale");
    else if (rec?.state === "needs_review" && rec.reason === "configured_categorizer")
      note = tr("Категоризатор вернул тип значения. Проверьте фрагмент и выберите действие вручную.");
    else if (rec?.state === "needs_review" || item.scenario_result?.verdict === "review")
      note = reviewError(rec?.reason || item.scenario_result?.reason || "review_inconclusive");
    else if (categoryStage(item) === "failed") note = learningError(item.reason || "categorization_failed");
    else if (item.protected) note = reviewError("protected_credential");
    else note = tr(canAsk ? "Решения о маскировании пока нет. Проверьте фрагмент или запросите предложение модели." : "Проверьте фрагмент и выберите ручное решение.");
    verdict.append(h("p", {}, note));
    if (!currentJob && canAsk) primary.append(workButton("Получить предложение ИИ", () => requestReview([item]), "secondary", ro));
  } else if (item.state === "active" || item.state === "masked" || item.state === "kept" || item.state === "covered") {
    primary.append(workButton("Открыть правило", () => openFindingRule(item), "secondary"));
  }
  if (primary.childElementCount) verdict.append(primary);
  const excerpt = h("blockquote", {class:"candidate-fragment"}, candidateExcerpt(item.context,item.value,item.context_span));
  const origin = h("div", {class:"origin-reading"});
  const fields = h("dl", {class:"origin-fields"});
  function field(label, value) { if (value) fields.append(h("dt", {}, tr(label)), h("dd", {}, value)); }
  field("Источник", item.source || findingOrigin(item));
  if (item.origin === "detector") {
    field("В исходном запросе", tr(item.applied === "mask" ? "Замаскировано" : "Оставлено в тексте"));
    field("Оценка детектора", reviewPercent(item.probability));
    field("Основание", detectorReason(item));
    field("Детектор", item.recognizer);
    field("Признаки", (item.evidence || []).map(e => e.source+" · "+e.logit.toFixed(2)).join(", "));
  } else {
    field("Модель обнаружения", modelName(item.model));
    if (item.extraction_confidence != null) field("Оценка обнаружения", reviewPercent(item.extraction_confidence));
    field("Категоризация", stageText(item));
    field("Модель категоризации", modelName(item.classifier));
    if (item.classification_confidence != null) field("Оценка категоризации", reviewPercent(item.classification_confidence));
    if (item.proposed_kind && item.proposed_kind !== item.kind) field("Исходный тип модели", kindLabel(item.proposed_kind));
    if (item.classification_proposed_kind && item.classification_proposed_kind !== item.kind)
      field("Тип до проверки формата", kindLabel(item.classification_proposed_kind));
    if (item.reason) field("Основание", reviewError(item.reason));
  }
  if (item.validation_reason) field("Проверка формата", reviewError(item.validation_reason));
  if (rec?.model) field("Модель пересмотра", modelName(rec.model));
  if (rec?.confidence != null) field("Оценка пересмотра", reviewPercent(rec.confidence));
  origin.append(fields);
  const sourceLinks = h("div", {class:"source-links"});
  if (item.event_id) sourceLinks.append(workButton(tr("Открыть запрос {{0}}",[item.event_id]), () => openInspector(item.event_id,"review")));
  if (item.job_id && !item.job_id.startsWith("document/"))
    sourceLinks.append(workButton("Открыть задание", () => showTab("jobs/learning/"+encodeURIComponent(item.job_id))));
  if (rec?.job_id) sourceLinks.append(workButton("Задание пересмотра", () => showTab("jobs/review/"+encodeURIComponent(rec.job_id))));
  if (hasProposal && canAsk) sourceLinks.append(workButton("Запросить повторную проверку", () => requestReview([item]), "text-button", ro));
  if (sourceLinks.childElementCount) origin.append(sourceLinks);
  const tabs = contentTabs("Подробности находки", [
    {key:"excerpt", title:"Фрагмент", content:excerpt}, {key:"origin", title:"Происхождение", content:origin}]);
  const footer = h("footer", {class:"decision-footer"});
  if (pending) footer.append(workButton("Решить вручную", () => manualDecision(item), !canAsk && !hasProposal ? "primary" : "secondary", ro));
  if (item.state === "dismissed") footer.append(workButton("Вернуть на проверку", () => applyReview([{item,action:"reopen"}]), "secondary", ro));
  if (item.state === "active" || item.decision?.rule_id && ["masked","kept"].includes(item.state))
    footer.append(workButton("Отменить правило", () => confirmReview([item],"rollback"), "secondary", ro));
  footer.append(item.in_dataset
    ? workButton("Открыть в датасете", () => openFindingExample(item.dataset_id), "text-button")
    : workButton("В датасет", () => collectFindings({keys:[item.key]}), "text-button", ro));
  article.append(heading, verdict, tabs, footer);
  return article;
}
function reviewField(id, label, control) {
  control.id = id;
  return h("div", {class:"field"}, h("label", {for:id}, tr(label)), control);
}
function manualDecision(item) {
  const dialog = $("#review-confirm"), draft = reviewDraft(item);
  const kind = makeKindSelect(draft.kind), scope = makeScopeSelect(draft.scope);
  const kindField = reviewField("decision-kind", "Тип значения", kind);
  const scopeField = reviewField("decision-scope", "Область правила", scope);
  const dismiss = item.origin === "detector" ? "dismiss" : "reject";
  const options = [["mask","Скрывать значение"], ...(item.origin === "detector" ? [["keep","Создать исключение"]] : []),
    [dismiss, item.origin === "detector" ? "Закрыть без правила" : "Отклонить находку"]];
  const action = h("select", {autofocus:true}, options.map(([value,label]) => h("option", {value}, tr(label))));
  const effect = h("p", {class:"decision-effect"});
  function update() {
    kindField.hidden = scopeField.hidden = action.value === dismiss;
    effect.textContent = decisionEffect(item, action.value);
  }
  kind.onchange = () => { draft.kind = kind.value; if (!draft.scopeChanged) scope.value = draft.scope = defaultScopeForKind(kind.value); };
  scope.onchange = () => { draft.scopeChanged = true; draft.scope = scope.value; };
  action.onchange = update; update();
  const cancel = h("button", {type:"button",class:"secondary",onclick:()=>dialog.close()}, tr("Отмена"));
  dialog.replaceChildren(h("div", {class:"review-confirm-body"}, h("h2",{id:"review-confirm-title"},tr("Ручное решение")),
    h("code", {class:"decision-value"}, item.value), reviewField("decision-action", "Действие", action),
    h("div", {class:"manual-fields"}, kindField,scopeField), effect,
    h("div", {class:"dialog-actions"},cancel,h("button", {id:"review-confirm-apply",class:"primary",type:"button",onclick:()=>{
      dialog.close(); applyReview([{item, action:action.value, kind:kind.value, scope:scope.value}]);
    }},tr("Применить решение")))));
  dialog.showModal();
}
function decideProposal(item, accept) {
  if (accept && ["type_format_conflict","numeric_type_corrected","type_rechecked"].includes(item.validation_reason))
    return confirmReview([item], "proposal");
  return applyReview([{item,action:accept ? "accept-proposal" : "decline-proposal",proposal_id:item.proposal.id}]);
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
  ++ReviewUI.selectionVersion;
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
  if (item.event_id) return tr("Запрос {{0}}", [item.event_id]);
  return item.source || (item.session ? tr("Сессия · {{0}}",[item.session]) : null) || tr(item.origin === "detector" ? "Быстрый детектор" : "Фоновый анализ");
}
async function openFindingRule(item) {
  const rule=item.document_rule;
  const id=rule ? (rule.id || "index-"+rule.index) : item.decision?.rule_id || item.rule_id;
  const scope=rule?.scope || item.decision?.scope || item.scope;
  $("#rules-query").value = ""; $("#rules-scope").value = scope || "";
  showTab("rules");
  showRulesView("list");
  await loadRules();
  const row=document.getElementById("rule-"+scope+"-"+id);
  if(row){row.focus();row.scrollIntoView({block:"center"});}
}
async function adjacentFinding(key, step) {
  let data = ReviewUI.data;
  const index = data.items.findIndex(item => item.key === key);
  const target = data.offset + index + step;
  if (index < 0 || target < 0 || target >= data.total) return;
  if (target < data.offset || target >= data.offset + data.items.length) {
    ReviewUI.offset = Math.floor(target / data.limit) * data.limit;
    data = await loadReview();
    if (!data || location.hash !== "#review") return;
  }
  const item = data.items[target - data.offset];
  if (!item) return;
  showFinding(item, false);
  const button = $("#finding-detail").querySelector(step > 0 ? ".next-finding" : ".previous-finding");
  (button.disabled ? $("#finding-detail .back-to-findings") : button).focus({preventScroll:true});
  if (matchMedia("(min-width: 801px)").matches)
    document.getElementById("finding-"+item.key)?.scrollIntoView({block:"nearest"});
}
function showFinding(item, moveFocus = true) {
  const detail=$("#finding-detail");
  ReviewUI.detailKey=item?.key;
  detail.hidden=!item;
  detail.parentElement.classList.toggle("with-detail",!!item);
  for (const button of $("#review-list").querySelectorAll(".finding-value")) {
    const current = button.id === "finding-" + item?.key;
    button.setAttribute("aria-expanded", String(current));
    button.closest("tr")?.classList.toggle("previewing", current);
  }
  if (!item) { detail.replaceChildren(); return; }
  ReviewUI.detailVersion = JSON.stringify([item, activeReview(item)?.id, activeReview(item)?.state, I18N.language, ReviewUI.data.editable]);
  const card=reviewCard(item);
  if(item.job_recommendation && item.job_recommendation.job_id!==item.recommendation?.job_id){
    card.prepend(h("p",{class:"inline-notice"},tr("Решение в этом задании")," · ",item.job_recommendation.reason||reviewError(item.job_recommendation.error)));
  }
  const close=workButton("Назад к списку",()=>{showFinding(null);document.getElementById("finding-"+item.key)?.focus();}, "text-button back-to-findings");
  const data=ReviewUI.data, position=data.offset+data.items.findIndex(row=>row.key===item.key)+1;
  const nav=h("nav", {class:"finding-pager","aria-label":tr("Просмотр находок")}, close,
    h("span", {class:"hint"}, tr("{{0}} из {{1}}", [position, data.total])),
    workButton("Предыдущая находка",()=>adjacentFinding(item.key,-1),"secondary previous-finding",position<=1),
    workButton("Следующая находка",()=>adjacentFinding(item.key,1),"secondary next-finding",position>=data.total));
  for (const [button, name] of [[nav.querySelector(".previous-finding"),"previous"],[nav.querySelector(".next-finding"),"next"]]) {
    button.title=button.textContent; button.setAttribute("aria-label",button.title); button.replaceChildren(icon("arrow"));
    button.classList.add("pager-"+name);
  }
  detail.replaceChildren(nav,card);
  if (moveFocus) {
    close.focus({preventScroll:true});
    if(matchMedia("(max-width: 800px)").matches)detail.scrollIntoView({block:"start"});
  }
}
function findingRow(item) {
  const checkbox=h("input",{type:"checkbox",checked:picked(item),"aria-label":tr("Выбрать значение: {{0}}",[item.value]),
    onchange:event=>{selectReviewItem(item,event.target.checked);row.classList.toggle("picked",picked(item));syncPageCheck();}});
  const record=h("button",{class:"finding-value",id:"finding-"+item.key,"aria-controls":"finding-detail",
    "aria-expanded":String(item.key===ReviewUI.detailKey),onclick:()=>showFinding(item)},item.value);
  const row=h("tr",{class:picked(item)?"picked":"",onclick:e=>{
    if (!e.target.closest("button,input,label,a,select") && !e.target.closest("td:first-child") && window.getSelection()?.isCollapsed) showFinding(item);
  }},h("td",{},h("label",{class:"row-check"},checkbox)),
    h("td",{},record,h("small",{class:"mobile-source"},findingSource(item))),
    h("td",{class:"source-cell"},findingSource(item)),h("td",{},kindLabel(item.kind)),
    h("td",{},h("span",{class:"table-verdict", "data-proposal":!!item.proposal && !item.proposal.declined && pendingFinding(item)},decisionStatus(item)),item.in_dataset?h("small",{class:"hint"},tr("В датасете")):null));
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
  const filters=reviewFilters();
  const filtered=!!(filters.q || filters.kind || filters.action || filters.source!=="all" || filters.sort!=="recent");
  $("#review-clear-filters").hidden=!filtered;
  $("#review-match-count").textContent=tr("Находок в выборке · {{0}}", [number(data.total)]);
  if (ReviewUI.keys || ReviewUI.filterSet || ReviewUI.jobId || ReviewUI.learningJob) nodes.push(h("div",{class:"inline-notice"},tr("Результаты выбранного запуска"),
    workButton("Все находки",()=>showReview())));
  function table(items) {
    return h("table",{class:"findings-table"},h("thead",{},h("tr",{},...["","Значение","Источник","Тип","Состояние"].map((title,i)=>h("th",{class:i===2?"source-cell":""},tr(title))))),h("tbody",{},items.map(findingRow)));
  }
  if(!data.items.length) {
    const scoped=!!(ReviewUI.jobId || ReviewUI.learningJob || ReviewUI.keys || ReviewUI.filterSet);
    nodes.push(h("div",{class:"empty-state"}, icon("search"), h("strong",{},tr(ReviewUI.view === "proposals" && !filtered ? "Готовых предложений ИИ пока нет" : "Находок по этим условиям нет")),
      ReviewUI.view === "proposals" && !filtered ? h("p", {}, tr("Откройте находку на проверку и запросите предложение ИИ.")) : null,
      filtered ? workButton("Сбросить фильтры", resetReviewFilters) :
        ReviewUI.view === "proposals" ? workButton("На проверку", () => $("#review-navigation [data-review-view=pending]").click()) :
        scoped || ReviewUI.view==="history" ? workButton("Открыть находки",()=>showReview()) :
          workButton("Найти значения в документе",()=>{showTab("discover");showDiscoverView("input");})));
  }
  else if(ReviewUI.groups) {
    for(const kind of new Set(data.items.map(i=>i.kind)))nodes.push(h("details",{open:true,class:"review-group"},h("summary",{},kindLabel(kind)),table(data.items.filter(i=>i.kind===kind))));
  } else nodes.push(table(data.items));
  $("#review-list").replaceChildren(...nodes);
  $("#review-pagination").replaceChildren(workButton("Назад",()=>{ReviewUI.offset-=data.limit;loadReview(true);},"secondary",!ReviewUI.offset),
    h("span",{class:"hint"},tr("{{0}}–{{1}} из {{2}}",[data.total?data.offset+1:0,data.offset+data.items.length,data.total])),
    workButton("Далее",()=>{ReviewUI.offset+=data.limit;loadReview(true);},"secondary",data.offset+data.limit>=data.total));
  if(ReviewUI.detailKey) {
    const item=data.items.find(i=>i.key===ReviewUI.detailKey);
    if (item && ReviewUI.detailVersion !== JSON.stringify([item, activeReview(item)?.id, activeReview(item)?.state, I18N.language, data.editable])) {
      const focused = $("#finding-detail").contains(document.activeElement);
      showFinding(item, false);
      if (focused) $("#finding-detail h2").focus({preventScroll:true});
    }
    else if(!item)showFinding(null);
  }
  syncPageCheck();renderReviewSelection();
}
function renderReviewSelection() {
  const data=ReviewUI.data, panel=$("#review-selection"), quick=$("#review-quick-actions");
  if(!data)return;
  const selected=pickedCount(), count=selected || data.total, busy=ReviewUI.busy || !info.editable;
  panel.hidden=!selected || !data.total;
  quick.hidden=!data.total;
  const sources=(data.sources||[]).filter(s=>s.provider!=="none");
  const choices=sources.filter((s,index)=>sources.findIndex(other=>other.model===s.model && other.connection_id===s.connection_id)===index);
  const source=choices.find(s=>s.role===ReviewUI.modelSource)||choices[0];
  if(source)ReviewUI.modelSource=source.role;
  const modelLabel = id => (WORKER.data?.catalog || builtinModelChoices).find(row => row.id === id)?.label || modelName(id);
  const model=choices.length>1?h("select",{id:"review-model-source","aria-label":tr("Модель пересмотра"),onchange:e=>{ReviewUI.modelSource=e.target.value;ReviewUI.scenario="";renderReviewSelection();}},
    ...choices.map(s=>h("option",{value:s.role,selected:s===source},modelLabel(s.model)))):source?h("strong",{},modelLabel(source.model)):null;
  const dialog = $("#review-options");
  const study = dialog.querySelector(".review-study") || h("div", {class: "scenario-study review-study"});
  const showStudy = () => renderScenarioStudy(study, source.provider === "local" ? measuredModelId(source.model) : null,
    ReviewUI.scenario || source.scenario || "standard", mode);
  const mode = source?.supports_scenarios ? h("select", {"aria-label": tr("Способ проверки для запуска"), onchange: e => {
    ReviewUI.scenario = e.target.value; showStudy();
  }}, scenarioOptions(ReviewUI.scenario || source.scenario || "standard")) : null;
  const controls = h("div", {class: "form-actions"});
  const selectionHead = h("div", {class:"selection-head"}, h("strong", {}, tr("Выбрано: {{0}}",[selected])));
  const batchButton = (full, compact, action, cls, disabled) => {
    const button = workButton(full, action, cls, disabled);
    if (selected && full !== compact) {
      button.setAttribute("aria-label", full);
      button.replaceChildren(h("span", {class:"batch-full"}, full), h("span", {class:"batch-short"}, compact));
    }
    return button;
  };
  const list = [...ReviewUI.selected.values()];
  if (ReviewUI.view !== "history" && ReviewUI.view !== "proposals") {
    const manualCount = ReviewUI.snapshot ? pickedCount() : list.filter(pendingFinding).length;
    const label = manualCount && (!ReviewUI.snapshot || ReviewUI.view === "pending")
      ? tr("Создать правила маскирования · {{0}}", [manualCount]) : tr("Создать правила маскирования");
    if (selected) controls.append(batchButton(label, manualCount === selected ? tr("Скрыть") : label, maskSelectedFindings, "primary", busy || !manualCount));
    if (source) controls.append(batchButton(tr(selected ? "Проверить моделью · {{0}}" : "Проверить моделью все {{0}}", [count]),
      tr("Проверить моделью"), () => requestReview(), manualCount ? "secondary" : "primary", busy));
  }
  controls.append(batchButton(tr(selected?"В датасет · {{0}}":"В датасет все {{0}}",[count]), tr("В датасет"),async()=>collectFindings(await selectedPayload()),"secondary",busy));
  quick.replaceChildren();
  if(!ReviewUI.snapshot && data.total>selected)quick.append(workButton(tr("Выбрать все {{0}} по фильтру",[data.total]),async()=>{
    const version = ReviewUI.selectionVersion;
    const snap = await api("/api/v1/ui/review/selection", {method:"POST", body:reviewFilters()});
    if (location.hash !== "#review" || version !== ReviewUI.selectionVersion) return;
    clearPicking();ReviewUI.snapshot=snap;renderReview();
  }));
  if(selected)selectionHead.append(workButton("Снять выбор",()=>{clearPicking();renderReview(); $("#review-select-page").focus({preventScroll:true});}));
  const proposalView = ReviewUI.view === "proposals";
  const eligible = item => pendingFinding(item) && item.proposal && !item.proposal.declined &&
    (!ReviewUI.jobId || item.proposal.job === ReviewUI.jobId);
  if (proposalView || list.length && list.every(eligible) || ReviewUI.jobId && ReviewUI.outcome === "succeeded") {
    const openDecision = async operation => {
      if (selected && !ReviewUI.snapshot) return confirmReview(list.filter(eligible), operation);
      const version = ReviewUI.selectionVersion;
      const selection = await selectedPayload();
      const matches = [];
      for (let offset = 0; offset < data.total; offset += 250) {
        const params = new URLSearchParams({...reviewFilters(), offset, limit:250});
        if (selection.snapshot) params.set("snapshot", selection.snapshot);
        const page = await api("/api/v1/ui/review?" + params);
        if (location.hash !== "#review" || version !== ReviewUI.selectionVersion) return;
        matches.push(...page.items.filter(item => !ReviewUI.excluded.has(item.key) && eligible(item)));
        if (offset+page.limit >= page.total) break;
      }
      if (matches.length) confirmReview(matches,operation);
      else { await loadReview(); toast(tr("Предложения уже изменились. Список обновлён.")); }
    };
    controls.prepend(workButton("Принять предложения", () => openDecision("proposal"), "primary", busy));
    if (proposalView) controls.append(workButton("Отклонить предложения", () => openDecision("decline-proposal"), "secondary", busy));
  }
  if (ReviewUI.view !== "history" && !proposalView) {
    if (source) {
      const optionsTitle = tr("Параметры проверки");
      const open = h("button", {class:"text-button review-settings", type:"button", "aria-haspopup":"dialog", "aria-label":optionsTitle, title:optionsTitle, onclick:()=>dialog.showModal()},
        icon("settings"), h("span", {}, optionsTitle));
      (selected ? selectionHead : quick).append(open);
      dialog.onclose = () => { if (open.isConnected && location.hash === "#review") open.focus({preventScroll:true}); };
      const close = h("button", {class:"primary", type:"button", onclick:()=>dialog.close()}, tr("Готово"));
      const inputs = JSON.stringify([choices, source.role, I18N.language]);
      if (!dialog.open || inputs !== dialog.dataset.inputs) {
        dialog.dataset.inputs = inputs;
        const focusedModel = dialog.open && document.activeElement?.id === "review-model-source";
        if (mode) mode.autofocus = true;
        else close.autofocus = true;
        const header = h("header",{class:"options-heading"},h("span",{class:"dialog-symbol"},icon("settings")),
          h("div",{},h("h2",{id:"review-options-title"},tr("Параметры проверки")),
            h("p",{},tr("Модель и сценарий для следующего запуска из находок."))),
          h("button",{class:"dialog-close",type:"button","aria-label":tr("Закрыть"),onclick:()=>dialog.close()},"×"));
        const modelField = choices.length > 1 ? reviewField("review-model-source","Модель пересмотра",model) :
          h("div",{class:"model-identity"},h("span",{},tr("Модель пересмотра")),model,source.connection ? h("small",{class:"hint"},source.connection) : null);
        const settings = h("div",{class:"options-fields"},modelField,
          mode ? reviewField("review-scenario-choice","Сценарий проверки",mode) : null);
        dialog.replaceChildren(header,h("div",{class:"options-body"},settings,mode ? study : null),
          h("footer",{class:"dialog-actions"},close));
        if (focusedModel) model?.focus();
        if (mode) showStudy();
      }
    } else quick.append(workButton("Выбрать модель", () => showTab("settings")));
  }
  panel.replaceChildren();
  if (selected) {
    controls.setAttribute("role","toolbar");
    controls.setAttribute("aria-label",tr("Действия с выбранными находками"));
    controls.setAttribute("aria-controls","review-list");
    controls.setAttribute("focusgroup","toolbar wrap");
    if (!("focusGroup" in HTMLElement.prototype)) {
      const buttons=[...controls.querySelectorAll("button:enabled")];
      const rove = active => buttons.forEach(b=>{b.tabIndex = b === active ? 0 : -1;});
      rove(buttons[0]);
      controls.addEventListener("focusin", e => { if (buttons.includes(e.target)) rove(e.target); });
      controls.addEventListener("keydown", e => {
        const i=buttons.indexOf(e.target), n=buttons.length;
        if (i<0 || !["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) return;
        const step = getComputedStyle(controls).direction === "rtl" ? -1 : 1;
        const next = {Home:0, End:n-1, ArrowRight:(i+step+n)%n, ArrowLeft:(i-step+n)%n};
        e.preventDefault(); buttons[next[e.key]].focus();
      });
    }
    panel.append(selectionHead, controls);
  }
  else quick.prepend(controls);
  quick.hidden = !data.total || !quick.childElementCount;
}
function pendingFinding(item) {
  return item.state === "open" || item.state === "pending";
}
async function maskSelectedFindings() {
  const snapshot = ReviewUI.snapshot;
  let items = [...ReviewUI.selected.values()].filter(pendingFinding);
  if (snapshot) {
    const version = ReviewUI.selectionVersion, excluded = new Set(ReviewUI.excluded);
    items = [];
    for (let offset = 0; offset < snapshot.total; offset += 250) {
      const params = new URLSearchParams({snapshot: snapshot.snapshot, status: "all", offset, limit: 250});
      const page = await api("/api/v1/ui/review?" + params);
      if (location.hash !== "#review" || version !== ReviewUI.selectionVersion || snapshot !== ReviewUI.snapshot) return;
      items.push(...page.items.filter(item => pendingFinding(item) && !excluded.has(item.key)));
      if (offset + page.items.length >= page.total) break;
    }
  }
  if (items.length) confirmReview(items, "mask");
  else $("#review-result").textContent = tr("В выборке больше нет находок на проверку.");
}
function confirmReview(items, operation) {
  const dialog = $("#review-confirm"), entries = items.map((item) => {
    const draft = reviewDraft(item);
    if (operation === "proposal" || operation === "decline-proposal") {
      const proposal = item.proposal;
      return {item, action:operation === "proposal" ? "accept-proposal" : operation, proposal_id:proposal.id,
        kind:proposal.kind, scope:proposal.scope, effect:proposal.action};
    }
    return {item, action:operation, kind:draft.kind, scope:draft.scope, effect:operation};
  });
  if (!entries.length) return;
  const origin = document.activeElement;
  dialog.replaceChildren(h("div", { class: "review-confirm-body" }, h("h2", { id: "review-confirm-title" }, tr("Проверить изменения")),
    h("p", { class: "hint" }, tr("Записей: {{0}}", [entries.length])),
    operation === "mask" ? h("p", {}, tr("Выбранные значения будут скрываться в будущих запросах.")) : null,
    h("div", { class: "review-confirm-list" }, ...entries.map((entry) => h("div", {}, h("code", {}, entry.item.value),
      h("p", {}, `${kindLabel(entry.kind)} · ${scopeLabel(entry.scope)}`), operation === "mask" ? null : h("p", { class: "hint" }, operation === "decline-proposal" ? tr("Находка останется на ручной проверке.") : decisionEffect(entry.item,entry.effect)), entry.item.validation_reason ? h("p",{class:"hint"},reviewError(entry.item.validation_reason)) : null))),
    h("div", { class: "form-actions" }, h("button", { class: "primary", id: "review-confirm-apply", onclick: () => { dialog.close(); origin?.focus(); applyReview(entries); } }, tr("Применить изменения")),
      h("button", { class: "secondary", autofocus:true, onclick: () => { dialog.close(); origin?.focus(); } }, tr("Отмена")))));
  dialog.showModal();
}
function setReviewBusy(busy) {
  ReviewUI.busy = busy;
  $("#finding-detail").inert = busy;
  $("#finding-detail").setAttribute("aria-busy", String(busy));
}
async function applyReview(entries) {
  if (ReviewUI.busy) return;
  setReviewBusy(true);
  const failures = [], snapshot = ReviewUI.snapshot;
  let applied = 0; renderReview(true);
  for (const entry of entries) {
    const item = entry.item;
    try {
      const result = await api(`/api/v1/ui/review/${item.origin}/${encodeURIComponent(item.id)}/${entry.action}`, {
        method:"POST", body:{kind:entry.kind, scope:entry.scope, proposal_id:entry.proposal_id}});
      if (result.state === "conflict") throw new Error(reviewError("existing_rule"));
      applied++; ReviewUI.selected.delete(item.key); ReviewUI.drafts.delete(item.key);
      if (snapshot && snapshot === ReviewUI.snapshot) ReviewUI.excluded.add(item.key);
    } catch (error) { failures.push(`${item.value}: ${reviewError(error.messageKey || error.message)}`); }
    if (entries.length > 1) $("#review-result").textContent = tr("Обработано {{0}} из {{1}}", [applied + failures.length, entries.length]);
  }
  if (snapshot && snapshot === ReviewUI.snapshot && pickedCount() === 0) clearPicking();
  setReviewBusy(false);
  const single = entries.length === 1 && applied === 1 ? entries[0] : null;
  const message = single?.action === "decline-proposal" ? tr("Предложение отклонено. Находка осталась на проверке.") :
    single?.action === "accept-proposal" ? tr("Предложение принято.") : tr("Применено: {{0}}", [applied]);
  $("#review-result").textContent = message + (failures.length ? " · " + failures.join("; ") : "");
  $("#review-result").classList.toggle("err", !!failures.length);
  await Promise.all([loadReview(true), loadInfo()]);
}
async function requestReview(items) {
  if(ReviewUI.busy)return;
  if(!ReviewUI.data?.sources.some(s=>s.provider!=="none")){showTab("settings");return;}
  setReviewBusy(true);renderReviewSelection();
  try {
    const selection=await selectedPayload(items);
    const body={...selection,model_source:ReviewUI.modelSource||"auto",scenario:ReviewUI.scenario||undefined};
    const job=await postOnce("/api/v1/ui/review/jobs",body);
    rememberFindings(job.id);showTab("jobs/review/"+job.id);
  } catch(e){$("#review-result").textContent=reviewError(e.messageKey||e.message);}
  finally{setReviewBusy(false);renderReviewSelection();}
}
function lockReviewResults(locked) {
  for (const id of ["review-pagination", "review-list", "review-selection", "review-quick-actions"]) $("#" + id).inert = locked;
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
    if (!data.items.length && data.total && data.offset >= data.total) {
      ReviewUI.offset = Math.floor((data.total-1)/data.limit)*data.limit;
      return loadReview();
    }
    ReviewUI.activeJobs = new Map();
    const current = new Map(data.items.map(item => [item.key, item.recommendation?.job_id]));
    for (const job of data.jobs) for (const key of job.page_keys || []) {
      if (current.has(key) && current.get(key) !== job.id && !ReviewUI.activeJobs.has(key)) ReviewUI.activeJobs.set(key,job);
    }
    ReviewUI.data=data;ReviewUI.offset=data.offset;ReviewUI.browseSet=data.snapshot;
    for(const item of data.items)if(ReviewUI.selected.has(item.key))ReviewUI.selected.set(item.key,item);
    $("#review-badge").textContent=data.pending||"";$("#review-badge").hidden=!data.pending;
    renderReview();
    lockReviewResults(false);
    $("#review-result .review-load-error")?.remove();
    return data;
  }catch(e){
    if(request===ReviewUI.request)$("#review-result").replaceChildren(h("span",{class:"review-load-error"},
      reviewError(e.messageKey||e.message), workButton("Повторить",loadReview)));
  }
  finally{if(request===ReviewUI.request)$("#review-list").setAttribute("aria-busy", "false");}
}
function localizeReviewKinds() {
  const input = $("#review-kind"), selected = input.value;
  input.replaceChildren(h("option",{value:""},tr("Все типы")), ...STANDARD_KINDS.map(kind => h("option",{value:kind},kindLabel(kind))));
  input.value = selected;
}
localizeReviewKinds();
function changedFilter(event) {
  ReviewUI.browseSet=null;clearPicking();ReviewUI.offset=0;showFinding(null);
  if (event?.type !== "input") return loadReview();
  clearTimeout(ReviewUI.filterTimer);
  ++ReviewUI.request;
  lockReviewResults(true);
  $("#review-list").setAttribute("aria-busy", "true");
  ReviewUI.filterTimer = setTimeout(loadReview, 250);
}
function resetReviewFilters() {
  for(const [key, value] of [["source","all"],["sort","recent"],["query",""],["kind",""],["action",""]])
    $("#review-"+key).value=value;
  changedFilter();
  $("#review-query").focus();
}
$("#review-clear-filters").onclick=resetReviewFilters;
for(const b of document.querySelectorAll("[data-review-view]")) b.onclick=()=>{ReviewUI.view=b.dataset.reviewView;changedFilter();};
for(const id of ["review-source","review-sort","review-kind","review-action"])$("#"+id).onchange=changedFilter;
$("#review-query").oninput=changedFilter;
$("#review-refresh").onclick=()=>{ReviewUI.browseSet=null;loadReview();};
$("#review-size").value=localStorage.getItem("kaponir-page-size")||"100";
$("#review-size").onchange=e=>{localStorage.setItem("kaponir-page-size",e.target.value);ReviewUI.offset=0;loadReview();};
$("#review-select-page").onchange=event=>{
  ++ReviewUI.selectionVersion;
  for(const item of ReviewUI.data?.items||[]) {
    if(ReviewUI.snapshot){if(event.target.checked)ReviewUI.excluded.delete(item.key);else ReviewUI.excluded.add(item.key);}
    else if(event.target.checked)ReviewUI.selected.set(item.key,item);else ReviewUI.selected.delete(item.key);
  }
  if(ReviewUI.snapshot && pickedCount()===0)clearPicking();
  renderReview();
};
$("#review-groups").onclick=e=>{ReviewUI.groups=!ReviewUI.groups;e.currentTarget.setAttribute("aria-pressed",String(ReviewUI.groups));renderReview();};
TABS.review=loadReview;
