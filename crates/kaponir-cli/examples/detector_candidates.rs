//! Export the actual built-in candidate pool for offline validation experiments.
//! Reads documents from stdin; no vault, learning, provider, or rules are modified.
use kaponir_cli::{Config, build_engine, load_packs};
use kaponir_core::{Input, MaskOptions, MemoryVault, Origin, Polarity, discover};
use serde_json::json;
use std::{collections::BTreeMap, io::Read};

fn main() -> anyhow::Result<()> {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input)?;
    let documents: Vec<discover::Document> = serde_json::from_str(&input)?;
    println!("{}", serde_json::to_string(&export(documents)?)?);
    Ok(())
}

fn export(documents: Vec<discover::Document>) -> anyhow::Result<serde_json::Value> {
    discover::validate_documents(&documents).map_err(anyhow::Error::msg)?;
    anyhow::ensure!(
        documents.iter().all(|d| d.model_candidates.is_empty()),
        "detectors_only"
    );
    let cfg = Config::default();
    let engine = build_engine(&cfg, load_packs(&cfg)?, &[], [7; 32], None)?;
    let mut output = Vec::new();
    for document in documents {
        let input = Input {
            text: &document.text,
            format: document.format,
        };
        let masked = engine.mask_without_s1(
            &input,
            &MaskOptions {
                explain: true,
                journal: false,
                ..Default::default()
            },
            &mut MemoryVault::new(),
        )?;
        let mut candidates = BTreeMap::new();
        for c in engine.analyze(&input) {
            if c.origin != Origin::Builtin || c.polarity != Polarity::Mask {
                continue;
            }
            let applied = masked
                .explain
                .iter()
                .any(|e| e.applied && e.start == c.start && e.end == c.end && e.kind == c.kind);
            let protected = c.hard && c.category == kaponir_core::Category::Credential;
            let key = (c.start, c.end);
            if !candidates.contains_key(&key) || applied || protected {
                candidates.insert(key,json!({"start":c.start,"end":c.end,"value":&document.text[c.start..c.end],"kind":c.kind,
                    "protected":protected,"applied":applied,"origin":"builtin","recognizer":c.recognizer}));
            }
        }
        let suggestions = discover::analyze(&engine, std::slice::from_ref(&document));
        for s in suggestions.suggestions {
            for example in s.examples {
                let (start, end) = (example.start, example.end);
                anyhow::ensure!(
                    document.text.get(start..end) == Some(s.value.as_str()),
                    "discovery_offset_mismatch"
                );
                candidates.entry((start,end)).or_insert_with(||json!({"start":start,"end":end,"value":s.value,"kind":s.kind,
                    "protected":false,"applied":false,"origin":"discovery","recognizer":"business-discovery"}));
            }
        }
        output.push(json!({"file":document.name,"candidates":candidates.into_values().collect::<Vec<_>>(),"suggestions_truncated":suggestions.truncated}));
    }
    Ok(json!({"version":1,"s1":false,"rules":false,"documents":output}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn discovery_context_uses_the_actual_match_not_a_numeric_suffix() {
        let text = "# Private estimate\n\n| A | 236 800,00 ₽ |\n| B | 6 800,00 ₽ |\n";
        let documents =
            serde_json::from_value(json!([{"name":"estimate.md","text":text}])).unwrap();
        let result = export(documents).unwrap();
        let values: Vec<_> = result["documents"][0]["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["value"] == "6 800,00 ₽")
            .collect();
        assert!(!values.is_empty());
        for value in values {
            assert_eq!(
                value["start"].as_u64().unwrap() as usize,
                text.find("| B |").unwrap() + 6
            );
            assert_eq!(
                &text[value["start"].as_u64().unwrap() as usize
                    ..value["end"].as_u64().unwrap() as usize],
                "6 800,00 ₽"
            );
        }
    }
}
