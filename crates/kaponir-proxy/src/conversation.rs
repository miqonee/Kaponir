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

fn tool_result(block: &Value) -> bool {
    block["type"]
        .as_str()
        .is_some_and(|t| t == "tool_result" || t.ends_with("_tool_result"))
}

fn human_block(block: &Value) -> bool {
    match block["type"].as_str() {
        Some("file" | "document" | "image" | "image_url" | "input_audio") => true,
        Some("text") | None => block["text"].as_str().is_some_and(|s| !s.trim().is_empty()),
        _ => false,
    }
}

impl Turn {
    pub fn from_payload(session: &str, payload: &Value) -> Option<Self> {
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
        found.then(|| Self {
            id: hash.finalize().to_hex()[..24].into(),
            step,
        })
    }
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
