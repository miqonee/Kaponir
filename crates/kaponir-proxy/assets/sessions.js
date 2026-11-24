"use strict";

let refreshOpenSession;
function sessionTime(stamp) {
  return time(stamp, {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
}

function sessionMessage(label, text) {
  return h("section", {class: "session-message"}, h("h3", {}, tr(label)),
    h("div", {class: "message-content"}, text || tr("Текст не сохранён.")));
}
function sessionMessages(saved) {
  const text = saved.original_request || "";
  let request = sessionMessage("Исходный запрос клиента", text), history;
  if (Number.isInteger(saved.last_user_offset)) {
    const bytes = new TextEncoder().encode(text), decoder = new TextDecoder();
    const last = decoder.decode(bytes.subarray(saved.last_user_offset));
    if (last.startsWith("[user]\n")) {
      request = sessionMessage("Сообщение клиента", last.slice("[user]\n".length));
      if (saved.last_user_offset > 0) history = h("details", {class: "session-history"},
        h("summary", {}, tr("Предыдущие сообщения в запросе")),
        h("div", {class: "message-content"}, decoder.decode(bytes.subarray(0, saved.last_user_offset)).trimEnd()));
    }
  }
  return [request, sessionMessage("Ответ, полученный клиентом", saved.restored_response), ...(history ? [history] : [])];
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
      content.replaceChildren(...sessionMessages(detail.inspector || {}));
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
      h("div", {class: "form-actions"}, scan, inspect)))));
  dialog.showModal();
  refreshOpenSession();
}
