//! Формат токенов и их поиск (в том числе искажённых LLM).

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::fmt;

use crate::types::TokenForm;

/// Идентификатор записи внутри kind.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum TokenId {
    /// Нумерованная переменная `ANON_SVC_1`.
    Num(u64),
    /// HMAC-префикс в нижнем регистре без `h`: `ANON_SVC_h8f2a1c` → `8f2a1c`.
    Hash(String),
    /// Суррогат, сохраняющий формат (`240.17.3.15`).
    Surrogate(String),
}

impl fmt::Display for TokenId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TokenId::Num(n) => write!(f, "{n}"),
            TokenId::Hash(h) => write!(f, "h{h}"),
            TokenId::Surrogate(s) => f.write_str(s),
        }
    }
}

/// Найденный в тексте токен.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenMatch {
    /// Границы вместе со снятыми обёртками `${…}` / `$`.
    pub start: usize,
    pub end: usize,
    pub kind: String,
    pub id: TokenId,
    /// Границы id (`1`, `h8f2a1c`) — для разбора хвоста за HMAC-префиксом.
    pub id_start: usize,
    pub id_end: usize,
}

pub const MIN_HASH_LEN: usize = 6;
pub const MAX_HASH_LEN: usize = 16;

#[derive(Clone, Debug)]
pub struct TokenCodec {
    prefix: String,
    find: Regex,
}

impl TokenCodec {
    pub fn new(prefix: &str) -> Result<Self, String> {
        if prefix.is_empty()
            || !prefix
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic())
            || !prefix.chars().all(|c| c.is_ascii_alphanumeric())
        {
            return Err(format!(
                "token prefix `{prefix}` must match [A-Za-z][A-Za-z0-9]*"
            ));
        }
        let p = regex::escape(prefix);
        // Разделитель: `_` или `-`, возможно экранированный markdown (`\_`).
        let pattern = format!(
            r"(?i)(\$\{{[ \t]*|\$)?{p}\\?[_-]([a-z][a-z0-9]{{0,23}})\\?[_-](h[0-9a-f]{{{MIN_HASH_LEN},{MAX_HASH_LEN}}}|[0-9]{{1,12}})([ \t]*\}})?"
        );
        let find = Regex::new(&pattern).map_err(|e| e.to_string())?;
        Ok(Self {
            prefix: prefix.to_ascii_uppercase(),
            find,
        })
    }

    pub fn prefix(&self) -> &str {
        &self.prefix
    }

    pub fn format(&self, kind: &str, id: &TokenId, form: TokenForm) -> String {
        if let TokenId::Surrogate(s) = id {
            return s.clone();
        }
        match form {
            TokenForm::Snake => format!("{}_{}_{}", self.prefix, kind.to_ascii_uppercase(), id),
            TokenForm::Dns => format!("{}-{}-{}", self.prefix, kind, id).to_ascii_lowercase(),
        }
    }

    /// Все токены в тексте, без учёта регистра и вида разделителя.
    pub fn find_iter<'a>(&'a self, text: &'a str) -> impl Iterator<Item = TokenMatch> + 'a {
        self.find.captures_iter(text).filter_map(move |cap| {
            let whole = cap.get(0)?;
            let mut start = whole.start();
            let mut end = whole.end();
            let open = cap.get(1).map(|m| m.as_str());
            let close = cap.get(4);
            match (open, close) {
                (Some(o), Some(_)) if o.starts_with("${") => {}
                (Some(o), close) if o.starts_with("${") => {
                    // `${` без закрывающей скобки — не обёртка, оставляем как есть.
                    start += o.len();
                    debug_assert!(close.is_none());
                }
                (_, Some(c)) => end = c.start(),
                _ => {}
            }
            let kind = cap.get(2)?.as_str().to_ascii_uppercase();
            let id_m = cap.get(3)?;
            let raw_id = id_m.as_str();
            let id = if let Some(h) = raw_id.strip_prefix(['h', 'H']) {
                TokenId::Hash(h.to_ascii_lowercase())
            } else {
                TokenId::Num(raw_id.parse().ok()?)
            };
            Some(TokenMatch {
                start,
                end,
                kind,
                id,
                id_start: id_m.start(),
                id_end: id_m.end(),
            })
        })
    }

    /// Может ли `s` оказаться началом токена (или полным токеном, который ещё продолжится:
    /// цифрами, закрывающей `}` или буквенным хвостом). Нужен потоковому восстановлению.
    pub fn could_continue(&self, s: &str) -> bool {
        let b = s.as_bytes();
        let n = b.len();
        let mut i = 0;
        let mut braced = false;
        if b.first() == Some(&b'$') {
            i += 1;
            if i < n && b[i] == b'{' {
                braced = true;
                i += 1;
                while i < n && (b[i] == b' ' || b[i] == b'\t') {
                    i += 1;
                }
            }
        }
        for &p in self.prefix.as_bytes() {
            if i == n {
                return true;
            }
            if b[i].to_ascii_uppercase() != p {
                return false;
            }
            i += 1;
        }
        let sep = |i: &mut usize| -> Option<bool> {
            if *i == n {
                return Some(true);
            }
            if b[*i] == b'\\' {
                *i += 1;
                if *i == n {
                    return Some(true);
                }
            }
            if b[*i] != b'_' && b[*i] != b'-' {
                return Some(false);
            }
            *i += 1;
            None
        };
        if let Some(r) = sep(&mut i) {
            return r;
        }
        if i == n {
            return true;
        }
        if !b[i].is_ascii_alphabetic() {
            return false;
        }
        while i < n && b[i].is_ascii_alphanumeric() {
            i += 1;
        }
        if let Some(r) = sep(&mut i) {
            return r;
        }
        if i == n {
            return true;
        }
        if b[i] == b'h' || b[i] == b'H' {
            i += 1;
            while i < n && b[i].is_ascii_hexdigit() {
                i += 1;
            }
        } else if b[i].is_ascii_digit() {
            while i < n && b[i].is_ascii_digit() {
                i += 1;
            }
        } else {
            return false;
        }
        let rest = &s[i..];
        if braced {
            return rest.chars().all(|c| c == ' ' || c == '\t');
        }
        rest.chars().all(char::is_alphabetic)
    }

    /// Максимальная длина токена с обёртками — для потокового буфера.
    pub fn max_token_len(&self) -> usize {
        self.prefix.len() + 2 + 24 + 2 + 1 + MAX_HASH_LEN + 4
    }
}

impl Default for TokenCodec {
    fn default() -> Self {
        Self::new("ANON").expect("default prefix is valid")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(codec: &TokenCodec, text: &str) -> Vec<(String, String, TokenId)> {
        codec
            .find_iter(text)
            .map(|m| (text[m.start..m.end].to_string(), m.kind, m.id))
            .collect()
    }

    #[test]
    fn formats() {
        let c = TokenCodec::default();
        assert_eq!(
            c.format("SVC", &TokenId::Num(1), TokenForm::Snake),
            "ANON_SVC_1"
        );
        assert_eq!(
            c.format("SUB", &TokenId::Num(2), TokenForm::Dns),
            "anon-sub-2"
        );
        assert_eq!(
            c.format("SVC", &TokenId::Hash("8f2a1c".into()), TokenForm::Dns),
            "anon-svc-h8f2a1c"
        );
    }

    #[test]
    fn finds_distorted_tokens() {
        let c = TokenCodec::default();
        let found = ids(
            &c,
            "a anon_user_1 b ANON-USER-12 c ANON\\_USER\\_3 d anon-svc-h8f2a1c",
        );
        assert_eq!(found.len(), 4);
        assert_eq!(found[0].2, TokenId::Num(1));
        assert_eq!(found[1].2, TokenId::Num(12));
        assert_eq!(found[2].0, "ANON\\_USER\\_3");
        assert_eq!(found[3].2, TokenId::Hash("8f2a1c".into()));
    }

    #[test]
    fn strips_wrappers_only_when_balanced() {
        let c = TokenCodec::default();
        assert_eq!(ids(&c, "x=${ANON_USER_1}!")[0].0, "${ANON_USER_1}");
        assert_eq!(ids(&c, "x=$ANON_USER_1!")[0].0, "$ANON_USER_1");
        assert_eq!(ids(&c, "x=${ANON_USER_1 y")[0].0, "ANON_USER_1");
        assert_eq!(ids(&c, "{ANON_USER_1}")[0].0, "ANON_USER_1");
    }

    #[test]
    fn rejects_bad_prefix() {
        assert!(TokenCodec::new("1X").is_err());
        assert!(TokenCodec::new("A$").is_err());
    }
}
