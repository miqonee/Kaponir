"use strict";

const imageSettings = $("#ocr-settings");
const imageLab = { file: null, status: null, running: null, result: null, fileVersion: 0 };
let imageConfig, proof, catalogVersion = 0, loadingImageSettings = 0;
let ocrInstallTimer, ocrInstallSubmitting = false, ocrInstallWarning = "";

const ocrErrors = {
  inline_required: "Приложите статический PNG, JPEG или WebP в теле запроса.",
  format: "Выберите статический PNG, JPEG или WebP.",
  invalid_image: "Файл повреждён. Экспортируйте изображение заново.",
  animated: "Сохраните один кадр в PNG, JPEG или WebP.",
  limits: "Уменьшите размер изображения или число вложений.",
  unavailable: "RapidOCR или настроенный worker недоступен. Проверьте установку и адрес узла.",
  languages: "Установите выбранные языки OCR на worker.",
  empty: "Текст не удалось уверенно прочитать. Попробуйте снимок крупнее.",
  unreadable: "Текст не удалось уверенно прочитать. Попробуйте снимок крупнее.",
  boundaries: "Не удалось определить границы чувствительного значения. Отправка остановлена.",
  timeout: "Время проверки истекло. Попробуйте меньше изображений или увеличьте тайм-аут.",
  queue_full: "Очередь проверки заполнена. Повторите, когда освободится обработчик.",
  cancelled: "Проверка отменена.",
  encoding: "Не удалось сохранить обработанное изображение.",
  rules: "Не удалось применить локальные правила.",
  storage: "Не удалось сохранить изображение в инспекторе.",
  consent: "Подтвердите точный адрес получателя в настройках OCR.",
  vision_address: "Адрес Vision API недоступен.",
  vision_images: "Vision API не обработал синтетическое изображение.",
  vision_coordinates: "Vision API не вернул пригодные координаты.",
};

function ocrError(failure, fallback = "") {
  if (failure?.code === "boundaries" && failure.field)
    return tr("Не удалось определить границы значения {{0}}. Отправка остановлена.", [failure.field]);
  return failure?.code ? tr(ocrErrors[failure.code] || fallback) : fallback;
}

function currentOcrDraft() {
  const controls = imageSettings.elements;
  const saved = imageConfig.settings;
  return Object.fromEntries(Object.entries(saved).map(([key, value]) => {
    if (key === "approved_recipient") return [key, $("#ocr-consent").checked ? draftRecipient() : ""];
    const control = controls.namedItem(key);
    return [key, typeof value === "boolean" ? control.checked :
      typeof value === "number" ? control.valueAsNumber : control.value.trim()];
  }));
}

function draftRecipient() {
  const controls = imageSettings.elements;
  if (controls.mode.value === "vision")
    return imageConfig.connections.find(c => c.id === controls.connection_id.value)?.recipient || "";
  return controls.node.value === "worker" ? imageConfig.worker || "" : "";
}

function ocrRecipientText(recipient) {
  return recipient ? tr("Исходные пиксели получит {{0}}.", [recipient]) :
    tr("Исходник обрабатывается на компьютере прокси.");
}

function ocrEngineStatus(data, draft) {
  const box = $("#ocr-engine");
  const saved = data.settings;
  const recipient = draftRecipient();
  const matches = ["mode", "node", "languages", "connection_id", "model", "approved_recipient"]
    .every(key => draft[key] === saved[key]);
  const state = matches ? data.engine_state : "pending";
  const label = tr({ready:"ocr.ready", needs_setup:"Нужна настройка", unavailable:"Недоступно", pending:"Ожидает сохранения"}[state]);
  const worker = draft.mode === "local" && draft.node === "worker";
  const rapid = draft.mode === "local" && (draft.node === "proxy" ||
    saved.mode === "local" && saved.node === draft.node && data.engine === "rapidocr");
  const engine = rapid ? "RapidOCR · PP-OCRv6 Small" : worker ? tr("Отдельный worker") : draft.model;
  const languages = rapid ? tr("Многоязычное распознавание") : draft.mode === "local" && matches && data.installed_languages.length
    ? draft.languages.split("+").map(lang => ({rus:tr("русский"),eng:tr("английский")})[lang] || lang).join(", ") : "";
  box.textContent = [label, engine, languages].filter(Boolean).join(" · ");
  box.dataset.state = state;
  let reason;
  if (worker && !recipient) {
    reason = tr("Задайте KAPONIR_WORKER_URL в окружении прокси и перезапустите его.");
  } else if (worker && draft.approved_recipient !== recipient) {
    reason = tr("Подтвердите адрес получателя и нажмите «Сохранить изменения».");
  } else if (!matches) {
    reason = tr("Сохраните параметры, чтобы применить их к прокси.");
  } else if (worker && data.failure === "unavailable") {
    reason = data.worker_managed
      ? tr("Проверьте зависимости OCR и журнал запуска worker.")
      : tr("Запустите worker и проверьте его адрес и KAPONIR_WORKER_TOKEN.");
  } else {
    reason = ocrError(data.failure ? {code:data.failure} : null);
  }
  const installed = !rapid && draft.mode === "local" && matches && data.installed_languages.length
    ? tr("Установлены · {{0}}", [data.installed_languages.join(", ")]) : "";
  const missing = matches && data.failure === "languages" ?
    tr("Не хватает языков · {{0}}",[draft.languages.split("+").filter(lang => !data.installed_languages.includes(lang)).join(", ")]) : "";
  $("#ocr-recipient").textContent = [worker && !recipient ? "" : ocrRecipientText(recipient), installed, missing, reason].filter(Boolean).join(" ");
  const install = $("#ocr-install");
  const needsInstall = matches && draft.mode === "local" && data.failure === "unavailable" &&
    (!worker || data.worker_managed);
  install.hidden = !needsInstall && !(worker && data.worker_managed);
  if (needsInstall) install.textContent = data.can_install ? tr("Установка на сервере прокси.") :
    tr("Проверьте окружение Python, указанное при запуске.");
  else if (!install.hidden) install.textContent = tr("Прокси запускает локальный worker автоматически после подтверждения адреса.");

  const installing = ocrInstallSubmitting || data.install_state === "installing";
  const button = $("#ocr-install-run");
  button.hidden = !needsInstall || !data.can_install;
  button.disabled = installing;
  button.textContent = tr(data.install_state === "failed" ? "Повторить установку" : "Установить RapidOCR");
  const progress = $("#ocr-install-progress");
  const message = ocrInstallWarning || (installing ? "Скачиваем и проверяем RapidOCR…" :
    needsInstall && data.install_state === "failed" ? "Не удалось установить RapidOCR. Проверьте Python с модулем venv и доступ к PyPI, затем повторите." :
    matches && draft.mode === "local" && (!worker || data.worker_managed) && data.install_state === "ready"
      ? "RapidOCR установлен." : "");
  progress.textContent = message ? tr(message) : "";
  progress.hidden = !message;
}

function updateOcrForm() {
  if (!imageConfig) return;
  const controls = imageSettings.elements, saved = imageConfig.settings;
  const vision = controls.mode.value === "vision";
  const recipient = draftRecipient();
  $("#ocr-vision").hidden = !vision;
  const worker = !vision && controls.node.value === "worker";
  $("#ocr-node-field").hidden = vision || (!imageConfig.worker && !worker);
  const workerLanguages = worker && saved.mode === "local" && saved.node === "worker" &&
    imageConfig.engine === "worker" && imageConfig.installed_languages.length > 0;
  $("#ocr-languages-field").hidden = !workerLanguages;
  $("#ocr-languages").disabled = !workerLanguages;
  $("#ocr-disabled-note").hidden = controls.enabled.checked;
  $("#ocr-consent-row").hidden = !recipient;
  $("#ocr-consent-label").textContent = tr("Разрешить передачу исходных пикселей на {{0}}", [recipient]);
  $("#ocr-retention").hidden = !controls.save_originals.checked;
  $("#ocr-check").disabled = !vision || !controls.connection_id.value || !controls.model.value.trim();
  const draft = currentOcrDraft();
  const changed = Object.keys(saved).some(key => draft[key] !== saved[key]);
  const authorizedRecipient = (!worker && !recipient) || (!!recipient && draft.approved_recipient === recipient);
  const checkedVision = !vision || (draft.connection_id === saved.connection_id && draft.model === saved.model &&
    draft.approved_recipient === saved.approved_recipient && imageConfig.engine_state === "ready") ||
    (proof?.recipient === recipient && proof?.model === draft.model && proof?.connection === draft.connection_id);
  $("#ocr-save").disabled = !changed || !imageSettings.checkValidity() ||
    (draft.enabled && (!authorizedRecipient || !checkedVision));
  ocrEngineStatus(imageConfig, draft);
}

function loadOcrModelOptions(models) {
  $("#ocr-models").replaceChildren(...models.map(value => h("option", { value })));
}

function renderOcrSettings(data) {
  imageConfig = data;
  const controls = imageSettings.elements;
  $("#ocr-connection").replaceChildren(h("option", {value:""},tr("Выберите подключение")),
    ...data.connections.map(connection => h("option", {value:connection.id}, connection.name)));
  for (const [key, value] of Object.entries(data.settings)) {
    const control = controls.namedItem(key);
    if (!control) continue;
    if (typeof value === "boolean") control.checked = value;
    else control.value = value;
  }
  $("#ocr-consent").checked = !!draftRecipient() && data.settings.approved_recipient === draftRecipient();
  const models = data.connections.find(c => c.id === controls.connection_id.value)?.models || [];
  loadOcrModelOptions(models);
  updateOcrForm();
  watchOcrInstallation();
}

function watchOcrInstallation() {
  clearTimeout(ocrInstallTimer);
  if (location.hash === "#ocr" && imageConfig?.install_state === "installing") {
    ocrInstallTimer = setTimeout(refreshOcrInstallation, 1000);
  }
}

async function refreshOcrInstallation() {
  if (location.hash !== "#ocr") return;
  const current = imageConfig;
  try {
    const {state} = await api("/api/v1/ui/ocr/install", {timeoutMs:8000});
    if (location.hash !== "#ocr" || imageConfig !== current) return;
    ocrInstallWarning = "";
    if (state !== "installing") {
      const data = await api("/api/v1/ui/ocr/settings");
      if (location.hash !== "#ocr" || imageConfig !== current) return;
      // Installation doesn't edit OCR settings or the user's unsaved form.
      if (Object.keys(current.settings).every(key => current.settings[key] === data.settings[key])) {
        imageConfig = data;
      } else {
        renderOcrSettings(data);
      }
    }
  } catch {
    if (location.hash !== "#ocr" || imageConfig !== current) return;
    ocrInstallWarning = "Связь прервалась. Повторяем проверку установки…";
  }
  updateOcrForm();
  watchOcrInstallation();
}

$("#ocr-install-run").addEventListener("click", async () => {
  if (ocrInstallSubmitting || !imageConfig) return;
  ocrInstallSubmitting = true;
  ocrInstallWarning = "";
  updateOcrForm();
  try {
    const {state} = await api("/api/v1/ui/ocr/install", {method:"POST",body:{}});
    imageConfig.install_state = state;
  } catch (error) {
    ocrInstallWarning = error.messageKey === "local_ocr_install_unavailable"
      ? error.messageKey : "Не удалось запустить установку RapidOCR. Попробуйте ещё раз.";
  } finally {
    ocrInstallSubmitting = false;
    updateOcrForm();
    watchOcrInstallation();
  }
});

async function loadImageSettings() {
  const seq = ++loadingImageSettings;
  const data = await api("/api/v1/ui/ocr/settings");
  if (seq === loadingImageSettings && location.hash === "#ocr") {
    ocrInstallWarning = "";
    renderOcrSettings(data);
  }
}
TABS.ocr = loadImageSettings;

imageSettings.addEventListener("input", event => {
  if (!imageConfig) return;
  if (["connection_id", "mode", "node"].includes(event.target.name)) {
    $("#ocr-consent").checked = false;
    proof = null;
  }
  if (["connection_id", "mode", "node", "model"].includes(event.target.name)) $("#ocr-check-result").textContent = "";
  $("#ocr-save-result").textContent = "";
  updateOcrForm();
});
$("#ocr-connection").addEventListener("change", async event => {
  const version = ++catalogVersion, connection = event.target.value;
  $("#ocr-model").value = "";
  const existing = imageConfig.connections.find(c => c.id === connection);
  loadOcrModelOptions(existing?.models || []);
  updateOcrForm();
  if (!existing) return;
  try {
    const data = await api("/api/v1/ui/ocr/models", {method:"POST",body:{connection_id:connection}});
    if (version === catalogVersion && imageSettings.elements.connection_id.value === connection)
      loadOcrModelOptions(data.models);
  } catch { /* Manual model ID entry remains available. */ }
});

$("#ocr-check").addEventListener("click", async () => {
  const check = $("#ocr-check"), status = $("#ocr-check-result");
  const {connection_id, model} = currentOcrDraft();
  check.disabled = true;
  proof = null;
  status.textContent = tr("Проверяем адрес, поддержку изображений и координаты…");
  try {
    const data = await api("/api/v1/ui/ocr/check", {
      method:"POST", body:{connection_id,model}, timeoutMs:65000,
    });
    if (imageSettings.elements.connection_id.value !== connection_id ||
      imageSettings.elements.model.value.trim() !== model) return;
    const checks = [[data.reachable,"Адрес доступен"],[data.images,"Изображения поддерживаются"],
      [data.coordinates,"Координаты пригодны"]].filter(([ok]) => ok === true).map(([, label]) => tr(label));
    const problem = ocrError(data.failure);
    status.textContent = [...checks, problem].filter(Boolean).join(" · ");
    if (data.coordinates) proof = {recipient:data.recipient, model, connection:connection_id};
  } catch (error) {
    status.textContent = ocrError(error.details,error.message);
  } finally {
    updateOcrForm();
  }
});
imageSettings.addEventListener("submit", async event => {
  event.preventDefault();
  const status = $("#ocr-save-result");
  $("#ocr-save").disabled = true;
  try {
    const data = await api("/api/v1/ui/ocr/settings", {method:"PUT",body:currentOcrDraft()});
    renderOcrSettings(data);
    status.textContent = tr("Настройки сохранены");
  } catch (error) {
    status.textContent = ocrError(error.details,error.message);
    updateOcrForm();
  }
});

function ocrStageLabel(audit) {
  const total = audit.total ?? audit.attachments?.length ?? 0;
  if (audit.stage === "reading" && total > 1)
    return tr("Читаем текст · изображение {{0}} из {{1}}", [audit.current, total]);
  return tr(({queued:"Ожидает свободного обработчика", validating:"Проверяем формат и размер", reading:"Читаем текст", masking:"Закрашиваем области",
    encoding:"Сохраняем обработанное изображение", ready:"Готово к отправке", sending:"Передаём поставщику…",
    sent:"Отправлено поставщику", provider_error:"Ошибка поставщика после обработки",
    blocked:"Отправка остановлена", disabled:"Защита была выключена", cancelled:"Проверка отменена.",
    legacy:"Данные об обработке изображения отсутствуют"})[audit.stage] || "");
}

function imageButton(label, action, className = "secondary") {
  return h("button",{type:"button",class:className,onclick:action},tr(label));
}
function imageAreaLabel(area) {
  const kind = area.category === "credential" ? tr("Секрет") : kindLabel(area.kind);
  const scope = tr({personal:"Личные", team:"Командные", global:"Корпоративные"}[area.scope] || area.scope);
  const cause = area.field ? tr("Чувствительное поле {{0}}", [area.field]) :
    tr(area.reason === "vision" ? "Область от доверенного OCR" : "Локальное правило");
  return kind + " · " + scope + " · " + cause;
}
function coveredCount(areas) {
  if (!areas.length) return tr("Совпадений нет");
  const plural = new Intl.PluralRules(I18N.language).select(areas.length);
  return tr(({one:"Закрыта {{0}} область",few:"Закрыто {{0}} области"})[plural] || "Закрыто {{0}} областей",
    [number(areas.length)]);
}

function positionImageRegion(node, rect, image) {
  for (const [key,value,max] of [["left",rect.x0,image.width],["top",rect.y0,image.height],
    ["width",rect.x1-rect.x0,image.width],["height",rect.y1-rect.y0,image.height]])
    node.style.setProperty(key,(value/max*100)+"%");
}

function imageComparison(image, images, { initialArea = null, onSelect } = {}) {
  const selection = new Map();
  let selected = initialArea, clicked = false, factor = 1;
  const viewports = [], surfaces = [];
  const select = area => {
    selected = clicked && selected === area ? null : area;
    clicked = true;
    for (const [id, nodes] of selection)
      for (const node of nodes) node.setAttribute("aria-pressed", String(id === selected));
    const mark = surfaces[0].querySelector('[data-ocr-area="' + selected + '"]');
    if (mark) mark.scrollIntoView({block:"nearest",inline:"nearest"});
    onSelect?.(selected, surfaces[0]);
  };
  function mark(area, node) {
    node.setAttribute("aria-pressed", String(area.id === selected));
    node.dataset.ocrArea = area.id;
    node.addEventListener("click", () => select(area.id));
    if (!selection.has(area.id)) selection.set(area.id, []);
    selection.get(area.id).push(node);
    return node;
  }
  for (const {source,label} of images) {
    const surface = h("div",{class:"ocr-surface"},h("img",{src:source,alt:tr(label), draggable:"false",
      width:image.width,height:image.height}));
    for (const area of image.areas) for (const rect of area.rectangles) {
      const marker = mark(area,h("button",{type:"button",class:"ocr-marker","aria-label":area.id + ". " + imageAreaLabel(area)},
        h("span",{},area.id)));
      positionImageRegion(marker, rect, image);
      surface.append(marker);
    }
    const viewport = h("div",{class:"ocr-viewport",tabindex:"0",role:"region","aria-label":tr(label)},surface);
    viewports.push(viewport);
    surfaces.push(surface);
  }
  function fit() {
    if (!viewports[0].isConnected) return;
    const width = Math.min(image.width,...viewports.map(viewport => viewport.clientWidth).filter(Boolean));
    for (const surface of surfaces) surface.style.width = (width * factor) + "px";
  }
  for (const view of viewports) {
    view.addEventListener("scroll",() => {
      for (const other of viewports.filter(item => item !== view))
        if (other.scrollLeft !== view.scrollLeft || other.scrollTop !== view.scrollTop)
          other.scrollTo(view.scrollLeft,view.scrollTop);
    },{passive:true});
    let drag;
    view.addEventListener("pointerdown", event => {
      if (event.button !== 0 || event.target.closest("button")) return;
      drag = {id:event.pointerId, x:event.clientX, y:event.clientY, left:view.scrollLeft, top:view.scrollTop};
      view.setPointerCapture(event.pointerId);
    });
    view.addEventListener("pointermove", event => {
      if (drag?.id === event.pointerId) view.scrollTo(drag.left + drag.x - event.clientX, drag.top + drag.y - event.clientY);
    });
    view.addEventListener("lostpointercapture", () => { drag = null; });
  }
  const resize = new ResizeObserver(fit);
  for (const view of viewports) resize.observe(view);
  const root = h("div", {class:"ocr-result"},
    h("div",{class:"ocr-image-tools"},
      imageButton("Вписать",()=>{factor=1;fit();for (const view of viewports) view.scrollTo(0,0);}),
      imageButton("Уменьшить",()=>{factor=Math.max(1,factor/1.35);fit();}),
      imageButton("Увеличить",()=>{factor=Math.min(8,factor*1.35);fit();}),
      h("span",{class:"hint"},tr("Размер у прокси · {{0}}×{{1}} px",[number(image.width),number(image.height)]))),
    h("div",{class:"ocr-image-panes"},images.map(({label},index)=>h("section",{},
      h("h3",{},tr(label)), viewports[index]))),
    image.areas.length ? h("ol",{class:"ocr-regions","aria-label":tr("Закрытые области")},image.areas.map(area =>
      h("li",{},mark(area,h("button",{type:"button",class:"ocr-region"},
        h("span",{class:"ocr-region-number"},area.id),
        h("span",{},imageAreaLabel(area),h("small",{},tr(({field_value:"Значение скрыто целиком",
          field_row:"Для надёжности скрыта строка целиком"})[area.reason] || "Закрыто непрозрачной заливкой")))))))) : null);
  root.addEventListener("ocr-dispose", () => resize.disconnect(), {once:true});
  return root;
}

function replaceImageView(host, ...children) {
  for (const result of host.querySelectorAll(".ocr-result")) result.dispatchEvent(new Event("ocr-dispose"));
  host.replaceChildren(...children);
}

async function loadImageLab() {
  const data = await api("/api/v1/ui/ocr/settings");
  if (location.hash !== "#image-lab") return;
  imageLab.status = data;
  $("#ocr-upload-limits").textContent = tr("PNG, JPEG, WebP · до {{0}} МиБ и {{1}} Мп · до {{2}} пикселей по стороне",
    [data.settings.max_mib, data.settings.max_megapixels, data.max_edge]);
  const engine = !data.settings.enabled || data.engine === "rapidocr"
    ? "RapidOCR" : data.engine === "vision" ? data.settings.model : tr("Отдельный worker");
  const recipient = data.settings.enabled
    ? tr("Используются сохранённые настройки.") + " " + ocrRecipientText(data.recipient)
    : tr("Локальная проба на компьютере прокси. Защита в прокси выключена.");
  $("#ocr-lab-recipient").textContent = [engine, recipient].filter(Boolean).join(" · ");
  $("#ocr-run").disabled = !imageLab.file || !!imageLab.running;
  if (imageLab.result) renderImagePreview(imageLab.result);
}
TABS["image-lab"] = loadImageLab;

function chooseOcrFile(file) {
  if (!file) return;
  imageLab.fileVersion++;
  imageLab.running?.abort();
  imageLab.file = file;
  imageLab.result = null;
  if (labOriginalUrl) URL.revokeObjectURL(labOriginalUrl);
  labOriginalUrl = null;
  const input = $("#ocr-file");
  if (input.files[0] !== file) {
    const selection = new DataTransfer();
    selection.items.add(file);
    input.files = selection.files;
  }
  $("#ocr-file-name").textContent = file.name;
  $("#ocr-lab-status").textContent = "";
  replaceImageView($("#ocr-lab-output"));
  $("#ocr-run").disabled = !imageLab.status || !!imageLab.running;
}
$("#ocr-file").addEventListener("change",event=>chooseOcrFile(event.target.files[0]));
const drop = $("#ocr-drop");
for (const name of ["dragenter","dragover"]) drop.addEventListener(name, event => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  drop.classList.add("dragging");
});
drop.addEventListener("dragleave",()=>drop.classList.remove("dragging"));
drop.addEventListener("drop",event=>{
  event.preventDefault();
  drop.classList.remove("dragging");
  chooseOcrFile(event.dataTransfer.files[0]);
});
document.addEventListener("paste", event => {
  if (location.hash !== "#image-lab") return;
  const file = [...(event.clipboardData?.files || [])].find(file => file.type.startsWith("image/"));
  if (file) { event.preventDefault(); chooseOcrFile(file); }
});
$("#ocr-example").addEventListener("click",async()=>{
  try {
    const response = await fetch("ocr-example.png");
    if (!response.ok) throw new Error(tr("Не удалось открыть пример."));
    chooseOcrFile(new File([await response.blob()],"values.yaml.png",{type:"image/png"}));
  } catch (error) { $("#ocr-lab-status").textContent = error.message; }
});

async function fetchOcrBody(path, options = {}) {
  const headers = {...options.headers};
  if (token) headers.authorization = "Bearer " + token;
  const response = await fetch(path, {...options,headers,cache:"no-store"});
  if (!response.ok) {
    const data = await response.json().catch(()=>null);
    const error = new Error(data?.error?.message || ("HTTP " + response.status));
    error.details = data?.error?.details;
    throw error;
  }
  return response;
}

let labOriginalUrl;
function renderImagePreview(result) {
  if (labOriginalUrl) URL.revokeObjectURL(labOriginalUrl);
  labOriginalUrl = URL.createObjectURL(result.original);
  const status = result.image.areas.length ? coveredCount(result.image.areas) :
    tr("Текст прочитан. Совпадений с правилами нет.");
  const comparison = imageComparison(result.image,[{source:labOriginalUrl,label:"Оригинал"},
    {source:result.data,label:"После защиты"}]);
  replaceImageView($("#ocr-lab-output"),h("section",{class:"panel ocr-preview"},
    h("header",{class:"ocr-lab-head"},h("h2",{},status),
      h("a",{class:"secondary",href:result.data,download:"kaponir-protected.png"},tr("Скачать обработанное изображение"))),
    h("p",{class:"hint"},tr("Время обработки · {{0}} мс",[number(result.elapsed_ms)]), " · ",
      tr("Распознавание · {{0}} мс",[number(result.timings.ocr_ms)])),comparison));
}

$("#ocr-run").addEventListener("click",async()=>{
  const file = imageLab.file;
  const saved = imageLab.status;
  if (!file || !saved || imageLab.running) return;
  const status = $("#ocr-lab-status"), version = imageLab.fileVersion;
  if (file.size > saved.settings.max_mib * 1024 * 1024) {
    status.textContent = ocrError({code:"limits"});
    return;
  }
  const control = new AbortController();
  const started = Date.now();
  const progress = () => {
    status.textContent = tr("Проверяем изображение · {{0}} с", [Math.floor((Date.now()-started)/1000)]);
  };
  progress();
  const timer = setInterval(progress, 1000);
  const headers = {"content-type":file.type || "application/octet-stream"};
  if (saved.settings.enabled && saved.recipient) headers["x-kaponir-ocr-recipient"] = saved.recipient;
  imageLab.running = control;
  $("#ocr-run").disabled = true;
  $("#ocr-cancel").hidden = false;
  try {
    const response = await fetchOcrBody("/api/v1/ui/ocr/preview",{method:"POST", body:file, headers, signal:control.signal});
    const data = await response.json();
    if (version !== imageLab.fileVersion) return;
    imageLab.result = {...data,original:file};
    renderImagePreview(imageLab.result);
    status.textContent = tr("Проверка завершена.");
  } catch (error) {
    if (version !== imageLab.fileVersion) return;
    const text = error.name === "AbortError" ? ocrError({code:"cancelled"}) : ocrError(error.details,error.message);
    status.textContent = text;
    replaceImageView($("#ocr-lab-output"),h("p",{class:"event-error-banner"},text));
  } finally {
    clearInterval(timer);
    imageLab.running = null;
    $("#ocr-run").disabled = !imageLab.file;
    $("#ocr-cancel").hidden = true;
  }
});
$("#ocr-cancel").addEventListener("click",()=>imageLab.running?.abort());

function imagePath(event, attachment, variant) {
  return "/api/v1/ui/ocr/images/" + [event.session_id || "request-" + event.id,
    event.id, attachment.id, variant].map(encodeURIComponent).join("/");
}

function attachmentStatus(audit, attachment) {
  if (audit.stage === "sent") return coveredCount(attachment.areas);
  return ocrStageLabel(audit);
}
function ocrAttachmentCards(audit, event, filter = () => true) {
  if (!audit) return null;
  const attachments = audit.attachments.filter(filter);
  if (!attachments.length) return audit.stage === "legacy"
    ? h("p",{class:"hint"},ocrStageLabel(audit)) : null;
  const status = audit.stage === "blocked" ? h("div",{class:"event-error-banner",role:"status"},
    h("strong",{},tr("Поставщику ничего не отправлено")), " ",
    Number.isInteger(audit.failure?.attachment) && audit.attachments.length > 1
      ? tr("Изображение {{0}}",[audit.failure.attachment+1]) + " · " : "",
    ocrError(audit.failure,event.error?.message || "")) : null;
  const list = h("ul",{class:"ocr-attachments","aria-label":tr("Вложения изображения")});
  for (const [index,attachment] of attachments.entries()) {
    const label = h("span",{class:"ocr-attachment-label"},
      h("strong",{},attachmentStatus(audit,attachment)),
      h("small",{},tr("Изображение {{0}}",[index+1])));
    const thumbnail = h("span",{class:"ocr-thumbnail","aria-hidden":"true"},icon("eye-off"));
    const trigger = h("button",{type:"button",class:"ocr-attachment",
      "data-session-focus":"image-"+attachment.id,
      onclick:()=>openImageAttachment(event,audit,attachment)},thumbnail,label);
    if (attachment.preview_until_ms > Date.now()) {
      fetchOcrBody(imagePath(event,attachment,"sent")).then(response=>response.blob()).then(blob=>{
        if (!trigger.isConnected) return;
        const src = URL.createObjectURL(blob);
        const img = h("img",{src,alt:"",onload:()=>URL.revokeObjectURL(src),onerror:()=>URL.revokeObjectURL(src)});
        thumbnail.replaceChildren(img);
      }).catch(()=>{});
    }
    list.append(h("li",{},trigger));
  }
  return h("div",{class:"ocr-attachment-group"},status,list);
}

async function openImageAttachment(event,audit,attachment,areaId) {
  const dialog = $("#ocr-image-dialog");
  const result = h("div",{class:"inspector-body"});
  const status = h("p",{class:"form-status",role:"status"});
  const title = h("h2",{id:"ocr-image-title"},attachmentStatus(audit,attachment));
  const close = h("form",{method:"dialog"},h("button",{class:"secondary",autofocus:true},tr("Закрыть")));
  const sent = imageButton("Отправлено поставщику",()=>load("sent",true));
  const original = imageButton("Показать оригинал",()=>load("original",true));
  const originalDescription = h("span",{class:"hint"});
  function originalProblem() {
    return attachment.original_until_ms == null ? "Оригинал не сохранялся" :
      attachment.original_until_ms <= Date.now() ? "Срок хранения истёк" : "";
  }
  function updateControls() {
    const problem = originalProblem();
    original.disabled = !!problem;
    originalDescription.hidden = !problem && !attachment.areas.length;
    originalDescription.textContent = tr(problem || "Нажмите на область, чтобы показать её оригинал.");
    sent.disabled = !attachment.preview_until_ms;
    sent.textContent = tr(audit.stage === "sent" ? "Отправлено поставщику" : "После защиты");
  }
  updateControls();
  const controller = new AbortController();
  let variation = 0, revealVersion = 0, currentUrl, refresh;
  const pending = new Set(["queued","validating","reading","masking","encoding","ready","sending"]);
  function describe() {
    const elapsed = pending.has(audit.stage) && event.started_at_ms ?
      tr("{{0}} с",[Math.max(0,Math.floor((Date.now()-event.started_at_ms)/1000))]) : "";
    status.textContent = [ocrStageLabel(audit),elapsed,ocrError(audit.failure),
      audit.stage === "blocked" ? tr("Поставщику ничего не отправлено") : ""].filter(Boolean).join(" · ");
  }
  function imageLoadError(error) {
    return tr(({attachment_expired:"Срок хранения истёк",attachment_removed:"Изображение удалено по лимиту хранения",
      original_not_saved:"Оригинал не сохранялся"})[error.message] || error.message);
  }
  async function reveal(area, surface) {
    const version = ++revealVersion;
    for (const image of surface.querySelectorAll(".ocr-revealed")) image.remove();
    if (area == null) {
      status.textContent = tr(audit.stage === "sent" ? "Отправлено поставщику" : "После защиты");
      return;
    }
    if (originalProblem()) {
      status.textContent = tr(originalProblem());
      return;
    }
    status.textContent = tr("Загрузка…");
    try {
      const response = await fetchOcrBody(imagePath(event,attachment,"area-" + area),{signal:controller.signal});
      const {fragments} = await response.json();
      if (version !== revealVersion || !surface.isConnected || !dialog.open) return;
      for (const fragment of fragments) {
        const image = h("img",{class:"ocr-revealed",src:fragment.data,alt:"",draggable:"false"});
        positionImageRegion(image, fragment.rect, attachment);
        surface.append(image);
      }
      status.textContent = tr("Выбранная область раскрыта");
    } catch (error) {
      if (version === revealVersion && !controller.signal.aborted) status.textContent = imageLoadError(error);
    }
  }
  async function load(variant, explicit) {
    const version = ++variation;
    revealVersion++;
    if (variant === "original" && !explicit) return;
    sent.setAttribute("aria-pressed",String(variant === "sent"));
    original.setAttribute("aria-pressed",String(variant === "original"));
    status.textContent = tr("Загрузка…");
    try {
      const response = await fetchOcrBody(imagePath(event,attachment,variant),{signal:controller.signal});
      const blob = await response.blob();
      const src = URL.createObjectURL(blob);
      if (!dialog.open || version !== variation) { URL.revokeObjectURL(src); return; }
      if (currentUrl) URL.revokeObjectURL(currentUrl);
      currentUrl = src;
      const label = variant === "original" ? "Оригинал" : audit.stage === "sent" ? "Отправлено поставщику" : "После защиты";
      const view = imageComparison(attachment,[{source:src,label}],{
        initialArea: areaId == null ? null : Number(areaId),
        onSelect: variant === "sent" ? reveal : undefined,
      });
      replaceImageView(result,view);
      status.textContent = variant === "sent" ? tr(label) : tr("Оригинал");
      if (areaId != null) view.querySelector('.ocr-marker[data-ocr-area="' + Number(areaId) + '"]')
        ?.scrollIntoView({block:"nearest",inline:"nearest"});
    } catch (error) {
      if (version !== variation || controller.signal.aborted) return;
      status.textContent = imageLoadError(error);
    }
  }
  dialog.replaceChildren(h("div",{class:"inspector-shell"},
    h("header",{class:"inspector-header"},title,close),
    h("div",{class:"ocr-dialog-controls"},sent,original,originalDescription),
    status,result));
  dialog.addEventListener("close",()=>{
    controller.abort();
    clearTimeout(refresh);
    if (currentUrl) URL.revokeObjectURL(currentUrl);
    replaceImageView(result);
  },{once:true});
  dialog.showModal();
  if (attachment.preview_until_ms) await load("sent",false);
  else {
    describe();
    async function updateProgress() {
      try {
        const response = await fetchOcrBody("/api/v1/ui/events/" + event.id,{signal:controller.signal});
        const updated = await response.json();
        if (!dialog.open || controller.signal.aborted || !updated.inspector?.images) return;
        audit = updated.inspector.images;
        attachment = audit.attachments.find(item=>item.id===attachment.id) || attachment;
        title.textContent = attachmentStatus(audit,attachment);
        updateControls();
        if (!pending.has(audit.stage)) {
          if (attachment.preview_until_ms) {
            await load("sent",false);
          } else describe();
          return;
        }
        describe();
        refresh = setTimeout(updateProgress,1200);
      } catch (error) {
        if (!controller.signal.aborted) status.textContent = error.message;
      }
    }
    if (pending.has(audit.stage)) refresh = setTimeout(updateProgress,1200);
  }
}
