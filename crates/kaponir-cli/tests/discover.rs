use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

struct Temp(PathBuf);

impl Temp {
    fn new(name: &str) -> Self {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "kaponir-cli-discover-{name}-{}-{stamp}",
            std::process::id()
        ));
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

fn rules(home: &Path) -> String {
    std::fs::read_to_string(home.join("rules/personal.toml")).unwrap()
}

#[test]
fn discover_preview_is_inert_and_selection_applies_exact_mask_rules() {
    let temp = Temp::new("preview");
    ok(&temp.0, &["init"]);
    let before = rules(&temp.0);
    let document = temp.0.join("budget.txt");
    std::fs::write(&document, "Бюджет: 1 250 000 ₽\n").unwrap();
    let document = document.to_str().unwrap();

    let preview: Value =
        serde_json::from_str(&ok(&temp.0, &["discover", document, "--json"])).unwrap();
    let suggestion = preview["suggestions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["kind"] == "AMOUNT")
        .unwrap();
    assert_eq!(suggestion["value"], "1 250 000 ₽");
    let id = suggestion["id"].as_str().unwrap();
    assert_eq!(rules(&temp.0), before, "preview must not append rules");

    for ids in [["forged", "forged"], [id, id]] {
        let out = call(
            &temp.0,
            &["discover", document, "--select", ids[0], "--select", ids[1]],
        );
        assert!(!out.status.success());
        assert_eq!(rules(&temp.0), before);
    }

    ok(&temp.0, &["discover", document, "--select", id]);
    let saved = rules(&temp.0);
    assert!(saved.contains("kind = \"AMOUNT\""));
    assert!(saved.contains("action = \"mask\""));
    assert!(saved.contains("value = \"1 250 000 ₽\""));
    let masked = ok(&temp.0, &["mask", document, "--dry-run"]);
    assert!(masked.contains("ANON_AMOUNT_1"), "{masked}");
}

#[test]
fn init_learn_previews_or_applies_only_explicit_selection() {
    let temp = Temp::new("learn");
    let document = temp.0.join("budget.txt");
    std::fs::write(&document, "Смета: USD 250.00\n").unwrap();
    let document = document.to_str().unwrap();

    let out = ok(&temp.0, &["init", "--learn", document]);
    assert!(out.contains("USD 250.00"), "{out}");
    assert!(!rules(&temp.0).contains("USD 250.00"));

    let preview: Value =
        serde_json::from_str(&ok(&temp.0, &["discover", document, "--json"])).unwrap();
    let id = preview["suggestions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["kind"] == "AMOUNT")
        .unwrap()["id"]
        .as_str()
        .unwrap()
        .to_string();
    ok(&temp.0, &["init", "--learn", document, "--select", &id]);
    assert!(rules(&temp.0).contains("value = \"USD 250.00\""));
}

#[test]
fn optional_setup_wizard_skips_and_requires_final_confirmation() {
    use std::io::Write;
    use std::process::Stdio;
    let temp = Temp::new("wizard");
    ok(&temp.0, &["init"]);
    let initial = rules(&temp.0);
    let wizard = |answers: &str| {
        let mut process = Command::new(env!("CARGO_BIN_EXE_kaponir"))
            .arg("--home")
            .arg(&temp.0)
            .args(["init", "--wizard", "--language", "en"])
            .env_remove("KAPONIR_VAULT_KEY")
            .env_remove("KAPONIR_TOKEN_KEY")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        process
            .stdin
            .take()
            .unwrap()
            .write_all(answers.as_bytes())
            .unwrap();
        let output = process.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    };
    wizard("n\n");
    assert_eq!(rules(&temp.0), initial);
    wizard("");
    assert_eq!(rules(&temp.0), initial);
    wizard("y\n1\ncorp.example\nVega Ledger\nn\n");
    assert_eq!(rules(&temp.0), initial);
    wizard("y\n1\ncorp.example\nVega Ledger\ny\n");
    let saved = rules(&temp.0);
    assert!(saved.contains("corp.example") && saved.contains("Vega Ledger"));
    wizard("y\n1\ncorp.example\nVega Ledger\ny\n");
    assert_eq!(
        rules(&temp.0),
        saved,
        "repeating setup must not duplicate rules"
    );
}

#[test]
fn saved_collection_deduplicates_and_negative_labels_train_without_keep_rules() {
    let temp = Temp::new("feedback");
    ok(&temp.0, &["init"]);
    let document = temp.0.join("prices.txt");
    std::fs::write(&document,"Price: 101 USD\nPrice: 102 USD\nPrice: 103 USD\nPrice: 201 USD\nPrice: 202 USD\nPrice: 203 USD").unwrap();
    let d = document.to_str().unwrap();
    let preview: Value = serde_json::from_str(&ok(
        &temp.0,
        &["discover", d, "--s1", "--remember", "--json"],
    ))
    .unwrap();
    assert_eq!(preview["s1_status"], "off");
    ok(&temp.0, &["discover", d, "--remember", "--json"]);
    let saved: Value =
        serde_json::from_str(&ok(&temp.0, &["discover", "--saved", "--json"])).unwrap();
    let amounts: Vec<_> = saved["suggestions"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["kind"] == "AMOUNT")
        .collect();
    assert_eq!(amounts.len(), 6);
    assert!(amounts.iter().all(|s| s["count"] == 1));
    let mut args = vec!["discover", "--saved"];
    for s in &amounts {
        args.push(if s["value"].as_str().unwrap().starts_with('1') {
            "--select"
        } else {
            "--reject"
        });
        args.push(s["id"].as_str().unwrap());
    }
    ok(&temp.0, &args);
    let rules = rules(&temp.0);
    assert!(
        kaponir_core::rules::RuleFile::parse(&rules)
            .unwrap()
            .rules
            .iter()
            .all(|r| r.action == kaponir_core::rules::RuleAction::Mask)
    );
    assert!(!rules.contains("201 USD"));
    let after: Value =
        serde_json::from_str(&ok(&temp.0, &["discover", "--saved", "--json"])).unwrap();
    assert!(after["suggestions"].as_array().unwrap().is_empty());
    let trained: Value = serde_json::from_str(&ok(&temp.0, &["s1", "train"])).unwrap();
    assert_eq!(trained["model"]["mask_labels"], 3);
    assert_eq!(trained["model"]["keep_labels"], 3);
}

#[test]
fn concurrent_worker_imports_do_not_overwrite_collections() {
    let temp = Temp::new("workers");
    ok(&temp.0, &["init"]);
    let mut children = Vec::new();
    for n in 0..4 {
        let file = temp.0.join(format!("doc-{n}.json"));
        std::fs::write(&file,serde_json::to_vec(&serde_json::json!([{"name":format!("worker-{n}"),"text":format!("Price: {} USD",100+n)}])).unwrap()).unwrap();
        children.push(
            Command::new(env!("CARGO_BIN_EXE_kaponir"))
                .arg("--home")
                .arg(&temp.0)
                .args(["discover", "--documents-json"])
                .arg(file)
                .args(["--remember", "--json"])
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::piped())
                .spawn()
                .unwrap(),
        );
    }
    for child in children {
        let r = child.wait_with_output().unwrap();
        assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
    }
    let report: Value =
        serde_json::from_str(&ok(&temp.0, &["discover", "--saved", "--json"])).unwrap();
    assert_eq!(
        report["suggestions"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|s| s["kind"] == "AMOUNT")
            .count(),
        4
    );
}

#[test]
fn local_worker_import_does_not_require_external_s1_credentials() {
    let temp = Temp::new("local-no-s1");
    ok(&temp.0, &["init"]);
    let home = kaponir_cli::Home::new(temp.0.clone());
    let mut cfg = home.load_config().unwrap();
    cfg.s1.provider = kaponir_s1::ProviderKind::Http;
    cfg.s1.http.url = "http://127.0.0.1:9/score".into();
    cfg.s1.http.token_env = Some("KAPONIR_TEST_UNSET_WORKER_KEY_98372".into());
    std::fs::write(home.config_path(), toml::to_string(&cfg).unwrap()).unwrap();
    let file = temp.0.join("docs.json");
    std::fs::write(&file, r#"[{"name":"source","text":"Price: 982 USD"}]"#).unwrap();
    let result = ok(
        &temp.0,
        &[
            "discover",
            "--documents-json",
            file.to_str().unwrap(),
            "--remember",
            "--json",
        ],
    );
    let report: Value = serde_json::from_str(&result).unwrap();
    assert_eq!(report["s1_status"], "not_requested");
    assert!(!report["suggestions"].as_array().unwrap().is_empty());
}
