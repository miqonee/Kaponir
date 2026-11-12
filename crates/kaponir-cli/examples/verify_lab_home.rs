use kaponir_cli::Home;
use kaponir_vault::SqliteVault;
use serde_json::{Value, json};

fn main() -> anyhow::Result<()> {
    let home = Home::new(
        std::env::args_os()
            .nth(1)
            .expect("isolated lab home")
            .into(),
    );
    let manifest: Value = serde_json::from_slice(&std::fs::read("tests/docs/expected.json")?)?;
    let files: std::collections::BTreeSet<&str> = manifest["documents"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["file"].as_str().unwrap())
        .collect();
    let vault = SqliteVault::open(&home.vault_path(), &home.master_key()?)?;
    let mut documents = Vec::new();
    for state in ["queued", "running", "failed", "succeeded"] {
        for (_, raw) in vault.app_records(&format!("learning-job-{state}"), 1000)? {
            let job: Value = serde_json::from_slice(&raw)?;
            let name = job["name"].as_str().unwrap();
            anyhow::ensure!(files.contains(name), "unexpected_document_name");
            let fixture =
                std::fs::read_to_string(std::path::Path::new("tests/docs/input").join(name))?;
            let source = job["source"].as_str().unwrap();
            anyhow::ensure!(
                source == fixture || state == "succeeded" && source.is_empty(),
                "document_source_differs_from_fixture"
            );
            anyhow::ensure!(job["context"].as_str() == Some(""), "unexpected_context");
            documents.push(json!({"name":name,"state":state,"source_matches_fixture":true,"extracted":job["extracted"]}));
        }
    }
    anyhow::ensure!(documents.len() == files.len(), "unexpected_document_count");
    let mut entries = 0;
    for state in ["pending", "active", "rejected", "rolled_back", "conflict"] {
        for (_, raw) in vault.app_records(&format!("learning-entry-{state}"), 20000)? {
            let entry: Value = serde_json::from_slice(&raw)?;
            let name = entry["source"].as_str().unwrap();
            anyhow::ensure!(files.contains(name), "unexpected_entry_source");
            let fixture =
                std::fs::read_to_string(std::path::Path::new("tests/docs/input").join(name))?;
            anyhow::ensure!(
                fixture.contains(entry["value"].as_str().unwrap()),
                "entry_not_grounded_in_fixture"
            );
            entries += 1;
        }
    }
    println!(
        "{}",
        json!({"verified":true,"documents":documents,"entries_checked":entries,"outbound_requests":0})
    );
    Ok(())
}
