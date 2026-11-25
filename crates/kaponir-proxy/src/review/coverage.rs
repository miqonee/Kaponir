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
                    "path_prefix": rule.directory_prefix(set.scope)
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
        let recorded = span.filter(|[start, end]| {
            context.get(*start..*end).is_some_and(|surface| {
                surface == value
                    || surface.to_lowercase() == value.to_lowercase()
                    || surface == value.replace(['\r', '\n'], " ")
            })
        });
        let has_excerpt = recorded.is_some() || context.contains(value);
        let text = if has_excerpt { context } else { value };
        let mut candidates = Vec::new();
        self.matcher.analyze(
            &Input::text(text),
            &AnalyzeCtx::new(&Lexicon::default()),
            &mut candidates,
        );
        let address = |c: &Candidate| match &c.origin {
            Origin::Rule { id, .. } => self.rules.get(id.parse::<usize>().ok()?),
            _ => None,
        };
        candidates.retain(|c| {
            (has_excerpt
                || address(c).is_some_and(|r| {
                    r["literal"] == true || (kind == "PATH" && r["path_prefix"].is_string())
                }))
                && (c.polarity != Polarity::Keep || !protected || c.origin.is_enforced())
        });
        let effective = Engine::resolve_rule_matches(candidates);
        let at = |start: usize, end: usize| {
            if let Some(mask) = effective
                .iter()
                .find(|c| c.polarity == Polarity::Mask && c.start < end && start < c.end)
            {
                let rule = address(mask)?;
                let prefix = rule["path_prefix"].as_str();
                let directory = kind == "PATH"
                    && mask.start == start
                    && prefix.is_some_and(|p| {
                        super::paths::directories(value).contains(&p)
                            && mask.end == start + p.len() - 1
                            && mask.end < end
                    });
                return (mask.start <= start && end <= mask.end || directory).then_some(rule);
            }
            let keep = effective
                .iter()
                .filter(|c| c.polarity == Polarity::Keep && c.start <= start && end <= c.end)
                .max_by_key(|c| (c.origin.rank(), c.len()))?;
            address(keep)
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
