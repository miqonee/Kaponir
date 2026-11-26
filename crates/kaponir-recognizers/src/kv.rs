//! Проход по ключам конфигов без пересериализации: YAML, `.env`, INI, TOML, JSON, XML, флаги CLI.
//! Значение под чувствительным ключом маскируется целиком, даже если похоже на обычное слово.
//! Возвращаются только позиции — форматирование, кавычки и комментарии не трогаются.

use kaponir_core::{AnalyzeCtx, Candidate, Input, Lexicon, Recognizer};
use regex::Regex;

use crate::{is_placeholder, key_kind_class, kind_for_key};

const ID: &str = "config-keys";

pub struct KeyValue {
    line: Regex,
    json: Regex,
    xml: Regex,
    flag: Regex,
    /// Пары `name: KEY` + `value: V` (env в Kubernetes, ECS, docker-compose): ключ — в значении `name`.
    yaml_pair: Regex,
    json_pair: Regex,
    /// Чувствительные пары внутри прозы: `...; password: value; ...`.
    inline: Regex,
}

impl Default for KeyValue {
    fn default() -> Self {
        Self::new()
    }
}

impl KeyValue {
    pub fn new() -> Self {
        Self {
            line: Regex::new(
                r#"(?m)^([ \t]*(?:-[ \t]+)?(?:export[ \t]+)?)(["']?)([\p{L}_][\p{L}\p{N}_.\-]*)["']?[ \t]*(?::[ \t]+|:$|[ \t]*=[ \t]*)([^\r\n]*)"#,
            )
            .expect("valid regex"),
            json: Regex::new(r#""([^"\\\r\n]{1,80})"[ \t\r\n]*:[ \t\r\n]*"((?:[^"\\\r\n]|\\.)*)""#).expect("valid regex"),
            xml: Regex::new(r"<([A-Za-z_][\w.\-]*)>([^<\r\n]{1,512})</([A-Za-z_][\w.\-]*)>").expect("valid regex"),
            flag: Regex::new(r#"(?:^|[\s])--([A-Za-z][A-Za-z0-9\-]*)(?:=|[ \t]+)("[^"\r\n]*"|'[^'\r\n]*'|[^\s"'\-][^\s"']*)"#)
                .expect("valid regex"),
            yaml_pair: Regex::new(
                r#"(?m)^[ \t]*(?:-[ \t]+)?name:[ \t]*["']?([\p{L}_][\p{L}\p{N}_.\-]*)["']?[ \t]*\r?\n[ \t]*value:([^\r\n]*)"#,
            )
            .expect("valid regex"),
            json_pair: Regex::new(
                r#""name"[ \t\r\n]*:[ \t\r\n]*"([^"\\\r\n]{1,80})"[ \t\r\n]*,[ \t\r\n]*"value"[ \t\r\n]*:[ \t\r\n]*"((?:[^"\\\r\n]|\\.)*)""#,
            )
            .expect("valid regex"),
            inline: Regex::new(
                r#"(?:^|[^\p{L}\p{N}_.\-])([\p{L}_][\p{L}\p{N}_.\-]*)[ \t]*[:=][ \t]*("(?:[^"\\\r\n]|\\.)*"|'(?:[^'\r\n]|'')*'|[^\s,;"']+)"#,
            ).expect("valid regex"),
        }
    }

    fn emit(
        &self,
        lex: &Lexicon,
        key: &str,
        start: usize,
        end: usize,
        text: &str,
        out: &mut Vec<Candidate>,
    ) {
        let Some(kind) = kind_for_key(lex, key) else {
            return;
        };
        if start >= end {
            return;
        }
        let hard = key_kind_class(kind).1;
        let quoted = start > 0 && matches!(text.as_bytes()[start - 1], b'\'' | b'"');
        let value = &text[start..end];
        let placeholder = if hard && quoted {
            quoted_credential_placeholder(value)
        } else {
            is_placeholder(value)
        };
        if placeholder {
            return;
        }
        out.push(key_candidate(key, kind, start, end));
    }
}

fn key_candidate(key: &str, kind: &str, start: usize, end: usize) -> Candidate {
    let (category, hard, prior) = key_kind_class(kind);
    let candidate = Candidate::new(start, end, kind, category, ID, prior)
        .with_evidence(format!("key:{key}"), 1.0);
    if hard { candidate.hard() } else { candidate }
}

/// Tool arguments have already been parsed as JSON. Keep the field context
/// while reporting byte offsets into its decoded string, so restoration is exact.
pub fn credential_field(lex: &Lexicon, key: &str, value: &str) -> Option<Candidate> {
    let kind = kind_for_key(lex, key)?;
    (key_kind_class(kind).1 && !quoted_credential_placeholder(value))
        .then(|| key_candidate(key, kind, 0, value.len()))
}

fn quoted_credential_placeholder(raw: &str) -> bool {
    let value = raw.trim();
    if let Some(rest) = value.strip_prefix("${") {
        // A shell default or a suffix can itself contain a literal password.
        return rest.strip_suffix('}').is_some_and(|name| {
            !name.is_empty() && name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
        });
    }
    for (open, close) in [("{{", "}}"), ("$(", ")"), ("%(", ")s"), ("#{", "}")] {
        if let Some(rest) = value.strip_prefix(open) {
            return rest.split_once(close).is_some_and(|(expression, tail)| {
                tail.is_empty() && !expression.contains(['\'', '"'])
            });
        }
    }
    if value.len() < 3 {
        return value.is_empty()
            || value
                .chars()
                .all(|c| matches!(c, '*' | '•' | '.' | '…' | 'x' | 'X'));
    }
    is_placeholder(value)
}

/// Значение строки `key: value`: кавычки снимаются, комментарий в конце отрезается.
fn value_span(raw: &str, base: usize) -> Option<(usize, usize)> {
    let lead = raw.len() - raw.trim_start().len();
    let v = raw.trim_start();
    let b = base + lead;
    let first = v.chars().next()?;
    if first == '"' || first == '\'' {
        let bytes = v.as_bytes();
        let mut i = 1;
        while i < bytes.len() {
            if bytes[i] == b'\\' && first == '"' {
                i += 2;
                continue;
            }
            if bytes[i] == b'\'' && first == '\'' && bytes.get(i + 1) == Some(&b'\'') {
                i += 2;
                continue;
            }
            if bytes[i] == first as u8 {
                return Some((b + 1, b + i));
            }
            i += 1;
        }
        return None;
    }
    let mut end = v.len();
    for pat in [" #", "\t#", " ;", " //"] {
        if let Some(p) = v.find(pat) {
            end = end.min(p);
        }
    }
    let trimmed = v[..end].trim_end();
    let trimmed = trimmed.strip_suffix(',').unwrap_or(trimmed);
    (!trimmed.is_empty()).then_some((b, b + trimmed.len()))
}

/// YAML permits a quoted scalar to continue on more-indented lines. A clipped
/// file still contains sensitive bytes up to the end of the available scalar.
fn line_value_span(text: &str, value: regex::Match<'_>) -> Option<(usize, usize)> {
    let raw = value.as_str();
    if let Some(span) = value_span(raw, value.start()) {
        return Some(span);
    }
    let trimmed = raw.trim_start();
    if !trimmed.starts_with(['\'', '"']) {
        return None;
    }
    let line_start = text[..value.start()].rfind('\n').map_or(0, |p| p + 1);
    let prefix = &text[line_start..value.start()];
    let indent = prefix.len() - prefix.trim_start().len();
    let next_line = text[value.end()..].find('\n').map(|p| value.end() + p + 1);
    let end = next_line
        .and_then(|pos| block_span(text, pos, indent))
        .map_or(value.end(), |(_, end)| end);
    let start = value.start() + raw.len() - trimmed.len();
    value_span(&text[start..end], start).or(Some((start + 1, end)))
}

/// Блочный скаляр YAML (`key: |`): строки с отступом больше, чем у ключа.
fn block_span(text: &str, after_line: usize, key_indent: usize) -> Option<(usize, usize)> {
    let mut pos = after_line;
    let (mut start, mut end) = (None, None);
    while pos < text.len() {
        let line_end = text[pos..].find('\n').map_or(text.len(), |i| pos + i);
        let line = text[pos..line_end].trim_end_matches('\r');
        if line.trim().is_empty() {
            pos = line_end + 1;
            continue;
        }
        let indent = line.len() - line.trim_start().len();
        if indent <= key_indent {
            break;
        }
        start.get_or_insert(pos + indent);
        end = Some(pos + line.len());
        pos = line_end + 1;
    }
    Some((start?, end?))
}

impl Recognizer for KeyValue {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        let lex = ctx.lexicon;
        for cap in self.line.captures_iter(text) {
            let key = cap.get(3).expect("key");
            let val = cap.get(4).expect("value");
            let raw = val.as_str();
            let t = raw.trim();
            if t.starts_with('{') || t.starts_with('[') {
                continue; // вложенная структура: значения найдут JSON-проход и детекторы
            }
            let is_block = t.len() <= 3 && (t.starts_with('|') || t.starts_with('>'));
            let span = if is_block {
                let indent = cap.get(1).map_or(0, |m| m.as_str().len());
                let line_end = text[val.end()..]
                    .find('\n')
                    .map_or(text.len(), |i| val.end() + i + 1);
                block_span(text, line_end.min(text.len()), indent)
            } else {
                line_value_span(text, val)
            };
            if let Some((s, e)) = span {
                self.emit(lex, key.as_str(), s, e, text, out);
            }
        }
        for cap in self.json.captures_iter(text) {
            let key = cap.get(1).expect("key");
            let val = cap.get(2).expect("value");
            self.emit(lex, key.as_str(), val.start(), val.end(), text, out);
        }
        for cap in self.xml.captures_iter(text) {
            let (open, close) = (cap.get(1).expect("open"), cap.get(3).expect("close"));
            if open.as_str() != close.as_str() {
                continue;
            }
            let val = cap.get(2).expect("value");
            let lead = val.as_str().len() - val.as_str().trim_start().len();
            let s = val.start() + lead;
            let e = s + val.as_str().trim().len();
            self.emit(lex, open.as_str(), s, e, text, out);
        }
        for cap in self.flag.captures_iter(text) {
            let key = cap.get(1).expect("flag");
            let val = cap.get(2).expect("value");
            let (mut s, mut e) = (val.start(), val.end());
            if val.as_str().starts_with(['"', '\'']) && e - s >= 2 {
                s += 1;
                e -= 1;
            }
            self.emit(lex, key.as_str(), s, e, text, out);
        }
        for cap in self.yaml_pair.captures_iter(text) {
            let key = cap.get(1).expect("name");
            let val = cap.get(2).expect("value");
            if let Some((s, e)) = line_value_span(text, val) {
                self.emit(lex, key.as_str(), s, e, text, out);
            }
        }
        for cap in self.json_pair.captures_iter(text) {
            let key = cap.get(1).expect("name");
            let val = cap.get(2).expect("value");
            self.emit(lex, key.as_str(), val.start(), val.end(), text, out);
        }
        for cap in self.inline.captures_iter(text) {
            let key = cap.get(1).expect("key");
            // В прозе этот проход касается только учётных данных, а не произвольных меток.
            if !kind_for_key(lex, key.as_str()).is_some_and(|kind| key_kind_class(kind).1) {
                continue;
            }
            let val = cap.get(2).expect("value");
            if let Some((s, e)) = value_span(val.as_str(), val.start()) {
                // Полный проход конфигов уже мог скрыть значение вместе с пробелами.
                if !out.iter().any(|c| c.hard && c.start <= s && c.end >= e) {
                    self.emit(lex, key.as_str(), s, e, text, out);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex() -> Lexicon {
        let mut l = Lexicon::default();
        for (k, v) in [
            ("password", "SECRET"),
            ("token", "SECRET"),
            ("secretaccesskey", "SECRET"),
            ("dsn", "DSN"),
            ("bucket", "BUCKET"),
        ] {
            l.keys.insert(k.into(), v.into());
        }
        l
    }

    fn found(text: &str) -> Vec<(String, String)> {
        let l = lex();
        let mut out = Vec::new();
        KeyValue::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        out.into_iter()
            .map(|c| (c.kind.clone(), text[c.start..c.end].to_string()))
            .collect()
    }

    #[test]
    fn yaml_env_json_flags() {
        let text = "db:\n  password: \"s3cr3t pass\" # comment\n  name: billing\nAWS_SECRET_ACCESS_KEY=abcd1234\n{\"token\": \"t0k\\\"en\"}\nrun --password=hunter22 --user bob\nbucket = 'corp-backups'\n<password>xml-secret</password>\n";
        let f = found(text);
        assert!(
            f.contains(&("SECRET".into(), "s3cr3t pass".into())),
            "{f:?}"
        );
        assert!(f.contains(&("SECRET".into(), "abcd1234".into())), "{f:?}");
        assert!(f.contains(&("SECRET".into(), "t0k\\\"en".into())), "{f:?}");
        assert!(f.contains(&("SECRET".into(), "hunter22".into())), "{f:?}");
        assert!(
            f.contains(&("BUCKET".into(), "corp-backups".into())),
            "{f:?}"
        );
        assert!(f.contains(&("SECRET".into(), "xml-secret".into())), "{f:?}");
        assert!(!f.iter().any(|(_, v)| v == "billing"));
    }

    #[test]
    fn skips_references_and_names() {
        let text = "password: ${DB_PASSWORD}\nsecretName: my-secret\ntoken_url: https://x\npassword: \"{{ .Values.pw }}\"\n";
        assert!(found(text).is_empty(), "{:?}", found(text));
    }

    #[test]
    fn ellipses_are_not_passwords_but_actual_values_remain_protected() {
        assert!(found("password: ...\ntoken: …\nsecret: ******").is_empty());
        assert_eq!(
            found("password: genuine-short-value"),
            [("SECRET".into(), "genuine-short-value".into())]
        );
    }

    #[test]
    fn name_value_pairs() {
        let text = "env:\n  - name: DB_PASSWORD\n    value: \"hunter2\"\n  - name: LOG_LEVEL\n    value: debug\n\
                    [{\"name\": \"API_TOKEN\", \"value\": \"t0ps3cret\"}]\n";
        let f = found(text);
        assert_eq!(
            f,
            vec![
                ("SECRET".to_string(), "hunter2".to_string()),
                ("SECRET".to_string(), "t0ps3cret".to_string())
            ]
        );
    }

    #[test]
    fn yaml_block_scalar() {
        let text = "tls:\n  password: |\n    line-one\n    line-two\n  other: 1\n";
        let f = found(text);
        assert_eq!(
            f,
            vec![("SECRET".to_string(), "line-one\n    line-two".to_string())]
        );
    }

    #[test]
    fn credentials_inside_prose_are_hard_masks() {
        let text = "Сервер готов; password: SynTest_47!Private; token='two words'; db.password=second42, done";
        let l = lex();
        let mut out = Vec::new();
        KeyValue::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        let values: Vec<_> = out.iter().map(|c| &text[c.start..c.end]).collect();
        assert_eq!(values, ["SynTest_47!Private", "two words", "second42"]);
        assert!(
            out.iter()
                .all(|c| c.hard && c.category == kaponir_core::Category::Credential)
        );
    }

    #[test]
    fn inline_pairs_preserve_references_and_nonsecret_labels() {
        assert!(found("note password: ${DB_PASSWORD}; token='{{ .Values.token }}'; bucket: public; secretName: resource").is_empty());
        // Значение строки конфига с пробелами по-прежнему скрывается целиком и без дублей.
        assert_eq!(
            found("password: two words\n"),
            [("SECRET".into(), "two words".into())]
        );
    }

    #[test]
    fn helm_quoted_passwords_cover_the_entire_yaml_scalar() {
        for (source, value) in [
            (
                "postgresPassword: 'little''synthetic / #; tail' # note",
                "little''synthetic / #; tail",
            ),
            ("42:  db.password: 'left''right' # note", "left''right"),
            (
                "env:\r\n  - name: DB_PASSWORD\r\n    value: \"one\\\"line\r\n      and another\" # note",
                "one\\\"line\r\n      and another",
            ),
            (
                "password: '<starts-with-symbol/and-space !'",
                "<starts-with-symbol/and-space !",
            ),
            ("password: '***suffix-is-private'", "***suffix-is-private"),
        ] {
            let matches = found(source);
            assert!(
                matches.contains(&("SECRET".to_owned(), value.to_owned())),
                "{matches:?}"
            );
        }
        for literal in [
            "${TOKEN}-suffix",
            "${TOKEN:-not-a-placeholder}",
            "{{ .Values.token }}-tail",
            "AA",
        ] {
            assert_eq!(
                found(&format!("password: '{literal}'")),
                [("SECRET".into(), literal.into())]
            );
        }
        assert!(
            found("password: '<PASSWORD>'\npassword: '***'\nimageTag: 'ordinary-public-string'")
                .is_empty()
        );
    }
}
