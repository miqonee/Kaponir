//! Воспроизводит локальный аудит, не обращаясь к провайдерам и не меняя артефакты.
use kaponir_cli::{Config, build_engine, load_packs};
use kaponir_core::discover::{Document, analyze, selected_rules};
use kaponir_core::rules::{RuleFile, ScopedRules};
use kaponir_core::{Escape, Format, Input, MaskOptions, MemoryVault, Principal, Scope};
use regex::Regex;
use serde::Deserialize;
use std::path::Path;

#[derive(Deserialize)]
struct Case {
    id: String,
    file: String,
    format: Format,
    entities: Vec<Entity>,
    public: Vec<String>,
}
#[derive(Deserialize)]
struct Entity {
    value: String,
}
fn occurrences(text: &str, value: &str) -> usize {
    let re = Regex::new(&format!(r"(?i){}", regex::escape(value))).unwrap();
    let ident = |c: char| c.is_alphanumeric() || c == '_';
    re.find_iter(text)
        .filter(|m| {
            !text[..m.start()].chars().next_back().is_some_and(ident)
                && !text[m.end()..].chars().next().is_some_and(ident)
        })
        .count()
}

#[test]
fn corporate_corpus_after_confirmed_discovery_has_no_leaks_or_public_losses() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/corporate-corpus");
    let cases: Vec<Case> =
        serde_json::from_str(&std::fs::read_to_string(root.join("manifest.json")).unwrap())
            .unwrap();
    assert_eq!(cases.len(), 64, "корпус не должен незаметно уменьшаться");
    let documents: Vec<Document> = cases
        .iter()
        .map(|c| Document {
            name: c.id.clone(),
            text: std::fs::read_to_string(root.join(&c.file)).unwrap(),
            format: c.format,
            model_candidates: vec![],
        })
        .collect();
    let mut rules = vec![ScopedRules {
        scope: Scope::Team,
        file: RuleFile::parse(
            &std::fs::read_to_string(root.join("reviewed/rules/team.toml")).unwrap(),
        )
        .unwrap(),
    }];
    let cfg = Config::default();
    let engine = build_engine(&cfg, load_packs(&cfg).unwrap(), &rules, [3; 32], None).unwrap();
    let report = analyze(&engine, &documents);
    // Подтверждённая политика этой синтетической компании: название проекта и все
    // суммы с валютой конфиденциальны. Это НЕ автоматическое принятие всех предложений.
    let ids: Vec<String> = report
        .suggestions
        .iter()
        .filter(|s| (s.kind == "PROJ" && s.value == "kedrledger") || s.kind == "AMOUNT")
        .map(|s| s.id.clone())
        .collect();
    assert_eq!(ids.len(), 65, "один проект и 64 различные суммы");
    rules[0]
        .file
        .rules
        .extend(selected_rules(&report, &ids).unwrap());
    let engine = build_engine(&cfg, load_packs(&cfg).unwrap(), &rules, [3; 32], None).unwrap();
    let mut vault = MemoryVault::new();
    let opts = MaskOptions {
        owner: "audit".into(),
        journal: false,
        ..Default::default()
    };
    let mut values = 0;
    let mut total = 0;
    for (case, doc) in cases.iter().zip(&documents) {
        let masked = engine
            .mask(
                &Input {
                    text: &doc.text,
                    format: doc.format,
                },
                &opts,
                &mut vault,
            )
            .unwrap();
        for e in &case.entities {
            let count = occurrences(&doc.text, &e.value);
            assert!(count > 0, "{}: отсутствует размеченное значение", case.id);
            total += count;
            values += 1;
            assert_eq!(
                occurrences(&masked.text, &e.value),
                0,
                "{}: утечка {}",
                case.id,
                e.value
            );
        }
        for word in &case.public {
            assert_eq!(
                occurrences(&doc.text, word),
                occurrences(&masked.text, word),
                "{}: лишняя маска {}",
                case.id,
                word
            );
        }
        let restored = engine
            .restore(
                &masked.text,
                &Principal::local("audit"),
                &vault,
                Escape::None,
            )
            .unwrap();
        assert!(restored.unknown.is_empty() && restored.denied.is_empty());
        assert_eq!(doc.text, restored.text, "{}: восстановление", case.id);
    }
    assert_eq!(values, 1344);
    assert_eq!(total, 8808);
}

#[test]
fn ocr_regression_covers_contracts_passwords_and_public_paths() {
    let cfg = Config::default();
    let engine = build_engine(&cfg, load_packs(&cfg).unwrap(), &[], [3; 32], None).unwrap();
    let text = "Contract ID: CN-48291\nПароль базы данных: Cedar-47!Plum\nPublic website: https://example.org/pricing";
    let report = analyze(
        &engine,
        &[Document {
            name: "ocr-regression".into(),
            text: text.into(),
            format: Format::Text,
            model_candidates: vec![],
        }],
    );
    let ids: Vec<_> = report
        .suggestions
        .iter()
        .filter(|s| {
            (s.kind == "ID" && s.value == "CN-48291")
                || (s.kind == "SECRET" && s.value == "Cedar-47!Plum")
        })
        .map(|s| s.id.clone())
        .collect();
    // Values already protected by built-in detectors are intentionally omitted
    // from discovery. Check confidentiality, rather than requiring duplicate rules.
    assert!(
        report
            .suggestions
            .iter()
            .any(|s| s.kind == "ID" && s.value == "CN-48291")
    );
    let baseline = engine
        .mask(
            &Input::text(text),
            &MaskOptions::default(),
            &mut kaponir_core::MemoryVault::new(),
        )
        .unwrap();
    assert!(
        report
            .suggestions
            .iter()
            .any(|s| s.kind == "SECRET" && s.value == "Cedar-47!Plum")
            || baseline
                .replacements
                .iter()
                .any(|r| r.kind == "SECRET" && r.original == "Cedar-47!Plum"),
        "password must be protected or suggested"
    );
    assert!(
        !report.suggestions.iter().any(|s| s.value == "pricing"),
        "публичный путь не должен становиться кандидатом"
    );
    let rules = vec![ScopedRules {
        scope: Scope::Team,
        file: RuleFile {
            rules: selected_rules(&report, &ids).unwrap(),
            ..RuleFile::default()
        },
    }];
    let engine = build_engine(&cfg, load_packs(&cfg).unwrap(), &rules, [3; 32], None).unwrap();
    let masked = engine
        .mask(
            &Input::text(text),
            &MaskOptions {
                journal: false,
                ..Default::default()
            },
            &mut MemoryVault::new(),
        )
        .unwrap();
    assert!(!masked.text.contains("CN-48291"), "{}", masked.text);
    assert!(!masked.text.contains("Cedar-47!Plum"), "{}", masked.text);
    assert!(
        masked.text.contains("https://example.org/pricing"),
        "{}",
        masked.text
    );
}

#[test]
fn discovery_generalizes_and_requires_confirmation_for_business_data() {
    let cfg = Config::default();
    let engine = build_engine(&cfg, load_packs(&cfg).unwrap(), &[], [3; 32], None).unwrap();
    let text = "Проект: vegaledger; Project = orioncore\nПроект: nginx\nБюджет: 9 876,54 ₽; USD 42.50\nВерсия 1.25, 250 пользователей, 42 RUBbish\n17\n250 EUR";
    let report = analyze(
        &engine,
        &[Document {
            name: "independent".into(),
            text: text.into(),
            format: Format::Text,
            model_candidates: vec![],
        }],
    );
    for value in [
        "vegaledger",
        "orioncore",
        "9 876,54 ₽",
        "USD 42.50",
        "250 EUR",
    ] {
        assert!(
            report.suggestions.iter().any(|s| s.value == value),
            "не найдено {value}"
        );
    }
    assert!(
        !report
            .suggestions
            .iter()
            .any(|s| s.value == "nginx" || s.value.contains('\n') || s.value.contains("RUBbish"))
    );
    assert!(
        !report
            .suggestions
            .iter()
            .any(|s| s.kind == "AMOUNT" && ["1.25", "250", "17"].contains(&s.value.as_str()))
    );
    assert!(selected_rules(&report, &[]).unwrap().is_empty());
    let masked = engine
        .mask(
            &Input::text(text),
            &MaskOptions::default(),
            &mut MemoryVault::new(),
        )
        .unwrap();
    assert!(masked.text.contains("vegaledger") && masked.text.contains("9 876,54 ₽"));
}
