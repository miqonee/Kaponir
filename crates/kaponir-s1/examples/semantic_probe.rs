//! Audit harness: the marked candidates are supplied by the corpus, not discovered here.
//! Uses the production HTTP adapter, then replays scores through the production engine.
use kaponir_core::s1::{
    Calibration, CandidateView, Disclosure, S1Config, S1Error, S1Provider, features,
};
use kaponir_core::{
    AnalyzeCtx, Candidate, Category, Engine, Escape, Input, MaskOptions, MemoryVault, Principal,
    Recognizer,
};
use kaponir_s1::http::{HttpProvider, HttpSettings};
use serde::Deserialize;
use serde_json::json;
use std::io::{self, Read};
use std::sync::{Arc, Mutex};

#[derive(Deserialize)]
struct Case {
    id: String,
    surface: String,
    context: String,
    category: Category,
}
#[derive(Deserialize)]
struct Request {
    cases: Vec<Case>,
    http: HttpSettings,
    /// Optional recorded scores for artifact verification; no HTTP call in this mode.
    probabilities: Option<Vec<f32>>,
}
struct Detector(Vec<Candidate>);
impl Recognizer for Detector {
    fn id(&self) -> &str {
        "audit-marked-candidate"
    }
    fn analyze(&self, _: &Input<'_>, _: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        out.extend(self.0.clone());
    }
}
struct Recorded {
    p: f32,
    seen: Arc<Mutex<Vec<CandidateView>>>,
}
impl S1Provider for Recorded {
    fn id(&self) -> &str {
        "systemone-recorded"
    }
    fn max_disclosure(&self) -> Disclosure {
        Disclosure::L2
    }
    fn score(&self, views: &[CandidateView]) -> Result<Vec<f32>, S1Error> {
        assert_eq!(views.len(), 1, "hard secret must never enter the S1 batch");
        self.seen.lock().unwrap().extend_from_slice(views);
        Ok(vec![self.p])
    }
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = String::new();
    io::stdin().read_to_string(&mut raw)?;
    let request: Request = serde_json::from_str(&raw)?;
    let views: Vec<_> = request
        .cases
        .iter()
        .map(|c| CandidateView {
            kind: "VALUE".into(),
            category: c.category,
            recognizer: "audit-marked-candidate".into(),
            features: features(&c.surface, false),
            surface: Some(c.surface.clone()),
            context: Some(c.context.clone()),
        })
        .collect();
    let replay = request.probabilities.is_some();
    let scores = match request.probabilities {
        Some(p) => Ok(p),
        None => HttpProvider::new(&request.http)?.score(&views),
    };
    let probabilities = match scores {
        Ok(p) => p,
        Err(e) => {
            println!("{}", json!({"error":e.to_string(),"provider_ok":false}));
            return Ok(());
        }
    };
    if probabilities.len() != request.cases.len()
        || probabilities
            .iter()
            .any(|p| !p.is_finite() || !(0.0..=1.0).contains(p))
    {
        return Err("invalid recorded scores".into());
    }
    let mut rows = Vec::new();
    for (case, p) in request.cases.iter().zip(probabilities) {
        let secret = "SynthetiC_Only_Q7!\\\"";
        let original = format!("{}\npassword: {}", case.context, secret);
        let start = original
            .find(&case.surface)
            .ok_or("surface not in context")?;
        let end = start + case.surface.len();
        let hard_start = original.rfind(secret).unwrap();
        let mut variants = Vec::new();
        for mode in ["off", "shadow", "active"] {
            let seen = Arc::new(Mutex::new(Vec::new()));
            let mut builder = Engine::builder([23; 32]).recognizer(Box::new(Detector(vec![
                Candidate::new(
                    start,
                    end,
                    "VALUE",
                    case.category,
                    "audit-marked-candidate",
                    0.5,
                ),
                Candidate::new(
                    hard_start,
                    hard_start + secret.len(),
                    "SECRET",
                    Category::Credential,
                    "audit-hard-secret",
                    0.99,
                )
                .hard(),
            ])));
            if mode != "off" {
                builder = builder.s1(S1Config {
                    provider: Box::new(Recorded {
                        p,
                        seen: seen.clone(),
                    }),
                    calibration: Calibration::default(),
                    weight: 1.0,
                    keep_below: 0.1,
                    shadow: mode == "shadow",
                    context_bytes: 60,
                    profile: Default::default(),
                });
            }
            let engine = builder.build()?;
            let mut vault = MemoryVault::new();
            let masked = engine.mask(
                &Input::text(&original),
                &MaskOptions {
                    explain: true,
                    owner: "local".into(),
                    ..MaskOptions::default()
                },
                &mut vault,
            )?;
            let principal = Principal::local("local");
            let restored = engine.restore(&masked.text, &principal, &vault, Escape::None)?;
            let pairs: Vec<_> = masked
                .replacements
                .iter()
                .map(|r| json!({"token":r.token,"original":r.original}))
                .collect();
            let vector = masked
                .replacements
                .iter()
                .map(|r| r.token.as_str())
                .collect::<Vec<_>>()
                .join("\n");
            let expected = masked
                .replacements
                .iter()
                .map(|r| r.original.as_str())
                .collect::<Vec<_>>()
                .join("\n");
            let tokens = engine.restore(&vector, &principal, &vault, Escape::None)?;
            let scored_seen = seen.lock().unwrap();
            let hard_hidden_from_s1 = scored_seen.iter().all(|c| {
                c.surface.as_deref() != Some(secret)
                    && c.context.as_ref().is_none_or(|s| !s.contains(secret))
            });
            let target_masked = masked
                .replacements
                .iter()
                .any(|r| r.start == start && r.end == end);
            let hard_secret_masked = !masked.text.contains(secret);
            variants.push(json!({"mode":mode,"masked":masked,"restore_exact":restored.text==original,
                "tokens_exact":tokens.text==expected,"unknown":restored.unknown,"denied":restored.denied,
                "restored_text":restored.text,"tokens_input":vector,"tokens_expected":expected,"tokens_actual":tokens.text,
                "target_masked":target_masked,
                "hard_secret_masked":hard_secret_masked,
                "hard_hidden_from_s1":hard_hidden_from_s1,"pairs":pairs}));
        }
        rows.push(json!({"id":case.id,"probability":p,"original":original,"variants":variants}));
    }
    println!(
        "{}",
        json!({"provider_ok":true,"score_source":if replay {"recorded"} else {"http"},"rows":rows})
    );
    Ok(())
}
