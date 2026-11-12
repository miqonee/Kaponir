//! Учётные данные: известные форматы ключей провайдеров, JWT, PEM, Bearer,
//! сложные сгенерированные пароли и высокоэнтропийные строки (по скользящему окну Шеннона).

use kaponir_core::s1::shannon_entropy;
use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Lexicon, Recognizer};
use regex::Regex;

use crate::word_bounded;

const ID: &str = "secrets";
const PASSWORD_SPECIALS: &str = "!@#$%^&*+~?";

pub struct Secrets {
    known: Vec<(&'static str, Regex)>,
    pem: Regex,
    auth: Regex,
    entropy: Regex,
    password_token: Regex,
    uuid: Regex,
    public_metadata: Regex,
}

impl Default for Secrets {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum CharClass {
    Lower,
    Upper,
    Digit,
    Special,
    Other,
}

fn char_class(c: char) -> CharClass {
    if c.is_ascii_lowercase() {
        CharClass::Lower
    } else if c.is_ascii_uppercase() {
        CharClass::Upper
    } else if c.is_ascii_digit() {
        CharClass::Digit
    } else if PASSWORD_SPECIALS.contains(c) {
        CharClass::Special
    } else {
        CharClass::Other
    }
}

/// Максимальная энтропия Шеннона по скользящему окну шириной `window` символов (и по всей строке).
fn sliding_window_entropy(s: &str, window: usize) -> f32 {
    let chars: Vec<char> = s.chars().collect();
    let full = shannon_entropy(s);
    if chars.len() <= window {
        return full;
    }
    let mut max_e = full;
    for w in chars.windows(window) {
        let sub: String = w.iter().collect();
        let e = shannon_entropy(&sub);
        if e > max_e {
            max_e = e;
        }
    }
    max_e
}

fn class_transitions(s: &str) -> usize {
    let mut prev: Option<CharClass> = None;
    let mut count = 0;
    for c in s.chars() {
        let cls = char_class(c);
        if let Some(p) = prev
            && p != cls
        {
            count += 1;
        }
        prev = Some(cls);
    }
    count
}

impl Secrets {
    pub fn new() -> Self {
        let known = [
            ("aws-access-key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"),
            ("github", r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
            ("github-pat", r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
            ("gitlab", r"\bgl(?:pat|rt|dt|ptt|cbt|ft|imt|agent)-[A-Za-z0-9_\-]{20,}\b"),
            ("slack", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
            ("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"),
            ("google-api", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
            ("openai-anthropic", r"\bsk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}\b"),
            ("stripe", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
            ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
            ("telegram-bot", r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"),
            ("yandex-cloud", r"\b(?:AQVN[A-Za-z0-9_\-]{35,38}|y[0-3]_[A-Za-z0-9_\-]{55}|t1\.[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-]{86}=*)"),
            ("npm", r"\bnpm_[A-Za-z0-9]{36}\b"),
            ("pypi", r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{50,}"),
            ("hashicorp-vault", r"\bhv[sbr]\.[A-Za-z0-9_\-]{24,}\b"),
            ("sendgrid", r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b"),
            ("azure-storage", r"AccountKey=[A-Za-z0-9+/=]{40,}"),
        ]
        .into_iter()
        .map(|(n, p)| (n, Regex::new(p).expect("valid secret regex")))
        .collect();
        Self {
            known,
            pem: Regex::new(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY|[A-Z0-9 ]*CERTIFICATE REQUEST)-----([\s\S]*?)-----END [A-Z0-9 ]+-----")
                .expect("valid regex"),
            auth: Regex::new(r"(?i)\b(?:bearer|basic|token|apikey|api-key)[ \t]+([A-Za-z0-9._~+/\-]{16,}=*)")
                .expect("valid regex"),
            entropy: Regex::new(r"[A-Za-z0-9+/_\-]{20,}={0,2}").expect("valid regex"),
            password_token: Regex::new(r"[A-Za-z0-9!@#$%^&*+~=?_\-]{12,128}").expect("valid regex"),
            uuid: Regex::new(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$").expect("valid regex"),
            public_metadata: Regex::new(r"(?i)(?:\.(?:py|md|rs|js|ts|json|yaml|yml|txt)\?ref=[a-f0-9]{7,64}\b|\bMDEwOlJlcG9zaXRvcnk[A-Za-z0-9+/]{8,}={0,2})").expect("metadata regex"),
        }
    }

    fn high_entropy(&self, s: &str, lex: &Lexicon) -> Option<f32> {
        if self.uuid.is_match(s) || s.chars().all(|c| c.is_ascii_hexdigit()) {
            return None;
        }
        let upper = s.chars().filter(char::is_ascii_uppercase).count();
        let lower = s.chars().filter(char::is_ascii_lowercase).count();
        let digit = s.chars().filter(char::is_ascii_digit).count();
        if upper < 2 || lower < 2 || digit < 2 {
            return None;
        }
        // Длинные серии строчных букв — признак идентификатора, если нет частых переходов классов.
        let mut max_lower_run = 0;
        let mut run = 0;
        for c in s.chars() {
            run = if c.is_ascii_lowercase() { run + 1 } else { 0 };
            max_lower_run = max_lower_run.max(run);
        }
        let max_allowed_run = if s.len() >= 24 { 7 } else { 6 };
        if max_lower_run >= max_allowed_run {
            return None;
        }
        let parts = s.split(['-', '_', '/']).filter(|p| !p.is_empty()).count();
        if parts >= 4 {
            return None;
        }
        // Если токен состоит преимущественно из словарных слов (camelCase / snake_case), пропускаем.
        let camel_words = kaponir_core::lexicon::split_camel(s);
        if camel_words.len() >= 2 {
            let known_bytes: usize = camel_words
                .iter()
                .filter(|w| w.len() >= 4 && lex.is_word(&w.to_lowercase()))
                .map(|w| w.len())
                .sum();
            if known_bytes * 2 >= s.len() {
                return None;
            }
        }
        let e = sliding_window_entropy(s, 18);
        let threshold = if s.len() >= 24 { 3.7 } else { 3.85 };
        (e >= threshold).then_some(e)
    }

    /// Распознавание сложных паролей со спецсимволами (`Xd13*8sdasdhujNsda!#fsd` и др.)
    /// по разнообразию классов символов, числу переходов между классами и скользящей энтропии Шеннона.
    fn complex_password(&self, s: &str) -> Option<f32> {
        if s.len() < 12
            || crate::is_placeholder(s)
            || self.uuid.is_match(s)
            || s.starts_with("--")
            || s.contains("://")
        {
            return None;
        }
        let upper = s.chars().filter(char::is_ascii_uppercase).count();
        let lower = s.chars().filter(char::is_ascii_lowercase).count();
        let digit = s.chars().filter(char::is_ascii_digit).count();
        let special = s.chars().filter(|&c| PASSWORD_SPECIALS.contains(c)).count();
        if special == 0 {
            return None;
        }
        let classes = (upper > 0) as usize
            + (lower > 0) as usize
            + (digit > 0) as usize
            + (special > 0) as usize;
        let transitions = class_transitions(s);
        let win_e = sliding_window_entropy(s, 16);

        // 4 класса символов (строчные + заглавные + цифры + спецсимволы пароля)
        if classes == 4 && s.len() >= 12 && transitions >= 4 && win_e >= 3.25 {
            return Some(win_e);
        }
        // 3 класса символов (включая хотя бы 2 спецсимвола или высокую энтропию) при длине >= 14
        if classes >= 3
            && s.len() >= 14
            && (special >= 2 || digit >= 2)
            && transitions >= 5
            && win_e >= 3.45
        {
            return Some(win_e);
        }
        None
    }
}

impl Recognizer for Secrets {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        let metadata: Vec<_> = self
            .public_metadata
            .find_iter(text)
            .map(|m| (m.start(), m.end()))
            .collect();
        for (name, re) in &self.known {
            for m in re.find_iter(text) {
                let (mut s, e) = (m.start(), m.end());
                if *name == "azure-storage" {
                    s += "AccountKey=".len();
                }
                out.push(
                    Candidate::new(s, e, "SECRET", Category::Credential, ID, 0.98)
                        .hard()
                        .with_evidence(format!("format:{name}"), 1.0),
                );
            }
        }
        for cap in self.pem.captures_iter(text) {
            let body = cap.get(2).expect("group 2");
            let trimmed = body.as_str().trim();
            if trimmed.is_empty() {
                continue;
            }
            let s = body.start() + (body.as_str().len() - body.as_str().trim_start().len());
            out.push(
                Candidate::new(
                    s,
                    s + trimmed.len(),
                    "SECRET",
                    Category::Credential,
                    ID,
                    0.99,
                )
                .hard()
                .with_evidence("format:pem", 1.0),
            );
        }
        for cap in self.auth.captures_iter(text) {
            let v = cap.get(1).expect("group 1");
            if crate::is_placeholder(v.as_str()) {
                continue;
            }
            out.push(
                Candidate::new(v.start(), v.end(), "SECRET", Category::Credential, ID, 0.95)
                    .hard()
                    .with_evidence("format:auth-header", 1.0),
            );
        }
        for m in self.password_token.find_iter(text) {
            if metadata.iter().any(|&(a, b)| m.start() < b && a < m.end()) {
                continue;
            }
            let (mut s, mut e) = (m.start(), m.end());
            // Если токен имеет вид KEY=VALUE (например, в env или командной строке), оцениваем само значение.
            let raw = &text[s..e];
            if let Some(eq_pos) = raw.find('=') {
                let prefix = &raw[..eq_pos];
                if !prefix.is_empty()
                    && prefix
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
                {
                    s += eq_pos + 1;
                }
            }
            // Отсекаем одиночный завершающий знак вопроса/восклицания в конце предложения,
            // если внутри токена больше нет спецсимволов пароля.
            while e > s && text.as_bytes()[e - 1].is_ascii_punctuation() {
                let last = text.as_bytes()[e - 1] as char;
                let inner_specials = text[s..e - 1]
                    .chars()
                    .filter(|&c| PASSWORD_SPECIALS.contains(c))
                    .count();
                if (last == '?' || last == '!') && inner_specials == 0 {
                    e -= 1;
                } else {
                    break;
                }
            }
            if e <= s || !word_bounded(text, s, e) {
                continue;
            }
            let candidate_str = &text[s..e];
            if let Some(ent) = self.complex_password(candidate_str) {
                out.push(
                    Candidate::new(s, e, "SECRET", Category::Credential, ID, 0.92)
                        .hard()
                        .with_evidence(format!("password-entropy:{ent:.2}"), 1.2),
                );
            }
        }
        for m in self.entropy.find_iter(text) {
            if metadata.iter().any(|&(a, b)| m.start() < b && a < m.end()) {
                continue;
            }
            if !word_bounded(text, m.start(), m.end()) {
                continue;
            }
            if out.iter().any(|c| c.start <= m.start() && m.end() <= c.end) {
                continue;
            }
            if let Some(e) = self.high_entropy(m.as_str(), ctx.lexicon) {
                out.push(
                    Candidate::new(m.start(), m.end(), "SECRET", Category::Credential, ID, 0.65)
                        .with_evidence(format!("entropy:{e:.2}"), (e - 3.7) * 1.5),
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_complex_password_without_keyword_prefix() {
        let lex = Lexicon::default();
        let secrets = Secrets::new();
        let text = "Подключись к базе, используй Xd13*8sdasdhujNsda!#fsd для входа.";
        let mut out = Vec::new();
        secrets.analyze(&Input::text(text), &AnalyzeCtx::new(&lex), &mut out);
        let found: Vec<&str> = out.iter().map(|c| &text[c.start..c.end]).collect();
        assert!(
            found.contains(&"Xd13*8sdasdhujNsda!#fsd"),
            "Expected password to be detected without prefix, got: {found:?}"
        );
        let cand = out
            .iter()
            .find(|c| &text[c.start..c.end] == "Xd13*8sdasdhujNsda!#fsd")
            .unwrap();
        assert_eq!(cand.kind, "SECRET");
        assert_eq!(cand.category, Category::Credential);
        assert!(cand.hard);
    }

    #[test]
    fn ignores_ordinary_words_and_questions() {
        let lex = Lexicon::default();
        let secrets = Secrets::new();
        let text = "Почему система работает нестабильно? Проверьте конфигурацию!";
        let mut out = Vec::new();
        secrets.analyze(&Input::text(text), &AnalyzeCtx::new(&lex), &mut out);
        assert!(out.is_empty(), "Unexpected candidates: {out:?}");
    }

    #[test]
    fn git_references_and_repository_ids_are_not_entropy_passwords() {
        let text = "https://github.com/acme/repo/file.py?ref=a7acf7442f68a01bff5fbbb028ad357ac88dfb49 file.md?ref=fc3c0ffa989a3860cda84ef5198226410e1ec6ed MDEwOlJlcG9zaXRvcnkxMjY1MTM3NzI2";
        let lex = Lexicon::default();
        let mut out = Vec::new();
        Secrets::new().analyze(&Input::text(text), &AnalyzeCtx::new(&lex), &mut out);
        assert!(out.is_empty(), "{out:?}");
        let text = "Bearer ghp_1234567890abcdefghijklmnopqrstuvwx123456";
        Secrets::new().analyze(&Input::text(text), &AnalyzeCtx::new(&lex), &mut out);
        assert!(out.iter().any(|c| c.hard));
    }
}
