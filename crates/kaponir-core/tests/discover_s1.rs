use kaponir_core::discover::{self, Document, Options, Profile};
use kaponir_core::s1::{Calibration, CandidateView, Disclosure, S1Config, S1Error, S1Provider};
use kaponir_core::{AnalyzeCtx, Candidate, Category, Engine, Input, Recognizer};
use std::sync::{Arc, Mutex};

struct Secrets;
impl Recognizer for Secrets {
    fn id(&self) -> &str {
        "test-secret"
    }
    fn analyze(&self, input: &Input<'_>, _: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        for (start, _) in input.text.match_indices("KEY_") {
            let end = input.text[start..]
                .find('\n')
                .map_or(input.text.len(), |n| start + n);
            let mut c = Candidate::new(
                start,
                end,
                "SECRET",
                Category::Credential,
                "test-secret",
                0.99,
            );
            c.hard = true;
            out.push(c);
        }
    }
}
struct Recording {
    seen: Arc<Mutex<Vec<CandidateView>>>,
    level: Disclosure,
    invalid: bool,
}
impl S1Provider for Recording {
    fn id(&self) -> &str {
        "test"
    }
    fn max_disclosure(&self) -> Disclosure {
        self.level
    }
    fn score(&self, batch: &[CandidateView]) -> Result<Vec<f32>, S1Error> {
        self.seen.lock().unwrap().extend_from_slice(batch);
        if self.invalid {
            return Ok(vec![f32::NAN; batch.len()]);
        }
        Ok(batch
            .iter()
            .map(|v| {
                if v.context.as_ref().is_some_and(|c| c.contains("Публичная")) {
                    0.1
                } else {
                    0.9
                }
            })
            .collect())
    }
}
fn engine(level: Disclosure, invalid: bool) -> (Engine, Arc<Mutex<Vec<CandidateView>>>) {
    let seen = Arc::new(Mutex::new(vec![]));
    (
        Engine::builder([1; 32])
            .recognizer(Box::new(Secrets))
            .s1(S1Config {
                provider: Box::new(Recording {
                    seen: seen.clone(),
                    level,
                    invalid,
                }),
                calibration: Calibration::default(),
                weight: 1.0,
                keep_below: 0.1,
                shadow: false,
                context_bytes: 160,
                profile: Default::default(),
            })
            .build()
            .unwrap(),
        seen,
    )
}
fn doc(name: &str, text: &str) -> Document {
    Document {
        name: name.into(),
        text: text.into(),
        format: Default::default(),
        model_candidates: vec![],
    }
}

#[test]
fn discovers_complete_names_and_values_without_calling_s1() {
    let (engine, seen) = engine(Disclosure::L2, false);
    let report = discover::analyze(
        &engine,
        &[doc(
            "a",
            "Проект: Северный Вектор.\nОрганизация: «Тихий Берег»\nclient: Atlas Works\ncontract_id: CN-381\nЦена: 1801234.37 RUB",
        )],
    );
    for (kind, value) in [
        ("PROJ", "Северный Вектор"),
        ("ORG", "Тихий Берег"),
        ("CLIENT", "Atlas Works"),
        ("ID", "CN-381"),
        ("AMOUNT", "1801234.37 RUB"),
    ] {
        assert!(
            report
                .suggestions
                .iter()
                .any(|s| s.kind == kind && s.value == value),
            "missing {kind}: {value}; {report:?}"
        );
    }
    assert!(!report.suggestions.iter().any(|s| s.value == "Северный"));
    assert!(seen.lock().unwrap().is_empty());
}

#[test]
fn s1_sees_only_selected_value_and_masked_context_and_scores_distinct_examples() {
    let (engine, seen) = engine(Disclosure::L2, false);
    let docs = [
        doc(
            "private",
            "KEY_TOP_SECRET\nЗакрытая цена: 250 USD. Проект: Северный Вектор.",
        ),
        doc("public", "Публичная цена: 250 USD.\nKEY_OTHER_SECRET"),
    ];
    let report = discover::analyze_with_options(&engine, &docs, &Options { use_s1: true });
    assert_eq!(report.s1_status, "scored");
    let amount = report
        .suggestions
        .iter()
        .find(|s| s.kind == "AMOUNT")
        .unwrap();
    assert_eq!((amount.count, amount.documents), (2, 2));
    assert!(amount.examples[0].s1_score.unwrap() > 0.8);
    assert!(amount.examples[1].s1_score.unwrap() < 0.2);
    let views = seen.lock().unwrap();
    let wire = format!("{views:?}");
    assert!(!wire.contains("KEY_TOP_SECRET") && !wire.contains("KEY_OTHER_SECRET"));
    let project = views.iter().find(|v| v.kind == "PROJ").unwrap();
    assert!(project.context.as_ref().unwrap().contains("[AMOUNT]"));
    assert!(project.context.as_ref().unwrap().contains("[SECRET]"));
    assert!(!report.suggestions.iter().any(|s| s.kind == "SECRET"));
}

#[test]
fn labeled_password_is_a_local_only_candidate() {
    let (engine, seen) = engine(Disclosure::L2, false);
    let report = discover::analyze_with_options(
        &engine,
        &[doc("password", "Пароль базы данных: Cedar-47!Plum")],
        &Options { use_s1: true },
    );
    let secret = report
        .suggestions
        .iter()
        .find(|s| s.kind == "SECRET" && s.value == "Cedar-47!Plum")
        .expect("labeled password must be discoverable");
    assert_eq!(report.s1_status, "no_candidates");
    assert!(seen.lock().unwrap().is_empty());
    let rules = discover::selected_rules(&report, std::slice::from_ref(&secret.id)).unwrap();
    assert_eq!(rules[0].category, Some(Category::Credential));
}

#[test]
fn l0_never_discloses_values_and_invalid_scores_preserve_suggestions() {
    let (engine, seen) = engine(Disclosure::L0, true);
    let docs = [doc("a", "Бюджет: 250 USD")];
    let local = discover::analyze(&engine, &docs);
    let remote = discover::analyze_with_options(&engine, &docs, &Options { use_s1: true });
    assert_eq!(remote.s1_status, "unavailable");
    assert_eq!(remote.suggestions, local.suggestions);
    assert!(
        seen.lock()
            .unwrap()
            .iter()
            .all(|v| v.surface.is_none() && v.context.is_none())
    );
}

#[test]
fn overlapping_secret_is_never_a_candidate_and_boundary_secret_is_redacted() {
    let (engine, seen) = engine(Disclosure::L2, false);
    let secret = format!("KEY_{}", "z".repeat(400));
    let text = format!("Проект: KEY_HIDDEN\n{secret}\nЦена: 250 USD");
    let report =
        discover::analyze_with_options(&engine, &[doc("x", &text)], &Options { use_s1: true });
    assert_eq!(report.suggestions.len(), 1);
    let wire = format!("{:?}", seen.lock().unwrap());
    assert!(!wire.contains("KEY_") && !wire.contains("zzzz"));
}

#[test]
fn profile_deduplicates_documents_and_labels_only_explicit_decisions() {
    let (engine, _) = engine(Disclosure::L2, false);
    let a = doc("a", "Цена: 250 USD; ещё 250 USD");
    let r =
        discover::analyze_with_options(&engine, &[a.clone(), a.clone()], &Options { use_s1: true });
    assert_eq!(r.suggestions[0].count, 2);
    let mut p = Profile::default();
    p.merge(&r).unwrap();
    p.merge(&r).unwrap();
    assert_eq!(p.suggestions[0].count, 2);
    assert!(p.feedback.is_empty());
    let other = discover::analyze(&engine, &[doc("b", "Цена: 250 USD")]);
    p.merge(&other).unwrap();
    assert_eq!((p.suggestions[0].count, p.suggestions[0].documents), (3, 2));
    let id = p.suggestions[0].id.clone();
    assert!(
        p.decide(&p.report(), &[id.clone(), id.clone()], false)
            .is_err()
    );
    assert!(p.feedback.is_empty());
    p.decide(&p.report(), &[id], false).unwrap();
    assert_eq!(p.feedback.len(), 1);
    assert!(!p.feedback[0].sensitive);
    p.merge(&r).unwrap();
    assert!(p.suggestions.is_empty());
}

#[test]
fn candidate_and_scoring_limits_are_visible() {
    let (engine, seen) = engine(Disclosure::L2, false);
    let text = (0..300)
        .map(|n| format!("Цена: {} USD\n", 1000 + n))
        .collect::<String>();
    let r =
        discover::analyze_with_options(&engine, &[doc("many", &text)], &Options { use_s1: true });
    assert!(r.truncated);
    assert_eq!(r.suggestions.len(), 256);
    assert_eq!(r.s1_status, "partial");
    assert_eq!(seen.lock().unwrap().len(), 96);
}

#[test]
fn model_candidates_are_grounded_merged_and_scored_without_labels() {
    use discover::ModelCandidate;
    let (engine, seen) = engine(Disclosure::L2, false);
    let mut document = doc(
        "model",
        "Наш движок называется Лунная Слива. Фраза доступа: туман-482-ирис. Лунная Слива закрыта.",
    );
    document.model_candidates = vec![
        ModelCandidate {
            value: "Лунная Слива".into(),
            kind: "PROJ".into(),
            quote: "Наш движок называется Лунная Слива.".into(),
        },
        ModelCandidate {
            value: "туман-482-ирис".into(),
            kind: "SECRET".into(),
            quote: "Фраза доступа: туман-482-ирис.".into(),
        },
    ];
    discover::validate_documents(&[document.clone()]).unwrap();
    let report =
        discover::analyze_with_options(&engine, &[document.clone()], &Options { use_s1: true });
    let project = report
        .suggestions
        .iter()
        .find(|s| s.value == "Лунная Слива")
        .unwrap();
    assert_eq!(project.count, 2);
    assert!(project.reason.contains("Xiaomi"));
    assert!(project.examples.iter().all(|e| e.s1_score == Some(0.9)));
    for example in &project.examples {
        assert_eq!(&document.text[example.start..example.end], project.value);
    }
    assert!(
        report
            .suggestions
            .iter()
            .any(|s| s.kind == "SECRET" && s.examples.iter().all(|e| e.s1_score.is_none()))
    );
    assert!(
        seen.lock()
            .unwrap()
            .iter()
            .all(|v| !format!("{v:?}").contains("туман-482-ирис"))
    );
    let mut profile = Profile::default();
    profile.merge(&report).unwrap();
    profile.merge(&report).unwrap();
    assert_eq!(profile.report().suggestions, report.suggestions);
    assert!(profile.feedback.is_empty());
    document.model_candidates[0].quote = "выдуманная цитата".into();
    assert!(discover::validate_documents(&[document.clone()]).is_err());
    let report = discover::analyze(&engine, &[document]);
    assert!(!report.suggestions.iter().any(|s| s.value == "Лунная Слива"));
}

#[test]
fn model_and_local_duplicate_share_occurrences_and_provenance() {
    let (engine, _) = engine(Disclosure::L0, false);
    let mut document = doc("both", "Проект: Северный Вектор");
    let local = discover::analyze(&engine, &[document.clone()]);
    document.model_candidates.push(discover::ModelCandidate {
        value: "Северный Вектор".into(),
        kind: "CUSTOM".into(),
        quote: document.text.clone(),
    });
    let both = discover::analyze(&engine, &[document]);
    assert_eq!(both.suggestions.len(), local.suggestions.len());
    let candidate = both
        .suggestions
        .iter()
        .find(|s| s.value == "Северный Вектор")
        .unwrap();
    assert_eq!(candidate.count, 1);
    assert_eq!(candidate.kind, "PROJ");
    assert!(candidate.reason.contains("Xiaomi"));
    let mut profile = Profile::default();
    profile.merge(&local).unwrap();
    profile.merge(&both).unwrap();
    assert!(
        profile
            .suggestions
            .iter()
            .find(|s| s.value == candidate.value)
            .unwrap()
            .reason
            .contains("Xiaomi")
    );
}
