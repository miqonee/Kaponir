//! OpenAI Chat Completions: маскирование сообщений и аргументов `tool_calls`, восстановление
//! ответа и потока `chat.completion.chunk`.
//!
//! Рассуждения (`reasoning_content`, `reasoning`) у OpenAI-совместимых провайдеров не подписаны,
//! поэтому восстанавливаются и маскируются наравне с текстом.

use kaponir_core::{Escape, StreamRestorer};
use serde_json::{Value, json};
use std::collections::BTreeMap;

use crate::sse::{Event, Rewriter, encode};
use crate::state::Cx;
use crate::walk::{mask_field, mask_leaves, restore_field, ty};

/// Текстовые поля сообщения и дельты помимо `content`.
const REASONING: &[&str] = &["reasoning_content", "reasoning"];

/// Что восстанавливается внутри одного choice потока.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Slot {
    /// `content` или поле рассуждений.
    Field(&'static str),
    /// Аргументы `tool_calls[index]`.
    Tool(u64),
}

pub fn mask_request(body: &mut Value, cx: &mut Cx<'_>, hint: Option<&str>) -> anyhow::Result<()> {
    let Some(Value::Array(messages)) = body.get_mut("messages") else {
        return Ok(());
    };
    for m in messages.iter_mut() {
        match m.get_mut("content") {
            Some(Value::String(s)) => *s = cx.mask(s)?,
            Some(Value::Array(parts)) => {
                for p in parts {
                    if ty(p) == "text" {
                        mask_field(p, "text", cx)?;
                    }
                }
            }
            _ => {}
        }
        for f in REASONING {
            mask_field(m, f, cx)?;
        }
        if let Some(Value::Array(calls)) = m.get_mut("tool_calls") {
            for c in calls {
                if let Some(Value::String(args)) = c.pointer_mut("/function/arguments") {
                    *args = mask_arguments(args, cx)?;
                }
            }
        }
    }
    if let Some(h) = hint {
        add_hint(messages, h);
    }
    Ok(())
}

/// Аргументы — JSON в строке: маскируются значения, а не сырой текст, иначе оригинал
/// с экранированными символами восстановился бы неверно.
fn mask_arguments(args: &str, cx: &mut Cx<'_>) -> anyhow::Result<String> {
    match serde_json::from_str::<Value>(args) {
        Ok(mut v) => {
            mask_leaves(&mut v, cx)?;
            Ok(serde_json::to_string(&v)?)
        }
        Err(_) => cx.mask(args),
    }
}

fn add_hint(messages: &mut Vec<Value>, hint: &str) {
    let has_system = messages.first().is_some_and(|m| {
        matches!(
            m.get("role").and_then(Value::as_str),
            Some("system" | "developer")
        )
    });
    if !has_system {
        messages.insert(0, json!({"role": "system", "content": hint}));
        return;
    }
    let m = &mut messages[0];
    match m.get_mut("content") {
        Some(Value::String(s)) => {
            s.push_str("\n\n");
            s.push_str(hint);
        }
        Some(Value::Array(parts)) => parts.push(json!({"type": "text", "text": hint})),
        _ => m["content"] = Value::String(hint.into()),
    }
}

pub fn restore_response(body: &mut Value, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    if let Some(Value::Array(choices)) = body.get_mut("choices") {
        for ch in choices {
            let Some(msg) = ch.get_mut("message") else {
                continue;
            };
            for f in std::iter::once(&"content").chain(REASONING) {
                restore_field(msg, f, cx, Escape::None)?;
            }
            if let Some(Value::Array(calls)) = msg.get_mut("tool_calls") {
                for c in calls {
                    if let Some(f) = c.get_mut("function") {
                        restore_field(f, "arguments", cx, Escape::Json)?;
                    }
                }
            }
        }
    }
    Ok(())
}

/// Восстановители по (choice, слот). Хвост дописывается в дельту
/// чанка с `finish_reason`, а если его не было — отдельным чанком перед `[DONE]`.
#[derive(Default)]
pub struct StreamRewriter {
    restorers: BTreeMap<(u64, Slot), StreamRestorer>,
    /// Поля чанка (id, model, created) для синтетического чанка с хвостами.
    template: Option<Value>,
}

impl StreamRewriter {
    fn flush_all(&mut self, cx: &mut Cx<'_>) -> anyhow::Result<Vec<String>> {
        let mut choices: BTreeMap<u64, Value> = BTreeMap::new();
        for ((ci, slot), mut r) in std::mem::take(&mut self.restorers) {
            let rest = cx.flush(&mut r)?;
            if rest.is_empty() {
                continue;
            }
            let ch = choices
                .entry(ci)
                .or_insert_with(|| json!({"index": ci, "delta": {}, "finish_reason": null}));
            append(ch, slot, &rest);
        }
        if choices.is_empty() {
            return Ok(Vec::new());
        }
        let mut chunk = self
            .template
            .clone()
            .unwrap_or_else(|| json!({"object": "chat.completion.chunk"}));
        chunk["choices"] = Value::Array(choices.into_values().collect());
        Ok(vec![encode(None, &chunk.to_string())])
    }
}

impl Rewriter for StreamRewriter {
    fn event(&mut self, ev: Event, cx: &mut Cx<'_>) -> anyhow::Result<Vec<String>> {
        if ev.data.trim() == "[DONE]" {
            let mut out = self.flush_all(cx)?;
            out.push(ev.raw);
            return Ok(out);
        }
        let Ok(mut v) = serde_json::from_str::<Value>(&ev.data) else {
            return Ok(vec![ev.raw]);
        };
        let Some(choices) = v.get_mut("choices").and_then(Value::as_array_mut) else {
            return Ok(vec![ev.raw]);
        };
        for ch in choices.iter_mut() {
            let ci = ch.get("index").and_then(Value::as_u64).unwrap_or(0);
            if let Some(delta) = ch.get_mut("delta") {
                for f in std::iter::once("content").chain(REASONING.iter().copied()) {
                    if let Some(Value::String(s)) = delta.get_mut(f) {
                        let r = self
                            .restorers
                            .entry((ci, Slot::Field(f)))
                            .or_insert_with(|| StreamRestorer::new(Escape::None));
                        let out = cx.push(r, s)?;
                        *s = out;
                    }
                }
                if let Some(Value::Array(calls)) = delta.get_mut("tool_calls") {
                    for call in calls {
                        let ti = call.get("index").and_then(Value::as_u64).unwrap_or(0);
                        if let Some(Value::String(a)) = call.pointer_mut("/function/arguments") {
                            let r = self
                                .restorers
                                .entry((ci, Slot::Tool(ti)))
                                .or_insert_with(|| StreamRestorer::new(Escape::Json));
                            let out = cx.push(r, a)?;
                            *a = out;
                        }
                    }
                }
            }
            if ch.get("finish_reason").is_some_and(|f| !f.is_null()) {
                let keys: Vec<_> = self
                    .restorers
                    .keys()
                    .filter(|k| k.0 == ci)
                    .copied()
                    .collect();
                for k in keys {
                    let mut r = self.restorers.remove(&k).expect("key from map");
                    let rest = cx.flush(&mut r)?;
                    if !rest.is_empty() {
                        append(ch, k.1, &rest);
                    }
                }
            }
        }
        if self.template.is_none() {
            let mut t = v.clone();
            if let Some(o) = t.as_object_mut() {
                o.remove("usage");
            }
            self.template = Some(t);
        }
        Ok(vec![encode(ev.event.as_deref(), &v.to_string())])
    }

    fn end(&mut self, cx: &mut Cx<'_>) -> anyhow::Result<Vec<String>> {
        self.flush_all(cx)
    }
}

fn append(ch: &mut Value, slot: Slot, text: &str) {
    if !ch.get("delta").is_some_and(Value::is_object) {
        ch["delta"] = json!({});
    }
    let delta = &mut ch["delta"];
    match slot {
        Slot::Field(f) => {
            let cur = delta
                .get(f)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            delta[f] = Value::String(cur + text);
        }
        Slot::Tool(ti) => {
            if !delta.get("tool_calls").is_some_and(Value::is_array) {
                delta["tool_calls"] = json!([]);
            }
            let calls = delta["tool_calls"].as_array_mut().expect("array");
            match calls
                .iter_mut()
                .find(|c| c.get("index").and_then(Value::as_u64) == Some(ti))
            {
                Some(c) => {
                    let cur = c
                        .pointer("/function/arguments")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    c["function"]["arguments"] = Value::String(cur + text);
                }
                None => calls.push(json!({"index": ti, "function": {"arguments": text}})),
            }
        }
    }
}
