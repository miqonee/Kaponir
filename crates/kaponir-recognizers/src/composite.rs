//! Составные идентификаторы в свободном тексте: хостнеймы, поды, сервисы
//! (`prod-billing-auth-7d9f8b-x2v`). Чтобы не калечить обычный код, срабатывает только на
//! kebab-идентификаторах с сильным признаком: часть-окружение или суффикс пода/реплики.

use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Recognizer};
use regex::Regex;

use crate::{Slot, char_after, char_before, push_slots};

const ID: &str = "composite";

const ENVS: &[&str] = &[
    "prod",
    "production",
    "prd",
    "stage",
    "staging",
    "stg",
    "dev",
    "develop",
    "test",
    "qa",
    "uat",
    "preprod",
    "sandbox",
    "demo",
    "int",
    "canary",
    "perf",
    "load",
];

pub struct Composite {
    ident: Regex,
    pod: Regex,
}

impl Default for Composite {
    fn default() -> Self {
        Self::new()
    }
}

impl Composite {
    pub fn new() -> Self {
        Self {
            ident: Regex::new(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+").expect("valid regex"),
            // deployment-<replicaset hash>-<pod suffix> или statefulset-<n>.
            pod: Regex::new(r"(?:-[a-z0-9]{8,10}-[a-z0-9]{5}|-[a-z0-9]{5}|-\d{1,3})$")
                .expect("valid regex"),
        }
    }
}

impl Recognizer for Composite {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        let slot = Slot {
            kind: "SVC",
            category: Category::Infra,
            prior: 0.75,
            dns: true,
            recognizer: ID,
        };
        for m in self.ident.find_iter(text) {
            let before = char_before(text, m.start());
            let after = char_after(text, m.end());
            if before.is_some_and(|c| c.is_alphanumeric() || "._/-@:".contains(c))
                || after.is_some_and(|c| {
                    c.is_alphanumeric() || c == '_' || c == '/' || c == '@' || c == '-'
                })
                || (after == Some('.')
                    && char_after(text, m.end() + 1).is_some_and(char::is_alphanumeric))
            {
                continue;
            }
            let s = m.as_str();
            let parts: Vec<&str> = s.split('-').collect();
            let env = parts.iter().any(|p| ENVS.contains(p));
            let pod_suffix = self
                .pod
                .find(s)
                .filter(|_| parts.len() >= 3 && s.chars().any(|c| c.is_ascii_digit()));
            if !env && pod_suffix.is_none() {
                continue;
            }
            // Суффикс пода/реплики — случайный, его не анализируем.
            let end = m.start() + pod_suffix.map_or(s.len(), |p| p.start());
            let before_len = out.len();
            push_slots(ctx.lexicon, text, m.start(), end, &slot, out);
            let evidence = if env { "env-part" } else { "pod-suffix" };
            for c in &mut out[before_len..] {
                c.evidence.push(kaponir_core::Evidence {
                    source: evidence.into(),
                    logit: 0.0,
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kaponir_core::Lexicon;

    #[test]
    fn pod_name() {
        let mut l = Lexicon::default();
        for w in ["prod", "auth", "well", "known", "cert", "manager"] {
            l.words.insert(w.into());
        }
        let text =
            "pod prod-billing-auth-7d9f8b-x2v failed; well-known cert-manager-7d9f8b6c4-abcde";
        let mut out = Vec::new();
        Composite::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        let f: Vec<&str> = out.iter().map(|c| &text[c.start..c.end]).collect();
        assert_eq!(f, vec!["billing"]);
    }
}
