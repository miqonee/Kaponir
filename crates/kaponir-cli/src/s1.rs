//! Управление локальной моделью. Обучение использует только подтверждённые человеком метки.

use crate::{Home, load_packs};
use anyhow::Result;
use clap::Subcommand;
use kaponir_core::rules::{RuleAction, ScopedRules};
use kaponir_core::{Category, Lexicon};
use kaponir_s1::ngram::{self, Model, Sample};
use kaponir_vault::{JournalRow, SqliteVault};
use serde_json::json;
use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Subcommand)]
pub enum Command {
    /// Обучить и сохранить модель на решениях review и точных правилах; конфиг не меняется.
    Train,
    /// Настройки S1, версии моделей и качество по перекрёстной проверке (JSON).
    Status,
    /// Поменять местами текущую и предыдущую локальные модели.
    Rollback,
}

pub fn run(home: &Home, command: Command) -> Result<()> {
    let cfg = home.load_config()?;
    cfg.s1.validate()?;
    let dir = home.s1_dir();
    match command {
        Command::Train => {
            let mut lex = Lexicon::default();
            for pack in load_packs(&cfg)? {
                lex.merge(pack.lexicon());
            }
            let vault = SqliteVault::open(&home.vault_path(), &home.master_key()?)?;
            let ttl = i64::from(cfg.journal_ttl_days) * 86_400;
            vault.purge_journal(ttl)?;
            let mut training_rules = home.load_rules()?;
            let feedback = kaponir_proxy::discovery::load(&vault)?.feedback;
            training_rules.push(ScopedRules {
                scope: kaponir_core::Scope::Personal,
                file: kaponir_core::rules::RuleFile {
                    rules: feedback
                        .into_iter()
                        .map(|f| kaponir_core::rules::Rule {
                            category: kaponir_core::discover::profile_categories()
                                .get(f.kind.as_str())
                                .copied(),
                            kind: f.kind,
                            value: Some(f.value),
                            action: if f.sensitive {
                                RuleAction::Mask
                            } else {
                                RuleAction::Keep
                            },
                            ..Default::default()
                        })
                        .collect(),
                    ..Default::default()
                },
            });
            let (samples, conflicts) = samples(
                &lex,
                &vault.journal(None, usize::MAX >> 1)?,
                &training_rules,
            );
            let mut version = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
            if dir.join(ngram::MODEL_FILE).exists() {
                version = version.max(
                    Model::load(&dir.join(ngram::MODEL_FILE))?
                        .version()
                        .saturating_add(1),
                );
            }
            let model = ngram::train(&samples, version)?;
            model.install(&dir)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "model": summary(&model), "conflicting_values_skipped": conflicts,
                    "path": dir.join(ngram::MODEL_FILE), "configured_provider": cfg.s1.provider,
                }))?
            );
            eprintln!(
                "To try the model, set [s1] provider = \"ngram\" and shadow = true in {}.",
                home.config_path().display()
            );
        }
        Command::Status => {
            let read = |name| -> Result<_> {
                let path = dir.join(name);
                if !path.exists() {
                    return Ok(None);
                }
                Ok(Some(summary(&Model::load(&path)?)))
            };
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "provider": cfg.s1.provider, "shadow": cfg.s1.shadow,
                    "weight": cfg.s1.weight, "keep_below": cfg.s1.keep_below,
                    "model": read(ngram::MODEL_FILE)?, "previous": read(ngram::PREV_FILE)?,
                }))?
            );
        }
        Command::Rollback => {
            ngram::rollback(&dir)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&summary(&Model::load(
                    &dir.join(ngram::MODEL_FILE)
                )?))?
            );
        }
    }
    if !matches!(cfg.s1.provider, kaponir_s1::ProviderKind::None) {
        eprintln!(
            "A running `kaponir serve` loads S1 changes on restart or the next rules reload."
        );
    }
    Ok(())
}

fn summary(model: &Model) -> serde_json::Value {
    json!({"version": model.version(), "mask_labels": model.positives,
        "keep_labels": model.negatives, "cross_validation": model.cv})
}

/// Автоматическое обновление локальной модели ngram при подтверждении решений (если достаточно меток).
pub fn auto_train(home: &Home) -> Result<bool> {
    let cfg = home.load_config()?;
    let dir = home.s1_dir();
    let mut lex = Lexicon::default();
    for pack in load_packs(&cfg)? {
        lex.merge(pack.lexicon());
    }
    let vault = SqliteVault::open(&home.vault_path(), &home.master_key()?)?;
    let mut training_rules = home.load_rules()?;
    if let Ok(profile) = kaponir_proxy::discovery::load(&vault) {
        training_rules.push(ScopedRules {
            scope: kaponir_core::Scope::Personal,
            file: kaponir_core::rules::RuleFile {
                rules: profile
                    .feedback
                    .into_iter()
                    .map(|f| kaponir_core::rules::Rule {
                        category: kaponir_core::discover::profile_categories()
                            .get(f.kind.as_str())
                            .copied(),
                        kind: f.kind,
                        value: Some(f.value),
                        action: if f.sensitive {
                            RuleAction::Mask
                        } else {
                            RuleAction::Keep
                        },
                        ..Default::default()
                    })
                    .collect(),
                ..Default::default()
            },
        });
    }
    let (samples, _) = samples(
        &lex,
        &vault.journal(None, usize::MAX >> 1)?,
        &training_rules,
    );
    let mut version = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    if dir.join(ngram::MODEL_FILE).exists()
        && let Ok(existing) = Model::load(&dir.join(ngram::MODEL_FILE))
    {
        version = version.max(existing.version().saturating_add(1));
    }
    if let Ok(model) = ngram::train(&samples, version) {
        model.install(&dir)?;
        return Ok(true);
    }
    Ok(false)
}

/// Повторы не увеличивают вес примера; конфликтующие метки исключаются целиком.
/// Шаблоны и regex не являются примерами строк. Open/dismissed не являются разметкой.
pub fn samples(
    lex: &Lexicon,
    journal: &[JournalRow],
    rules: &[ScopedRules],
) -> (Vec<Sample>, usize) {
    let mut unique: BTreeMap<String, (Sample, bool)> = BTreeMap::new();
    let mut add = |value: &str, label, kind: Option<&str>, category, recognizer: Option<&str>| {
        if value.trim().is_empty() {
            return;
        }
        let sample = Sample::new(value, lex.is_word(value), kind, category, recognizer, label);
        let key = value.to_lowercase();
        match unique.entry(key) {
            std::collections::btree_map::Entry::Vacant(e) => {
                e.insert((sample, false));
            }
            std::collections::btree_map::Entry::Occupied(mut e) => {
                if e.get().0.label != label {
                    e.get_mut().1 = true;
                }
            }
        }
    };
    for row in journal {
        let label = match row.status.as_str() {
            "masked" => true,
            "kept" => false,
            _ => continue,
        };
        let category: Option<Category> = serde_json::from_value(json!(row.category)).ok();
        add(
            &row.value,
            label,
            Some(&row.kind),
            category,
            Some(&row.recognizer),
        );
    }
    for set in rules {
        for rule in set.file.rules.iter().filter(|r| !r.disabled) {
            if let Some(value) = &rule.value {
                add(
                    value,
                    rule.action == RuleAction::Mask,
                    Some(&rule.kind),
                    rule.category,
                    None,
                );
            }
        }
    }
    let conflicts = unique.values().filter(|(_, conflict)| *conflict).count();
    (
        unique
            .into_values()
            .filter_map(|(s, conflict)| (!conflict).then_some(s))
            .collect(),
        conflicts,
    )
}
