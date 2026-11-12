//! Account handles in prose; dotted words without account context are not sufficient evidence.
use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Recognizer};
use regex::Regex;

pub struct Users {
    pattern: Regex,
}
impl Default for Users {
    fn default() -> Self {
        Self::new()
    }
}
impl Users {
    pub fn new() -> Self {
        Self { pattern: Regex::new(r"(?i)\b(?:user(?:name)?|login|author|пользовател[ьяюем]{1,3}|автор(?:а|ом)?|логин(?:а|ом)?)\s*[:=]?\s+([a-z][a-z0-9_-]{0,31}\.[a-z][a-z0-9_.-]{2,63})\b").expect("account regex") }
    }
}
impl Recognizer for Users {
    fn id(&self) -> &str {
        "users"
    }
    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        for captures in self.pattern.captures_iter(input.text) {
            let value = captures.get(1).expect("handle");
            let suffix = value.as_str().rsplit('.').next().unwrap_or_default();
            if ctx.lexicon.tlds.contains(suffix)
                || matches!(
                    suffix,
                    "json" | "yaml" | "yml" | "md" | "py" | "rs" | "txt" | "toml"
                )
                || input.text[value.end()..].starts_with(['@', '/'])
            {
                continue;
            }
            out.push(
                Candidate::new(
                    value.start(),
                    value.end(),
                    "USER",
                    Category::Identity,
                    "users",
                    0.9,
                )
                .with_evidence("account-context", 1.5),
            );
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accounts_need_context_and_are_not_domains_or_files() {
        let lex = kaponir_core::Lexicon::default();
        let mut values = Vec::new();
        let text = "Сообщение от пользователя m.goloshanov; Author: a.petrov. user config.json; m.goloshanov";
        Users::new().analyze(&Input::text(text), &AnalyzeCtx::new(&lex), &mut values);
        assert_eq!(
            values.iter().map(|v| v.surface(text)).collect::<Vec<_>>(),
            vec!["m.goloshanov", "a.petrov"]
        );
    }
}
