//! Versioned settings transfer shared by CLI and Web UI. This is not a vault backup.
use crate::{Config, Home, S1_TOKEN_SETTING, build_engine, load_packs, write_secret};
use anyhow::{Context, Result, ensure};
use kaponir_core::{
    Scope,
    rules::{RuleFile, ScopedRules},
};
use kaponir_proxy::providers::{self, Registry};
use kaponir_vault::SqliteVault;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::net::ToSocketAddrs;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Bundle {
    format: String,
    version: u32,
    config: Config,
    rules: BTreeMap<String, RuleFile>,
    providers: Registry,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    s1_key: Option<String>,
}

impl Home {
    pub fn export_settings(&self, include_secrets: bool) -> Result<Value> {
        let vault = SqliteVault::open(&self.vault_path(), &self.master_key()?)?;
        let mut config = self.load_config()?;
        if !include_secrets {
            config.serve.api_token = None;
        }
        let loaded = self.load_rules()?;
        let rules = [Scope::Global, Scope::Team, Scope::Personal]
            .into_iter()
            .map(|scope| {
                (
                    scope.to_string(),
                    loaded
                        .iter()
                        .find(|r| r.scope == scope)
                        .map(|r| r.file.clone())
                        .unwrap_or_default(),
                )
            })
            .collect();
        Ok(serde_json::to_value(Bundle {
            format: "kaponir-settings".into(),
            version: 1,
            config,
            rules,
            providers: providers::export_settings(&vault, include_secrets)?,
            s1_key: if include_secrets {
                vault
                    .private_setting(S1_TOKEN_SETTING)?
                    .map(String::from_utf8)
                    .transpose()?
            } else {
                None
            },
        })?)
    }

    pub fn import_settings(&self, value: Value) -> Result<()> {
        // Do not echo parser input: the bundle can contain credentials.
        let mut bundle: Bundle = serde_json::from_value(value)
            .map_err(|_| anyhow::anyhow!("invalid settings bundle"))?;
        ensure!(
            bundle.format == "kaponir-settings" && bundle.version == 1,
            "unsupported settings format or version"
        );
        ensure!(
            bundle.rules.len() == 3
                && ["global", "team", "personal"]
                    .iter()
                    .all(|s| bundle.rules.contains_key(*s)),
            "expected global, team and personal rules"
        );
        bundle.config.worker.validate()?;
        let master = self.master_key()?;
        let vault = SqliteVault::open(&self.vault_path(), &master)?;
        let previous = self.load_config()?;
        // Absence means preserve, never implicitly erase a local credential.
        if bundle.config.serve.api_token.is_none() {
            bundle.config.serve.api_token = previous.serve.api_token;
        }
        let saved_key = vault
            .private_setting(S1_TOKEN_SETTING)?
            .map(String::from_utf8)
            .transpose()?;
        // Never send a retained S1 credential to a different endpoint.
        let same_s1 = bundle.config.s1.http.url == previous.s1.http.url;
        let key = bundle
            .s1_key
            .clone()
            .or(if same_s1 { saved_key } else { None });
        bundle.config.s1.http.token = key.clone();
        let rules: Vec<_> = [Scope::Global, Scope::Team, Scope::Personal]
            .into_iter()
            .map(|scope| ScopedRules {
                scope,
                file: bundle.rules[&scope.to_string()].clone(),
            })
            .collect();
        let registry = providers::prepare_import(&vault, bundle.providers)?;
        // Build before writing, checking packs, regexes, thresholds and S1 settings.
        build_engine(
            &bundle.config,
            load_packs(&bundle.config)?,
            &rules,
            self.token_key(&master)?,
            kaponir_s1::build(&bundle.config.s1, &self.s1_dir(), &rules)?,
        )?;
        ensure!(
            !bundle.config.owner.trim().is_empty(),
            "owner must not be empty"
        );
        let addresses: Vec<_> = bundle
            .config
            .serve
            .listen
            .to_socket_addrs()
            .context("invalid listen address")?
            .collect();
        ensure!(!addresses.is_empty(), "listen address did not resolve");
        let access_key = bundle
            .config
            .serve
            .api_token
            .as_deref()
            .filter(|s| !s.is_empty())
            .is_some()
            || std::env::var("KAPONIR_API_TOKEN").is_ok_and(|s| !s.is_empty());
        ensure!(
            access_key || addresses.iter().all(|a| a.ip().is_loopback()),
            "non-loopback listen requires an API token"
        );
        let mut files = vec![(self.config_path(), toml::to_string_pretty(&bundle.config)?)];
        for set in &rules {
            files.push((
                self.rules_path(set.scope),
                toml::to_string_pretty(&set.file)?,
            ));
        }
        std::fs::create_dir_all(self.dir.join("rules"))?;
        // Stage every file first, then rename. Roll back files and DB on ordinary I/O errors.
        let stage = self.dir.join(".settings-import");
        std::fs::create_dir(&stage)
            .context("another settings import is active; check .settings-import")?;
        let result = (|| -> Result<()> {
            let mut originals = Vec::new();
            for (i, (path, content)) in files.iter().enumerate() {
                originals.push(match std::fs::read(path) {
                    Ok(bytes) => Some(bytes),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
                    Err(e) => return Err(e.into()),
                });
                write_secret(&stage.join(i.to_string()), content)?;
            }
            vault.begin()?;
            let mut applied = 0;
            let committed = (|| -> Result<()> {
                vault.set_private_setting("providers:v2", &registry)?;
                if let Some(key) = &key {
                    vault.set_private_setting(S1_TOKEN_SETTING, key.as_bytes())?;
                } else {
                    vault.delete_private_setting(S1_TOKEN_SETTING)?;
                }
                for (i, (path, _)) in files.iter().enumerate() {
                    std::fs::rename(stage.join(i.to_string()), path)?;
                    applied += 1;
                }
                vault.commit()?;
                Ok(())
            })();
            if committed.is_err() {
                vault.rollback()?;
                for i in 0..applied {
                    let path = &files[i].0;
                    if let Some(bytes) = &originals[i] {
                        std::fs::write(path, bytes)?;
                    } else {
                        std::fs::remove_file(path)?;
                    }
                }
            }
            committed
        })();
        let cleanup = std::fs::remove_dir_all(&stage);
        result?;
        cleanup?;
        Ok(())
    }
}
