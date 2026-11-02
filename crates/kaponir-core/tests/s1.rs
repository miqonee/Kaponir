use kaponir_core::s1::{
    Calibration, CandidateView, Disclosure, ProfileValue, S1Config, S1Error, S1Provider,
};
use kaponir_core::{
    AnalyzeCtx, Candidate, Category, Engine, Input, MaskOptions, MemoryVault, Origin, Recognizer,
    Scope,
};
use std::sync::{Arc, Mutex};

struct Detector(Vec<Candidate>);
impl Recognizer for Detector {
    fn id(&self) -> &str {
        "fixture"
    }
    fn analyze(&self, _: &Input<'_>, _: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        out.extend(self.0.clone());
    }
}

struct Provider {
    seen: Arc<Mutex<Vec<CandidateView>>>,
    probabilities: Vec<f32>,
    fail: bool,
}
impl S1Provider for Provider {
    fn id(&self) -> &str {
        "fixture"
    }
    fn max_disclosure(&self) -> Disclosure {
        Disclosure::L2
    }
    fn score(&self, batch: &[CandidateView]) -> Result<Vec<f32>, S1Error> {
        self.seen.lock().unwrap().extend_from_slice(batch);
        if self.fail {
            Err(S1Error::Timeout)
        } else {
            Ok(self.probabilities.clone())
        }
    }
}

fn candidate(text: &str, word: &str, prior: f32) -> Candidate {
    let start = text.find(word).unwrap();
    Candidate::new(
        start,
        start + word.len(),
        "PROJ",
        Category::Infra,
        "fixture",
        prior,
    )
}

fn mask(
    text: &str,
    candidates: Vec<Candidate>,
    probabilities: Vec<f32>,
    shadow: bool,
    fail: bool,
    calibration: Calibration,
) -> (kaponir_core::Masked, Vec<CandidateView>) {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let engine = Engine::builder([1; 32])
        .recognizer(Box::new(Detector(candidates)))
        .s1(S1Config {
            provider: Box::new(Provider {
                seen: seen.clone(),
                probabilities,
                fail,
            }),
            calibration,
            weight: 1.0,
            keep_below: 0.1,
            shadow,
            context_bytes: 60,
            profile: Arc::new(Vec::new()),
        })
        .build()
        .unwrap();
    let result = engine
        .mask(
            &Input::text(text),
            &MaskOptions {
                explain: true,
                ..MaskOptions::default()
            },
            &mut MemoryVault::new(),
        )
        .unwrap();
    let views = seen.lock().unwrap().clone();
    (result, views)
}

struct ProfileProvider {
    disclosure: Disclosure,
    profiles: Arc<Mutex<Vec<Vec<ProfileValue>>>>,
    seen: Arc<Mutex<Vec<CandidateView>>>,
}

impl S1Provider for ProfileProvider {
    fn id(&self) -> &str {
        "profile-fixture"
    }

    fn max_disclosure(&self) -> Disclosure {
        self.disclosure
    }

    fn score(&self, batch: &[CandidateView]) -> Result<Vec<f32>, S1Error> {
        self.seen.lock().unwrap().extend_from_slice(batch);
        Ok(vec![0.9; batch.len()])
    }

    fn score_with_profile(
        &self,
        batch: &[CandidateView],
        profile: &[ProfileValue],
    ) -> Result<Vec<f32>, S1Error> {
        self.profiles.lock().unwrap().push(profile.to_vec());
        self.score(batch)
    }
}

#[test]
fn context_size_is_configurable_and_profile_requires_l2() {
    for (disclosure, context_bytes, expected_context, expect_profile) in [
        (Disclosure::L0, 60, None, false),
        (Disclosure::L1, 60, None, false),
        (Disclosure::L2, 3, Some("789abc012"), true),
        (Disclosure::L2, 0, Some("abc"), true),
    ] {
        let text = "0123456789abc0123456789";
        let profiles = Arc::new(Mutex::new(Vec::new()));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let profile = vec![ProfileValue {
            kind: "ORG".into(),
            value: "Acme".into(),
        }];
        let engine = Engine::builder([1; 32])
            .recognizer(Box::new(Detector(vec![candidate(text, "abc", 0.5)])))
            .s1(S1Config {
                provider: Box::new(ProfileProvider {
                    disclosure,
                    profiles: profiles.clone(),
                    seen: seen.clone(),
                }),
                calibration: Calibration::default(),
                weight: 1.0,
                keep_below: 0.1,
                shadow: true,
                context_bytes,
                profile: Arc::new(profile.clone()),
            })
            .build()
            .unwrap();
        engine
            .mask(
                &Input::text(text),
                &MaskOptions::default(),
                &mut MemoryVault::new(),
            )
            .unwrap();
        assert_eq!(seen.lock().unwrap()[0].context.as_deref(), expected_context);
        assert_eq!(profiles.lock().unwrap().len(), usize::from(expect_profile));
        if expect_profile {
            assert_eq!(profiles.lock().unwrap()[0], profile);
        }
    }
}

#[test]
fn only_uncertain_builtin_candidates_can_be_changed() {
    let text = "maybe sure safe hard ruled allowed";
    let mut hard = candidate(text, "hard", 0.5).hard();
    hard.category = Category::Credential;
    let mut ruled = candidate(text, "ruled", 0.5);
    ruled.origin = Origin::Rule {
        scope: Scope::Team,
        enforced: false,
        id: "r".into(),
    };
    let allowed = candidate(text, "allowed", 0.5);
    let keep = Candidate::keep(
        allowed.start,
        allowed.end,
        "rules",
        Origin::Rule {
            scope: Scope::Personal,
            enforced: false,
            id: "k".into(),
        },
    );
    let candidates = vec![
        candidate(text, "maybe", 0.5),
        candidate(text, "sure", 0.95),
        candidate(text, "safe", 0.01),
        hard,
        ruled,
        allowed,
        keep,
    ];
    let (out, seen) = mask(
        text,
        candidates,
        vec![0.01],
        false,
        false,
        Calibration::default(),
    );
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].surface.as_deref(), Some("maybe"));
    assert_eq!(
        out.text,
        "maybe ANON_PROJ_1 safe ANON_PROJ_2 ANON_PROJ_3 allowed"
    );
    assert!(!seen[0].context.as_ref().unwrap().contains("hard"));
}

#[test]
fn weak_keep_predictions_and_provider_failures_preserve_policy() {
    for (probs, fail) in [
        (vec![0.2], false),
        (vec![], false),
        (vec![f32::NAN], false),
        (vec![1.1], false),
        (vec![-0.1], false),
        (vec![0.0], true),
    ] {
        let (out, _) = mask(
            "maybe",
            vec![candidate("maybe", "maybe", 0.5)],
            probs,
            false,
            fail,
            Calibration::default(),
        );
        assert_eq!(out.text, "ANON_PROJ_1");
        assert_eq!(out.uncertain, 1);
    }
}

#[test]
fn shadow_keeps_decisions_and_calibration_controls_active_predictions() {
    let (out, _) = mask(
        "maybe",
        vec![candidate("maybe", "maybe", 0.5)],
        vec![0.01],
        true,
        false,
        Calibration::default(),
    );
    assert_eq!(out.text, "ANON_PROJ_1");
    assert!(
        out.explain[0]
            .evidence
            .iter()
            .any(|e| e.source.starts_with("s1-shadow:fixture:") && e.logit == 0.0)
    );
    let (out, _) = mask(
        "maybe",
        vec![candidate("maybe", "maybe", 0.5)],
        vec![0.5],
        false,
        false,
        Calibration { a: 1.0, b: -5.0 },
    );
    assert_eq!(out.text, "maybe");
    assert_eq!(out.uncertain, 0);
    let (out, _) = mask(
        "maybe",
        vec![candidate("maybe", "maybe", 0.5)],
        vec![0.99],
        false,
        false,
        Calibration::default(),
    );
    assert_eq!(out.text, "ANON_PROJ_1");
    assert_eq!(out.uncertain, 0);
}

#[test]
fn credentials_and_overlapping_fragments_only_disclose_features() {
    let text = "privatecredential other";
    let mut credential = candidate(text, "privatecredential", 0.1);
    credential.category = Category::Credential;
    let fragment = candidate(text, "credential", 0.5);
    let (_, seen) = mask(
        text,
        vec![credential, fragment, candidate(text, "other", 0.5)],
        vec![0.9; 3],
        true,
        false,
        Calibration::default(),
    );
    assert_eq!(seen.len(), 3);
    for view in &seen[..2] {
        assert!(view.surface.is_none() && view.context.is_none());
    }
    assert!(
        !seen[2]
            .context
            .as_ref()
            .unwrap()
            .contains("privatecredential")
    );
}

#[test]
fn l2_hides_other_uncertain_values_and_secrets_crossing_window_boundaries() {
    let secret = "z".repeat(100);
    let text = format!("{secret} maybe other");
    let mut credential = candidate(&text, &secret, 0.99).hard();
    credential.category = Category::Credential;
    let (_, seen) = mask(
        &text,
        vec![
            credential,
            candidate(&text, "maybe", 0.5),
            candidate(&text, "other", 0.5),
        ],
        vec![0.9; 2],
        true,
        false,
        Calibration::default(),
    );
    assert_eq!(seen.len(), 2);
    let context = seen[0].context.as_ref().unwrap();
    assert!(!context.contains('z'));
    assert!(!context.contains("other"));
    assert!(context.contains("[PROJ]"));
}

#[test]
fn s1_metrics_distinguish_failures_and_do_not_store_candidate_data() {
    struct Recorder(std::sync::atomic::AtomicU64);
    impl kaponir_core::metrics::S1FailureSink for Recorder {
        fn record(&self, _: &kaponir_core::metrics::S1Failure) {
            self.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }
    for (fail, probabilities, expected_errors, code) in [
        (true, vec![], 70, Some("timeout")),
        (false, vec![f32::NAN], 70, Some("invalid_response")),
        (false, vec![0.9], 0, None),
    ] {
        let text = "confidential-project";
        let engine = Engine::builder([1; 32])
            .recognizer(Box::new(Detector(vec![candidate(text, text, 0.5)])))
            .s1(S1Config {
                provider: Box::new(Provider {
                    seen: Default::default(),
                    probabilities,
                    fail,
                }),
                calibration: Calibration::default(),
                weight: 1.0,
                keep_below: 0.1,
                shadow: false,
                context_bytes: 60,
                profile: Arc::new(Vec::new()),
            })
            .build()
            .unwrap();
        let recorder = Arc::new(Recorder(std::sync::atomic::AtomicU64::new(0)));
        engine.metrics().set_s1_failure_sink(recorder.clone());
        let mut vault = MemoryVault::new();
        for _ in 0..70 {
            let masked = engine
                .mask(&Input::text(text), &MaskOptions::default(), &mut vault)
                .unwrap();
            assert!(!masked.text.contains(text), "сбой не должен снимать маску");
        }
        let s = engine.metrics().snapshot();
        assert_eq!(s.s1.calls, 70);
        assert_eq!(s.s1.errors, expected_errors);
        assert_eq!(
            recorder.0.load(std::sync::atomic::Ordering::Relaxed),
            expected_errors
        );
        assert_eq!(s.s1_candidates, 70);
        assert_eq!(s.mask.calls, 70);
        assert_eq!(s.mask.errors, 0);
        assert_eq!(s.replacements, 70);
        assert_eq!(
            s.recent_s1_failures.len(),
            if code.is_some() { 64 } else { 0 }
        );
        let json = toml::to_string(&s).unwrap();
        assert!(!json.contains(text));
        if let Some(code) = code {
            assert!(json.contains(code));
        }
        assert!(s.s1.buckets.windows(2).all(|b| b[0] <= b[1]));
        assert!(s.s1.buckets[7] <= s.s1.calls);
    }
}

#[test]
fn confirmed_profile_excludes_credentials_unknown_kinds_and_aliases() {
    use kaponir_core::rules::{Rule, RuleFile, ScopedRules};
    use kaponir_core::s1::confirmed_profile;
    let value = |kind: &str, value: &str, category| Rule {
        kind: kind.into(),
        value: Some(value.into()),
        category,
        ..Rule::default()
    };
    let rules = [ScopedRules {
        scope: Scope::Personal,
        file: RuleFile {
            rules: vec![
                value("ORG", "Acme", None),
                value("SECRET", "SyntheticSecret42", None),
                value("PROJ", "SyntheticSecret42", None),
                value("ORG", "prefix-syntheticsecret42-suffix", None),
                value("PROJ", "TypedCredential73", Some(Category::Credential)),
                value("ORG", "TypedCredential73", None),
                value("CUSTOM", "UnknownSecret64", None),
                value("AMOUNT", "250 USD", None),
            ],
            ..RuleFile::default()
        },
    }];
    let profile = confirmed_profile(&rules);
    assert_eq!(profile.len(), 2);
    assert!(profile.iter().any(|p| p.value == "Acme"));
    assert!(profile.iter().any(|p| p.value == "250 USD"));
}

#[test]
fn engine_filters_manually_supplied_profile_before_provider() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let profiles = Arc::new(Mutex::new(Vec::new()));
    let engine = Engine::builder([1; 32])
        .recognizer(Box::new(Detector(vec![candidate("abc", "abc", 0.5)])))
        .s1(S1Config {
            provider: Box::new(ProfileProvider {
                disclosure: Disclosure::L2,
                profiles: profiles.clone(),
                seen,
            }),
            calibration: Calibration::default(),
            weight: 1.0,
            keep_below: 0.1,
            shadow: true,
            context_bytes: 20,
            profile: Arc::new(vec![
                ProfileValue {
                    kind: "SECRET".into(),
                    value: "SyntheticSecret42".into(),
                },
                ProfileValue {
                    kind: "ORG".into(),
                    value: "Acme".into(),
                },
            ]),
        })
        .build()
        .unwrap();
    engine
        .mask(
            &Input::text("abc"),
            &MaskOptions::default(),
            &mut MemoryVault::new(),
        )
        .unwrap();
    assert_eq!(
        profiles.lock().unwrap()[0],
        vec![ProfileValue {
            kind: "ORG".into(),
            value: "Acme".into()
        }]
    );
}
