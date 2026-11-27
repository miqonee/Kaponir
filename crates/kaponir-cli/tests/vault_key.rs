use std::fs;
use std::path::Path;
use std::process::{Command, Output};

mod common;

const KEY: &str = "KAPONIR_VAULT_KEY";
const FILE: &str = "KAPONIR_VAULT_KEY_FILE";

fn cli(home: &Path, action: &str) -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_kaponir"));
    cmd.arg("--home").arg(home).arg(action);
    cmd.env_remove(KEY)
        .env_remove(FILE)
        .env_remove("KAPONIR_TOKEN_KEY");
    cmd
}

fn success(cmd: &mut Command) -> Output {
    let output = cmd.output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output
}

fn fails(cmd: &mut Command, reason: &str) {
    let output = cmd.output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains(reason));
}

#[test]
fn a_mounted_key_reopens_mappings_created_with_the_environment_key() {
    let dir = common::temp_dir("external-vault");
    let home = dir.join("data");
    let secret = dir.join("mounted-secret");
    let key = kaponir_vault::key_to_hex(&kaponir_vault::generate_key().unwrap());
    let init = success(cli(&home, "init").env(KEY, &key));
    let message = String::from_utf8(init.stderr).unwrap();
    assert!(message.contains("externally managed"));
    assert!(!message.contains(&key));
    assert!(!home.join("vault.key").exists());

    let input = dir.join("input.txt");
    let masked = dir.join("masked.txt");
    let text = "synthetic@example.org\n";
    fs::write(&input, text).unwrap();
    let output = success(cli(&home, "mask").arg(&input).env(KEY, &key));
    assert_eq!(output.stdout, b"ANON_EMAIL_1\n");
    fs::write(&masked, output.stdout).unwrap();
    fs::write(&secret, format!("{key}\n")).unwrap();
    let before = fs::read(home.join("config.toml")).unwrap();
    fails(
        cli(&home, "init")
            .arg("--without-dataset")
            .env(KEY, "ab".repeat(32)),
        "wrong vault key",
    );
    fails(&mut cli(&home, "init"), "requires its original key");
    assert_eq!(fs::read(home.join("config.toml")).unwrap(), before);
    success(cli(&home, "init").env(FILE, &secret));
    let restored = success(
        cli(&home, "restore")
            .arg("--strict")
            .arg(masked)
            .env(FILE, secret),
    );
    assert_eq!(restored.stdout, text.as_bytes());
    assert!(!home.join("vault.key").exists());
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn explicitly_configured_secrets_never_fall_back_to_a_local_key() {
    let dir = common::temp_dir("legacy-vault");
    success(&mut cli(&dir, "init"));
    let local = fs::read_to_string(dir.join("vault.key")).unwrap();
    let secret = dir.join("mounted-secret");
    fails(
        cli(&dir, "stats").env(FILE, &secret),
        "cannot read KAPONIR_VAULT_KEY_FILE",
    );
    fs::write(&secret, "+0".repeat(32)).unwrap();
    fails(cli(&dir, "stats").env(FILE, &secret), "64 hex");
    fails(cli(&dir, "stats").env(KEY, ""), "64 hex");
    fails(
        cli(&dir, "stats").env(KEY, local.trim()).env(FILE, &secret),
        "only one",
    );
    #[cfg(unix)]
    {
        use std::ffi::OsStr;
        use std::os::unix::ffi::OsStrExt;
        fails(
            cli(&dir, "stats").env(KEY, OsStr::from_bytes(&[0xff])),
            "must be hex",
        );
    }
    success(cli(&dir, "init").env(KEY, local.trim()));
    assert_eq!(fs::read_to_string(dir.join("vault.key")).unwrap(), local);
    success(&mut cli(&dir, "stats"));
    fs::remove_dir_all(dir).unwrap();
}
