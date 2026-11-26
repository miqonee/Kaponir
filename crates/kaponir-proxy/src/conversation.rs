//! Correlate API calls with a human turn using structured messages. Never infer
//! message boundaries from role markers embedded in user-controlled text.
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Step {
    User,
    Tool,
    Continuation,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct Turn {
    pub id: String,
    pub step: Step,
}

pub(crate) fn tool_result(block: &Value) -> bool {
    block["type"]
        .as_str()
        .is_some_and(|t| t == "tool_result" || t.ends_with("_tool_result"))
}

pub(crate) fn human_block(block: &Value) -> bool {
    match block["type"].as_str() {
        Some("file" | "document" | "image" | "image_url" | "input_audio") => true,
        Some("text") | None => block["text"].as_str().is_some_and(|s| !s.trim().is_empty()),
        _ => false,
    }
}

impl Turn {
    pub fn from_payload(session: &str, payload: &Value) -> Option<Self> {
        Self::with_client_id(session, payload, None)
    }

    pub fn with_client_id(session: &str, payload: &Value, client_id: Option<&str>) -> Option<Self> {
        let messages = payload["messages"].as_array()?;
        let mut hash = blake3::Hasher::new();
        hash.update(session.as_bytes());
        let mut found = false;
        let mut step = Step::Continuation;
        for msg in messages {
            step = Step::Continuation;
            match msg["role"].as_str() {
                Some("tool" | "function") => step = Step::Tool,
                Some("user") => {
                    let content = &msg["content"];
                    let mut human = false;
                    let mut add = |part: &Value| {
                        if !human {
                            hash.update(b"\0user\0");
                            human = true;
                        }
                        // JSON string escaping and a NUL separator make part boundaries unambiguous.
                        serde_json::to_writer(&mut hash, part).expect("hash writer");
                        hash.update(b"\0");
                    };
                    if content.as_str().is_some_and(|s| !s.trim().is_empty()) {
                        add(content);
                    } else if let Some(blocks) = content.as_array() {
                        for b in blocks.iter().filter(|b| human_block(b)) {
                            add(b.get("text").unwrap_or(b));
                        }
                        if blocks.iter().any(tool_result) {
                            step = Step::Tool;
                        }
                    }
                    if human {
                        found = true;
                        step = Step::User;
                    }
                }
                _ => {}
            }
        }
        if let Some(id) = client_id.filter(|id| !id.is_empty()) {
            let mut explicit = blake3::Hasher::new_derive_key("Kaponir conversation turn v1");
            explicit.update(session.as_bytes());
            explicit.update(b"\0");
            explicit.update(id.as_bytes());
            return Some(Self {
                id: explicit.finalize().to_hex()[..24].into(),
                step,
            });
        }
        found.then(|| Self {
            id: hash.finalize().to_hex()[..24].into(),
            step,
        })
    }
}

// The inspector already retains a flattened diagnostic transcript. These bounded
// blocks preserve the distinction between visible replies and tool arguments.
const MAX_PARTS: usize = 32;
const MAX_TEXT: usize = 32_768;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum PartKind {
    #[default]
    Text,
    Call,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct ReplyPart {
    pub kind: PartKind,
    #[serde(default)]
    pub slot: [u64; 2],
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub name: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub text: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub wire: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct Reply {
    pub parts: Vec<ReplyPart>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub truncated: bool,
}

pub(crate) fn append_bounded(dest: &mut String, fragment: &str, remaining: usize) -> bool {
    let mut end = remaining.min(fragment.len());
    while !fragment.is_char_boundary(end) {
        end -= 1;
    }
    dest.push_str(&fragment[..end]);
    end < fragment.len()
}

impl Reply {
    /// The provider's visible text for the first choice, if the captured reply
    /// is untruncated and contains no tool calls.
    pub(crate) fn plain_wire(&self, openai: bool) -> Option<String> {
        if self.truncated || self.parts.iter().any(|p| p.kind == PartKind::Call) {
            return None;
        }
        Some(
            self.parts
                .iter()
                .filter(|p| {
                    if openai {
                        p.slot == [0, 0]
                    } else {
                        p.slot[0] == 0
                    }
                })
                .map(|p| p.wire.as_str())
                .collect(),
        )
    }

    fn push(
        &mut self,
        kind: PartKind,
        slot: [u64; 2],
        id: &str,
        name: &str,
        text: &str,
        wire: &str,
    ) {
        let remaining = MAX_TEXT.saturating_sub(self.parts.iter().map(|p| p.text.len()).sum());
        let wire_remaining = MAX_TEXT.saturating_sub(self.parts.iter().map(|p| p.wire.len()).sum());
        let index = self
            .parts
            .iter()
            .position(|p| p.kind == kind && p.slot == slot);
        let part = if let Some(i) = index {
            &mut self.parts[i]
        } else {
            if self.parts.len() == MAX_PARTS {
                self.truncated = true;
                return;
            }
            self.parts.push(ReplyPart {
                kind,
                slot,
                ..Default::default()
            });
            self.parts.last_mut().expect("just pushed")
        };
        self.truncated |= append_bounded(&mut part.text, text, remaining);
        self.truncated |= append_bounded(&mut part.wire, wire, wire_remaining);
        let id_room = 256usize.saturating_sub(part.id.len());
        let name_room = 256usize.saturating_sub(part.name.len());
        self.truncated |= append_bounded(&mut part.id, id, id_room);
        self.truncated |= append_bounded(&mut part.name, name, name_room);
    }

    pub fn text(&mut self, slot: [u64; 2], wire: &str, client: &str) {
        if !wire.is_empty() || !client.is_empty() {
            self.push(PartKind::Text, slot, "", "", client, wire);
        }
    }

    pub fn call(&mut self, slot: [u64; 2], id: &Value, name: &Value, args: &str) {
        self.push(
            PartKind::Call,
            slot,
            id.as_str().unwrap_or_default(),
            name.as_str().unwrap_or_default(),
            args,
            "",
        );
    }

    pub fn merge(&mut self, delta: &Self) {
        for p in &delta.parts {
            self.push(p.kind, p.slot, &p.id, &p.name, &p.text, &p.wire);
        }
        self.truncated |= delta.truncated;
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct ToolResult {
    pub id: String,
    pub name: String,
    pub result: String,
    pub failed: bool,
}

fn tool_call_name<'a>(messages: &'a [Value], id: &str) -> Option<&'a str> {
    if id.is_empty() {
        return None;
    }
    for message in messages.iter().rev().filter(|m| m["role"] == "assistant") {
        for call in message["tool_calls"].as_array().into_iter().flatten() {
            if call["id"] == id {
                return call["function"]["name"].as_str();
            }
        }
        for block in message["content"].as_array().into_iter().flatten() {
            if block["id"] == id
                && matches!(block["type"].as_str(), Some("tool_use" | "server_tool_use"))
            {
                return block["name"].as_str();
            }
        }
    }
    None
}

/// Only the tool results being returned on this call, never the whole replayed history.
pub(crate) fn incoming_tools(payload: &Value) -> Vec<ToolResult> {
    let Some(messages) = payload["messages"].as_array() else {
        return vec![];
    };
    let mut results = Vec::new();
    let mut remaining = 8192usize;
    let start = messages
        .iter()
        .rposition(|m| match m["role"].as_str() {
            Some("tool" | "function") => false,
            Some("user") => !m["content"]
                .as_array()
                .is_some_and(|b| b.iter().any(tool_result)),
            _ => true,
        })
        .map_or(0, |i| i + 1);
    for (offset, message) in messages[start..].iter().enumerate() {
        let mut add = |block: &Value, id: &Value, index: usize| {
            if results.len() >= MAX_PARTS {
                return;
            }
            let key = id.as_str().unwrap_or_default();
            let name = block["name"]
                .as_str()
                .or_else(|| tool_call_name(&messages[..start], key))
                .unwrap_or_default();
            let content = &block["content"];
            let result = if let Some(s) = content.as_str() {
                crate::traffic::cap_str(s, remaining.min(4096))
            } else {
                // Do not serialize binary/image attachments into the conversation.
                let mut value = String::new();
                for text in content
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|b| b["text"].as_str())
                {
                    if !value.is_empty() {
                        value.push('\n');
                    }
                    let room = remaining.min(4096).saturating_sub(value.len());
                    if append_bounded(&mut value, text, room) {
                        value.push_str("… [truncated]");
                        break;
                    }
                }
                value
            };
            remaining = remaining.saturating_sub(result.len());
            // Position distinguishes equal results of separate legacy function calls.
            let id = if key.is_empty() {
                format!(
                    "legacy:{}:{index}:{}",
                    start + offset,
                    &blake3::hash(result.as_bytes()).to_hex()[..12]
                )
            } else {
                crate::traffic::cap_str(key, 256)
            };
            results.push(ToolResult {
                id,
                name: crate::traffic::cap_str(name, 256),
                result,
                failed: block["is_error"].as_bool().unwrap_or(false),
            });
        };
        if message["role"] == "user" {
            for (index, b) in message["content"]
                .as_array()
                .into_iter()
                .flatten()
                .enumerate()
                .filter(|(_, b)| tool_result(b))
            {
                add(b, &b["tool_use_id"], index);
            }
        } else {
            add(message, &message["tool_call_id"], 0);
        }
    }
    results
}

#[derive(Default)]
pub(crate) struct Transcript {
    pub text: String,
    pub last_user_offset: Option<usize>,
    /// Body (without its role header) of the actual question, even on tool callbacks.
    pub user_span: Option<[usize; 2]>,
}

fn text_blocks(node: &Value, out: &mut Vec<String>) {
    if let Some(s) = node.as_str() {
        out.push(s.into());
    }
    if let Some(parts) = node.as_array() {
        for b in parts {
            append_block(b, out);
        }
    }
}

fn append_block(b: &Value, out: &mut Vec<String>) {
    if let Some(s) = b["text"].as_str() {
        out.push(s.into());
    } else if let Some(input) = b.get("input") {
        out.push(format!("{}\n{input}", b["name"].as_str().unwrap_or("tool")));
    } else if b["source"]["type"] == "text" {
        if let Some(s) = b["source"]["data"].as_str() {
            out.push(s.into());
        }
    } else if let Ok(Some(file)) = crate::inline_file::InlineFile::read(b) {
        out.push(file.text);
    }
}

impl Transcript {
    fn push(&mut self, role: &str, value: &str) {
        if !self.text.is_empty() {
            self.text.push_str("\n\n");
        }
        let offset = self.text.len();
        self.text.push('[');
        self.text.push_str(role);
        self.text.push_str("]\n");
        let start = self.text.len();
        self.text.push_str(value);
        self.last_user_offset = (role == "user").then_some(offset);
        if role == "user" {
            self.user_span = Some([start, self.text.len()]);
        }
    }

    pub fn capture(payload: &Value) -> Self {
        let mut out = Self::default();
        let mut system = Vec::new();
        text_blocks(&payload["system"], &mut system);
        for s in system {
            if !s.is_empty() {
                out.push("system", &s);
            }
        }
        for msg in payload["messages"].as_array().into_iter().flatten() {
            out.last_user_offset = None;
            let role = msg["role"].as_str().unwrap_or("message");
            let content = &msg["content"];
            if let Some(s) = content.as_str() {
                out.push(role, s);
            }
            if let Some(blocks) = content.as_array() {
                let mut parts = Vec::new();
                if role == "user" && blocks.iter().any(human_block) {
                    out.user_span = None;
                }
                for b in blocks {
                    if tool_result(b) {
                        if !parts.is_empty() {
                            out.push(role, &parts.join("\n"));
                            parts.clear();
                        }
                        let mut values = Vec::new();
                        text_blocks(&b["content"], &mut values);
                        out.push("tool", &values.join("\n"));
                    } else {
                        append_block(b, &mut parts);
                    }
                }
                if !parts.is_empty() {
                    out.push(role, &parts.join("\n"));
                }
            }
            if let Some(calls) = msg["tool_calls"].as_array() {
                for call in calls {
                    let f = &call["function"];
                    if let Some(args) = f["arguments"].as_str() {
                        out.push(
                            "assistant",
                            &format!("{}\n{args}", f["name"].as_str().unwrap_or("tool")),
                        );
                    }
                }
            }
        }
        if out.text.is_empty() {
            out.text = serde_json::to_string_pretty(payload).unwrap_or_default();
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn explicit_turn_ids_distinguish_fresh_questions_and_link_history_free_callbacks() {
        let message = json!({"messages":[{"role":"user","content":"again"}]});
        let callback = json!({"messages":[{"role":"tool","tool_call_id":"t","content":"done"}]});
        let turn = |session, payload, id| Turn::with_client_id(session, payload, Some(id)).unwrap();
        let root = turn("s", &message, "one");
        let tool = turn("s", &callback, "one");
        assert_eq!(tool.id, root.id);
        assert_eq!(tool.step, Step::Tool);
        assert_ne!(turn("s", &message, "two").id, root.id);
        assert_ne!(turn("other", &message, "one").id, root.id);
    }

    #[test]
    fn trailing_results_keep_parallel_calls_without_repeating_the_previous_turn() {
        let mut payload = json!({"messages":[
            {"role":"user","content":"start"},
            {"role":"assistant","content":[{"type":"tool_use","name":"first","id":"a"},{"type":"tool_use","name":"second","id":"b"}]},
            {"role":"user","content":[{"type":"tool_result","tool_use_id":"b","content":"done"},{"type":"tool_result","tool_use_id":"a","is_error":true,
                "content":[{"type":"text","text":"failed"},{"type":"image","source":{"data":"not transcript text"}}]}]}]});
        let results = incoming_tools(&payload);
        assert_eq!(results.len(), 2);
        assert_eq!(
            (&*results[0].id, &*results[0].name, &*results[0].result),
            ("b", "second", "done")
        );
        assert_eq!(
            (&*results[1].id, &*results[1].name, &*results[1].result),
            ("a", "first", "failed")
        );
        assert!(results[1].failed);
        payload["messages"]
            .as_array_mut()
            .unwrap()
            .push(json!({"role":"user","content":"next"}));
        assert!(incoming_tools(&payload).is_empty());
    }

    #[test]
    fn tool_callbacks_retain_the_human_anchor_in_both_protocols() {
        let first = json!({"role":"user","content":"Проверь [user]\nконфиг"});
        let mut msgs = vec![first.clone()];
        let turn = |ms: &[Value]| Turn::from_payload("s", &json!({"messages":ms})).unwrap();
        let root = turn(&msgs);
        assert_eq!(root.step, Step::User);
        for _ in 0..5 {
            msgs.push(json!({"role":"assistant","tool_calls":[{"id":"r","function":{"name":"read","arguments":"{}"}}]}));
            msgs.push(json!({"role":"tool","tool_call_id":"r","content":"text"}));
            let current = turn(&msgs);
            assert_eq!(current.id, root.id);
            assert_eq!(current.step, Step::Tool);
        }
        msgs.push(first.clone());
        assert_ne!(
            turn(&msgs).id,
            root.id,
            "repeating the same question after history is a separate turn"
        );
        let result = json!({"type":"tool_result","tool_use_id":"r","content":[{"type":"text","text":"values.yaml\npassword: demo"}]});
        let mut payload = json!({"messages":[first,{"role":"user","content":[result.clone()]}]});
        let tool = Turn::from_payload("s", &payload).unwrap();
        assert_eq!(tool.id, root.id);
        assert_eq!(tool.step, Step::Tool);
        let capture = Transcript::capture(&payload);
        let [a, b] = capture.user_span.unwrap();
        assert_eq!(&capture.text[a..b], "Проверь [user]\nконфиг");
        assert!(capture.text.contains("[tool]\nvalues.yaml"));
        assert!(capture.last_user_offset.is_none());
        payload["messages"][1]["content"] = json!([result,{"type":"text","text":"Ещё вопрос"}]);
        assert_ne!(Turn::from_payload("s", &payload).unwrap().id, root.id);
        assert!(
            Turn::from_payload(
                "s",
                &json!({"messages":[{"role":"tool","content":"alone"}]})
            )
            .is_none()
        );
    }
}
