"use strict";
// One accessible help surface for settings, including touch and keyboard users.
const settingHelp = h("div", { id: "setting-help-popup", class: "setting-help-popup", role: "tooltip", hidden: true });
document.body.append(settingHelp);
let helpOwner = null, helpTimer;
function closeSettingHelp() {
  if (helpOwner) helpOwner.setAttribute("aria-expanded", "false");
  helpOwner = null; settingHelp.hidden = true;
}
function showSettingHelp(button) {
  clearTimeout(helpTimer);
  if (helpOwner && helpOwner !== button) helpOwner.setAttribute("aria-expanded", "false");
  helpOwner = button; button.setAttribute("aria-expanded", "true");
  settingHelp.textContent = tr(button.dataset.help);
  settingHelp.hidden = false;
  const rect = button.getBoundingClientRect(), width = Math.min(340, innerWidth - 24);
  settingHelp.style.width = width + "px";
  settingHelp.style.left = Math.max(12, Math.min(rect.right - width, innerWidth - width - 12)) + "px";
  const height = settingHelp.getBoundingClientRect().height;
  settingHelp.style.top = Math.max(12, rect.bottom + height + 12 <= innerHeight ? rect.bottom + 8 : rect.top - height - 8) + "px";
}
function helpButton(text, label = "Пояснение настройки") {
  const button = h("button", { type: "button", class: "setting-help-button", "aria-label": tr(label),
    "data-i18n-aria-label": label, "aria-expanded": "false", "aria-describedby": settingHelp.id, "data-help": text }, "?");
  button.addEventListener("click", (event) => {
    event.preventDefault(); event.stopPropagation();
    if (helpOwner === button && settingHelp.dataset.clicked === "true") { closeSettingHelp(); settingHelp.dataset.clicked = "false"; }
    else { settingHelp.dataset.clicked = "true"; showSettingHelp(button); }
  });
  for (const event of ["mouseenter", "focus"]) button.addEventListener(event, () => { settingHelp.dataset.clicked = "false"; showSettingHelp(button); });
  for (const event of ["mouseleave", "blur"]) button.addEventListener(event, () => {
    if (settingHelp.dataset.clicked !== "true") helpTimer = setTimeout(closeSettingHelp, 180);
  });
  return button;
}
settingHelp.addEventListener("mouseenter", () => clearTimeout(helpTimer));
settingHelp.addEventListener("mouseleave", () => { if (settingHelp.dataset.clicked !== "true") closeSettingHelp(); });
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".setting-help-button,#setting-help-popup")) closeSettingHelp();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && helpOwner) { const button = helpOwner; button.focus(); closeSettingHelp(); }
});
window.addEventListener("resize", closeSettingHelp);
document.addEventListener("scroll", (event) => {
  if (settingHelp.contains(event.target) || !helpOwner) return;
  const rect = helpOwner.getBoundingClientRect();
  if (!helpOwner.isConnected || rect.bottom <= 0 || rect.top >= innerHeight) closeSettingHelp();
  else if (document.activeElement === helpOwner || settingHelp.dataset.clicked === "true") showSettingHelp(helpOwner);
  else closeSettingHelp();
}, true);
function installSettingHelp() {
  for (const input of document.querySelectorAll("input[aria-describedby],select[aria-describedby]")) {
    if (input.dataset.helpInstalled) continue;
    const note = document.getElementById(input.getAttribute("aria-describedby").split(" ")[0]), label = input.closest("label");
    if (!note || !label || !note.classList.contains("hint")) continue;
    input.dataset.helpInstalled = "true";
    const text = note.dataset.i18n || note.textContent, wrapper = h("div", { class: label.classList.contains("field") ? "help-field" : "help-check" });
    label.before(wrapper); wrapper.append(label, helpButton(text)); note.hidden = true;
  }
  for (const input of document.querySelectorAll("input[type=checkbox]")) {
    if (input.dataset.helpInstalled) continue;
    const label = input.closest("label"), note = label?.nextElementSibling;
    if (!label || !note?.matches("p.hint[data-i18n]")) continue;
    if (!note.id) note.id = input.id ? input.id + "-help" : "help-note-" + Math.random().toString(36).slice(2);
    input.setAttribute("aria-describedby", note.id);
  }
}
installSettingHelp();
let helpScheduled = false;
new MutationObserver(() => {
  if (helpScheduled) return; helpScheduled = true;
  requestAnimationFrame(() => { helpScheduled = false; installSettingHelp(); });
}).observe(document.getElementById("content"), { childList: true, subtree: true });
