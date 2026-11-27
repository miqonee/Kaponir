//! Additional system instructions for agent requests through the proxy.

use anyhow::Result;
use kaponir_vault::SqliteVault;

const KEY: &str = "proxy:system-prompt:v1";

pub fn load(vault: &SqliteVault) -> Result<Option<String>> {
    Ok(vault
        .private_setting(KEY)?
        .map(String::from_utf8)
        .transpose()?)
}

pub fn save(vault: &SqliteVault, prompt: &str) -> Result<()> {
    if prompt.trim().is_empty() {
        vault.delete_private_setting(KEY)?;
    } else {
        vault.set_private_setting(KEY, prompt.as_bytes())?;
    }
    Ok(())
}
