//! Encrypted persistent discovery shortlist and explicit human training labels.
use kaponir_core::discover::Profile;
use kaponir_vault::SqliteVault;

const SETTING: &str = "discover:profile:v1";

pub fn load(vault: &SqliteVault) -> anyhow::Result<Profile> {
    Ok(vault
        .private_setting(SETTING)?
        .map(|b| serde_json::from_slice(&b))
        .transpose()?
        .unwrap_or_default())
}

pub fn save(vault: &SqliteVault, profile: &Profile) -> anyhow::Result<()> {
    vault.set_private_setting(SETTING, &serde_json::to_vec(profile)?)?;
    Ok(())
}

/// Atomic across the web process and separate CLI workers sharing SQLite.
pub fn update<T>(
    vault: &SqliteVault,
    change: impl FnOnce(&mut Profile) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    vault.begin()?;
    let result = (|| {
        let mut profile = load(vault)?;
        let value = change(&mut profile)?;
        save(vault, &profile)?;
        vault.commit()?;
        Ok(value)
    })();
    if result.is_err() {
        let _ = vault.rollback();
    }
    result
}
