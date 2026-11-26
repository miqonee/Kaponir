//! Local, first-line instructions in structured user messages. A stored receipt binds
//! a disclosure to the original recipient even when another connection replays the chat.
use crate::conversation::{Turn, human_block, tool_result};
use aho_corasick::AhoCorasick;
use axum::http::HeaderMap;
use kaponir_core::PositionEdits;
use kaponir_vault::SqliteVault;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, HashSet};
use std::ops::Range;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
enum Action {
    Open,
    Hide,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Command {
    action: Action,
    session: bool,
    values: Vec<String>,
}

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub(crate) struct Invalid(pub &'static str);

fn word(s: &str) -> (&str, &str) {
    let (head, tail) = s.split_at(s.find(char::is_whitespace).unwrap_or(s.len()));
    (head, tail.trim_start())
}

fn directive(line: &str) -> Option<Result<(Action, &str), Invalid>> {
    if !line.starts_with(['k', 'K', 'к', 'К']) {
        return None;
    }
    let (prefix, rest) = word(line);
    if !(prefix.eq_ignore_ascii_case("kpn")
        || (prefix.len() == "кпн".len() && prefix.to_lowercase() == "кпн"))
    {
        return None;
    }
    let (verb, rest) = word(rest);
    let action = match verb.to_lowercase().as_str() {
        "открой" | "раскрой" | "unmask" => Action::Open,
        "скрой" | "mask" => Action::Hide,
        _ => {
            return Some(Err(Invalid(
                "Неизвестная команда Kaponir. Используйте «скрой» или «открой»",
            )));
        }
    };
    Some(Ok((action, rest)))
}

fn parse(action: Action, rest: &str) -> Result<Command, Invalid> {
    let (flag, tail) = word(rest);
    let session = matches!(flag, "-с" | "-c" | "-s" | "-С" | "-C" | "-S");
    if flag.starts_with('-') && !session {
        return Err(Invalid(
            "Для сессии используйте флаг -с. Значение с начальным дефисом заключите в кавычки",
        ));
    }
    let mut rest = if session { tail } else { rest };
    let mut values = Vec::new();
    loop {
        let value;
        if let Some(quoted) = rest.strip_prefix('"') {
            let mut decoded = String::new();
            let mut quoted = quoted;
            loop {
                let (part, tail) = quoted
                    .split_once('"')
                    .ok_or(Invalid("Незакрытые кавычки в команде Kaponir"))?;
                decoded.push_str(part);
                if let Some(next) = tail.strip_prefix('"') {
                    decoded.push('"');
                    quoted = next;
                } else {
                    rest = tail.trim_start();
                    break;
                }
            }
            value = decoded;
        } else {
            let (part, tail) = rest.split_at(rest.find(';').unwrap_or(rest.len()));
            value = part.trim().to_owned();
            rest = tail;
        }
        if value.trim().is_empty() {
            return Err(Invalid(
                "Укажите слово или фразу для каждого элемента списка Kaponir",
            ));
        }
        values.push(value);
        if rest.is_empty() {
            break;
        }
        rest = rest
            .strip_prefix(';')
            .ok_or(Invalid(
                "После кавычек ожидается разделитель списка Kaponir",
            ))?
            .trim_start();
    }
    Ok(Command {
        action,
        session,
        values,
    })
}

fn first_text(content: &Value) -> Option<(usize, &str)> {
    if let Some(s) = content.as_str() {
        return Some((0, s));
    }
    let blocks = content.as_array()?;
    if blocks.iter().any(tool_result) {
        return None;
    }
    let (i, first) = blocks
        .iter()
        .enumerate()
        .find(|(_, b)| b["type"] == "text")?;
    Some((i, first["text"].as_str()?))
}

fn first_line(s: &str) -> &str {
    s.split('\n')
        .next()
        .unwrap_or_default()
        .trim_end_matches('\r')
}

#[derive(Serialize, Deserialize)]
struct Receipt {
    turn: String,
    history: String,
    recipient: String,
    command: Command,
}

#[derive(Default)]
pub(crate) struct Request {
    pub turn: Option<Turn>,
    current: Option<Command>,
    history: HashSet<String>,
    last_command_id: String,
    service_values: Vec<String>,
    signed_history: bool,
}

impl Request {
    pub fn read(
        session: &str,
        body: &mut Value,
        client_turn: Option<&str>,
        title: bool,
    ) -> Result<Self, Invalid> {
        let mut req = Self::default();
        req.turn = Turn::scan(session, body, client_turn, |msg, hash| {
            if first_text(&msg["content"])
                .and_then(|(_, s)| directive(first_line(s)))
                .is_some()
            {
                let id = hash.finalize().to_hex()[..24].to_owned();
                req.history.insert(id.clone());
                req.last_command_id = id;
            }
        });
        let Some(messages) = body.get_mut("messages").and_then(Value::as_array_mut) else {
            return Ok(req);
        };
        let last_dialogue = messages
            .iter()
            .rposition(|m| !matches!(m["role"].as_str(), Some("system" | "developer")));
        req.signed_history = messages.iter().any(|m| {
            m["role"] == "assistant"
                && m["content"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|b| b["type"] == "thinking"))
        });
        for (i, msg) in messages
            .iter_mut()
            .enumerate()
            .filter(|(_, m)| m["role"] == "user")
        {
            let Some(content) = msg.get_mut("content") else {
                continue;
            };
            if title {
                // Naming calls can wrap the original question in a client-generated
                // envelope. Strip control lines wherever that envelope copied them.
                match content {
                    Value::String(s) => clean_title(s, &mut req.service_values)?,
                    Value::Array(a) => {
                        for b in a.iter_mut().filter(|b| b["type"] == "text") {
                            if let Some(Value::String(s)) = b.get_mut("text") {
                                clean_title(s, &mut req.service_values)?;
                            }
                        }
                    }
                    _ => {}
                }
                continue;
            }
            let Some((block, s)) = first_text(content) else {
                continue;
            };
            let Some(parsed) = directive(first_line(s)) else {
                continue;
            };
            let current = Some(i) == last_dialogue;
            if current {
                let (action, rest) = parsed?;
                req.current = Some(parse(action, rest)?);
            }
            let consumed = s.find('\n').map_or(s.len(), |n| n + 1);
            match content {
                Value::String(s) => {
                    s.drain(..consumed);
                }
                Value::Array(parts) => {
                    if let Value::String(s) = &mut parts[block]["text"] {
                        s.drain(..consumed);
                        if s.trim().is_empty() {
                            parts.remove(block);
                        }
                    }
                }
                _ => unreachable!("first_text accepts only strings and text blocks"),
            }
            if current
                && !(content.as_str().is_some_and(|s| !s.trim().is_empty())
                    || content
                        .as_array()
                        .is_some_and(|a| a.iter().any(human_block)))
            {
                return Err(Invalid("Напишите вопрос после строки команды Kaponir"));
            }
        }
        Ok(req)
    }

    pub fn resolve(
        &self,
        vault: &SqliteVault,
        chat: &str,
        recipient: &str,
        inferred: bool,
        auxiliary: bool,
    ) -> anyhow::Result<Active> {
        let mut receipts: Vec<Receipt> = vault
            .chat_commands(chat)?
            .map(|v| serde_json::from_slice(&v))
            .transpose()?
            .unwrap_or_default();
        let current_id = self.turn.as_ref().map(|t| t.id.as_str());
        if !auxiliary && let (Some(command), Some(turn)) = (&self.current, current_id) {
            if let Some(old) = receipts.iter().find(|r| r.turn == turn) {
                if old.command != *command {
                    return Err(Invalid("Для новой команды Kaponir нужен новый вопрос").into());
                }
            } else {
                receipts.push(Receipt {
                    turn: turn.to_owned(),
                    history: self.last_command_id.clone(),
                    recipient: recipient.to_owned(),
                    command: command.clone(),
                });
                vault.save_chat_commands(chat, &serde_json::to_vec(&receipts)?)?;
            }
        }
        let mut rules = BTreeMap::new();
        for r in &receipts {
            if !r.command.session && Some(r.turn.as_str()) != current_id {
                continue;
            }
            let witnessed = !inferred || self.history.contains(&r.history);
            for value in &r.command.values {
                if !witnessed {
                    // Replaying an old branch cannot reverse a later revocation.
                    // An unwitnessed disclosure cannot erase a witnessed mask.
                    if r.command.action == Action::Hide && rules.get(value) == Some(&Action::Open) {
                        rules.insert(value.clone(), Action::Hide);
                    }
                } else {
                    let action = if auxiliary || r.recipient != recipient {
                        Action::Hide
                    } else {
                        r.command.action
                    };
                    rules.insert(value.clone(), action);
                }
            }
        }
        if auxiliary {
            for value in self
                .service_values
                .iter()
                .chain(self.current.iter().flat_map(|c| &c.values))
            {
                rules.insert(value.to_owned(), Action::Hide);
            }
        }
        let mut active = Active::from_rules(rules)?;
        if self.signed_history && !receipts.is_empty() {
            // An expired one-question grant can still appear in signed history.
            let terms: HashSet<_> = receipts
                .iter()
                .filter(|r| !inferred || self.history.contains(&r.history))
                .flat_map(|r| r.command.values.iter())
                .chain(self.current.iter().flat_map(|c| &c.values))
                .chain(&self.service_values)
                .collect();
            if !terms.is_empty() {
                active.signed_terms = Some(AhoCorasick::new(terms)?);
            }
        }
        Ok(active)
    }
}

fn clean_title(text: &mut String, values: &mut Vec<String>) -> Result<(), Invalid> {
    let mut clean = String::with_capacity(text.len());
    for line in text.split_inclusive('\n') {
        let content = line.trim_end_matches(['\r', '\n']);
        let tag = ["<message>", "<conversation>"]
            .into_iter()
            .find(|tag| content.starts_with(tag))
            .unwrap_or("");
        let Some(parsed) = directive(&content[tag.len()..]) else {
            clean.push_str(line);
            continue;
        };
        let (action, rest) = parsed?;
        values.extend(parse(action, rest)?.values);
        if !tag.is_empty() {
            clean.push_str(tag);
            clean.push('\n');
        }
    }
    *text = clean;
    Ok(())
}

/// Session IDs identify a chat, not the credential that authorized a disclosure.
pub(crate) fn namespace(session: &str, owner: &str, headers: &HeaderMap) -> String {
    fingerprint(
        "Kaponir chat command caller v1",
        [session.as_bytes(), owner.as_bytes()].into_iter().chain(
            ["authorization", "x-api-key", "x-kaponir-token"]
                .map(|h| headers.get(h).map_or(&[][..], |v| v.as_bytes())),
        ),
    )
}

pub(crate) fn recipient(
    protocol: &str,
    default_url: &str,
    provider: Option<&crate::providers::Provider>,
) -> String {
    let (id, url, key) = provider.map_or(("", default_url, ""), |p| {
        (&*p.id, &*p.base_url, &*p.api_key)
    });
    fingerprint(
        "Kaponir chat command recipient v1",
        [protocol, id, url, key].map(str::as_bytes),
    )
}

fn fingerprint<'a>(domain: &'static str, parts: impl IntoIterator<Item = &'a [u8]>) -> String {
    let mut hash = blake3::Hasher::new_derive_key(domain);
    for data in parts {
        hash.update(&(data.len() as u64).to_le_bytes());
        hash.update(data);
    }
    hash.finalize().to_hex().to_string()
}

#[derive(Clone, Default)]
pub(crate) struct Active {
    matcher: Option<AhoCorasick>,
    actions: Vec<Action>,
    signed_terms: Option<AhoCorasick>,
}

fn whole_value(text: &str, range: &Range<usize>) -> bool {
    let ident = |c: char| c.is_alphanumeric() || c == '_';
    let value = &text[range.clone()];
    !(value.chars().next().is_some_and(ident)
        && text[..range.start].chars().next_back().is_some_and(ident)
        || value.chars().next_back().is_some_and(ident)
            && text[range.end..].chars().next().is_some_and(ident))
}

impl Active {
    fn from_rules(rules: BTreeMap<String, Action>) -> anyhow::Result<Self> {
        if rules.is_empty() {
            return Ok(Self::default());
        }
        let matcher = AhoCorasick::new(rules.keys())?;
        Ok(Self {
            matcher: Some(matcher),
            actions: rules.into_values().collect(),
            signed_terms: None,
        })
    }

    fn matches(&self, text: &str, mut visit: impl FnMut(Range<usize>, usize)) {
        let Some(matcher) = &self.matcher else { return };
        for m in matcher.find_overlapping_iter(text) {
            let r = m.start()..m.end();
            if whole_value(text, &r) {
                visit(r, m.pattern().as_usize());
            }
        }
    }

    pub fn open_in(&self, text: &str) -> bool {
        let mut open = false;
        self.matches(text, |_, id| open |= self.actions[id] == Action::Open);
        open
    }

    pub fn has_open(&self) -> bool {
        self.actions.contains(&Action::Open)
    }

    pub fn protect_overlaps(&mut self, text: &str, protected: &[Range<usize>]) {
        if protected.is_empty() {
            return;
        }
        let mut hide = Vec::new();
        self.matches(text, |r, id| {
            if protected.iter().any(|p| p.start < r.end && r.start < p.end) {
                hide.push(id);
            }
        });
        for id in hide {
            self.actions[id] = Action::Hide;
        }
    }

    pub fn signed_mentions(&self, text: &str) -> Vec<Range<usize>> {
        let mut spans = Vec::new();
        if let Some(matcher) = &self.signed_terms {
            spans.extend(
                matcher
                    .find_overlapping_iter(text)
                    .map(|m| m.start()..m.end())
                    .filter(|r| whole_value(text, r)),
            );
        } else {
            self.matches(text, |r, _| spans.push(r));
        }
        spans
    }
    pub fn may_match(&self, text: &str) -> bool {
        self.matcher.as_ref().is_some_and(|m| m.is_match(text))
    }

    pub fn edits(&self, text: &str, base: &mut Option<PositionEdits>) {
        self.matches(text, |r, id| {
            let edits = base.get_or_insert_with(Default::default);
            match self.actions[id] {
                Action::Open => edits.chat_open.push(r),
                Action::Hide => edits.chat_hide.push(r),
            }
        });
    }

    pub(crate) fn redact_for_learning(&self, text: &mut String) {
        let mut ranges = Vec::new();
        self.matches(text, |r, _| ranges.push(r));
        if ranges.is_empty() {
            return;
        }
        ranges.sort_unstable_by_key(|r| r.start);
        let mut out = String::new();
        let mut end = 0;
        for r in ranges {
            if r.start >= end {
                out.push_str(&text[end..r.start]);
                out.push_str("[скрыто]");
            }
            end = end.max(r.end);
        }
        out.push_str(&text[end..]);
        *text = out;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn hidden_headers_are_removed_from_multipart_history_and_auxiliary_titles() {
        let mut body = json!({"messages":[{"role":"user","content":[
            {"type":"document","source":{"type":"text","data":"kpn mask from a document"}},
            {"type":"text","text":"КПН СКРОЙ -s \"А; Б\"; Северка\r\n"},
            {"type":"text","text":"А; Б и Северка"}]}]});
        let cmd = Request::read("one", &mut body, None, false).unwrap();
        assert_eq!(body["messages"][0]["content"].as_array().unwrap().len(), 2);
        let active = cmd
            .resolve(
                &SqliteVault::open_in_memory(&[7; 32]).unwrap(),
                "scope",
                "route",
                false,
                false,
            )
            .unwrap();
        let mut capture = crate::learning::question::capture(&body["messages"]);
        capture.commands = active;
        capture.answer.append([0, 0], "А; Б и Сев");
        capture.answer.append([0, 0], "ерка");
        let captured = capture.into_input();
        assert_eq!(captured["parts"][0]["content"], "[скрыто] и [скрыто]");
        assert_eq!(captured["answer"], "[скрыто] и [скрыто]");
        for opening in ["<message>\n", "<message>"] {
            let text = format!(
                "Here is the first message of the conversation:\n{opening}кпн открой -с Северка\nНазови Северка\n</message>\nReply with only the title."
            );
            let mut title = json!({"messages":[{"role":"user","content":text}]});
            let service = Request::read("one", &mut title, None, true).unwrap();
            assert!(!title.to_string().contains("кпн"));
            assert_eq!(service.service_values, ["Северка"]);
            assert!(service.current.is_none());
        }
    }

    #[test]
    fn encrypted_command_lifetime_survives_restart_and_expires_with_session_purge() {
        let key = kaponir_vault::generate_key().unwrap();
        let path =
            std::env::temp_dir().join(format!("kpn-chat-{}.db", kaponir_vault::key_to_hex(&key)));
        let read = |s: &str| {
            Request::read(
                "s",
                &mut json!({"messages":[{"role":"user","content":s}]}),
                None,
                false,
            )
            .unwrap()
        };
        {
            let vault = SqliteVault::open(&path, &key).unwrap();
            read("kpn unmask -s private-fixture-marker\nquestion")
                .resolve(&vault, "chat", "route", false, false)
                .unwrap();
        }
        assert!(
            !std::fs::read(&path)
                .unwrap()
                .windows(b"private-fixture-marker".len())
                .any(|b| b == b"private-fixture-marker")
        );
        {
            let vault = SqliteVault::open(&path, &key).unwrap();
            let request = read("next question");
            assert!(
                request
                    .resolve(&vault, "chat", "route", false, false)
                    .unwrap()
                    .open_in("private-fixture-marker")
            );
            assert!(
                !request
                    .resolve(&vault, "chat", "other", false, false)
                    .unwrap()
                    .open_in("private-fixture-marker")
            );
            vault.purge_sessions(-1).unwrap();
            assert!(vault.chat_commands("chat").unwrap().is_none());
        }
        std::fs::remove_file(path).unwrap();
    }
}
