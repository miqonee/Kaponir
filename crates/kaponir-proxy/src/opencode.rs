//! OpenCode's native session IDs associate naming calls with conversations
//! and identify the direct parent of a child session.
use crate::{
    State, sse,
    traffic::{Event, Span},
};
use axum::http::HeaderMap;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    borrow::Cow,
    collections::{BTreeMap, BTreeSet},
};

fn session(headers: &HeaderMap) -> Option<&str> {
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

// OpenCode sends routing affinity and session identity separately. Only the
// native child ID paired with a direct parent ID establishes a relationship.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct SessionEvidence {
    native: bool,
    parent: Option<String>,
    spawn_prompt: bool,
    conflict: bool,
}

impl SessionEvidence {
    pub(crate) fn merge(&mut self, other: &Self) {
        self.native |= other.native;
        self.spawn_prompt |= other.spawn_prompt;
        self.conflict |= other.conflict
            || matches!((&self.parent, &other.parent),
            (Some(a), Some(b)) if a != b);
        if self.conflict {
            self.parent = None;
        } else if self.parent.is_none() {
            self.parent.clone_from(&other.parent);
        }
    }
}

pub(crate) struct Client<'a> {
    pub id: Option<&'a str>,
    pub known: bool,
    pub isolate: bool,
    pub evidence: Option<SessionEvidence>,
}

fn single_id<'a>(headers: &'a HeaderMap, key: &str) -> Option<&'a str> {
    let mut values = headers.get_all(key).iter();
    let id = values.next()?.to_str().ok()?;
    (values.next().is_none() && !id.is_empty() && !id.contains([',', ' ', '\t'])).then_some(id)
}

const SPAWN_PROMPT: &str = "You are a subagent spawned by another session.";

fn spawn_prompt(body: &Value) -> bool {
    let Some(first) = body["messages"].as_array().and_then(|messages| {
        messages
            .iter()
            .find(|m| !matches!(m["role"].as_str(), Some("system" | "developer")))
    }) else {
        return false;
    };
    let content = &first["content"];
    let text = content.as_str().or_else(|| {
        let part = content.as_array()?.first()?;
        (part["type"] == "text")
            .then(|| part["text"].as_str())
            .flatten()
    });
    first["role"] == "user" && text.and_then(|s| s.lines().next()) == Some(SPAWN_PROMPT)
}

pub(crate) fn client<'a>(headers: &'a HeaderMap, body: &Value) -> Client<'a> {
    let id = session(headers);
    let known =
        id.is_some() || user_agent(headers) || headers.contains_key("x-opencode-parent-session-id");
    let native = single_id(headers, "x-opencode-session-id");
    let spawn_prompt = spawn_prompt(body);
    let mut parent = None;
    let mut conflict = false;
    let mut has_parent = false;
    if known {
        for key in ["x-opencode-parent-session-id", "x-parent-session-id"] {
            if !headers.contains_key(key) {
                continue;
            }
            has_parent = true;
            match single_id(headers, key) {
                Some(candidate)
                    if Some(candidate) != native && parent.is_none_or(|p| p == candidate) =>
                {
                    parent = Some(candidate);
                }
                _ => conflict = true,
            }
        }
    }
    let isolate = native.is_none() && (has_parent || spawn_prompt);
    let evidence = (native.is_some() || isolate).then(|| SessionEvidence {
        native: native.is_some(),
        parent: parent
            .filter(|_| native.is_some() && !conflict)
            .map(|p| crate::server::session_tag(&format!("opencode:{p}"))),
        spawn_prompt,
        conflict: conflict || (has_parent && native.is_none()),
    });
    Client {
        id,
        known,
        isolate,
        evidence,
    }
}

/// Resolve against all retained and in-flight requests before applying UI filters.
pub(crate) fn session_relations<'a>(
    events: impl Iterator<Item = &'a Event>,
) -> BTreeMap<&'a str, Value> {
    let mut evidence = BTreeMap::<&str, SessionEvidence>::new();
    for event in events {
        if let (Some(id), Some(observation)) =
            (event.session_id.as_deref(), event.opencode.as_ref())
        {
            evidence.entry(id).or_default().merge(observation);
        }
    }
    let mut result = BTreeMap::new();
    for (&id, observed) in &evidence {
        let kind = match (
            observed.parent.is_some(),
            observed.spawn_prompt,
            observed.conflict,
        ) {
            (true, true, _) => "subagent",
            (true, false, _) => "child",
            (false, true, _) => "possible_subagent",
            (false, false, true) => "child",
            _ => continue,
        };
        let reported = observed.parent.as_deref();
        let mut parent = reported.filter(|p| evidence.get(p).is_some_and(|e| e.native));
        let pending = reported.filter(|_| parent.is_none());
        let mut seen = BTreeSet::from([id]);
        let mut ancestor = parent;
        while let Some(next) = ancestor {
            if !seen.insert(next) {
                parent = None;
                break;
            }
            ancestor = evidence.get(next).and_then(|e| e.parent.as_deref());
        }
        let mut relation = json!({"kind":kind,"parent_id":parent});
        if let Some(pending) = pending {
            relation["pending_parent_id"] = json!(pending);
        }
        result.insert(id, relation);
    }
    result
}

fn user_agent(headers: &HeaderMap) -> bool {
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

const FIRST: &str = "Here is the first message of the conversation:";
const HISTORY: &str = "Here is the conversation so far:";

pub(crate) enum TitleRequest {
    /// Reconstruct the user's first message for normal session inference.
    WrappedFirst(Value),
    /// The original question was already present before the naming prompt.
    WithOriginal,
    /// History summaries and older prompts need a client-supplied session ID.
    NeedsId,
}

fn text_content(content: &Value) -> Option<Cow<'_, str>> {
    if let Some(text) = content.as_str() {
        return Some(Cow::Borrowed(text));
    }
    let parts = content.as_array()?;
    if let [part] = parts.as_slice() {
        return (part["type"] == "text")
            .then(|| part["text"].as_str())
            .flatten()
            .map(Cow::Borrowed);
    }
    let mut text = String::new();
    for part in parts {
        if part["type"] != "text" {
            return None;
        }
        if !text.is_empty() {
            text.push('\n');
        }
        text.push_str(part["text"].as_str()?);
    }
    Some(Cow::Owned(text))
}

fn naming_text(content: &Value) -> Option<Cow<'_, str>> {
    if any_text(content, |s| {
        let s = s.trim_start();
        s.starts_with(FIRST) || s.starts_with(HISTORY)
    }) {
        text_content(content)
    } else {
        None
    }
}

fn envelope(text: &str) -> Option<(&str, bool)> {
    let (header, rest) = text.trim().split_once('\n')?;
    let (open, close, first) = match header.trim_end() {
        FIRST => ("<message>", "</message>", true),
        HISTORY => ("<conversation>", "</conversation>", false),
        _ => return None,
    };
    let (body, instruction) = rest.trim().rsplit_once('\n')?;
    if instruction.trim() != "Reply with only the title." {
        return None;
    }
    let inner = body.trim().strip_prefix(open)?.strip_suffix(close)?;
    // Remove the delimiters' line breaks, preserving whitespace belonging to
    // the actual question. Session inference hashes its exact JSON content.
    let inner = inner
        .strip_prefix("\r\n")
        .or_else(|| inner.strip_prefix('\n'))
        .unwrap_or(inner);
    let inner = inner
        .strip_suffix("\r\n")
        .or_else(|| inner.strip_suffix('\n'))
        .unwrap_or(inner);
    (!inner.trim().is_empty()).then_some((inner, first))
}

fn user_message(text: &str, blocks: bool) -> Value {
    let content = if blocks {
        json!([{"type":"text","text":text}])
    } else {
        json!(text)
    };
    json!({"role":"user","content":content})
}

pub(crate) fn title_request(body: &Value, known_client: bool) -> Option<TitleRequest> {
    let messages = body["messages"].as_array()?;
    let mut dialogue = messages
        .iter()
        .filter(|m| !matches!(m["role"].as_str(), Some("system" | "developer")));
    let first = dialogue.next()?;
    if first["role"] != "user" {
        return None;
    }
    let content = &first["content"];
    // This complete envelope survives gateways that replace system prompts or
    // strip client headers. Quoted instructions, follow-up turns and tool
    // messages must stay in the conversation.
    let title_agent = || {
        let agent = |s: &str| {
            s.lines()
                .any(|line| line.trim_start().starts_with("You are a title generator."))
        };
        known_client
            && (any_text(&body["system"], agent)
                || messages.iter().any(|m| {
                    matches!(m["role"].as_str(), Some("system" | "developer"))
                        && any_text(&m["content"], agent)
                }))
    };
    let next = dialogue.next();
    if let Some(next) = next
        && next["role"] == "user"
        && dialogue.next().is_none()
        && let Some(text) = naming_text(&next["content"])
        && let Some((inner, true)) = envelope(&text)
        && text_content(content).as_deref() == Some(inner)
    {
        return Some(TitleRequest::WithOriginal);
    }
    if let Some(text) = naming_text(content)
        && let Some((inner, first)) = envelope(&text)
        && (next.is_none() || title_agent())
    {
        return Some(if first {
            TitleRequest::WrappedFirst(user_message(inner, content.is_array()))
        } else {
            TitleRequest::NeedsId
        });
    }
    let legacy = |s: &str| {
        s.trim()
            .lines()
            .next()
            .is_some_and(|s| s.trim() == "Generate a title for this conversation:")
    };
    (known_client && any_text(content, legacy) && title_agent()).then_some(TitleRequest::NeedsId)
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

fn save_title(
    st: &State,
    mask_session: &str,
    tag: &str,
    started: u64,
    id: u64,
    line: &str,
) -> anyhow::Result<()> {
    let mut text = st.with(mask_session, true, |cx| cx.mask(line))?;
    if text.chars().count() > 100 {
        text = text.chars().take(97).collect();
        text.push_str("...");
    }
    st.with_vault(|_, vault| Ok(vault.save_session_title(tag, started, id, &text)?))
}

pub(crate) fn capture_title(st: &State, session: &str, span: &Span, openai: bool) {
    let line = span
        .plain_reply_wire(openai)
        .as_deref()
        .and_then(title_line);
    let tag = span
        .session
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .clone();
    if let (Some(line), Some(session_info)) = (line, tag)
        && save_title(
            st,
            session,
            &session_info.tag,
            span.started_at_ms,
            span.id,
            &line,
        )
        .is_err()
    {
        eprintln!("kaponir: could not store OpenCode session title");
    }
}

/// Repair only retained structured generation requests. Saving the title first
/// makes a retry after an interrupted event rewrite safe (titles are versioned).
pub(crate) fn repair_event(st: &State, event: &mut Event) -> anyhow::Result<()> {
    let orphan = event.request_kind == "session_title" && event.session_id.is_none();
    let token_count = event.request_kind == "count_tokens";
    if !(orphan || token_count || matches!(event.request_kind.as_str(), "completion" | "")) {
        return Ok(());
    }
    let Some(insp) = &event.inspector else {
        return Ok(());
    };
    let known = event.session_source.as_deref() == Some("opencode");
    let payload = &insp.masked_payload_json;
    if !payload.contains(FIRST)
        && !payload.contains(HISTORY)
        && !(known && payload.contains("Generate a title for this conversation:"))
    {
        return Ok(());
    }
    let Some(title) = serde_json::from_str(payload)
        .ok()
        .and_then(|body| title_request(&body, known))
    else {
        return Ok(());
    };
    if orphan || event.session_source.as_deref() == Some("inferred") {
        // Inference hashes original input. Masked placeholders would produce
        // a different session ID, so recover the structured user span first.
        let original = insp
            .turn_excerpt
            .as_ref()
            .map(|e| e.original.as_str())
            .or_else(|| {
                let [a, b] = insp.user_span?;
                insp.original_request.get(a..b)
            });
        event.session_id = match title {
            TitleRequest::WrappedFirst(msg) => {
                original.and_then(envelope).and_then(|(text, first)| {
                    first.then(|| {
                        let user = user_message(text, msg["content"].is_array());
                        crate::server::session_tag(&crate::server::inferred_session(
                            &event.provider,
                            Some(&user),
                        ))
                    })
                })
            }
            TitleRequest::WithOriginal => event.session_id.take(),
            TitleRequest::NeedsId => None,
        };
        if orphan && event.session_id.is_none() {
            return Ok(());
        }
        event.session_source = event.session_id.as_ref().map(|_| "inferred".into());
    }
    event.request_kind = "session_title".into();
    event.turn = None;
    if !token_count
        && event.sent
        && event.outcome == "ok"
        && (200..300).contains(&event.status)
        && let Some(tag) = &event.session_id
    {
        // Legacy inspectors elided an untruncated plain reply when wire text
        // matched raw_response. An explicitly empty reply means reasoning only.
        let line = match &insp.reply {
            Some(reply) => reply
                .plain_wire(event.provider == "openai")
                .as_deref()
                .and_then(title_line),
            None if !insp.raw_response.contains("… [truncated]") => {
                title_line(&insp.raw_response)
            }
            None => None,
        };
        if let Some(line) = line {
            save_title(
                st,
                &format!("title-request:{}", event.id),
                tag,
                event.started_at_ms,
                event.id,
                &line,
            )?;
        }
    }
    let bytes = serde_json::to_vec(event)?;
    st.with_vault(|_, v| Ok(v.save_traffic_event(event.id, event.time_ms / 1000, &bytes, 0)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn templates_require_a_complete_plain_text_service_message() {
        let body = |content: Value| json!({"messages":[{"role":"system","content":"Gateway preamble"},{"role":"user","content":content}]});
        let text = format!(
            "{FIRST}\r\n\r\n<message>\r\nГде отчёт?\r\n</message>\r\n\r\nReply with only the title."
        );
        let Some(TitleRequest::WrappedFirst(first)) = title_request(&body(json!(text)), false)
        else {
            panic!("missing first question")
        };
        assert_eq!(first["content"], "Где отчёт?");
        let parts = json!([{"type":"text","text":FIRST},{"type":"text","text":"<message>Где отчёт?</message>"},{"type":"text","text":"Reply with only the title."}]);
        let Some(TitleRequest::WrappedFirst(parsed)) = title_request(&body(parts), false) else {
            panic!("missing question in text blocks")
        };
        assert_eq!(parsed["content"][0]["text"], "Где отчёт?");
        let history =
            format!("{HISTORY}\n<conversation>Example</conversation>\nReply with only the title.");
        assert!(matches!(
            title_request(&body(json!(history)), false),
            Some(TitleRequest::NeedsId)
        ));
        let mut duplicate = body(json!("Где отчёт?"));
        duplicate["messages"]
            .as_array_mut()
            .unwrap()
            .push(json!({"role":"user","content":text}));
        assert!(matches!(
            title_request(&duplicate, false),
            Some(TitleRequest::WithOriginal)
        ));
        duplicate["messages"][1]["content"] = json!("Другой вопрос");
        assert!(title_request(&duplicate, false).is_none());
        for ordinary in [
            text.replace("</message>", ""),
            format!("{text}\nExplain."),
            format!("Quote: {text}"),
            text.replace("Где отчёт?", ""),
            "Reply with only the title.".to_string(),
        ] {
            assert!(title_request(&body(json!(ordinary)), true).is_none());
        }
        let mut dialogue = body(json!(text));
        dialogue["messages"]
            .as_array_mut()
            .unwrap()
            .push(json!({"role":"user","content":"Сравни ответы"}));
        assert!(title_request(&dialogue, true).is_none());
        assert!(
            title_request(
                &body(json!([{"type":"text","text":text},{"type":"image"}])),
                true
            )
            .is_none()
        );
    }
}
