//! Anthropic Messages API: что маскировать в запросе и как восстанавливать ответ.
//!
//! Блоки `thinking` не трогаются ни в запросе, ни в ответе: провайдер проверяет их подпись,
//! и любое изменение сломало бы следующий запрос. Модель видела только токены, так что
//! в рассуждениях остаются плейсхолдеры, а не оригиналы.

use kaponir_core::{Escape, StreamRestorer};
use serde_json::{Value, json};
use std::collections::BTreeMap;

use crate::sse::{Event, Rewriter, encode};
use crate::state::Cx;
use crate::walk::{mask_field, mask_leaves, restore_field, restore_leaves, ty};

/// Маскирует system, текстовые блоки, `tool_use.input`, `tool_result` и текстовые документы
/// в истории. Схемы инструментов не трогаются.
pub fn mask_request(body: &mut Value, cx: &mut Cx<'_>, hint: Option<&str>) -> anyhow::Result<()> {
    if let Some(system) = body.get_mut("system") {
        mask_content(system, cx)?;
    }
    if let Some(Value::Array(messages)) = body.get_mut("messages") {
        for m in messages {
            if let Some(c) = m.get_mut("content") {
                mask_content(c, cx)?;
            }
        }
    }
    if let Some(h) = hint {
        add_hint(body, h);
    }
    Ok(())
}

fn mask_content(c: &mut Value, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    match c {
        Value::String(s) => *s = cx.mask(s)?,
        Value::Array(blocks) => {
            for b in blocks {
                mask_block(b, cx)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn mask_block(b: &mut Value, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    match ty(b).as_str() {
        "text" => mask_field(b, "text", cx)?,
        "tool_use" | "server_tool_use" => {
            if let Some(input) = b.get_mut("input") {
                mask_leaves(input, cx)?;
            }
        }
        "tool_result" => {
            if let Some(c) = b.get_mut("content") {
                mask_content(c, cx)?;
            }
        }
        "document" => {
            if let Some(src) = b.get_mut("source")
                && ty(src) == "text"
            {
                mask_field(src, "data", cx)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// Подсказка дописывается в конец system: начало system prompt клиента не меняется.
fn add_hint(body: &mut Value, hint: &str) {
    let Some(obj) = body.as_object_mut() else {
        return;
    };
    match obj.get_mut("system") {
        Some(Value::String(s)) => {
            if !s.is_empty() {
                s.push_str("\n\n");
            }
            s.push_str(hint);
        }
        Some(Value::Array(blocks)) => blocks.push(json!({"type": "text", "text": hint})),
        _ => {
            obj.insert("system".into(), Value::String(hint.into()));
        }
    }
}

pub fn restore_response(body: &mut Value, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    cx.record_token_usage(body);
    if let Some(Value::Array(blocks)) = body.get_mut("content") {
        for b in blocks {
            match ty(b).as_str() {
                "text" => restore_field(b, "text", cx, Escape::None)?,
                "tool_use" => {
                    if let Some(input) = b.get_mut("input") {
                        restore_leaves(input, cx)?;
                    }
                }
                _ => {}
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum Block {
    Text,
    /// `input_json_delta`: восстановленные значения экранируются для JSON-строки.
    Json,
}

impl Block {
    fn field(self) -> &'static str {
        match self {
            Block::Text => "text",
            Block::Json => "partial_json",
        }
    }

    fn delta_type(self) -> &'static str {
        match self {
            Block::Text => "text_delta",
            Block::Json => "input_json_delta",
        }
    }

    fn escape(self) -> Escape {
        match self {
            Block::Text => Escape::None,
            Block::Json => Escape::Json,
        }
    }
}

/// Восстановление потока: свой восстановитель на каждый блок `text` / `tool_use`, хвост
/// отдаётся отдельной дельтой перед `content_block_stop`.
#[derive(Default)]
pub struct StreamRewriter {
    blocks: BTreeMap<u64, (Block, StreamRestorer)>,
}

impl Rewriter for StreamRewriter {
    fn event(&mut self, ev: Event, cx: &mut Cx<'_>) -> anyhow::Result<Vec<String>> {
        let Ok(mut v) = serde_json::from_str::<Value>(&ev.data) else {
            return Ok(vec![ev.raw]);
        };
        cx.record_token_usage(&v);
        let index = v.get("index").and_then(Value::as_u64);
        match (ty(&v).as_str(), index) {
            ("content_block_start", Some(i)) => {
                let block = match v.pointer("/content_block/type").and_then(Value::as_str) {
                    Some("text") => Some(Block::Text),
                    Some("tool_use") => Some(Block::Json),
                    _ => None,
                };
                if let Some(b) = block {
                    self.blocks.insert(i, (b, StreamRestorer::new(b.escape())));
                }
                Ok(vec![ev.raw])
            }
            ("content_block_delta", Some(i)) => {
                let Some((block, r)) = self.blocks.get_mut(&i) else {
                    return Ok(vec![ev.raw]);
                };
                let field = block.field();
                let Some(chunk) = v
                    .get("delta")
                    .and_then(|d| d.get(field))
                    .and_then(Value::as_str)
                    .map(str::to_string)
                else {
                    return Ok(vec![ev.raw]);
                };
                let out = cx.push(r, &chunk)?;
                if out.is_empty() {
                    return Ok(Vec::new());
                }
                v["delta"][field] = Value::String(out);
                Ok(vec![encode(ev.event.as_deref(), &v.to_string())])
            }
            ("content_block_stop", Some(i)) => {
                let mut out = Vec::new();
                if let Some((b, mut r)) = self.blocks.remove(&i) {
                    let rest = cx.flush(&mut r)?;
                    if !rest.is_empty() {
                        out.push(delta(i, b, &rest));
                    }
                }
                out.push(ev.raw);
                Ok(out)
            }
            _ => Ok(vec![ev.raw]),
        }
    }

    fn end(&mut self, cx: &mut Cx<'_>) -> anyhow::Result<Vec<String>> {
        let mut out = Vec::new();
        for (i, (b, mut r)) in std::mem::take(&mut self.blocks) {
            let rest = cx.flush(&mut r)?;
            if !rest.is_empty() {
                out.push(delta(i, b, &rest));
            }
        }
        Ok(out)
    }
}

fn delta(index: u64, b: Block, text: &str) -> String {
    let mut d = json!({"type": b.delta_type()});
    d[b.field()] = Value::String(text.into());
    encode(
        Some("content_block_delta"),
        &json!({"type": "content_block_delta", "index": index, "delta": d}).to_string(),
    )
}
