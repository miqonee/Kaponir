//! Export detector evidence and actual policy decisions for an offline audit.
//! No rules, S1 provider, user vault or external service are used.
use kaponir_cli::{Config, build_engine, load_packs};
use kaponir_core::{Input, MaskOptions, MemoryVault, discover};
use serde_json::json;
use std::io::Read;

fn main() -> anyhow::Result<()> {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input)?;
    let documents: Vec<discover::Document> = serde_json::from_str(&input)?;
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
        let raw = engine
            .analyze(&input)
            .into_iter()
            .map(|c| {
                json!({"start":c.start,"end":c.end,"value":c.surface(&document.text),
                    "kind":c.kind,"category":c.category,"recognizer":c.recognizer,
                    "origin":c.origin,"polarity":c.polarity,"hard":c.hard,
                    "probability":c.probability(),"evidence":c.evidence})
            })
            .collect::<Vec<_>>();
        let masked = engine.mask_without_s1(
            &input,
            &MaskOptions {
                explain: true,
                journal: false,
                ..Default::default()
            },
            &mut MemoryVault::new(),
        )?;
        let discovery = discover::analyze(&engine, std::slice::from_ref(&document));
        let applied_masks = masked
            .replacements
            .iter()
            .map(|replacement| {
                let mut value = json!(replacement);
                // `original` can be a stored lemma. The replaced input span can
                // be a shorter stem with the grammatical ending left in place.
                value["surface"] = json!(&document.text[replacement.start..replacement.end]);
                value
            })
            .collect::<Vec<_>>();
        output.push(json!({"file":document.name,"raw_candidates":raw,
            "policy_decisions":masked.explain,"applied_masks":applied_masks,
            "masked_text":masked.text,"discovery":discovery}));
    }
    println!(
        "{}",
        serde_json::to_string(&json!({"version":1,"s1":false,"rules":false,
            "policy":engine.policy(),"documents":output}))?
    );
    Ok(())
}
