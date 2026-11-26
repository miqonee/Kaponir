//! Resolve a saved finding against the same literal, template and regex matcher as the proxy.
use crate::RuleSet;
use kaponir_core::{
    AnalyzeCtx, Candidate, Engine, Input, Lexicon, Origin, Polarity, Recognizer, rules::ScopedRules,
};
use kaponir_recognizers::RulesRecognizer;
use serde_json::{Value, json};

pub(crate) struct ActiveRules {
    matcher: RulesRecognizer,
    rules: Vec<Value>,
}

fn matching_text<'a>(
    value: &'a str,
    context: &'a str,
    span: Option<[usize; 2]>,
) -> (&'a str, Option<[usize; 2]>, bool) {
    let recorded = span.filter(|[start, end]| {
        context.get(*start..*end).is_some_and(|surface| {
            surface == value
                || surface.to_lowercase() == value.to_lowercase()
                || surface == value.replace(['\r', '\n'], " ")
        })
    });
    let has_excerpt = recorded.is_some() || context.contains(value);
    (
        if has_excerpt { context } else { value },
        recorded,
        has_excerpt,
    )
}

impl ActiveRules {
    pub(crate) fn new(sets: Vec<RuleSet>) -> anyhow::Result<Self> {
        let mut rules = Vec::new();
        let mut scoped = Vec::with_capacity(sets.len());
        for set in sets {
            let mut file = set.file;
            for (index, rule) in file
                .rules
                .iter_mut()
                .enumerate()
                .filter(|(_, r)| !r.disabled)
            {
                let slot = rules.len();
                rules.push(json!({
                    "id": rule.id, "scope": set.scope, "index": index,
                    "action": rule.action, "kind": rule.kind, "literal": rule.value.is_some(),
                    "path_prefix": rule.directory_prefix(set.scope), "value": rule.value
                }));
                // IDs in user-managed files can be empty or reused. Keep the exact UI address.
                rule.id = slot.to_string();
            }
            scoped.push(ScopedRules {
                scope: set.scope,
                file,
            });
        }
        Ok(Self {
            matcher: RulesRecognizer::new(&scoped)?,
            rules,
        })
    }

    pub(crate) fn covering(
        &self,
        value: &str,
        context: &str,
        span: Option<[usize; 2]>,
        protected: bool,
    ) -> Option<&Value> {
        self.covering_finding(value, context, span, protected, "")
    }

    fn rule_for(&self, candidate: &Candidate) -> Option<&Value> {
        match &candidate.origin {
            Origin::Rule { id, .. } => self.rules.get(id.parse::<usize>().ok()?),
            _ => None,
        }
    }

    fn candidates(
        &self,
        text: &str,
        has_excerpt: bool,
        protected: bool,
        kind: &str,
    ) -> Vec<Candidate> {
        let mut candidates = Vec::new();
        self.matcher.analyze(
            &Input::text(text),
            &AnalyzeCtx::new(&Lexicon::default()),
            &mut candidates,
        );
        candidates.retain(|c| {
            (has_excerpt
                || self.rule_for(c).is_some_and(|r| {
                    r["literal"] == true || (kind == "PATH" && r["path_prefix"].is_string())
                }))
                && (c.polarity != Polarity::Keep || !protected || c.origin.is_enforced())
        });
        candidates
    }

    pub(crate) fn covering_finding(
        &self,
        value: &str,
        context: &str,
        span: Option<[usize; 2]>,
        protected: bool,
        kind: &str,
    ) -> Option<&Value> {
        if self.rules.is_empty() || value.is_empty() {
            return None;
        }
        let (text, recorded, has_excerpt) = matching_text(value, context, span);
        let effective =
            Engine::resolve_rule_matches(self.candidates(text, has_excerpt, protected, kind));
        let at = |start: usize, end: usize| {
            let mut masks = effective
                .iter()
                .filter(|c| c.polarity == Polarity::Mask && c.start < end && start < c.end)
                .peekable();
            if masks.peek().is_some() {
                return masks.find_map(|mask| {
                    let rule = self.rule_for(mask)?;
                    let directory = kind == "PATH"
                        && mask.start == start
                        && rule["path_prefix"].as_str().is_some_and(|p| {
                            super::path_prefixes::directories(value).contains(&p)
                                && mask.end == start + p.len() - 1
                                && mask.end < end
                        });
                    (mask.start <= start && end <= mask.end || directory).then_some(rule)
                });
            }
            let keep = effective
                .iter()
                .filter(|c| c.polarity == Polarity::Keep && c.start <= start && end <= c.end)
                .max_by_key(|c| (c.origin.rank(), c.len()))?;
            self.rule_for(keep)
        };
        if let Some([start, end]) = recorded {
            return at(start, end);
        }
        // Older vaults have excerpts without offsets. Do not assume which duplicate was meant.
        let mut hits = text.match_indices(value);
        let (first, _) = hits.next()?;
        let rule = at(first, first + value.len())?;
        hits.all(|(pos, _)| at(pos, pos + value.len()) == Some(rule))
            .then_some(rule)
    }

    // An existing rule is a conflict only if the proposed directory matched before precedence.
    pub(super) fn directory_conflict(
        &self,
        item: &Value,
        directory_id: &str,
        prefix: &str,
    ) -> Option<Value> {
        let value = item["value"].as_str()?;
        let (text, recorded, has_excerpt) = matching_text(
            value,
            item["context"].as_str().unwrap_or_default(),
            serde_json::from_value(item["context_span"].clone()).ok(),
        );
        let candidates = self.candidates(text, has_excerpt, false, "PATH");
        let effective = Engine::resolve_rule_matches(candidates.clone());
        let starts = recorded
            .map(|[start, _]| vec![start])
            .unwrap_or_else(|| text.match_indices(value).map(|(start, _)| start).collect());
        for start in starts {
            let end = start + prefix.len() - 1;
            let proposed = candidates.iter().find(|c| {
                c.start == start
                    && c.end == end
                    && self
                        .rule_for(c)
                        .is_some_and(|r| r["id"] == directory_id && r["path_prefix"] == prefix)
            })?;
            if effective
                .iter()
                .any(|c| c.origin == proposed.origin && c.start == start && c.end == end)
            {
                continue;
            }
            let blocker = effective
                .iter()
                .filter(|c| match c.polarity {
                    Polarity::Keep => {
                        c.start <= start
                            && end <= c.end
                            && c.origin.rank() >= proposed.origin.rank()
                    }
                    Polarity::Mask => c.start < end && start < c.end,
                })
                .max_by_key(|c| (c.polarity == Polarity::Keep, c.origin.rank(), c.len()))?;
            return Some(json!({
                "rule":self.rule_for(blocker)?, "matched":text.get(blocker.start..blocker.end)?
            }));
        }
        None
    }

    pub(crate) fn project(&self, item: &mut Value) {
        let previous_conflict = item["state"] == "conflict" && item["reason"] == "existing_rule";
        let protected = item["protected"] == true || item["kind"] == "SECRET";
        let rule = self.covering_finding(
            item["value"].as_str().unwrap_or_default(),
            item["context"]
                .as_str()
                .or(item["quote"].as_str())
                .unwrap_or_default(),
            serde_json::from_value(item["context_span"].clone()).ok(),
            protected,
            item["kind"].as_str().unwrap_or_default(),
        );
        if let Some(rule) = rule {
            item["document_rule"] = rule.clone();
            if previous_conflict || matches!(item["state"].as_str(), Some("open" | "pending")) {
                item["state"] = json!("covered");
            }
        } else {
            if previous_conflict {
                item["state"] = json!("pending");
                item["reason"] = json!("");
            }
            if matches!(item["state"].as_str(), Some("open" | "pending"))
                && let Some(literal) = self.covering(
                    item["value"].as_str().unwrap_or_default(),
                    "",
                    None,
                    protected,
                )
            {
                item["unmatched_rule"] = literal.clone();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kaponir_core::{
        Scope,
        rules::{Rule, RuleAction, RuleFile},
    };

    fn set(scope: Scope, rules: Vec<Rule>) -> RuleSet {
        RuleSet {
            scope,
            file: RuleFile {
                rules,
                ..Default::default()
            },
            location: String::new(),
        }
    }

    fn literal(value: &str) -> Rule {
        Rule {
            value: Some(value.into()),
            kind: "USER".into(),
            ..Default::default()
        }
    }

    #[test]
    fn full_value_and_effective_scope_determine_coverage_across_origins() {
        let mask = literal("M.Fixture");
        let keep = Rule {
            action: RuleAction::Keep,
            ..mask.clone()
        };
        let rules = ActiveRules::new(vec![
            set(Scope::Personal, vec![mask]),
            set(Scope::Team, vec![keep]),
        ])
        .unwrap();
        for (origin, state) in [
            ("detector", "open"),
            ("learning", "pending"),
            ("learning", "conflict"),
        ] {
            let mut row = json!({"origin":origin,"state":state,"reason":"existing_rule","kind":"PERSON","value":"m.fixture","context":"user m.fixture."});
            rules.project(&mut row);
            assert_eq!(row["state"], "covered");
            assert_eq!(row["document_rule"]["action"], "keep");
            assert_eq!(row["document_rule"]["scope"], "team");
        }
        assert!(
            rules
                .covering("m.fixture.io", "user m.fixture.io", None, false)
                .is_none()
        );
        assert_eq!(
            rules
                .covering("m.fixture", "user m.fixture.", None, true)
                .unwrap()["action"],
            "mask"
        );
        let rules = ActiveRules::new(vec![set(
            Scope::Team,
            vec![Rule {
                disabled: true,
                ..literal("m.fixture")
            }],
        )])
        .unwrap();
        let mut old = json!({"state":"conflict","reason":"existing_rule","value":"m.fixture"});
        rules.project(&mut old);
        assert_eq!(old["state"], "pending");
        old["state"] = json!("conflict");
        old["reason"] = json!("rule_was_edited");
        rules.project(&mut old);
        assert_eq!(old["state"], "conflict");
    }

    #[test]
    fn directory_conflict_requires_a_match_before_precedence() {
        let prefix = "/work/demo/";
        let value = "/work/demo/notes/report.txt";
        let directory = Rule::for_directory(prefix, Scope::Team).unwrap();
        let directory_id = directory.id.clone();
        for (action, pattern) in [(RuleAction::Mask, "demo"), (RuleAction::Keep, "/work/demo")] {
            let existing = Rule {
                id: "existing".into(),
                action,
                ..literal(pattern)
            };
            let rules = ActiveRules::new(vec![
                set(Scope::Global, vec![existing]),
                set(Scope::Team, vec![directory.clone()]),
            ])
            .unwrap();
            let row = json!({"value":value, "context":format!("Read {value}, then verify.")});
            let conflict = rules
                .directory_conflict(&row, &directory_id, prefix)
                .unwrap();
            assert_eq!(conflict["rule"]["id"], "existing");
            assert_eq!(conflict["rule"]["scope"], "global");
            assert_eq!(conflict["rule"]["action"], json!(action));
            assert_eq!(conflict["matched"], pattern);

            let nested = json!({"value":value, "context":format!("Read /var/lib{value}.")});
            assert!(
                rules
                    .directory_conflict(&nested, &directory_id, prefix)
                    .is_none()
            );
        }
    }

    #[test]
    fn separate_filename_mask_does_not_hide_directory_coverage() {
        let prefix = "/work/demo/";
        let value = "/work/demo/notes/private-name.txt";
        let directory = Rule::for_directory(prefix, Scope::Team).unwrap();
        let rules = ActiveRules::new(vec![set(
            Scope::Team,
            vec![literal("private-name"), directory],
        )])
        .unwrap();
        let text = format!("Read {value}, then verify.");
        let start = text.find(value).unwrap();
        let rule = rules
            .covering_finding(
                value,
                &text,
                Some([start, start + value.len()]),
                false,
                "PATH",
            )
            .unwrap();
        assert_eq!(rule["path_prefix"], prefix);
    }

    #[test]
    fn captures_case_and_byte_offsets_resolve_the_actual_occurrence() {
        let capture = Rule {
            regex: Some("user=(X42)".into()),
            case_sensitive: true,
            ..Default::default()
        };
        let template = Rule {
            template: Some("{env}-{SVC}-{num}".into()),
            ..Default::default()
        };
        let rules = ActiveRules::new(vec![set(Scope::Team, vec![capture, template])]).unwrap();
        let text = "я user=X42, public=X42";
        let first = text.find("X42").unwrap();
        let second = text.rfind("X42").unwrap();
        assert!(rules.covering("X42", text, None, false).is_none());
        assert!(
            rules
                .covering("X42", text, Some([first, first + 3]), false)
                .is_some()
        );
        assert!(
            rules
                .covering("X42", text, Some([second, second + 3]), false)
                .is_none()
        );
        assert!(rules.covering("x42", "user=x42", None, false).is_none());
        assert!(rules.covering("X42", "", None, false).is_none());
        assert!(
            rules
                .covering("orchid", "pod prod-orchid-123", None, false)
                .is_some()
        );
        assert!(
            rules
                .covering("orchid", "plain orchid", None, false)
                .is_none()
        );
    }
}
