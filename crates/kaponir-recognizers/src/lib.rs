//! Встроенные языконезависимые распознаватели. Слова, ключи и домены берутся из словаря
//! (пакетов), а не зашиты в код.

pub mod composite;
pub mod kv;
pub mod names;
pub mod network;
pub mod paths;
pub mod rules;
pub mod secrets;
pub mod validators;
pub mod web;

use kaponir_core::Recognizer;

pub use rules::RulesRecognizer;

/// Набор встроенных распознавателей по умолчанию.
pub fn builtin() -> Vec<Box<dyn Recognizer>> {
    vec![
        Box::new(secrets::Secrets::new()),
        Box::new(kv::KeyValue::new()),
        Box::new(network::Network::new()),
        Box::new(web::Web::new()),
        Box::new(paths::Paths::new()),
        Box::new(composite::Composite::new()),
        Box::new(validators::Validators::new()),
        Box::new(names::Names::new()),
    ]
}

/// Символ, продолжающий идентификатор: граница слова для всех распознавателей.
pub(crate) fn is_ident(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

pub(crate) fn char_before(text: &str, i: usize) -> Option<char> {
    text[..i].chars().next_back()
}

pub(crate) fn char_after(text: &str, i: usize) -> Option<char> {
    text[i..].chars().next()
}

/// `[start, end)` не продолжает соседние идентификаторы.
pub(crate) fn word_bounded(text: &str, start: usize, end: usize) -> bool {
    !char_before(text, start).is_some_and(is_ident) && !char_after(text, end).is_some_and(is_ident)
}

/// Категория и «жёсткость» значения по KIND ключа конфига.
pub(crate) fn key_kind_class(kind: &str) -> (kaponir_core::Category, bool, f32) {
    match kind {
        "SECRET" | "DSN" | "PASSWORD" | "TOKEN" | "KEY" => {
            (kaponir_core::Category::Credential, true, 0.97)
        }
        "USER" | "LOGIN" | "PERSON" => (kaponir_core::Category::Identity, false, 0.9),
        "EMAIL" | "PHONE" => (kaponir_core::Category::Contact, false, 0.9),
        _ => (kaponir_core::Category::Infra, false, 0.9),
    }
}

/// Ищет KIND по имени ключа: проверяются последние 1–3 слова ключа (`db.password`,
/// `AWS_SECRET_ACCESS_KEY`, `clientSecret`). `secretName`, `token_url` — не секреты.
pub(crate) fn kind_for_key<'a>(lex: &'a kaponir_core::Lexicon, key: &str) -> Option<&'a str> {
    let mut words: Vec<String> = Vec::new();
    for part in key
        .split(|c: char| !c.is_alphanumeric())
        .filter(|p| !p.is_empty())
    {
        for w in kaponir_core::lexicon::split_camel(part) {
            words.push(w.to_lowercase());
        }
    }
    for k in 1..=3.min(words.len()) {
        let joined: String = words[words.len() - k..].concat();
        if let Some(kind) = lex.key_kind(&joined) {
            return Some(kind);
        }
    }
    None
}

pub(crate) struct Slot<'a> {
    pub kind: &'a str,
    pub category: kaponir_core::Category,
    pub prior: f32,
    /// Форма токена для DNS-меток и имён подов.
    pub dns: bool,
    pub recognizer: &'a str,
}

/// Deny-by-default для одного структурного сегмента: неизвестные части становятся слотами,
/// известные слова, числа и хеши остаются.
pub(crate) fn push_slots(
    lex: &kaponir_core::Lexicon,
    text: &str,
    start: usize,
    end: usize,
    slot: &Slot<'_>,
    out: &mut Vec<kaponir_core::Candidate>,
) {
    for (a, b) in kaponir_core::lexicon::unknown_slots(lex, &text[start..end], start) {
        let mut c = kaponir_core::Candidate::new(
            a,
            b,
            slot.kind,
            slot.category,
            slot.recognizer,
            slot.prior,
        )
        .with_evidence("unknown-part", 0.0);
        if slot.dns {
            c = c.dns().canonical(text[a..b].to_lowercase());
        }
        out.push(c);
    }
}

/// Значения, которые являются ссылками или заглушками, а не данными.
pub(crate) fn is_placeholder(v: &str) -> bool {
    let t = v.trim();
    if t.is_empty() || t.len() < 3 {
        return true;
    }
    let l = t.to_lowercase();
    [
        "${",
        "{{",
        "$(",
        "%(",
        "#{",
        "<",
        "***",
        "!vault",
        "secretkeyref",
        "valuefrom",
    ]
    .iter()
    .any(|p| l.starts_with(p))
        || (t.starts_with('$')
            && t[1..]
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_'))
        || [
            "null", "none", "nil", "true", "false", "yes", "no", "changeme", "xxx", "xxxx",
            "redacted", "example",
        ]
        .contains(&l.as_str())
        || t.chars().all(|c| c == '*' || c == 'x' || c == 'X')
}
