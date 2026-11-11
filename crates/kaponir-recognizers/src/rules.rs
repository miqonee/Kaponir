//! Правила контуров Global/Team/Personal: точные значения, составные шаблоны и регулярные выражения.
//!
//! Шаблон: `{env:prod|stage}-{SVC}-{hash}`. `{KIND}` в верхнем регистре — чувствительный слот,
//! в нижнем — сохраняемый класс (`hash`, `num`, `word`, `any`, `env`) или перечисление `{name:a|b}`.

use kaponir_core::rules::{RuleAction, ScopedRules};
use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Origin, Recognizer, TokenForm};
use regex::{Regex, RegexBuilder};
use std::collections::HashMap;

use crate::{char_after, char_before, is_ident};

const ID: &str = "rules";

struct Meta {
    kind: String,
    origin: Origin,
    action: RuleAction,
    category: Category,
    strategy: Option<kaponir_core::Strategy>,
    form: Option<TokenForm>,
}

struct Pattern {
    re: Regex,
    meta: usize,
    /// Номера групп со слотами и их KIND; пусто — маскируется всё совпадение.
    slots: Vec<(usize, String)>,
    bounded: bool,
}

pub struct RulesRecognizer {
    metas: Vec<Meta>,
    literals_ci: Option<Regex>,
    literals_cs: Option<Regex>,
    by_literal: HashMap<String, Vec<usize>>,
    patterns: Vec<Pattern>,
}

#[derive(Debug, thiserror::Error)]
#[error("rule `{rule}` ({scope}): {msg}")]
pub struct RuleError {
    pub rule: String,
    pub scope: kaponir_core::Scope,
    pub msg: String,
}

/// Компилирует шаблон в регулярное выражение. Возвращает (regex, слоты по номерам групп).
pub fn compile_template(t: &str) -> Result<(String, Vec<(usize, String)>), String> {
    let mut re = String::new();
    let mut slots = Vec::new();
    let mut group = 0;
    let mut rest = t;
    while let Some(open) = rest.find('{') {
        re.push_str(&regex::escape(&rest[..open]));
        let close = rest[open..].find('}').ok_or("unclosed `{`")? + open;
        let spec = &rest[open + 1..close];
        let (name, options) = match spec.split_once(':') {
            Some((n, o)) => (n.trim(), Some(o)),
            None => (spec.trim(), None),
        };
        if name.is_empty() {
            return Err("empty slot `{}`".into());
        }
        if let Some(opts) = options {
            let alts: Vec<String> = opts.split('|').map(|o| regex::escape(o.trim())).collect();
            let alt = alts.join("|");
            if name.chars().next().is_some_and(char::is_uppercase) {
                group += 1;
                slots.push((group, name.to_string()));
                re.push_str(&format!("({alt})"));
            } else {
                re.push_str(&format!("(?:{alt})"));
            }
        } else if name.chars().next().is_some_and(char::is_uppercase) {
            group += 1;
            slots.push((group, name.to_string()));
            re.push_str(r"([\p{L}\p{N}](?:[\p{L}\p{N}_.\-]*?[\p{L}\p{N}])?)");
        } else {
            re.push_str(match name {
                "hash" => r"(?:[0-9a-f]{5,16}|[a-z0-9]{5})",
                "num" => r"\d+",
                "word" => r"\p{L}+",
                "any" => r"[\p{L}\p{N}_.\-]+?",
                "env" => r"(?:prod|production|prd|stage|staging|stg|dev|test|qa|uat|preprod)",
                other => {
                    return Err(format!(
                        "unknown class `{{{other}}}` (hash|num|word|any|env|NAME:a|b)"
                    ));
                }
            });
        }
        rest = &rest[close + 1..];
    }
    re.push_str(&regex::escape(rest));
    if slots.is_empty() {
        return Err("template has no sensitive `{KIND}` slot".into());
    }
    Ok((re, slots))
}

impl RulesRecognizer {
    pub fn new(sets: &[ScopedRules]) -> Result<Self, RuleError> {
        let mut metas = Vec::new();
        let mut by_literal: HashMap<String, Vec<usize>> = HashMap::new();
        let (mut ci, mut cs) = (Vec::new(), Vec::new());
        let mut patterns = Vec::new();
        for set in sets {
            for (n, r) in set.file.rules.iter().enumerate() {
                if r.disabled {
                    continue;
                }
                let id = if r.id.is_empty() {
                    format!("{}#{}", set.scope, n + 1)
                } else {
                    r.id.clone()
                };
                let err = |msg: String| RuleError {
                    rule: id.clone(),
                    scope: set.scope,
                    msg,
                };
                let meta = metas.len();
                metas.push(Meta {
                    kind: r.kind.to_ascii_uppercase(),
                    origin: Origin::Rule {
                        scope: set.scope,
                        enforced: r.enforced && set.scope == kaponir_core::Scope::Global,
                        id: id.clone(),
                    },
                    action: r.action,
                    category: r.category.unwrap_or(Category::Custom),
                    strategy: r.strategy,
                    form: r.form,
                });
                let kinds = usize::from(r.value.is_some())
                    + usize::from(r.template.is_some())
                    + usize::from(r.regex.is_some());
                if kinds != 1 {
                    return Err(err(
                        "exactly one of `value`, `template`, `regex` is required".into(),
                    ));
                }
                if let Some(v) = &r.value {
                    if v.is_empty() {
                        return Err(err("empty value".into()));
                    }
                    if r.case_sensitive {
                        cs.push(regex::escape(v));
                        by_literal.entry(format!("cs:{v}")).or_default().push(meta);
                    } else {
                        ci.push(regex::escape(v));
                        by_literal
                            .entry(format!("ci:{}", v.to_lowercase()))
                            .or_default()
                            .push(meta);
                    }
                }
                if let Some(t) = &r.template {
                    let (re, slots) = compile_template(t).map_err(err)?;
                    let re = RegexBuilder::new(&re)
                        .case_insensitive(!r.case_sensitive)
                        .build()
                        .map_err(|e| err(e.to_string()))?;
                    patterns.push(Pattern {
                        re,
                        meta,
                        slots,
                        bounded: true,
                    });
                }
                if let Some(p) = &r.regex {
                    let re = RegexBuilder::new(p)
                        .case_insensitive(!r.case_sensitive)
                        .build()
                        .map_err(|e| err(e.to_string()))?;
                    let slots = (1..re.captures_len())
                        .map(|g| (g, metas[meta].kind.clone()))
                        .collect();
                    patterns.push(Pattern {
                        re,
                        meta,
                        slots,
                        bounded: false,
                    });
                }
            }
        }
        let alt = |v: &mut Vec<String>, ci: bool| -> Result<Option<Regex>, RuleError> {
            if v.is_empty() {
                return Ok(None);
            }
            // Длинные варианты первыми: alternation в regex — leftmost-first.
            v.sort_by_key(|s| std::cmp::Reverse(s.len()));
            v.dedup();
            RegexBuilder::new(&v.join("|"))
                .case_insensitive(ci)
                .size_limit(64 << 20)
                .build()
                .map(Some)
                .map_err(|e| RuleError {
                    rule: "literals".into(),
                    scope: kaponir_core::Scope::Personal,
                    msg: e.to_string(),
                })
        };
        Ok(Self {
            literals_ci: alt(&mut ci, true)?,
            literals_cs: alt(&mut cs, false)?,
            metas,
            by_literal,
            patterns,
        })
    }

    fn candidate(
        &self,
        meta: usize,
        kind: &str,
        start: usize,
        end: usize,
        text: &str,
    ) -> Candidate {
        let m = &self.metas[meta];
        let mut c = match m.action {
            RuleAction::Keep => Candidate::keep(start, end, ID, m.origin.clone()),
            RuleAction::Mask => {
                Candidate::new(start, end, kind, m.category, ID, 0.99).origin(m.origin.clone())
            }
        };
        if let Some(s) = m.strategy {
            c = c.strategy(s);
        }
        let dns_like = text[start..end]
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-')
            && is_dns_context(text, start, end);
        if m.form == Some(TokenForm::Dns) || (m.form.is_none() && dns_like) {
            c = c.dns().canonical(text[start..end].to_lowercase());
        }
        c
    }
}

/// Слот окружён `-` или `.` — значит, он внутри хостнейма / имени пода.
fn is_dns_context(text: &str, start: usize, end: usize) -> bool {
    let b = char_before(text, start);
    let a = char_after(text, end);
    matches!(b, Some('-' | '.')) || matches!(a, Some('-' | '.'))
}

impl Recognizer for RulesRecognizer {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        for (re, ci) in [(&self.literals_ci, true), (&self.literals_cs, false)] {
            let Some(re) = re else { continue };
            for m in re.find_iter(text) {
                if char_before(text, m.start()).is_some_and(is_ident)
                    || char_after(text, m.end()).is_some_and(is_ident)
                {
                    continue;
                }
                let key = if ci {
                    format!("ci:{}", m.as_str().to_lowercase())
                } else {
                    format!("cs:{}", m.as_str())
                };
                for &meta in self.by_literal.get(&key).into_iter().flatten() {
                    let kind = self.metas[meta].kind.clone();
                    out.push(self.candidate(meta, &kind, m.start(), m.end(), text));
                }
            }
        }
        for p in &self.patterns {
            for cap in p.re.captures_iter(text) {
                let whole = cap.get(0).expect("match");
                if whole.is_empty()
                    || (p.bounded
                        && (char_before(text, whole.start()).is_some_and(is_ident)
                            || char_after(text, whole.end()).is_some_and(is_ident)))
                {
                    continue;
                }
                if p.slots.is_empty() {
                    let kind = self.metas[p.meta].kind.clone();
                    out.push(self.candidate(p.meta, &kind, whole.start(), whole.end(), text));
                    continue;
                }
                for (g, kind) in &p.slots {
                    if let Some(m) = cap.get(*g).filter(|m| !m.is_empty()) {
                        out.push(self.candidate(p.meta, kind, m.start(), m.end(), text));
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kaponir_core::Scope;
    use kaponir_core::rules::RuleFile;

    fn rec(toml: &str, scope: Scope) -> RulesRecognizer {
        RulesRecognizer::new(&[ScopedRules {
            scope,
            file: RuleFile::parse(toml).unwrap(),
        }])
        .unwrap()
    }

    fn found(r: &RulesRecognizer, text: &str) -> Vec<(String, String, bool)> {
        let l = kaponir_core::Lexicon::default();
        let mut out = Vec::new();
        r.analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        out.into_iter()
            .map(|c| {
                (
                    c.kind.clone(),
                    text[c.start..c.end].to_string(),
                    c.polarity == kaponir_core::Polarity::Keep,
                )
            })
            .collect()
    }

    #[test]
    fn template_slots() {
        let r = rec("[[rule]]\ntemplate = \"{env}-{SVC}-{hash}\"\n", Scope::Team);
        let f = found(&r, "pod prod-billing-auth-7d9f8b-x2v up");
        assert_eq!(
            f,
            vec![("SVC".to_string(), "billing-auth".to_string(), false)]
        );
    }

    #[test]
    fn literals_and_keep() {
        let r = rec(
            "[[rule]]\nkind = \"CLIENT\"\nvalue = \"Рога и Копыта\"\n[[rule]]\naction = \"keep\"\nvalue = \"postgres\"\n",
            Scope::Personal,
        );
        let f = found(&r, "клиент рога и копыта, db postgres, postgresql");
        assert_eq!(
            f,
            vec![
                ("CLIENT".to_string(), "рога и копыта".to_string(), false),
                ("KEEP".to_string(), "postgres".to_string(), true)
            ]
        );
    }

    #[test]
    fn template_errors() {
        assert!(compile_template("{env}-{hash}").is_err());
        assert!(compile_template("{nope}-{SVC}").is_err());
        assert!(compile_template("{SVC").is_err());
    }
}
