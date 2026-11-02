//! Shared, additive setup plan for CLI and Web. No automatic KEEP rules or external S1.
use crate::rules::Rule;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Default, Deserialize, Serialize)]
pub struct Answers {
    #[serde(default)]
    pub domains: Vec<String>,
    #[serde(default)]
    pub projects: Vec<String>,
}
#[derive(Serialize)]
pub struct Plan {
    pub domains: Vec<String>,
    pub rules: Vec<Rule>,
}

pub fn plan(answers: &Answers) -> Result<Plan, String> {
    if answers.domains.len() > 100 || answers.projects.len() > 100 {
        return Err("setup.too_many".into());
    }
    let mut domains = BTreeSet::new();
    for domain in &answers.domains {
        let domain = domain.trim().trim_end_matches('.').to_ascii_lowercase();
        if domain.is_empty() {
            continue;
        }
        if domain.len() > 253
            || !domain.contains('.')
            || domain.split('.').any(|label| {
                label.is_empty()
                    || label.len() > 63
                    || label.starts_with('-')
                    || label.ends_with('-')
                    || !label
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || c == b'-')
            })
        {
            return Err("setup.invalid_domain".into());
        }
        domains.insert(domain);
    }
    let mut projects = BTreeSet::new();
    let mut rules = Vec::new();
    for value in &answers.projects {
        let value = value.trim();
        if value.is_empty() {
            continue;
        }
        if value.chars().count() < 3 || value.len() > 200 || value.chars().any(char::is_control) {
            return Err("setup.invalid_project".into());
        }
        if projects.insert(value.to_lowercase()) {
            rules.push(Rule {
                kind: "PROJ".into(),
                value: Some(value.into()),
                category: Some(crate::Category::Infra),
                note: "Initial setup / Первоначальная настройка".into(),
                ..Rule::default()
            });
        }
    }
    Ok(Plan {
        domains: domains.into_iter().collect(),
        rules,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn plan_is_additive_deduplicated_and_rejects_urls() {
        let p = plan(&Answers {
            domains: vec![" Corp.Example ".into(), "corp.example".into()],
            projects: vec!["Vega Ledger".into(), "vega ledger".into()],
        })
        .unwrap();
        assert_eq!(p.domains, vec!["corp.example"]);
        assert_eq!(p.rules.len(), 1);
        assert_eq!(p.rules[0].action, crate::rules::RuleAction::Mask);
        for value in [
            "https://corp.example/path",
            "corp.example:9000",
            "*.example",
            "x..example",
        ] {
            assert!(
                plan(&Answers {
                    domains: vec![value.into()],
                    ..Default::default()
                })
                .is_err()
            );
        }
        assert!(plan(&Answers::default()).unwrap().rules.is_empty());
    }
}
