//! OpenCode names a chat with a separate model call. Its stable session ID,
//! rather than the generated text or the provider connection, links the calls.
use crate::{Shared, sse, traffic::Span};
use axum::http::HeaderMap;
use serde_json::Value;

pub(crate) fn session(headers: &HeaderMap) -> Option<&str> {
    ["x-opencode-session-id", "x-opencode-session"]
        .into_iter()
        .find_map(|key| {
            headers
                .get(key)?
                .to_str()
                .ok()
                .filter(|s| !s.trim().is_empty())
        })
        .or_else(|| {
            if !user_agent(headers) {
                return None;
            }
            headers
                .get("x-session-id")?
                .to_str()
                .ok()
                .filter(|s| !s.trim().is_empty())
        })
}

pub(crate) fn user_agent(headers: &HeaderMap) -> bool {
    headers
        .get("user-agent")
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.split_once('/'))
        .is_some_and(|(name, _)| name.eq_ignore_ascii_case("opencode"))
}

fn any_text(content: &Value, matches: impl Fn(&str) -> bool) -> bool {
    content.as_str().is_some_and(&matches)
        || content.as_array().is_some_and(|parts| {
            parts
                .iter()
                .any(|p| p["type"] == "text" && p["text"].as_str().is_some_and(&matches))
        })
}

pub(crate) fn is_title_call(body: &Value) -> bool {
    let Some(messages) = body["messages"].as_array() else {
        return false;
    };
    let title_agent = |s: &str| {
        s.lines()
            .any(|line| line.trim_start().starts_with("You are a title generator."))
    };
    let system = any_text(&body["system"], title_agent)
        || messages.iter().any(|m| {
            matches!(m["role"].as_str(), Some("system" | "developer"))
                && any_text(&m["content"], title_agent)
        });
    system
        && messages
            .iter()
            .find(|m| m["role"] == "user")
            .is_some_and(|m| {
                any_text(&m["content"], |s| {
                    let s = s.trim();
                    s.lines()
                        .next()
                        .is_some_and(|s| s.trim() == "Generate a title for this conversation:")
                        || ((s.starts_with(
                            "Here is the first message of the conversation:\n\n<message>",
                        ) || s
                            .starts_with("Here is the conversation so far:\n\n<conversation>"))
                            && s.ends_with("Reply with only the title."))
                })
            })
}

/// EOF alone can mean a truncated response. Some clients close as soon as
/// they receive the protocol's final event, before the transport reaches EOF.
pub(crate) fn stream_finished(event: &sse::Event) -> Option<bool> {
    match event.event.as_deref() {
        Some("error") => return Some(false),
        Some("message_stop") => return Some(true),
        _ => {}
    }
    if event.data.trim() == "[DONE]" {
        return Some(true);
    }
    let data: Value = serde_json::from_str(&event.data).ok()?;
    completion_status(&data)
}

pub(crate) fn completion_status(data: &Value) -> Option<bool> {
    if data.get("error").is_some() || data["type"] == "error" {
        return Some(false);
    }
    if data["type"] == "message_stop" {
        return Some(true);
    }
    data.pointer("/choices/0/finish_reason")
        .and_then(Value::as_str)
        .map(|s| s == "stop")
        .or_else(|| {
            data.pointer("/delta/stop_reason")
                .or_else(|| data.get("stop_reason"))
                .and_then(Value::as_str)
                .map(|s| matches!(s, "end_turn" | "stop_sequence"))
        })
}

fn title_line(mut wire: &str) -> Option<String> {
    let mut visible = String::new();
    while let Some((before, thought)) = wire.split_once("<think>") {
        visible.push_str(before);
        wire = thought.split_once("</think>")?.1;
    }
    visible.push_str(wire);
    let line = visible.lines().map(str::trim).find(|s| !s.is_empty())?;
    if line.len() > 4096 || line.contains("</think>") {
        return None;
    }
    let text: String = line
        .chars()
        .filter(|c| {
            !c.is_control() && !matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        })
        .collect();
    (!text.trim().is_empty()).then(|| text.trim().to_owned())
}

pub(crate) fn capture_title(st: &Shared, session: &str, span: &Span, openai: bool) {
    let Some(wire) = span.plain_reply_wire(openai) else {
        return;
    };
    let Some(line) = title_line(&wire) else {
        return;
    };
    // The inspector also contains restored text. Only the provider's visible
    // reply is eligible, and new sensitive values in it go through local rules.
    let result = st
        .with(session, true, |cx| cx.mask(&line))
        .and_then(|mut text| {
            if text.chars().count() > 100 {
                text = text.chars().take(97).collect();
                text.push_str("...");
            }
            let tag = super::server::session_tag(session);
            st.with_vault(|_, vault| {
                Ok(vault.save_session_title(&tag, span.started_at_ms, span.id, &text)?)
            })
        });
    if result.is_err() {
        eprintln!("kaponir: could not store OpenCode session title");
    }
}
