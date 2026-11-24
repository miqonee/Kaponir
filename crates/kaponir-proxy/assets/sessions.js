"use strict";

let refreshOpenSession;
function sessionTime(stamp) {
  return time(stamp, {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
}

function sessionMaskRanges(original, transmitted, items) {
  const tokens=new Map();
  for (const item of items || []) {
    if (!item.original || !item.token || item.original===item.token) continue;
    const group=tokens.get(item.token[0]) || [];
    if (!group.some(i=>i.token===item.token && i.original===item.original)) group.push(item);
    tokens.set(item.token[0],group);
  }
  for (const group of tokens.values()) group.sort((a,b)=>b.token.length-a.token.length);
  const spans=[];
  let o=0,t=0;
  while (o<original.length && t<transmitted.length) {
    const match=tokens.get(transmitted[t])?.find(i=>transmitted.startsWith(i.token,t) && original.startsWith(i.original,o) && !original.startsWith(i.token,o));
    if (match) {
      spans.push({start:o,end:o+match.original.length,masked:true}); o+=match.original.length;t+=match.token.length;
    } else if(original[o]===transmitted[t]) {o++;t++;}
    else break;
  }
  return spans;
}
function sessionReviewRanges(text, refs) {
  const spans = [];
  // The detector replaces each line break with one space in its saved context window.
  const flat = text.replace(/[\r\n]/g," ");
  for (const ref of refs || []) {
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
    if (match !== undefined) spans.push({start:match,end:match+value.length,review:true});
  }
  return spans;
}
function markedDialogue(text, ranges, base=0) {
  const end=base+text.length, inside=ranges.filter(r=>r.start>=base && r.end<=end);
  const edges=[...new Set([base,end,...inside.flatMap(r=>[r.start,r.end])])].sort((a,b)=>a-b);
  const result=document.createDocumentFragment();
  for(let i=1;i<edges.length;i++) {
    const start=edges[i-1], stop=edges[i], value=text.slice(start-base,stop-base);
    const covers=inside.filter(r=>r.start<=start && r.end>=stop);
    if(!covers.length) {result.append(value);continue;}
    const masked=covers.some(r=>r.masked),review=covers.some(r=>r.review);
    const label=tr(masked && review ? "Скрыто · спорное решение" : masked ? "Скрыто при передаче" : "Спорное решение");
    result.append(h("mark",{class:"dialogue-mark"+(masked?" masked":"")+(review?" uncertain":""),title:label,"aria-description":label},value));
  }
  return result;
}
function sessionMessage(label, text, ranges, base=0, reply=false) {
  return h("section", {class: reply ? "session-message reply" : "session-message"}, h("h3", {}, tr(label)),
    h("div", {class: "message-content"}, text ? markedDialogue(text,ranges,base) : tr("Текст не сохранён.")));
}
function dialogueLegend(ranges) {
  if (!ranges.length) return null;
  return h("div",{class:"dialogue-legend"},
    ranges.some(r=>r.masked) ? h("span",{class:"dialogue-key masked"},tr("Скрыто при передаче")) : null,
    ranges.some(r=>r.review) ? h("span",{class:"dialogue-key uncertain"},tr("Спорное решение")) : null);
}
function sessionMessages(event) {
  const saved=event.inspector || {}, usage=event.usage || {};
  const text=saved.original_request || "",response=saved.restored_response || "";
  const requestRanges=[...sessionMaskRanges(text,saved.masked_request || "",usage.masked_items),...sessionReviewRanges(text,usage.review_items)];
  const replyRanges=sessionMaskRanges(response,saved.raw_response || "",usage.masked_items);
  let request = sessionMessage("Исходный запрос клиента",text,requestRanges), history, visibleRanges=requestRanges;
  const offset=Number.isInteger(saved.last_user_offset) ? byteMap(text)(saved.last_user_offset) : null;
  if (offset!=null && text.startsWith("[user]\n",offset)) {
    const start=offset+"[user]\n".length;
    request=sessionMessage("Сообщение клиента",text.slice(start),requestRanges,start);
    visibleRanges=requestRanges.filter(r=>r.start>=start);
    if(offset>0) {
      const earlier=requestRanges.filter(r=>r.end<=offset);
      const count=new Set(earlier.map(r=>r.start+"-"+r.end)).size;
      history=h("details",{class:"session-history"},
        h("summary",{},tr("Предыдущие сообщения в запросе"),count ? " · "+tr("Отмечено вхождений · {{0}}",[number(count)]) : ""),
        dialogueLegend(earlier),h("div",{class:"message-content"},markedDialogue(text.slice(0,offset).trimEnd(),earlier)));
    }
  }
  const legend=dialogueLegend([...visibleRanges,...replyRanges]);
  return [legend,request,sessionMessage("Ответ, полученный клиентом",response,replyRanges,0,true),history].filter(Boolean);
}

function openSession(event) {
  const dialog = $("#session-dialog");
  const target = event.session_id ? {session: event.session_id} : {request: event.id};
  const title = event.session_id ? tr("Сессия {{0}}", [event.session_id]) : tr("Запрос {{0}}", [event.id]);
  const pick = h("select", {"aria-label": tr("Запрос в переписке")});
  const requestList = h("fieldset", {class:"conversation-requests"}, h("legend", {}, tr("Запросы сессии")));
  const position = h("span", {class:"conversation-position"});
  const content = h("div", {class: "session-messages", "aria-live": "polite"});
  const status = h("p", {class: "hint"});
  let events = [], currentId = event.id, generation = 0, signature = "";
  const previous = h("button", {class: "secondary", onclick: () => display(events[pick.selectedIndex - 1])}, tr("Предыдущий запрос"));
  const next = h("button", {class: "secondary", onclick: () => display(events[pick.selectedIndex + 1])}, tr("Следующий запрос"));
  const inspect = workButton("Подробности запроса", () => {
    dialog.close();
    return openInspector(currentId);
  }, "text-button");
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

  function navigation() {
    previous.disabled = pick.selectedIndex <= 0;
    next.disabled = pick.selectedIndex < 0 || pick.selectedIndex >= events.length - 1;
    position.textContent = pick.selectedIndex < 0 ? "" : tr("{{0}} из {{1}}", [pick.selectedIndex + 1, events.length]);
    const selected = requestList.querySelector('input[value="'+currentId+'"]');
    if (selected) {
      selected.checked = true;
      const label = selected.closest("label");
      if (requestList.offsetHeight && (label.offsetTop < requestList.scrollTop ||
          label.offsetTop + label.offsetHeight > requestList.scrollTop + requestList.clientHeight)) {
        requestList.scrollTop = label.offsetTop - requestList.clientHeight / 2;
      }
    }
  }
  async function display(row) {
    if (!row) return;
    currentId = row.id; pick.value = String(row.id); navigation();
    const version = ++generation;
    status.textContent = [row.model || row.provider, sessionTime(row.started_at_ms || row.time_ms), outcomeLabel(row)].filter(Boolean).join(" · ");
    content.replaceChildren();
    if (row.outcome === "in_progress") {
      content.textContent = tr("Запрос выполняется. Переписка появится после ответа.");
      return;
    }
    content.textContent = tr("Загрузка…");
    try {
      const detail = await api("/api/v1/ui/events/" + row.id);
      if (!dialog.open || version !== generation) return;
      content.replaceChildren(...sessionMessages(detail));
    } catch (error) {
      if (dialog.open && version === generation)
        content.replaceChildren(h("p", {}, error.message), workButton("Повторить", () => display(row)));
    }
  }
  pick.onchange = () => display(events[pick.selectedIndex]);
  refreshOpenSession = () => {
    const traffic = dashboard?.traffic || {};
    const updated = [...(traffic.events || []), ...(traffic.active || [])]
      .filter(row => target.session ? row.session_id === target.session : row.id === target.request)
      .sort((a,b) => (a.started_at_ms || a.time_ms) - (b.started_at_ms || b.time_ms) || a.id - b.id);
    const key = JSON.stringify(updated.map(row => [row.id, row.outcome, row.status, row.review_reasons]));
    if (signature === key) return;
    signature = key;
    const was = events.find(row => row.id === currentId);
    events = updated;
    const focusId = requestList.contains(document.activeElement) ? document.activeElement.value : null;
    requestList.replaceChildren(h("legend", {}, tr("Запросы сессии")), ...events.map(row => {
      const reasons = requestNotes(row);
      return h("label", {}, h("input", {type:"radio", name:"conversation-request", value:row.id, onchange:()=>display(row)}),
        h("span", {}, h("strong", {}, tr("Запрос {{0}}", [row.id])),
          h("small", {}, sessionTime(row.started_at_ms || row.time_ms)),
          h("small", {class: reasons.length ? "request-reasons" : ""}, reasons.join(" · ") || outcomeLabel(row))));
    }));
    if (focusId) requestList.querySelector('input[value="'+focusId+'"]')?.focus({preventScroll:true});
    dialog.classList.toggle("single-request", events.length <= 1);
    pick.replaceChildren(...events.map((row, index) => h("option", {value: row.id},
      tr("{{0}} из {{1}} · запрос {{2}}", [index + 1, events.length, row.id]) + " · " + sessionTime(row.started_at_ms || row.time_ms))));
    const selected = events.find(row => row.id === currentId) || events.at(-1);
    scan.disabled = !events.some(row => row.outcome !== "in_progress");
    inspect.disabled = !selected;
    if (!selected) {
      generation++; status.textContent = ""; content.textContent = tr("В истории больше нет запросов этой сессии."); navigation(); return;
    }
    pick.value = String(selected.id);
    navigation();
    if (selected.id !== was?.id || selected.outcome !== was?.outcome) display(selected);
  };
  const close = h("button", {class: "icon-button", "aria-label": tr("Закрыть"), onclick: () => dialog.close()}, "×");
  dialog.onclose = () => { generation++; refreshOpenSession = null; dialog.replaceChildren(); };
  dialog.replaceChildren(h("div", {class: "inspector-shell"},
    h("div", {class: "inspector-header"}, h("div", {}, h("h2", {id: "session-title"}, tr("Переписка")), h("p", {}, title)), close),
    h("div", {class: "conversation-layout"}, requestList, h("div", {class: "inspector-body"}, h("div", {class: "session-navigation"},
      h("label", {class: "field"}, tr("Запрос в переписке"), pick), position, previous, next), status, content,
      h("div", {class: "form-actions"}, scan, modelRun, inspect),destination))));
  dialog.showModal();
  refreshOpenSession();
}
