//! Учётные данные: известные форматы ключей провайдеров, JWT, PEM, Bearer и высокоэнтропийные строки.

use kaponir_core::s1::shannon_entropy;
use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Recognizer};
use regex::Regex;

use crate::word_bounded;

const ID: &str = "secrets";

pub struct Secrets {
    known: Vec<(&'static str, Regex)>,
    pem: Regex,
    auth: Regex,
    entropy: Regex,
    uuid: Regex,
}

impl Default for Secrets {
    fn default() -> Self {
        Self::new()
    }
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
            entropy: Regex::new(r"[A-Za-z0-9+/_\-]{24,}={0,2}").expect("valid regex"),
            uuid: Regex::new(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$").expect("valid regex"),
        }
    }

    fn high_entropy(&self, s: &str) -> Option<f32> {
        if self.uuid.is_match(s) || s.chars().all(|c| c.is_ascii_hexdigit()) {
            return None;
        }
        let upper = s.chars().filter(char::is_ascii_uppercase).count();
        let lower = s.chars().filter(char::is_ascii_lowercase).count();
        let digit = s.chars().filter(char::is_ascii_digit).count();
        if upper < 2 || lower < 2 || digit < 2 {
            return None;
        }
        // Длинные серии строчных букв — признак идентификатора, а не случайной строки.
        let mut run = 0;
        for c in s.chars() {
            run = if c.is_ascii_lowercase() { run + 1 } else { 0 };
            if run >= 7 {
                return None;
            }
        }
        let parts = s.split(['-', '_', '/']).filter(|p| !p.is_empty()).count();
        if parts >= 4 {
            return None;
        }
        let e = shannon_entropy(s);
        (e >= 3.7).then_some(e)
    }
}

impl Recognizer for Secrets {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
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
        for m in self.entropy.find_iter(text) {
            if !word_bounded(text, m.start(), m.end()) {
                continue;
            }
            if let Some(e) = self.high_entropy(m.as_str()) {
                out.push(
                    Candidate::new(m.start(), m.end(), "SECRET", Category::Credential, ID, 0.65)
                        .with_evidence(format!("entropy:{e:.2}"), (e - 3.7) * 1.5),
                );
            }
        }
    }
}
