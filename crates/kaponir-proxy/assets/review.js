"use strict";
const ReviewUI = { data: null, view: "pending", selected: new Map(), drafts: new Map(), offset: 0, busy: false,
  request: 0, selectionVersion: 0, groups: false, jobs: new Map(), modelSource: "", selectionKey: "", keys: new URLSearchParams(location.search).get("review_keys") || "" };
const reviewReasons = {
  review_unknown_kind: "В ответе модели неизвестный тип значения.",
  review_invalid_action: "В ответе модели не указано допустимое решение.",
  review_invalid_confidence: "В ответе модели отсутствует оценка уверенности от 0 до 1.",
  review_invalid_scope: "В ответе модели не указана допустимая область правила.",
  review_invalid_reason: "Обоснование решения отсутствует или превышает допустимую длину.",
  review_response_ids: "Не удалось сопоставить ответы модели с находками по идентификаторам.",
  invalid_classification_response: "Ответ модели не соответствует формату проверки.",
  human_keep: "Пользователь оставил значение открытым.",
  numeric_type_corrected: "Числовой формат исправлен на финансовое значение. Исходный тип модели сохранён в подробностях. Требуется подтверждение.",
  type_format_conflict: "Предложенный тип противоречит формату значения. Автоматическое применение запрещено.",
  type_rechecked: "Исходный тип противоречил формату значения и был перепроверен категоризатором.",
  protected_credential: "Защищённые учётные данные не передаются модели для пересмотра.",
  review_inconclusive: "Модели недостаточно данных. Выберите решение вручную или повторите пересмотр.",
  review_partial_failure: "Часть ответов модели некорректна. Остальные рекомендации сохранены; записи с ошибками можно пересмотреть отдельно.",
  review_stale: "Рекомендация устарела. Пересмотрите запись заново.",
  interrupted_review: "Пересмотр прерван перезапуском. Его можно запустить заново.",
  automatic_rule_failed: "Не удалось создать правило. Рекомендация сохранена.",
  automatic_review_stopped: "Автопроверка остановлена.",
  review_busy: "Уже выполняются задания пересмотра. Дождитесь их завершения.",
  select_up_to_64: "Выберите от 1 до 64 записей.",
  entry_not_found: "Запись больше недоступна. Обновите очередь.",
  rule_misses_occurrence: "Действующее правило пропускает выделение в этом фрагменте.",
  directory_rule_misses_occurrence: "Правило каталога пропускает этот фрагмент. Проверьте границы пути.",
  directory_rule_conflict: "Действующее правило перекрывает маску каталога.",
  rule_action_conflict: "Действующее правило задаёт другое действие. Откройте его во вкладке «Разобранные».",
  entry_not_pending: "По записи уже принято решение. Обновите очередь.",
  event_not_found: "Подробности события больше не хранятся.",
  job_not_found: "Задание пересмотра больше не хранится.",
  invalid_action: "Действие недоступно для этого состояния записи.",
  invalid_kind: "Выберите допустимый тип значения.",
  path_group_changed: "Состав группы изменился. Обновите находки и проверьте каталог ещё раз.",
  invalid_scope: "Выберите контур правила.",
};
function reviewError(code) { return tr(reviewReasons[code] || learningErrors[code] || code); }
function reviewState(item) {
  if (item.document_rule?.path_prefix && item.value.startsWith(item.document_rule.path_prefix)) return tr("Скрывается начало пути");
  if (item.state === "rejected" && item.reason === "human_keep") return tr("Оставлено открытым");
  if (item.state === "covered") return tr(item.document_rule?.action === "keep" ? "Оставляется по правилу" : "Скрывается по правилу");
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
  setReviewFeedback("");
  $("#review-advice-all").checked = true;
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
  if (!ReviewUI.drafts.has(item.key)) {
    const proposal = item.proposal?.declined ? null : item.proposal;
    const kind = proposal?.kind || item.kind;
    ReviewUI.drafts.set(item.key, {kind, scope:proposal?.scope || item.scope || item.recommended_scope || defaultScopeForKind(kind), scopeChanged:false});
  }
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
function reviewIssue(item) {
  const rec = item.recommendation;
  if (rec?.state === "failed") return rec.detail || rec.error || "review_failed";
  if (rec?.state === "needs_review" && ["type_format_conflict","protected_credential","invalid_classification_response"].includes(rec.reason)) return rec.reason;
  if (!rec && categoryStage(item) === "failed") return item.reason || "categorization_failed";
  return null;
}
function decisionStatus(item) {
  if (item.path_group) return tr("Проверить общий каталог");
  if (!pendingFinding(item)) return reviewState(item);
  const job = activeReview(item);
  if (job) return tr(job.state === "queued" ? "В очереди на проверку ИИ" : "Идёт проверка ИИ");
  if (item.proposal_conflict) return tr("Предложения ИИ различаются");
  if (item.proposal?.declined) return tr("Предложение отклонено");
  if (item.proposal) return tr(item.proposal.action === "mask" ? "ИИ предлагает скрыть" : "ИИ предлагает оставить");
  const rec = item.recommendation;
  if (rec?.state === "stale") return tr("Предложение устарело");
  const issue = reviewIssue(item);
  if (issue === "type_format_conflict") return tr("Нужно уточнить тип");
  if (issue === "protected_credential") return tr("Возможный секрет · вручную");
  if (rec?.state === "failed") return tr("Проверка не завершена");
  if (!rec && categoryStage(item) === "failed") return tr("Ошибка определения типа");
  if (rec?.state === "needs_review" && rec.reason === "configured_categorizer") return tr("Нет решения о маскировании");
  if (rec?.state === "needs_review" || item.scenario_result?.verdict === "review") return tr("ИИ просит решить вручную");
  if (categoryStage(item) === "succeeded") return tr("ИИ проверил только тип");
  if (item.origin === "learning" && item.model) return tr("ИИ нашёл значение");
  return tr("Без проверки ИИ");
}
function directoryTarget(item, action, kind) {
  return action === "mask" && kind === "PATH" ? item.account_directory : null;
}
function defaultDirectory(item) {
  const proposal = item.proposal?.declined ? null : item.proposal;
  return directoryTarget(item, proposal?.action || "mask", proposal?.kind || reviewDraft(item).kind);
}
function directoryPreview(item, prefix) {
  return h("div", {class:"directory-preview"},
    h("span", {}, tr("Каталог для правила")), h("code", {}, prefix),
    h("span", {}, tr("После маскирования")), h("code", {},
      h("mark", {class:"dialogue-mark masked"}, ReviewUI.data.path_token_example), item.value.slice(prefix.length - 1)));
}
function decisionEffect(item, action, prefix) {
  if (prefix) return tr("Правило скроет каталог учётной записи во всех путях. Имена вложенных папок и файлов останутся видны.");
  if (action === "mask") return tr("В будущих запросах это значение будет заменяться токеном.");
  if (action === "keep" && item.origin === "detector")
    return tr("Будет создано точное исключение для будущих запросов. Обязательная защита секретов сохранится.");
  if (action === "keep") return tr("Вы отметите значение как открытое в этом фрагменте. Поиск запомнит решение без создания правила.");
  if (action === "reject")
    return tr("Фоновый поиск запомнит отклонение этого значения. Правила маскирования сохранятся.");
  if (action === "dismiss") return tr("Находка перейдёт в «Разобранные». Её можно вернуть на проверку.");
  return tr("Созданное правило отключится. Сохранённые токены останутся доступны.");
}
function closeFindingAction(item) {
  return item.origin === "detector" ? "dismiss" : "reject";
}
function effectLabel(item, action, scope, prefix) {
  if (prefix) return tr("Скрывать каталог · {{0}}", [scopeLabel(scope)]);
  if (action === "keep" && item.origin === "learning") return tr("Оставить открытым");
  return tr(action === "keep" ? "Оставлять открытым · {{0}}" : "Скрывать · {{0}}", [scopeLabel(scope)]);
}
function datasetMark(item) {
  return tr(item.dataset_status === "checked" ? "Проверено в датасете" : "Черновик в датасете");
}
function pathGroupCounts(paths) {
  const plural=new Intl.PluralRules(I18N.language);
  const count=(n,labels)=>tr(labels[plural.select(n)] || labels.other,[number(n)]);
  return [count(paths.length,{one:"{{0}} путь",few:"{{0}} пути",other:"{{0}} путей"}),
    count(paths.reduce((n,p)=>n+p.count,0),{one:"{{0}} вхождение",few:"{{0}} вхождения",other:"{{0}} вхождений"})].join(" · ");
}
function pathGroupCard(item) {
  const group=item.path_group;
  const directory=h("select",{"aria-label":tr("Каталог для правила")},group.choices.map(prefix=>h("option",{value:prefix},prefix)));
  const summary=h("p",{class:"hint","aria-live":"polite"}), examples=h("ul",{class:"path-examples",role:"list"});
  const keys=()=>group.paths.filter(p=>p.value.startsWith(directory.value)).flatMap(p=>p.keys);
  const separate=workButton("Разобрать по одному",async()=>{
    const set=await api("/api/v1/ui/review/selection",{method:"POST",body:{status:"all",keys:keys().join(",")}});
    openFindingSet(set);
  },"secondary");
  const apply=workButton("Скрывать этот каталог",()=>applyReview([{item,action:"mask",prefix:directory.value,
    keys:keys(),scope:item.scope,kind:"PATH"}],true,null),"primary",!ReviewUI.data.editable);
  const explanation=h("p",{class:"hint"},tr("Это правило заменяет выбранный каталог во всех путях, включая новые."));
  function render() {
    const prefix=directory.value, paths=group.paths.filter(p=>p.value.startsWith(prefix));
    summary.textContent=pathGroupCounts(paths);
    const sample=ReviewUI.data.path_token_example;
    examples.replaceChildren(...paths.map(path=>h("li",{},h("code",{},h("mark",{class:"dialogue-mark masked"},sample),path.value.slice(prefix.length-1)),
      path.count>1 ? h("small",{class:"hint"},"×"+number(path.count)) : null)));
  }
  directory.value=group.prefix; directory.onchange=render; render();
  return h("article",{class:"panel review-card path-group-card","data-review-key":item.key},
    h("header",{class:"decision-heading"},h("div",{class:"finding-meta"},kindLabel("PATH")," / ",scopeLabel(item.scope)),
      h("h2",{tabindex:-1},tr("Общий каталог"))),
    group.choices.length>1 ? h("label",{class:"field"},tr("Каталог для правила"),directory) : h("code",{class:"review-value"},group.prefix),
    explanation,h("div",{class:"decision-actions"},apply,separate),
    h("div",{class:"path-preview-heading"},h("h3",{},tr("Пример действия правила")),summary),examples);
}

function reviewCard(item, selectedPane) {
  if (item.path_group) return pathGroupCard(item);
  const pending = pendingFinding(item), proposal = item.proposal, rec = item.recommendation;
  const ro = !ReviewUI.data.editable, currentJob = activeReview(item);
  const hasProposal = pending && proposal && !proposal.declined && !currentJob;
  const unmatched = pending && item.unmatched_rule;
  const canAsk = pending && !item.protected && reviewIssue(item) !== "protected_credential" && ReviewUI.data.sources.some(source => source.provider !== "none");
  const article = h("article", {class:"panel review-card", "data-review-key":item.key,
    "data-learning-id":item.origin === "learning" ? item.id : null});
  const heading = h("header", {class:"decision-heading"},
    h("div", {class:"finding-meta"}, findingOrigin(item), h("span", {}, kindLabel(item.kind))),
    h("h2", {class:"review-value", tabindex:-1}, item.value));
  const verdict = h("section", {class:"decision-summary", "data-tone":hasProposal ? "proposal" : "neutral"},
    h("h3", {tabindex:-1}, decisionStatus(item)));
  if (item.deferred_at) heading.append(h("small", {class:"hint"}, tr("Отложено")));
  const primary = h("div", {class:"decision-actions"});
  const target = pending && !unmatched && !item.proposal_conflict && !currentJob
    ? directoryTarget(item, hasProposal ? proposal.action : "mask", hasProposal ? proposal.kind : reviewDraft(item).kind) : null;
  if (unmatched) {
    verdict.append(h("p", {}, reviewError("rule_misses_occurrence")), ruleConflict(unmatched, item, ro));
  } else if (hasProposal) {
    const why = proposal.reason;
    verdict.append(h("p", {}, decisionEffect(item, proposal.action, target)));
    if (why && why !== "configured_categorizer") verdict.append(h("p", {class:"decision-reason"}, learningError(why)));
    verdict.append(h("p", {class:"hint"}, [modelName(proposal.model), kindLabel(proposal.kind), scopeLabel(proposal.scope)].filter(Boolean).join(" · ")));
    const accept = workButton(effectLabel(item, proposal.action, proposal.scope, target), () => decideProposal(item, true), "primary", ro);
    accept.dataset.acceptProposal = "";
    primary.append(accept);
  } else if (pending) {
    let note;
    if (currentJob) {
      note = tr("Результат появится здесь после ответа модели.");
      primary.append(workButton("Открыть задание", () => showTab("jobs/review/"+encodeURIComponent(currentJob.id)), "secondary"));
    } else if (item.proposal_conflict) note = tr("Откройте вхождения и сравните предложения по исходным фрагментам.");
    else if (proposal?.declined) note = tr("Правила не менялись. Можно принять ручное решение или запросить новое предложение.");
    else if (rec?.state === "failed" || rec?.state === "stale") note = reviewError(rec.detail || rec.error || "review_stale");
    else if (rec?.state === "needs_review" && rec.reason === "configured_categorizer")
      note = tr("Категоризатор вернул тип значения. Проверьте фрагмент и выберите действие вручную.");
    else if (rec?.state === "needs_review" || item.scenario_result?.verdict === "review")
      note = reviewError(rec?.reason || item.scenario_result?.reason || "review_inconclusive");
    else if (categoryStage(item) === "failed") note = learningError(item.reason || "categorization_failed");
    else if (item.protected) note = reviewError("protected_credential");
    else note = tr(canAsk ? "Решения о маскировании пока нет. Проверьте фрагмент или запросите предложение модели." : "Проверьте фрагмент и выберите ручное решение.");
    verdict.append(h("p", {}, note));
    if (!currentJob && reviewIssue(item) === "type_format_conflict" && canAsk) {
      primary.append(workButton("Уточнить тип моделью", () => requestReview(item, "standard"), "primary", ro));
    } else if (!item.proposal_conflict && reviewIssue(item) !== "type_format_conflict") {
      const draft = reviewDraft(item);
      primary.prepend(workButton(effectLabel(item, "mask", draft.scope, target), () =>
        item.validation_reason ? confirmReview([item], "mask", true) : applyReview([{item, action:"mask", kind:draft.kind, scope:draft.scope}], true), "primary", ro));
    }
  } else if (item.document_rule || item.state === "active" || item.state === "masked" || item.state === "kept") {
    primary.append(workButton("Открыть правило", () => openFindingRule(item), "secondary"));
  }
  if (pending) {
    const action = closeFindingAction(item);
    const skip = workButton("Пропустить", () => applyReview([{item, action}], true), "secondary", ro);
    skip.title = decisionEffect(item, action);
    primary.append(workButton("Исправить", () => manualDecision(item), "secondary", ro), skip,
      workButton("Отложить", () => applyReview([{item,action:"defer"}], true), "text-button", ro));
  }
  if (rec?.state === "failed" && ["invalid_classification_response","invalid_model_json","model_response_truncated"].includes(rec.error))
    verdict.append(h("p", {class:"hint"}, tr("При повторной проверке Kaponir отправит эту находку отдельно.")));
  if (rec?.redacted_git_revision) verdict.append(h("p", {class:"hint"}, tr("Хеш ревизии заменён на [GIT_REVISION] в примере для модели.")));
  if (rec?.type_recheck) verdict.append(h("p", {class:"hint"}, tr("Исходный тип не подходил. Для этой находки использована стандартная проверка типа и решения.")));
  if (target) verdict.append(directoryPreview(item, target));
  if (primary.childElementCount) verdict.append(primary);
  if (!pending && item.in_dataset) verdict.append(h("p", {class:"hint"}, datasetMark(item)));
  const fragment=candidateExcerpt(item.context,item.value,item.context_span);
  const prefix=item.document_rule?.path_prefix || target;
  if (prefix) {
    for (const mark of fragment.querySelectorAll("mark")) {
      if (mark.textContent.startsWith(prefix)) {
        const surface=mark.textContent, length=prefix.length-1;
        mark.textContent=surface.slice(0,length); mark.after(surface.slice(length));
      }
    }
    if (!target) verdict.append(h("code",{},prefix));
  }
  const quote = h("blockquote", {class:"candidate-fragment"}, fragment);
  const excerpt = item.path_suffix
    ? h("figure", {class:"finding-fragment"},
        h("figcaption", {}, tr("Начало пути уже скрыто. Решение касается только «{{0}}».", [item.value])), quote)
    : quote;
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
  if (hasProposal && canAsk) sourceLinks.append(workButton("Запросить повторную проверку", () => requestReview(item), "text-button", ro));
  if (sourceLinks.childElementCount || item.origin === "detector") origin.append(sourceLinks);
  if (item.origin === "detector" && /^\d+$/.test(item.id)) {
    api("/api/v1/ui/ocr/review-links/" + item.id).then(links=>{
      if (!sourceLinks.isConnected) return;
      for (const link of links) sourceLinks.append(workButton(tr("Открыть область изображения · запрос {{0}}",[link.event_id]),
        async()=>{
          await loadDashboard();
          openSession({id:link.event_id,session_id:link.session_id,turn_id:link.turn_id,imageLocation:link});
        }));
    }).catch(()=>{});
  }
  if (item.occurrence_keys?.length) heading.append(workButton(tr("Показать вхождения · {{0}}", [number(item.occurrence_keys.length)]),
    async () => {
      const snap = await api("/api/v1/ui/review/selection", {method:"POST", body:{keys:item.occurrence_keys.join(","), status:"all"}});
      openFindingSet(snap);
    }, "text-button"));
  const tabs = contentTabs("Подробности находки", [
    {key:"excerpt", title:"Фрагмент", content:excerpt}, {key:"origin", title:"Происхождение", content:origin}], selectedPane);
  const footer = h("footer", {class:"decision-footer"});
  if (hasProposal) footer.append(workButton("Отклонить предложение", () => decideProposal(item, false), "text-button", ro));
  else if (!currentJob && canAsk && reviewIssue(item) !== "type_format_conflict") {
    const issue = reviewIssue(item);
    const label = issue ? "Повторить проверку" : "Получить предложение ИИ";
    footer.append(workButton(label, () => requestReview(item), "text-button", ro));
  }
  if (item.state === "dismissed") footer.append(workButton("Вернуть на проверку", () => applyReview([{item,action:"reopen"}]), "secondary", ro));
  if (item.state === "active" || item.decision?.rule_id && ["masked","kept"].includes(item.state))
    footer.append(workButton("Отменить правило", () => confirmReview([item],"rollback"), "secondary", ro));
  if (datasetAvailable) footer.append(item.in_dataset
    ? workButton("Открыть в датасете", () => openFindingExample(item.dataset_id), "text-button")
    : workButton("В датасет", async () => collectFindings(await selectedPayload([item], true)), "text-button", ro));
  article.append(heading, tabs, verdict, footer);
  return article;
}
function reviewField(id, label, control) {
  control.id = id;
  return h("div", {class:"field"}, h("label", {for:id}, tr(label)), control);
}
function displayedExcerpt(item) {
  return ReviewUI.detailKey === item.key && $("#finding-detail [data-pane=excerpt][aria-selected=true]") &&
    $("#finding-detail .candidate-fragment mark") ? item.excerpt_id : null;
}
function manualDecision(item) {
  const seen = displayedExcerpt(item);
  const dialog = $("#review-confirm"), draft = reviewDraft(item);
  const kind = makeKindSelect(draft.kind), scope = makeScopeSelect(draft.scope);
  const kindField = reviewField("decision-kind", "Тип значения", kind);
  const scopeField = reviewField("decision-scope", "Область правила", scope);
  const dismiss = closeFindingAction(item);
  const options = [["mask","Скрывать значение"], ["keep",item.origin === "detector" ? "Создать исключение" : "Оставить открытым"],
    [dismiss, item.origin === "detector" ? "Закрыть без правила" : "Отклонить находку"]];
  const action = h("select", {autofocus:true}, options.map(([value,label]) => h("option", {value}, tr(label))));
  const target = h("select", {}, h("option", {value:"directory"}, tr("Каталог учётной записи")),
    h("option", {value:"whole"}, tr("Весь путь")));
  const targetField = reviewField("decision-target", "Что скрывать", target);
  const preview = h("div");
  const effect = h("p", {class:"decision-effect"});
  function update() {
    kindField.hidden = action.value === dismiss;
    scopeField.hidden = kindField.hidden || (item.origin === "learning" && action.value === "keep");
    const directory = directoryTarget(item, action.value, kind.value);
    targetField.hidden = !directory;
    const prefix = directory && target.value === "directory" ? directory : null;
    effect.textContent = decisionEffect(item, action.value, prefix);
    preview.replaceChildren(...(prefix ? [directoryPreview(item, prefix)] : []));
  }
  kind.onchange = () => { draft.kind = kind.value; if (!draft.scopeChanged) scope.value = draft.scope = defaultScopeForKind(kind.value); update(); };
  scope.onchange = () => { draft.scopeChanged = true; draft.scope = scope.value; };
  if (!item.proposal?.declined && item.proposal?.action === "keep") action.value = "keep";
  target.onchange = action.onchange = update; update();
  const cancel = h("button", {type:"button",class:"secondary",onclick:()=>dialog.close()}, tr("Отмена"));
  dialog.replaceChildren(h("div", {class:"review-confirm-body"}, h("h2",{id:"review-confirm-title"},tr("Ручное решение")),
    h("code", {class:"decision-value"}, item.value), reviewField("decision-action", "Действие", action),
    h("div", {class:"manual-fields"}, kindField,scopeField), targetField, effect, preview,
    h("div", {class:"dialog-actions"},cancel,h("button", {id:"review-confirm-apply",class:"primary",type:"button",onclick:()=>{
      dialog.close(); applyReview([{item, action:action.value, kind:kind.value, scope:scope.value, whole_path:!targetField.hidden && target.value === "whole"}], true, seen);
    }},tr("Применить решение")))));
  dialog.showModal();
}
function decideProposal(item, accept) {
  if (accept && ["type_format_conflict","numeric_type_corrected","type_rechecked"].includes(item.validation_reason))
    return confirmReview([item], "proposal", true);
  return applyReview([{item,action:accept ? "accept-proposal" : "decline-proposal",proposal_id:item.proposal.id}], true);
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
function reviewStatus() {
  return ReviewUI.view === "pending" ? $("#review-advice-filter input:checked").value : ReviewUI.view;
}
function reviewFilters() {
  const query = {status: reviewStatus(), source: $("#review-source").value, sort: $("#review-sort").value,
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
async function selectedPayload(items, includeOccurrences = false) {
  function explicit(rows) {
    const keys=[],prefixes={};
    for (const item of rows) {
      const prefix = item.path_group?.prefix || defaultDirectory(item);
      if (includeOccurrences && prefix) {
        for (const key of item.occurrence_keys || [item.key]) prefixes[key]=prefix;
      }
      keys.push(...(includeOccurrences ? item.occurrence_keys || [item.key] : item.path_group?.paths.map(p=>p.keys[0]) || [item.key]));
    }
    return Object.keys(prefixes).length ? {keys,prefixes} : {keys};
  }
  if (items) return explicit(items);
  if (ReviewUI.snapshot) return {snapshot:ReviewUI.snapshot.snapshot,excluded:[...ReviewUI.excluded]};
  if (ReviewUI.selected.size) return explicit(ReviewUI.selected.values());
  if(ReviewUI.data?.snapshot)return {snapshot:ReviewUI.data.snapshot};
  const snapshot = await api("/api/v1/ui/review/selection",{method:"POST",body:reviewFilters()});
  return {snapshot:snapshot.snapshot};
}
function findingSource(item) {
  if (item.path_group?.source_count>1) return tr("Источников · {{0}}",[number(item.path_group.source_count)]);
  if (item.event_id) return tr("Запрос {{0}}", [item.event_id]);
  return item.source || (item.session ? tr("Сессия · {{0}}",[item.session]) : null) || tr(item.origin === "detector" ? "Быстрый детектор" : "Фоновый анализ");
}
async function openFindingRule(item) {
  const rule=item.document_rule || item.unmatched_rule;
  const id=rule ? (rule.id || "index-"+rule.index) : item.decision?.rule_id || item.rule_id;
  const scope=rule?.scope || item.decision?.scope || item.scope;
  $("#rules-query").value = "";
  selectRuleState("all");
  chooseRuleScope(scope);
  showRuleSection("rules");
  showRulesView("list");
  await showTab("rules");
  const rowId = "rule-" + scope + "-" + id;
  const row = rule?.index == null ? document.getElementById(rowId) :
    [...$("#rules-list").querySelectorAll("[data-rule-index]")].find(row =>
      row.id === rowId && row.dataset.ruleIndex === String(rule.index));
  if (!row && rule) throw new Error(tr("rule_list_changed"));
  row?.focus();
  row?.scrollIntoView({block:"center"});
}

function reviewRuleInfo(rule) {
  return h("span", {class:"hint"}, scopeLabel(rule.scope), " · ",
    tr(rule.action === "keep" ? "Оставлять открытым" : "Маскировать"));
}

function ruleConflict(rule, finding, disabled = false) {
  return h("div", {class:"rule-conflict"},
    reviewRuleInfo(rule),
    h("code", {}, rule.value),
    workButton("Изменить правило", () => editFindingRule(rule, finding), "secondary", disabled));
}

async function editFindingRule(target, finding) {
  const sets = await api("/api/v1/rules");
  const set = sets.find(set => set.scope === target.scope);
  const rule = set?.file.rule?.[target.index];
  if (!rule || rule.disabled || (rule.id || "") !== target.id || rule.value !== target.value ||
      rule.kind !== target.kind || rule.action !== target.action) {
    await loadReview(true);
    throw new Error(tr("rule_list_changed"));
  }
  openRuleEditor(target.scope, {rule, finding, index:target.index, revision:set.revision, onSaved:() => {
    const failures = [...$("#review-error-list").children].filter(failure =>
      failure.dataset.ruleAddress === `${target.scope}/${target.index}`);
    failures.forEach(failure => failure.remove());
    if (failures.length) syncReviewFeedback();
  }});
}

const reviewErrors = $("#review-errors");
$("#review-open-errors").onclick = () => reviewErrors.showModal();
$("#review-close-errors").onclick = () => reviewErrors.close();

function setReviewFeedback(summary, failures = []) {
  $("#review-error-list").replaceChildren(...failures);
  $("#review-errors-summary").textContent = summary;
  syncReviewFeedback();
}

function syncReviewFeedback() {
  const count = $("#review-error-list").childElementCount;
  $("#review-open-errors").hidden = !count;
  if (!count && reviewErrors.open) {
    reviewErrors.close();
    $("#review-query").focus({preventScroll:true});
  }
  const summary = $("#review-errors-summary").textContent;
  const status = $("#review-result");
  status.textContent = [summary, count ? tr("Ошибок · {{0}}", [number(count)]) : ""].filter(Boolean).join(" · ");
  status.title = status.textContent;
  status.classList.remove("err");
}

function reviewFailure(item, error) {
  const {finding, rule, conflict} = error.details || {};
  const source = finding || item;
  const blocker = conflict?.rule;
  return h("div", {class:"review-failure", role:"listitem", "data-rule-address":rule ? `${rule.scope}/${rule.index}` : null},
    source.value && h("code", {}, source.value),
    h("p", {}, reviewError(error.messageKey || error.message)),
    rule && ruleConflict(rule, source),
    blocker && h("div", {class:"rule-conflict"}, reviewRuleInfo(blocker), h("code", {}, blocker.value ?? conflict.matched),
      workButton("Открыть правило", () => {
        reviewErrors.close();
        return openFindingRule({document_rule:blocker});
      }, "secondary")),
    finding && h("details", {}, h("summary", {}, tr("Фрагмент")),
      h("blockquote", {class:"candidate-fragment"}, candidateExcerpt(source.context, source.value, source.context_span))));
}

let findingNavigation = 0;
async function adjacentFinding(key, step) {
  let data = ReviewUI.data;
  const index = data.items.findIndex(item => item.key === key);
  const target = data.offset + index + step;
  if (index < 0 || target < 0 || target >= data.total) return;
  const navigation = ++findingNavigation;
  if (target < data.offset || target >= data.offset + data.items.length) {
    ReviewUI.offset = Math.floor(target / data.limit) * data.limit;
    data = await loadReview();
    if (!data || location.hash !== "#review" || navigation !== findingNavigation) return;
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
  if (moveFocus) ++findingNavigation;
  const detail=$("#finding-detail");
  const selectedPane = !moveFocus && item?.key === ReviewUI.detailKey
    ? detail.querySelector('[data-pane][aria-selected="true"]')?.dataset.pane : undefined;
  ReviewUI.detailKey=item?.key;
  detail.hidden=!item;
  detail.parentElement.classList.toggle("with-detail",!!item);
  for (const button of $("#review-list").querySelectorAll(".finding-value")) {
    const current = button.id === "finding-" + item?.key;
    button.setAttribute("aria-expanded", String(current));
  }
  if (!item) { detail.replaceChildren(); return; }
  ReviewUI.detailVersion = JSON.stringify([item, activeReview(item)?.id, activeReview(item)?.state, I18N.language, ReviewUI.data.editable]);
  const card=reviewCard(item, selectedPane);
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
  const issue = pendingFinding(item) && !activeReview(item) ? reviewIssue(item) : null;
  const rule = item.decision?.rule_id ? item.decision : item.document_rule ||
    (pendingFinding(item) && !item.proposal?.declined ? item.proposal : null);
  const withoutRule = !rule && ["dismissed","rejected"].includes(item.state);
  const scope = rule?.scope || item.scope;
  const checkbox=h("input",{type:"checkbox",checked:picked(item),"aria-label":tr("Выбрать значение: {{0}}",[item.value]),
    onchange:event=>{selectReviewItem(item,event.target.checked);row.classList.toggle("picked",picked(item));syncPageCheck();}});
  const record=h("button",{class:"finding-value",id:"finding-"+item.key,"aria-controls":"finding-detail",
    "aria-expanded":String(item.key===ReviewUI.detailKey),onclick:()=>showFinding(item)},item.value);
  const row=h("tr",{class:picked(item)?"picked":"",onclick:e=>{
    if (!e.target.closest("button,input,label,a,select") && !e.target.closest("td:first-child") && window.getSelection()?.isCollapsed) showFinding(item);
  }},h("td",{},h("label",{class:"row-check"},checkbox)),
    h("td",{},record,item.path_group ? h("small",{class:"hint"},pathGroupCounts(item.path_group.paths)) : item.occurrence_keys ? h("small",{class:"hint"},tr("Вхождений · {{0}}",[number(item.occurrence_keys.length)])) : null,h("small",{class:"mobile-source"},findingSource(item))),
    h("td",{class:"source-cell"},findingSource(item)),h("td",{},kindLabel(rule?.kind && rule.kind !== "KEEP" ? rule.kind : item.kind), " / ",
      h("span",{class:"hint"},withoutRule ? tr("Без правила") : scope ? scopeLabel(scope) : tr("Область не задана"))),
    h("td",{},h("span",{class:"event-status finding-status", "data-issue":!!issue, "data-proposal":!!item.proposal && !item.proposal.declined && pendingFinding(item)},decisionStatus(item)),issue ? h("small",{class:"hint review-issue"},reviewError(issue)) : null,item.deferred_at ? h("small",{class:"hint"},tr("Отложено")) : null,item.in_dataset?h("small",{class:"hint"},datasetMark(item)):null));
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
  $("#review-groups").disabled = !data.items.length;
  const nodes=[];
  const filters=reviewFilters();
  const filtered=!!(filters.q || filters.kind || filters.action || filters.source!=="all" || filters.sort!=="recent");
  const hasFilters = filtered || filters.status !== ReviewUI.view;
  $("#review-clear-filters").hidden = !hasFilters;
  $("#review-match-count").textContent=tr(ReviewUI.view === "pending" ? "Значений в выборке · {{0}}" : "Находок в выборке · {{0}}", [number(data.total)]);
  if (ReviewUI.keys || ReviewUI.filterSet || ReviewUI.jobId || ReviewUI.learningJob) nodes.push(h("div",{class:"inline-notice"},tr(ReviewUI.view === "all" ? "Все вхождения, включая обработанные" : ReviewUI.jobId || ReviewUI.learningJob ? "Результаты выбранного запуска" : "Находки из выбранных источников"),
    workButton("Все находки",()=>showReview())));
  function table(items, title) {
    const columns = ["Выбрать значение", "Значение", "Источник", "Тип / область правила", "Состояние"];
    return h("table", {class:"console-table findings-table"},
      h("caption", {class:"sr-only"}, title),
      h("thead", {}, h("tr", {}, columns.map((label, index) =>
        h("th", {scope:"col", class:index === 2 ? "source-cell" : null},
          index === 0 ? h("span", {class:"sr-only"}, tr(label)) : tr(label))))),
      h("tbody", {}, items.map(findingRow)));
  }
  if(!data.items.length) {
    const scoped=!!(ReviewUI.jobId || ReviewUI.learningJob || ReviewUI.keys || ReviewUI.filterSet);
    const emptyProposals = filters.status === "proposals" && !filtered;
    nodes.push(h("div",{class:"empty-state"}, icon("search"), h("strong",{},tr(emptyProposals ? "Готовых предложений ИИ пока нет" : "Находок по этим условиям нет")),
      emptyProposals ? h("p", {}, tr("Откройте находку на проверку и запросите предложение ИИ.")) : null,
      filters.q && ReviewUI.view === "pending"
        ? workButton("Искать в разобранных", () => $("#review-navigation [data-review-view=history]").click()) :
      emptyProposals ? workButton("Нужно решить", () => $("#review-navigation [data-review-view=pending]").click()) :
        hasFilters ? workButton("Сбросить фильтры", resetReviewFilters) :
        scoped || ReviewUI.view==="history" ? workButton("Открыть находки",()=>showReview()) :
          workButton("Найти значения в документе",()=>{showTab("discover");showDiscoverView("input");})));
  }
  else if(ReviewUI.groups) {
    for(const kind of new Set(data.items.map(i=>i.kind)))nodes.push(h("details",{open:true,class:"review-group"},h("summary",{},kindLabel(kind)),table(data.items.filter(i=>i.kind===kind),kindLabel(kind))));
  } else nodes.push(table(data.items,tr("Находки")));
  $("#review-list").replaceChildren(...nodes);
  $("#review-pagination").replaceChildren(workButton("Назад",()=>{ReviewUI.offset-=data.limit;loadReview(true);},"secondary",!ReviewUI.offset),
    h("span",{class:"hint"},tr("{{0}}–{{1}} из {{2}}",[data.total?data.offset+1:0,data.offset+data.items.length,data.total])),
    workButton("Далее",()=>{ReviewUI.offset+=data.limit;loadReview(true);},"secondary",data.offset+data.limit>=data.total));
  if(ReviewUI.detailKey) {
    const item=data.items.find(i=>i.key===ReviewUI.detailKey);
    if (item && ReviewUI.detailVersion !== JSON.stringify([item, activeReview(item)?.id, activeReview(item)?.state, I18N.language, data.editable])) {
      const detail = $("#finding-detail"), focused = document.activeElement;
      const focusTarget = focused?.matches(".decision-summary h3") ? ".decision-summary h3" : "h2";
      const restoreFocus = detail.contains(focused);
      showFinding(item, false);
      if (restoreFocus) detail.querySelector(focusTarget)?.focus({preventScroll:true});
    }
    else if(!item)showFinding(null,false);
  }
  syncPageCheck();renderReviewSelection();
}
function renderReviewSelection() {
  const data=ReviewUI.data, panel=$("#review-selection"), quick=$("#review-quick-actions");
  if(!data)return;
  const selected=pickedCount(), count=selected || data.total, busy=ReviewUI.busy || !info.editable;
  let modelCount=data.model_count ?? count;
  if (selected) {
    const sizes=ReviewUI.snapshot
      ? Object.entries(ReviewUI.snapshot.path_sizes || {}).filter(([key])=>!ReviewUI.excluded.has(key)).map(([,size])=>size)
      : [...ReviewUI.selected.values()].filter(item=>item.path_group).map(item=>item.path_group.paths.length);
    modelCount=selected+sizes.reduce((sum,size)=>sum+size-1,0);
  }
  panel.hidden=!selected || !data.total;
  quick.hidden=!data.total;
  const sources=(data.sources||[]).filter(s=>s.provider!=="none");
  const choices=sources.filter((s,index)=>sources.findIndex(other=>other.model===s.model && other.connection_id===s.connection_id)===index);
  const source=choices.find(s=>s.role===ReviewUI.modelSource)||choices[0];
  if(source)ReviewUI.modelSource=source.role;
  const modelLabel = source => source.provider === "local" ? modelName(source.model) : source.model || "S1";
  const model=choices.length>1?h("select",{id:"review-model-source","aria-label":tr("Модель пересмотра"),onchange:e=>{ReviewUI.modelSource=e.target.value;ReviewUI.scenario="";renderReviewSelection();}},
    ...choices.map(s=>h("option",{value:s.role,selected:s===source},modelLabel(s)))):source?h("strong",{},modelLabel(source)):null;
  const dialog = $("#review-options");
  const study = dialog.querySelector(".review-study") || h("div", {class: "scenario-study review-study"});
  const showStudy = () => renderScenarioStudy(study, measuredModelId(source.model),
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
  const manualCount = ReviewUI.snapshot ? pickedCount() : list.filter(pendingFinding).length;
  const proposalView = reviewStatus() === "proposals";
  if (ReviewUI.view !== "history" && !proposalView) {
    const label = manualCount && (!ReviewUI.snapshot || ReviewUI.view === "pending")
      ? tr("Создать правила маскирования · {{0}}", [manualCount]) : tr("Создать правила маскирования");
    if (selected) controls.append(batchButton(label, manualCount === selected ? tr("Скрыть") : label, maskSelectedFindings, "primary", busy || !manualCount));
    if (source) controls.append(batchButton(tr(selected || modelCount!==count ? "Проверить моделью · {{0}}" : "Проверить моделью все {{0}}", [modelCount]),
      tr("Проверить"), () => requestReview(), manualCount ? "secondary" : "primary", busy));
  }
  if (selected && manualCount && ReviewUI.view !== "history") {
    const skip = workButton("Пропустить", skipSelectedFindings, "secondary", busy);
    skip.title = tr("Закрыть без правила");
    controls.append(skip);
  }
  if (datasetAvailable) controls.append(batchButton(modelCount!==count ? tr("Добавить примеры в датасет") : tr(selected?"В датасет · {{0}}":"В датасет все {{0}}",[count]), tr("В датасет"),async()=>collectFindings(await selectedPayload(null, true)),"secondary",busy));
  quick.replaceChildren();
  const selectAll = $("#review-select-all");
  selectAll.replaceChildren();
  if(!ReviewUI.snapshot && data.total>selected)selectAll.append(workButton(tr("Выбрать все {{0}} по фильтру",[data.total]),async()=>{
    const version = ReviewUI.selectionVersion;
    const snap = await api("/api/v1/ui/review/selection", {method:"POST", body:reviewFilters()});
    if (location.hash !== "#review" || version !== ReviewUI.selectionVersion) return;
    clearPicking();ReviewUI.snapshot=snap;renderReview();
  }));
  if(selected)selectionHead.append(workButton("Снять выбор",()=>{clearPicking();renderReview(); $("#review-select-page").focus({preventScroll:true});}));
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
            mode ? h("p",{},tr("Модель и сценарий для следующего запуска из находок.")) : null),
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
async function skipSelectedFindings() {
  if (ReviewUI.busy || !pickedCount()) return;
  const version = ReviewUI.selectionVersion;
  const selection = await selectedPayload(null, true);
  if (location.hash !== "#review" || version !== ReviewUI.selectionVersion || ReviewUI.busy) return;

  const status = $("#review-result");
  setReviewFeedback("");
  setReviewBusy(true);
  renderReviewSelection();
  try {
    const result = await api("/api/v1/ui/review/skip", {method:"POST", body:selection});
    if (version === ReviewUI.selectionVersion) {
      clearPicking();
      ReviewUI.browseSet = null;
    }
    const failures = result.failures.map(({value, error}) => reviewFailure({value}, {messageKey:error}));
    const summary = [
      result.skipped || failures.length ? tr("Пропущено находок · {{0}}", [number(result.skipped)])
        : tr("В выборке больше нет находок на проверку."),
      result.dataset?.drafts ? tr("В датасет · проверено {{0}}, черновиков {{1}}", [0, result.dataset.drafts]) : "",
      result.dataset?.error ? tr("Решение сохранено. Добавьте пример из разобранных находок в датасете.") : "",
    ].filter(Boolean).join(" · ");
    setReviewFeedback(summary, failures);
  } catch (error) {
    status.textContent = reviewError(error.messageKey || error.message);
    status.classList.add("err");
  } finally {
    setReviewBusy(false);
    renderReviewSelection();
    if (location.hash === "#review") await Promise.all([loadReview(), loadInfo()]);
  }
}
async function maskSelectedFindings() {
  const snapshot = ReviewUI.snapshot;
  let items = [...ReviewUI.selected.values()].filter(pendingFinding);
  if (snapshot) {
    const version = ReviewUI.selectionVersion, excluded = new Set(ReviewUI.excluded);
    items = [];
    for (let offset = 0; ; offset += 250) {
      const params = new URLSearchParams({snapshot:snapshot.snapshot,status:snapshot.grouped?"selection":"all",offset,limit:250});
      const page = await api("/api/v1/ui/review?" + params);
      if (location.hash !== "#review" || version !== ReviewUI.selectionVersion || snapshot !== ReviewUI.snapshot) return;
      items.push(...page.items.filter(item => pendingFinding(item) && !excluded.has(item.key)));
      if (offset + page.items.length >= page.total) break;
    }
  }
  if (items.length) confirmReview(items, "mask");
  else setReviewFeedback(tr("В выборке больше нет находок на проверку."));
}
function confirmReview(items, operation, fromCard = false) {
  const seen = fromCard ? displayedExcerpt(items[0]) : null;
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
    h("div", { class: "review-confirm-list" }, ...entries.map((entry) => h("div", {}, h("code", {}, operation !== "decline-proposal" && directoryTarget(entry.item, entry.effect, entry.kind) || entry.item.value),
      h("p", {}, `${kindLabel(entry.kind)} · ${scopeLabel(entry.scope)}`), operation === "mask" ? null : h("p", { class: "hint" }, operation === "decline-proposal" ? tr("Находка останется на ручной проверке.") : decisionEffect(entry.item,entry.effect,directoryTarget(entry.item,entry.effect,entry.kind))), entry.item.validation_reason ? h("p",{class:"hint"},reviewError(entry.item.validation_reason)) : null))),
    h("div", { class: "form-actions" }, h("button", { class: "primary", id: "review-confirm-apply", onclick: () => { dialog.close(); origin?.focus(); applyReview(entries, fromCard, seen); } }, tr("Применить изменения")),
      h("button", { class: "secondary", autofocus:true, onclick: () => { dialog.close(); origin?.focus(); } }, tr("Отмена")))));
  dialog.showModal();
}
function setReviewBusy(busy) {
  ReviewUI.busy = busy;
  $("#finding-detail").inert = busy;
  $("#finding-detail").setAttribute("aria-busy", String(busy));
}
async function applyReview(entries, fromCard = false, seen = fromCard ? displayedExcerpt(entries[0].item) : null) {
  if (ReviewUI.busy) return;
  const page = ReviewUI.data, first = entries[0];
  const navigation = findingNavigation;
  const advance = fromCard && entries.length === 1 && ReviewUI.detailKey === first.item.key;
  const index = advance ? page.items.findIndex(i=>i.key===first.item.key) : -1;
  const following = index >= 0 ? page.items.slice(index+1).map(i=>i.key) : [];
  setReviewFeedback("");
  setReviewBusy(true);
  const failures = [], snapshot = ReviewUI.snapshot;
  let checked = 0, drafts = 0, captureError = false;
  let applied = 0, covered = 0, lastProgress = performance.now();
  renderReview(true);
  for (const entry of entries) {
    const item = entry.item;
    try {
      const result = item.path_group && entry.action==="mask" ? await api("/api/v1/ui/review/path-prefix",{method:"POST",body:{keys:entry.keys || item.occurrence_keys,prefix:entry.prefix || item.path_group.prefix,scope:item.scope}}) : await api(`/api/v1/ui/review/${item.origin}/${encodeURIComponent(item.id)}/${entry.action}`, {
        method:"POST", body:{kind:entry.kind, scope:entry.scope, proposal_id:entry.proposal_id, whole_path:entry.whole_path,
          occurrences:item.occurrence_keys, seen_excerpt:seen}});
      checked += result.dataset?.checked || 0;
      drafts += result.dataset?.drafts || 0;
      if (result.dataset?.error) captureError = true;
      if (result.state === "conflict") throw new Error(reviewError("existing_rule"));
      if (result.already_covered) covered++; else applied++;
      ReviewUI.selected.delete(item.key); ReviewUI.drafts.delete(item.key);
      if (snapshot && snapshot === ReviewUI.snapshot) ReviewUI.excluded.add(item.key);
    } catch (error) { failures.push(reviewFailure(item, error)); }
    const processed = applied + covered + failures.length;
    if (processed < entries.length && performance.now() - lastProgress >= 1000) {
      lastProgress = performance.now();
      $("#review-result").textContent = tr("Обработано {{0}} из {{1}}", [processed, entries.length]);
    }
  }
  if (snapshot && snapshot === ReviewUI.snapshot && pickedCount() === 0) clearPicking();
  setReviewBusy(false);
  const single = entries.length === 1 && applied === 1 ? entries[0] : null;
  const actionMessage = {
    defer: "Отложено до конца очереди.",
    "decline-proposal": "Предложение отклонено. Находка осталась на проверке.",
    "accept-proposal": "Предложение принято.",
    dismiss: "Закрыто без правила",
    reject: "Находка отклонена",
  }[single?.action];
  const message = actionMessage ? tr(actionMessage) : applied || !covered && !failures.length ? tr("Применено: {{0}}", [applied]) : "";
  const summary = [message, covered ? tr("Уже учтено правилами · {{0}}", [number(covered)]) : "",
    checked || drafts ? tr("В датасет · проверено {{0}}, черновиков {{1}}", [checked,drafts]) : "",
    captureError ? tr("Решение сохранено. Добавьте пример из разобранных находок в датасете.") : ""].filter(Boolean).join(" · ");
  setReviewFeedback(summary, failures);
  const sameCard = advance && !failures.length && ReviewUI.detailKey === first.item.key;
  let [updated] = await Promise.all([loadReview(true), loadInfo()]);
  if (!sameCard || !updated || navigation !== findingNavigation || location.hash !== "#review" || (ReviewUI.detailKey && ReviewUI.detailKey !== first.item.key)) return;
  const key = first.item.key;
  let next = following.map(id=>updated.items.find(i=>i.key===id)).find(Boolean);
  if (!next && updated.total) {
    const retained = updated.items.findIndex(i=>i.key===key);
    let target = Math.min(page.offset + index, updated.total - 1);
    if (retained >= 0) target = (updated.offset + retained + 1) % updated.total;
    if (target < updated.offset || target >= updated.offset + updated.items.length) {
      ReviewUI.offset = Math.floor(target / updated.limit) * updated.limit;
      updated = await loadReview();
    }
    if (!updated || navigation !== findingNavigation || location.hash !== "#review") return;
    next = updated.items[target - updated.offset];
  }
  if (next && next.key !== key && location.hash === "#review") {
    showFinding(next,false);
    $("#finding-detail h2").focus({preventScroll:true});
    document.getElementById("finding-"+next.key)?.scrollIntoView({block:"nearest"});
  } else if (!updated?.total) {
    $("#review-result").tabIndex = -1;
    $("#review-result").focus({preventScroll:true});
  }
}
async function requestReview(item, scenario) {
  if(ReviewUI.busy)return;
  if(!ReviewUI.data?.sources.some(s=>s.provider!=="none")){showTab("settings");return;}
  setReviewBusy(true);renderReviewSelection();
  try {
    const selection=await selectedPayload(item ? [item] : undefined);
    const modelSource = ReviewUI.modelSource || "auto";
    const source = ReviewUI.data.sources.find(s => s.role === modelSource);
    const body = {...selection, model_source: modelSource};
    if (source?.supports_scenarios) body.scenario = scenario || ReviewUI.scenario || undefined;
    const job=await postOnce("/api/v1/ui/review/jobs",body);
    rememberFindings(job.id);
    if (!item) {
      showTab("jobs/review/"+job.id);
      return;
    }
  } catch(e){
    setReviewFeedback(reviewError(e.messageKey||e.message));
    return;
  }
  finally{setReviewBusy(false);renderReviewSelection();}
  // A short review may finish before the queue poll sees it.
  desk.reviewProgress = null;
  if (location.hash !== "#review") return;
  void refreshJobs();
  const updated = await loadReview({background:true});
  if (updated && location.hash === "#review" && ReviewUI.detailKey === item.key && document.activeElement === document.body)
    $("#finding-detail .decision-summary h3")?.focus({preventScroll:true});
}
function lockReviewResults(locked) {
  for (const id of ["review-pagination", "review-list", "review-selection", "review-quick-actions", "review-select-all"]) $("#" + id).inert = locked;
  $("#review-select-page").disabled = locked || !ReviewUI.data?.items.length;
}
async function loadReview(options) {
  clearTimeout(ReviewUI.filterTimer);
  const preferenceVersion = captureVersion;
  const request=++ReviewUI.request;
  const background = options?.background === true;
  for (const button of document.querySelectorAll("[data-review-view]"))
    button.setAttribute("aria-pressed", String(button.dataset.reviewView === ReviewUI.view));
  $("#review-advice-filter").hidden = ReviewUI.view !== "pending";
  if (!background) lockReviewResults(true);
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
    for (const job of data.jobs) for (const key of job.page_keys || []) {
      if (!ReviewUI.activeJobs.has(key)) ReviewUI.activeJobs.set(key,job);
    }
    if (preferenceVersion === captureVersion) syncCapturePreference(data.capture_review, data.editable);
    ReviewUI.data=data;ReviewUI.offset=data.offset;ReviewUI.browseSet=data.snapshot;
    for(const item of data.items)if(ReviewUI.selected.has(item.key))ReviewUI.selected.set(item.key,item);
    $("#review-badge").textContent=data.pending||"";$("#review-badge").hidden=!data.pending;
    const focus = background ? document.activeElement : null;
    const rowId = focus?.closest("#review-list tr")?.querySelector(".finding-value")?.id;
    renderReview();
    lockReviewResults(false);
    if (rowId) {
      const value = document.getElementById(rowId);
      (focus.matches("input") ? value?.closest("tr")?.querySelector("input") : value)?.focus({preventScroll:true});
    }
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
  $("#review-advice-all").checked = true;
  for(const [key, value] of [["source","all"],["sort","recent"],["query",""],["kind",""],["action",""]])
    $("#review-"+key).value=value;
  changedFilter();
  $("#review-query").focus();
}
$("#review-actions-toggle").onclick = event => {
  const actions = $("#review-actions");
  actions.hidden = !actions.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!actions.hidden));
};
$("#review-advice-filter").onchange = changedFilter;
$("#review-clear-filters").onclick=resetReviewFilters;
for (const button of document.querySelectorAll("[data-review-view]")) button.onclick = () => {
  ReviewUI.view = button.dataset.reviewView;
  $("#review-advice-all").checked = true;
  changedFilter();
};
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
