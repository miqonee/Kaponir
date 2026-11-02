use kaponir_cli::Home;
use kaponir_core::{Scope, rules::Rule};
use kaponir_vault::SqliteVault;
use serde_json::{Value, json};
use std::{path::PathBuf, process::Command};

struct Temp(Home);
impl Temp {
    fn new() -> Self {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir =
            std::env::temp_dir().join(format!("kaponir-settings-{}-{stamp}", std::process::id()));
        assert!(
            Command::new(env!("CARGO_BIN_EXE_kaponir"))
                .arg("--home")
                .arg(&dir)
                .arg("init")
                .output()
                .unwrap()
                .status
                .success()
        );
        Self(Home::new(dir))
    }
    fn vault(&self) -> SqliteVault {
        SqliteVault::open(&self.0.vault_path(), &self.0.master_key().unwrap()).unwrap()
    }
    fn seed(&self) {
        self.vault().set_private_setting("providers:v2", &serde_json::to_vec(&json!({
            "connections":[{"id":"route-a","kind":"openai","name":"Route","base_url":"https://example.com","api_key":"fixture-provider-key","enabled":true,"pool":true,"models":["fixture-model"],"all_models":false}],"managed":["openai"]
        })).unwrap()).unwrap();
        self.vault()
            .set_private_setting("s1:http-token", b"fixture-s1-key")
            .unwrap();
        let mut cfg = self.0.load_config().unwrap();
        cfg.serve.api_token = Some("fixture-access-key".into());
        std::fs::write(self.0.config_path(), toml::to_string(&cfg).unwrap()).unwrap();
        self.0
            .add_rule(
                Scope::Team,
                Rule {
                    kind: "PROJ".into(),
                    value: Some("Северный Вектор".into()),
                    ..Rule::default()
                },
            )
            .unwrap();
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0.dir);
    }
}

#[test]
fn full_transfer_round_trip_and_cli_format() {
    let source = Temp::new();
    source.seed();
    let target = Temp::new();
    let file: PathBuf = target.0.dir.join("settings.json");
    let result = Command::new(env!("CARGO_BIN_EXE_kaponir"))
        .arg("--home")
        .arg(&source.0.dir)
        .args(["config", "export", "--include-secrets", "--output"])
        .arg(&file)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let exported: Value = serde_json::from_slice(&std::fs::read(&file).unwrap()).unwrap();
    assert_eq!(exported, source.0.export_settings(true).unwrap());
    let imported = Command::new(env!("CARGO_BIN_EXE_kaponir"))
        .arg("--home")
        .arg(&target.0.dir)
        .args(["config", "import"])
        .arg(&file)
        .output()
        .unwrap();
    assert!(
        imported.status.success(),
        "{}",
        String::from_utf8_lossy(&imported.stderr)
    );
    assert_eq!(target.0.export_settings(true).unwrap(), exported);
    assert_eq!(
        target
            .0
            .load_rules()
            .unwrap()
            .iter()
            .find(|r| r.scope == Scope::Team)
            .unwrap()
            .file
            .rules[0]
            .value
            .as_deref(),
        Some("Северный Вектор")
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(file).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}

#[test]
fn redacted_transfer_preserves_matching_keys_and_disables_new_connections() {
    let source = Temp::new();
    source.seed();
    let bundle = source.0.export_settings(false).unwrap();
    let text = bundle.to_string();
    for key in [
        "fixture-provider-key",
        "fixture-s1-key",
        "fixture-access-key",
    ] {
        assert!(!text.contains(key));
    }
    source.0.import_settings(bundle.clone()).unwrap();
    assert_eq!(
        source.0.export_settings(true).unwrap()["providers"]["connections"][0]["api_key"],
        "fixture-provider-key"
    );
    assert_eq!(
        source.0.load_config().unwrap().serve.api_token.as_deref(),
        Some("fixture-access-key")
    );
    let target = Temp::new();
    target.0.import_settings(bundle.clone()).unwrap();
    let output = target.0.export_settings(true).unwrap();
    assert_eq!(output["providers"]["connections"][0]["enabled"], false);
    assert!(
        output["providers"]["connections"][0]
            .get("api_key")
            .is_none()
    );
    let mut changed = bundle;
    changed["providers"]["connections"][0]["base_url"] = json!("https://other.example.com");
    changed["config"]["s1"]["http"]["url"] = json!("https://other.example.com/s1");
    source.0.import_settings(changed).unwrap();
    let output = source.0.export_settings(true).unwrap();
    assert!(
        output["providers"]["connections"][0]
            .get("api_key")
            .is_none()
    );
    assert!(output.get("s1_key").is_none());
}

#[test]
fn rejected_import_does_not_modify_files_or_keys() {
    let home = Temp::new();
    home.seed();
    let original = home.0.export_settings(true).unwrap();
    let config = std::fs::read(home.0.config_path()).unwrap();
    let mut cases = vec![];
    let mut b = original.clone();
    b["version"] = json!(2);
    cases.push(b);
    let mut b = original.clone();
    b["rules"]["team"]["rule"][0]["regex"] = json!("[");
    b["rules"]["team"]["rule"][0]
        .as_object_mut()
        .unwrap()
        .remove("value");
    cases.push(b);
    let mut b = original.clone();
    b["config"]["s1"]["weight"] = json!(99);
    cases.push(b);
    let mut b = original.clone();
    b["providers"]["connections"][0]["id"] = json!("../bad");
    cases.push(b);
    let mut b = original.clone();
    b["providers"]["connections"][0]["base_url"] = json!("https://user:secret@example.com");
    cases.push(b);
    let mut b = original.clone();
    b["config"]["serve"]["listen"] = json!("not-an-address");
    cases.push(b);
    for bundle in cases {
        assert!(home.0.import_settings(bundle).is_err());
        assert_eq!(home.0.export_settings(true).unwrap(), original);
        assert_eq!(std::fs::read(home.0.config_path()).unwrap(), config);
    }
}

#[test]
fn removed_provider_keeps_protocol_managed() {
    let home = Temp::new();
    home.seed();
    let mut bundle = home.0.export_settings(false).unwrap();
    bundle["providers"] = json!({"connections":[],"managed":[]});
    home.0.import_settings(bundle).unwrap();
    assert_eq!(
        home.0.export_settings(false).unwrap()["providers"]["managed"],
        json!(["openai"])
    );
}
