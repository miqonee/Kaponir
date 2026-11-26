"use strict";

let refreshOpenSession;
function sessionTime(stamp) {
  return time(stamp, {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
}

function sessionMaskRanges(original, transmitted, items) {
  const tokens=new Map();
  for (const item of items || []) {
    if (!item.token || !(item.original || item.restored_utf16 > 0) || item.original===item.token) continue;
    const group=tokens.get(item.token[0]) || [];
    if (!group.some(i=>i.token===item.token && i.original===item.original && i.restored_utf16===item.restored_utf16)) group.push(item);
    tokens.set(item.token[0],group);
  }
  for (const group of tokens.values()) group.sort((a,b)=>b.token.length-a.token.length);
  const spans=[];
  let o=0,t=0;
  while (o<original.length && t<transmitted.length) {
    const match=tokens.get(transmitted[t])?.find(item=>{
      if (!transmitted.startsWith(item.token,t) || original.startsWith(item.token,o)) return false;
      return item.original ? original.startsWith(item.original,o) : o+item.restored_utf16<=original.length;
    });
    if (match) {
      const length=match.original?.length ?? match.restored_utf16;
      spans.push({start:o,end:o+length,otherStart:t,otherEnd:t+match.token.length,masked:true});
      o+=length; t+=match.token.length;
    } else if(original[o]===transmitted[t]) {o++;t++;}
    else break;
  }
  return spans;
}
function sessionReplyRanges(saved, usage) {
  return sessionMaskRanges(saved.restored_response || "", saved.raw_response || "",
    [...(usage.restore_marks || []), ...(usage.masked_items || [])]);
}
function sessionRequestRanges(saved, items) {
  const text = saved.original_request || "", wire = saved.masked_request || "";
  const ranges = sessionMaskRanges(text, wire, items);
  if (saved.user_span && saved.masked_user_span) {
    // A gateway-added system hint can shift everything after the system prompt.
    const start = byteMap(text)(saved.user_span[0]), other = byteMap(wire)(saved.masked_user_span[0]);
    const tail = sessionMaskRanges(text.slice(start), wire.slice(other), items);
    return [...ranges.filter(r => r.start < start), ...tail.map(r => ({...r,
      start:r.start+start, end:r.end+start, otherStart:r.otherStart+other, otherEnd:r.otherEnd+other}))];
  }
  return ranges;
}
function sessionReviewRanges(text, refs) {
  const spans = [];
  // The detector replaces each line break with one space in its saved context window.
  const flat = text.replace(/[\r\n]/g," ");
  for (const [refIndex, ref] of (refs || []).entries()) {
    if (!ref.value) continue;
    const value = ref.value, context = ref.context || "", range = ref.context_span;
    let left = "", right = "";
    if (Array.isArray(range) && range.length === 2) {
      const map = byteMap(context), start = map(range[0]), end = map(range[1]);
      if (context.slice(start,end) !== value.replace(/[\r\n]/g," ")) continue;
      // Neighboring secrets have already been replaced by type labels in the journal.
      left = context.slice(0,start).split(/\[[A-Z0-9_]+\]/).at(-1);
      right = context.slice(end).split(/\[[A-Z0-9_]+\]/)[0];
    }
    let match, pos = 0;
    while ((pos = text.indexOf(value,pos)) >= 0) {
      if (flat.endsWith(left,pos) && flat.startsWith(right,pos+value.length)) {
        if (match !== undefined) { match = undefined; break; }
        match = pos;
      }
      pos += value.length;
    }
    if (match !== undefined) spans.push({start:match,end:match+value.length,review:true,refIndex});
  }
  return spans;
}
function markedDialogue(text, ranges, base=0, openReview=null) {
  const end=base+text.length, inside=ranges.filter(r=>r.start>=base && r.end<=end);
  const edges=[...new Set([base,end,...inside.flatMap(r=>[r.start,r.end])])].sort((a,b)=>a-b);
  const result=document.createDocumentFragment();
  for(let i=1;i<edges.length;i++) {
    const start=edges[i-1], stop=edges[i], value=text.slice(start-base,stop-base);
    const covers=inside.filter(r=>r.start<=start && r.end>=stop);
    if(!covers.length) {result.append(value);continue;}
    const masked=covers.some(r=>r.masked),review=covers.some(r=>r.review);
    const label=tr(masked && review ? "Скрыто · спорное решение" : review ? "Спорное решение" : (covers[0].label || "Скрыто при передаче"));
    const ref=covers.find(r=>r.review);
    const attrs={class:"dialogue-mark"+(masked?" masked":"")+(review?" uncertain":""),title:label,"aria-description":label};
    const target=ref && openReview ? h("button",{type:"button",class:"inline-decision","data-ref-index":ref.refIndex,
      "aria-label":tr("Открыть решение по значению {{0}}",[value]),onclick:()=>openReview(ref.refIndex)},value) : value;
    result.append(h("mark",attrs,target));
  }
  return result;
}
function sessionMessage(label, text, ranges, base=0, reply=false, openReview=null) {
  return h("section", {class: reply ? "session-message reply" : "session-message"}, h("h3", {}, tr(label)),
    h("div", {class: "message-content"}, text ? markedDialogue(text,ranges,base,openReview) : tr("Текст не сохранён.")));
}
function dialogueLegend(ranges) {
  if (!ranges.length) return null;
  return h("div",{class:"dialogue-legend"},
    ranges.some(r=>r.masked) ? h("span",{class:"dialogue-key masked"},tr("Скрыто при передаче")) : null,
    ranges.some(r=>r.review) ? h("span",{class:"dialogue-key uncertain"},tr("Спорное решение")) : null);
}
function sessionMessages(event, openReview=null, jump=null, questionEvent=null) {
  const saved=event.inspector || {}, usage=event.usage || {};
  const question=questionEvent?.inspector || saved, questionUsage=questionEvent?.usage || usage;
  const excerpt=question.turn_excerpt;
  const text=excerpt?.original ?? question.original_request ?? "",response=saved.restored_response || "";
  const masks=excerpt ? sessionMaskRanges(text,excerpt.masked,questionUsage.masked_items) : sessionRequestRanges(question,questionUsage.masked_items);
  const requestRanges=[...masks,...sessionReviewRanges(text,questionUsage.review_items)];
  const replyRanges=sessionReplyRanges(saved,usage);
  let history;
  const map=byteMap(text);
  const offset=Number.isInteger(question.last_user_offset) ? map(question.last_user_offset) : null;
  const header="[user]\n";
  const span=excerpt ? [0,text.length] : question.user_span?.map(map) || (offset!=null && text.startsWith(header,offset) &&
    (!event.turn || questionEvent?.turn?.step==="user") ? [offset+header.length,text.length] : null);
  let visibleRanges=requestRanges, request;
  if (span) {
    const [start,end]=span, before=start-header.length;
    visibleRanges=requestRanges.filter(r=>r.start>=start && r.end<=end);
    request=sessionMessage("Сообщение клиента",text.slice(start,end),visibleRanges,start,false,openReview);
    if (before>0 || end<text.length) {
      const other=requestRanges.filter(r=>r.end<=before || r.start>=end);
      const count=new Set(other.map(r=>r.start+"-"+r.end)).size;
      history=h("details",{class:"session-history"},
        h("summary",{},tr(end===text.length ? "Предыдущие сообщения в запросе" : "Контекст этого запроса"),
          count ? " · "+tr("Отмечено вхождений · {{0}}",[number(count)]) : ""),dialogueLegend(other),
        h("div",{class:"message-content"},
          markedDialogue(text.slice(0,Math.max(0,before)),other,0,openReview),
          markedDialogue(text.slice(end),other,end,openReview)));
    }
  } else {
    visibleRanges=event.turn ? [] : requestRanges;
    request=event.turn ? sessionMessage("Сообщение клиента","",[]) :
      sessionMessage("Исходный запрос клиента",text,visibleRanges,0,false,openReview);
  }
  const legend=dialogueLegend([...visibleRanges,...replyRanges]);
  const disputed=questionUsage.review_items || [];
  const links=jump && disputed.length ? h("div",{class:"disputed-links"},
    ...disputed.map((ref,index)=>h("button",{type:"button",class:"text-button",onclick:()=>jump(index)},
      tr("Спорное решение детектора")," · ",ref.value,disputed.length>1 ? " ("+(index+1)+"/"+disputed.length+")" : ""))) : null;
  const reply=event.outcome==="in_progress" ? h("p",{class:"hint"},tr("Ожидаем ответ поставщика…")) :
    event.request_kind==="count_tokens" ? null : sessionMessage("Ответ, полученный клиентом",response,replyRanges,0,true);
  return [links,legend,request,reply,history].filter(Boolean);
}

function conversationTurns(rows) {
  const groups=[], latest=new Map();
  let messages=0;
  for (const row of rows) {
    const anchor=row.turn?.id;
    const submission=row.turn?.step==="user" && row.request_kind!=="count_tokens";
    let group=latest.get(anchor);
    // Two standalone submissions of identical text are indistinguishable from
    // a retry. Retain both roots instead of silently hiding a user message.
    if (!anchor || !group || (submission && group.submitted)) {
      group={key:"turn-"+row.id, ordinal:anchor ? ++messages : null, linked:!!anchor, rows:[]};
      groups.push(group);
      if (anchor) latest.set(anchor,group);
    }
    if (submission) group.submitted=true;
    group.rows.push(row);
  }
  return groups;
}

function conversationStep(row) {
  if (row.request_kind==="count_tokens") return tr("Подсчёт токенов");
  return tr(({tool:"Результат инструмента",user:"Сообщение клиента",continuation:"Продолжение ответа"})[row.turn?.step] || "Запрос");
}

function openSession(event) {
  const dialog = $("#session-dialog");
  const target = event.session_id ? {session: event.session_id} : {request: event.id};
  const title = event.session_id ? tr("Сессия {{0}}", [event.session_id]) : tr("Запрос {{0}}", [event.id]);
  const pick = h("select", {"aria-label": tr("Сообщения пользователя")});
  const requestList = h("fieldset", {class:"conversation-requests"}, h("legend", {}, tr("Сообщения пользователя")));
  const position = h("span", {class:"conversation-position"});
  const content = h("div", {class: "session-messages", "aria-live": "polite"});
  const status = h("p", {class: "hint"});
  const steps = h("details", {class:"session-steps"});
  const totals = h("span", {class:"conversation-totals"});
  let turns = [], currentKey = null, generation = 0, signature = "", selection = "";
  const previous = h("button", {class:"secondary", onclick:()=>display(turns[pick.selectedIndex-1])}, tr("Предыдущее сообщение"));
  const next = h("button", {class:"secondary", onclick:()=>display(turns[pick.selectedIndex+1])}, tr("Следующее сообщение"));
  let selectedRequest;
  const inspect=workButton("Подробности запроса",()=>{
    dialog.close();
    return openInspector(selectedRequest);
  },"text-button");
  const scan = workButton(event.session_id ? "Найти значения в этой сессии" : "Найти значения в этом запросе", () => {
    if (D.busy) { toast(tr("Дождитесь окончания текущего поиска.")); return; }
    dialog.close();
    return analyzeProxySessions(target);
  }, "primary");

  let modelConfig;
  const modelRun=workButton("Анализировать переписку моделью",async()=>{
    if (!modelConfig?.analysis_enabled) { dialog.close(); showTab("analysis"); return; }
    const data=await api("/api/v1/ui/analysis/session",{method:"POST",body:{target,revision:modelConfig.analysis.revision}});
    const job=data.ids[0] || data.existing_ids[0];
    if (job) {dialog.close();showTab("jobs/learning/"+encodeURIComponent(job));}
    if(data.partial) toast(tr("Доступна часть переписки. Запросов с сохранённым текстом · {{0}} из {{1}}.",[data.used,data.total]));
  },"secondary");
  modelRun.disabled=true;
  const destination=h("p",{class:"hint"});
  api("/api/v1/ui/workspace/model").then(cfg=>{
    if(!dialog.open || !modelRun.isConnected)return;
    modelConfig=cfg;modelRun.disabled=false;
    if(cfg.analysis_enabled && cfg.analysis) {
      const destinations=[cfg.analysis,cfg.categorization].filter(Boolean).map(s=>[modelName(s.model),s.connection].filter(Boolean).join(" · "));
      destination.textContent=tr("Анализ сохранённой переписки · {{0}}",[[...new Set(destinations)].join(" / ")]);
    } else modelRun.textContent=tr("Настроить модель для переписки");
  }).catch(()=>{ modelRun.hidden=true; });

  function titleOf(group) {
    return group.linked ? tr("Сообщение {{0}}", [group.ordinal]) : tr("Запрос {{0}}", [group.rows[0].id]);
  }
  function stepList(group) {
    const focused = steps.contains(document.activeElement) ? document.activeElement.dataset.step : null;
    const hasMasks=group.rows.some(r=>r.usage?.replacements || r.usage?.cached_replacements);
    steps.replaceChildren(h("summary",{},icon("chevron"),tr("Шаги обработки · {{0}}",[group.rows.length]),
      hasMasks ? h("span",{class:"steps-mask-label"},tr("Есть скрытые значения")) : null),
      h("ol", {}, group.rows.map(row=>{
        const notes=requestNotes(row);
        const masks=(row.usage?.replacements || 0)+(row.usage?.cached_replacements || 0);
        const inspect=()=>openInspector(row.id,"request");
        return h("li",{},h("div",{},h("strong",{},conversationStep(row)),
          h("small",{},[sessionTime(row.started_at_ms || row.time_ms),outcomeLabel(row),
            masks ? tr("Скрыто при передаче · {{0}}",[masks]) : ""].filter(Boolean).join(" · ")),
          notes.length ? h("button",{type:"button",class:"text-button","data-step":"review-"+row.id,
            onclick:()=>openInspector(row.id,"review")},notes.join(" · ")) : null),
          h("button",{class:"secondary small",type:"button","data-step":row.id,onclick:inspect},tr("Запрос {{0}}",[row.id])));
      })));
    if (focused) steps.querySelector('[data-step="'+focused+'"]')?.focus({preventScroll:true});
  }
  async function display(group, issueRow=null) {
    if (!group) return;
    // Token counts are service requests and do not contain an assistant answer.
    const row=issueRow || group.rows.findLast(r=>r.request_kind!=="count_tokens") || group.rows.at(-1);
    selectedRequest=row.id;
    inspect.hidden=group.rows.length!==1;
    const changed=group.key!==currentKey;
    currentKey=group.key;
    pick.value=group.key;
    previous.disabled=pick.selectedIndex<=0;
    next.disabled=pick.selectedIndex>=turns.length-1;
    position.textContent=tr("{{0}} из {{1}}", [pick.selectedIndex+1,turns.length]);
    const radio=requestList.querySelector('input[value="'+group.key+'"]');
    if (radio) radio.checked=true;
    if (changed) steps.open=false;
    const detailKey=JSON.stringify([group.key,row.id,row.outcome,group.rows.map(r=>[r.id,r.outcome,r.review_reasons,r.usage?.replacements,r.usage?.cached_replacements])]);
    if (selection===detailKey && !issueRow) return;
    selection=detailKey;
    stepList(group);
    const version=++generation;
    status.textContent=[row.model || row.provider,sessionTime(row.started_at_ms || row.time_ms),outcomeLabel(row)].filter(Boolean).join(" · ");
    content.textContent=tr("Загрузка…");
    try {
      const detail=await api("/api/v1/ui/events/"+row.id);
      let question=detail;
      if (!issueRow && group.linked && !detail.inspector?.user_span && !detail.inspector?.turn_excerpt) {
        const first=group.rows.find(r=>r.turn?.step==="user" && r.id!==row.id && r.outcome!=="in_progress");
        if (first) question=await api("/api/v1/ui/events/"+first.id);
      }
      if (!dialog.open || version!==generation) return;
      const inspectRef=index=>openInspector(question.id,"review",index);
      function jump(index) {
        const mark=content.querySelector('.inline-decision[data-ref-index="'+index+'"]');
        if (!mark) return inspectRef(index);
        const history=mark.closest("details");
        if (history) history.open=true;
        mark.focus({preventScroll:true});
        mark.scrollIntoView({block:"center",inline:"nearest"});
      }
      content.replaceChildren(...sessionMessages(detail,inspectRef,jump,question));
      if (issueRow) {
        const links=content.querySelectorAll(".disputed-links button");
        if (links.length>1) links[0].focus();
        else if (links.length) jump(0);
        else inspectRef(detail.review_reasons?.find(r=>r.code==="legacy_uncertain")?.code);
      }
    } catch(error) {
      if (dialog.open && version===generation) content.replaceChildren(h("p",{},error.message),
        workButton("Повторить",()=>{selection="";return display(group);}));
    }
  }
  pick.onchange=()=>display(turns[pick.selectedIndex]);
  refreshOpenSession=()=>{
    const traffic=dashboard?.traffic || {};
    const rows=[...(traffic.events || []),...(traffic.active || [])]
      .filter(r=>target.session ? r.session_id===target.session : r.id===target.request)
      .sort((a,b)=>(a.started_at_ms || a.time_ms)-(b.started_at_ms || b.time_ms) || a.id-b.id);
    const key=JSON.stringify(rows.map(r=>[r.id,r.turn,r.outcome,r.status,r.review_reasons,r.usage?.replacements,r.usage?.cached_replacements]));
    if (signature===key) return;
    signature=key;
    const focus=requestList.contains(document.activeElement) ? document.activeElement.value : null;
    const focusIssue=document.activeElement.dataset.turnIssue;
    turns=conversationTurns(rows);
    const linked=turns.filter(g=>g.linked).length;
    totals.textContent=[linked ? tr("Сообщений пользователя · {{0}}",[linked]) : "",
      tr("Запросов · {{0}}",[rows.length])].filter(Boolean).join(" · ");
    requestList.replaceChildren(h("legend",{},tr("Переписка")), ...turns.map(g=>{
      const pending=g.rows.some(r=>r.outcome==="in_progress"), attention=g.rows.some(r=>requestNotes(r).length);
      const disputed=tr("Спорное решение детектора"), legacy=tr("Спорные решения без сохранённой связи");
      const issue=g.rows.find(r=>requestNotes(r).some(n=>n===disputed || n===legacy));
      return h("div",{class:"conversation-entry"},h("label",{},h("input",{type:"radio",name:"conversation-turn",value:g.key,onchange:()=>display(g)}),
        h("span",{},h("strong",{},titleOf(g)),h("small",{},g.linked ? tr("Шаги обработки · {{0}}",[g.rows.length]) : tr("Связь с сообщением неизвестна")),
          pending || attention ? h("small",{class:"request-reasons"},tr(pending ? "Выполняется" : "Требует проверки")) : null)),
        issue ? h("button",{type:"button",class:"text-button request-issue","data-turn-issue":g.key,
          onclick:()=>display(g,issue)},requestNotes(issue).includes(disputed) ? disputed : legacy) : null);
    }));
    if (focusIssue) requestList.querySelector('[data-turn-issue="'+focusIssue+'"]')?.focus({preventScroll:true});
    else if (focus) requestList.querySelector('input[value="'+focus+'"]')?.focus({preventScroll:true});
    pick.replaceChildren(...turns.map(g=>h("option",{value:g.key},titleOf(g))));
    dialog.classList.toggle("single-request",turns.length<=1);
    scan.disabled=!rows.some(r=>r.outcome!=="in_progress");
    if (!turns.length) {
      generation++;selection="";currentKey=null;status.textContent="";steps.hidden=true;inspect.hidden=true;
      content.textContent=tr("В истории больше нет запросов этой сессии.");return;
    }
    steps.hidden=false;
    const selected=turns.find(g=>g.key===currentKey) || turns.find(g=>g.rows.some(r=>r.id===event.id)) || turns.at(-1);
    display(selected);
  };
  const close = h("button", {class: "icon-button", "aria-label": tr("Закрыть"), onclick: () => dialog.close()}, "×");
  dialog.onclose = () => { generation++; refreshOpenSession = null; dialog.replaceChildren(); };
  dialog.replaceChildren(h("div", {class: "inspector-shell"},
    h("div", {class: "inspector-header"}, h("div", {}, h("h2", {id: "session-title"}, tr("Переписка")), h("p", {}, title," · ",totals)), close),
    h("div", {class: "conversation-layout"}, requestList, h("div", {class: "inspector-body"}, h("div", {class: "session-navigation"},
      h("label", {class: "field"}, tr("Сообщения пользователя"), pick), position, previous, next), status, content, steps,
      h("div", {class: "form-actions"}, scan, modelRun, inspect),destination))));
  dialog.showModal();
  refreshOpenSession();
}
