use kaponir_cli::s1::samples;
use kaponir_core::rules::{Rule, RuleAction, RuleFile, ScopedRules};
use kaponir_core::{Category, JournalEntry, Lexicon, Scope, Vault, Verdict};
use kaponir_vault::SqliteVault;
use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output};

struct Temp(std::path::PathBuf);
impl Temp {
    fn new() -> Self {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("kaponir-cli-s1-{}-{stamp}", std::process::id()));
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn call(home: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_kaponir"))
        .arg("--home")
        .arg(home)
        .args(args)
        .env_remove("KAPONIR_VAULT_KEY")
        .env_remove("KAPONIR_TOKEN_KEY")
        .env_remove("KAPONIR_API_TOKEN")
        .output()
        .unwrap()
}
fn ok(home: &Path, args: &[&str]) -> String {
    let out = call(home, args);
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn trains_only_unique_confirmed_values_and_excludes_conflicts() {
    let temp = Temp::new();
    let mut vault = SqliteVault::open(&temp.0.join("vault.db"), &[7; 32]).unwrap();
    for (value, status) in [
        ("confirmed-mask", "masked"),
        ("confirmed-keep", "kept"),
        ("pending", "open"),
        ("ignored", "dismissed"),
    ] {
        vault
            .record(&JournalEntry {
                kind: "PROJ".into(),
                category: Category::Infra,
                recognizer: "paths".into(),
                value: value.into(),
                shape: value.into(),
                context: String::new(),
                probability: 0.6,
                applied: Verdict::Mask,
                evidence: Vec::new(),
            })
            .unwrap();
        let id = vault
            .journal(None, 100)
            .unwrap()
            .into_iter()
            .find(|r| r.value == value)
            .unwrap()
            .id;
        vault.set_journal_status(id, status).unwrap();
    }
    let rule = |value: &str, action| Rule {
        value: Some(value.into()),
        action,
        ..Rule::default()
    };
    let rules = vec![ScopedRules {
        scope: Scope::Personal,
        file: RuleFile {
            rules: vec![
                rule("confirmed-mask", RuleAction::Mask),
                rule("confirmed-keep", RuleAction::Keep),
                rule("CONFLICT", RuleAction::Mask),
                rule("conflict", RuleAction::Keep),
                Rule {
                    regex: Some("x.*".into()),
                    ..Rule::default()
                },
            ],
            ..RuleFile::default()
        },
    }];
    let (samples, conflicts) = samples(
        &Lexicon::default(),
        &vault.journal(None, 100).unwrap(),
        &rules,
    );
    assert_eq!(conflicts, 1);
    assert_eq!(samples.len(), 2);
    assert_eq!(samples.iter().filter(|s| s.label).count(), 1);
    assert!(
        samples
            .iter()
            .all(|s| s.recognizer.as_deref() == Some("paths"))
    );
}

#[test]
fn cli_train_shadow_active_status_and_rollback() {
    let temp = Temp::new();
    ok(&temp.0, &["init"]);
    let empty: Value = serde_json::from_str(&ok(&temp.0, &["s1", "status"])).unwrap();
    assert!(empty["model"].is_null());
    let out = call(&temp.0, &["s1", "train"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("confirmed labels"));
    let mut rules = String::new();
    for (values, action) in [
        (["zorgpay", "zorgcore", "zorgbank"], "mask"),
        (["release", "templates", "helpers"], "keep"),
    ] {
        for value in values {
            rules.push_str(&format!(
                "[[rule]]\nkind = \"PROJ\"\naction = \"{action}\"\nvalue = \"{value}\"\n\n"
            ));
        }
    }
    std::fs::write(temp.0.join("rules/personal.toml"), rules).unwrap();
    let trained: Value = serde_json::from_str(&ok(&temp.0, &["s1", "train"])).unwrap();
    assert_eq!(trained["model"]["mask_labels"], 3);
    assert_eq!(trained["model"]["keep_labels"], 3);
    let original_version = trained["model"]["version"].as_u64().unwrap();
    let cfg = temp.0.join("config.toml");
    let content = std::fs::read_to_string(&cfg)
        .unwrap()
        .replace("provider = \"none\"", "provider = \"ngram\"");
    std::fs::write(&cfg, &content).unwrap();
    let input = temp.0.join("sample.txt");
    std::fs::write(&input, "/opt/zorgmail/config.yaml").unwrap();
    let out: Value =
        serde_json::from_str(&ok(&temp.0, &["mask", input.to_str().unwrap(), "--json"])).unwrap();
    assert!(out["explain"].as_array().unwrap().iter().any(|r| {
        r["evidence"].as_array().unwrap().iter().any(|e| {
            e["source"]
                .as_str()
                .unwrap()
                .starts_with("s1-shadow:ngram:")
        })
    }));
    std::fs::write(&cfg, content.replace("shadow = true", "shadow = false")).unwrap();
    let active: Value =
        serde_json::from_str(&ok(&temp.0, &["mask", input.to_str().unwrap(), "--json"])).unwrap();
    assert!(active["explain"].as_array().unwrap().iter().any(|r| {
        r["evidence"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["source"] == "s1:ngram")
    }));
    ok(&temp.0, &["s1", "train"]);
    let status: Value = serde_json::from_str(&ok(&temp.0, &["s1", "status"])).unwrap();
    assert!(status["model"]["version"].as_u64().unwrap() > original_version);
    assert_eq!(status["previous"]["version"], original_version);
    ok(&temp.0, &["s1", "rollback"]);
    let status: Value = serde_json::from_str(&ok(&temp.0, &["s1", "status"])).unwrap();
    assert_eq!(status["model"]["version"], original_version);
    // Повреждённая предыдущая версия не должна заменять рабочую модель.
    let current = std::fs::read(temp.0.join("s1/ngram.json")).unwrap();
    std::fs::write(temp.0.join("s1/ngram.prev.json"), "broken").unwrap();
    assert!(!call(&temp.0, &["s1", "rollback"]).status.success());
    assert_eq!(
        std::fs::read(temp.0.join("s1/ngram.json")).unwrap(),
        current
    );
}
