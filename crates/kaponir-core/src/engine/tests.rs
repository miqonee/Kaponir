use super::*;
use crate::vault::MemoryVault;

/// Находит `needle` только после `/` — как сегмент пути, но не в прозе.
struct PathWord(&'static str);

impl Recognizer for PathWord {
    fn id(&self) -> &str {
        "path-word"
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        for (i, _) in input.text.match_indices(&format!("/{}", self.0)) {
            out.push(Candidate::new(
                i + 1,
                i + 1 + self.0.len(),
                "PROJ",
                Category::Infra,
                "path-word",
                0.99,
            ));
        }
    }
}

fn engine(word: &'static str) -> Engine {
    Engine::builder([1; 32])
        .recognizer(Box::new(PathWord(word)))
        .build()
        .unwrap()
}

fn opts(known: Vec<Known>) -> MaskOptions {
    MaskOptions {
        owner: "me".into(),
        known,
        ..MaskOptions::default()
    }
}

#[test]
fn public_ranges_clip_only_allowed_bytes_and_keep_numeric_suffix_unambiguous() {
    let engine = engine("abc51.100xyz");
    let source = "/abc51.100xyz /abc51.100xyz";
    let mut vault = MemoryVault::new();
    let mut edits = PositionEdits::default();
    edits.public_ranges.push(4..10);
    let masked = engine
        .mask_without_s1_positioned(&Input::text(source), &opts(vec![]), &mut vault, &edits)
        .unwrap();
    let ranges: Vec<_> = masked.replacements.iter().map(|r| r.start..r.end).collect();
    assert_eq!(ranges, [1..4, 10..13, 15..27]);
    assert!(masked.text.contains("}51.100"));
    assert_eq!(
        engine
            .restore(&masked.text, &Principal::local("me"), &vault, Escape::None)
            .unwrap()
            .text,
        source
    );
}

#[test]
fn a_public_range_does_not_release_session_protection() {
    let engine = engine("inside");
    let mut vault = MemoryVault::new();
    let source = "/inside";
    let first = engine
        .mask_without_s1(&Input::text(source), &opts(vec![]), &mut vault)
        .unwrap();
    let mut edits = PositionEdits::default();
    edits.public_ranges.push(1..7);
    let masked = engine
        .mask_without_s1_positioned(&Input::text(source), &opts(first.known), &mut vault, &edits)
        .unwrap();
    assert_eq!(first.text, masked.text);
    assert_eq!(masked.replacements.len(), 1);
    assert!(masked.replacements[0].protected);
}

#[test]
fn explicit_short_values_roundtrip_without_weakening_automatic_session_memory() {
    let engine = engine("zorg");
    let mut vault = MemoryVault::new();
    let known = Known {
        text: "q7".into(),
        kind: "CUSTOM".into(),
        canonical: "q7".into(),
        scope: Scope::Personal,
        strategy: Strategy::Variable,
        form: TokenForm::Snake,
    };
    let text = "q7 q73 -q7 q7_1 q7";
    let automatic = engine
        .mask(&Input::text(text), &opts(vec![known.clone()]), &mut vault)
        .unwrap();
    assert_eq!(automatic.text, text);
    let selected = engine
        .mask_selected(&Input::text(text), &opts(vec![known]), &mut vault)
        .unwrap();
    assert_eq!(selected.replacements.len(), 3);
    assert!(selected.text.contains("q73") && selected.text.contains("q7_1"));
    let restored = engine
        .restore(
            &selected.text,
            &Principal::local("me"),
            &vault,
            Escape::None,
        )
        .unwrap();
    assert_eq!(restored.text, text);
    assert_eq!(
        engine
            .mask(&Input::text("q7"), &opts(restored.known), &mut vault)
            .unwrap()
            .text,
        "q7"
    );
}

#[test]
fn session_memory_masks_restored_values_in_prose() {
    let e = engine("zorg");
    let mut v = MemoryVault::new();
    let me = Principal::local("me");
    let m = e
        .mask(&Input::text("cd /srv/zorg"), &opts(vec![]), &mut v)
        .unwrap();
    assert_eq!(m.text, "cd /srv/ANON_PROJ_1");
    assert_eq!(m.known.len(), 1);

    let r = e
        .restore("ANON_PROJ_1 is down", &me, &v, Escape::None)
        .unwrap();
    assert_eq!(r.text, "zorg is down");
    assert_eq!(r.known, m.known);

    // Проза детекторами не ловится — без памяти сессии оригинал утёк бы.
    let plain = e
        .mask(
            &Input::text("zorg is down, zorgs ok"),
            &opts(vec![]),
            &mut v,
        )
        .unwrap();
    assert_eq!(plain.text, "zorg is down, zorgs ok");
    let again = e
        .mask(
            &Input::text("zorg is down, zorgs ok"),
            &opts(r.known),
            &mut v,
        )
        .unwrap();
    assert_eq!(again.text, "ANON_PROJ_1 is down, zorgs ok");
}

#[test]
fn stream_holds_only_token_prefix_and_escapes_json() {
    let e = engine(r#"a"b\c"#);
    let mut v = MemoryVault::new();
    let me = Principal::local("me");
    let m = e
        .mask(&Input::text(r#"x /a"b\c"#), &opts(vec![]), &mut v)
        .unwrap();
    assert_eq!(m.text, "x /ANON_PROJ_1");

    let mut s = StreamRestorer::new(Escape::Json);
    let mut out = String::new();
    for chunk in [
        "{\"path\": \"/srv/AN",
        "ON_PR",
        "OJ_",
        "1\", \"n\": 1",
        "0}",
    ] {
        out.push_str(&s.push(&e, &me, &v, chunk).unwrap());
        assert!(s.pending() <= e.max_hold());
    }
    out.push_str(&s.flush(&e, &me, &v).unwrap());
    assert_eq!(out, r#"{"path": "/srv/a\"b\\c", "n": 10}"#);
    assert_eq!(s.stats().restored, 1);
    assert_eq!(
        serde_json_like_unescape(&out),
        r#"{"path": "/srv/a"b\c", "n": 10}"#
    );
}

/// Находит email целиком (prior 0.9) — слабее, чем память сессии (hard).
struct Email;

impl Recognizer for Email {
    fn id(&self) -> &str {
        "email"
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        for (i, _) in input.text.match_indices("m.ivanov@zorg.ru") {
            out.push(Candidate::new(
                i,
                i + 16,
                "EMAIL",
                Category::Contact,
                "email",
                0.9,
            ));
        }
    }
}

#[test]
fn session_value_does_not_fragment_longer_mask() {
    let e = Engine::builder([1; 32])
        .recognizer(Box::new(PathWord("zorg")))
        .recognizer(Box::new(Email))
        .build()
        .unwrap();
    let mut v = MemoryVault::new();
    let known = e
        .mask(&Input::text("/zorg"), &opts(vec![]), &mut v)
        .unwrap()
        .known;
    let m = e
        .mask(
            &Input::text("mail m.ivanov@zorg.ru, zorg"),
            &opts(known.clone()),
            &mut v,
        )
        .unwrap();
    assert_eq!(m.text, "mail ANON_EMAIL_1, ANON_PROJ_1");

    // Email проиграл более сильному кандидату, который пересекает его, но не покрывает
    // `zorg`, — известное значение всё равно маскируется.
    let e = Engine::builder([1; 32])
        .recognizer(Box::new(Email))
        .recognizer(Box::new(Strong("id m.ivanov")))
        .build()
        .unwrap();
    let m = e
        .mask(&Input::text("id m.ivanov@zorg.ru"), &opts(known), &mut v)
        .unwrap();
    // Внутри домена — DNS-форма токена.
    assert_eq!(m.text, "ANON_KEY_1@anon-proj-1.ru");
}

/// Жёсткий кандидат на точное вхождение.
struct Strong(&'static str);

impl Recognizer for Strong {
    fn id(&self) -> &str {
        "strong"
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        for (i, _) in input.text.match_indices(self.0) {
            out.push(
                Candidate::new(
                    i,
                    i + self.0.len(),
                    "KEY",
                    Category::Credential,
                    "strong",
                    0.99,
                )
                .hard(),
            );
        }
    }
}

fn serde_json_like_unescape(s: &str) -> String {
    s.replace("\\\\", "\u{0}")
        .replace("\\\"", "\"")
        .replace('\u{0}', "\\")
}

#[test]
fn interval_indexes_preserve_legacy_priority_and_keep_semantics() {
    let engine = Engine::builder([1; 32]).build().unwrap();
    let mut seed = 713_u64;
    let mut next = || {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
        (seed >> 32) as usize
    };
    for _ in 0..12 {
        let scored: Vec<Scored> = (0..256)
            .map(|i| {
                let start = next() % 1024;
                let mut c = Candidate::new(
                    start,
                    start + 1 + next() % 90,
                    "PROJ",
                    Category::Infra,
                    if i % 4 == 0 { SESSION } else { "fixture" },
                    0.5,
                );
                if i % 7 == 0 {
                    c.polarity = Polarity::Keep;
                }
                c.hard = i % 3 == 0;
                c.origin = match i % 5 {
                    0 => Origin::Builtin,
                    rank => Origin::Rule {
                        scope: match rank {
                            1 => Scope::Personal,
                            2 => Scope::Team,
                            _ => Scope::Global,
                        },
                        enforced: rank == 4,
                        id: i.to_string(),
                    },
                };
                Scored {
                    verdict: engine.verdict(&c),
                    c,
                }
            })
            .collect();
        let index = KeepIndex::new(&scored);
        for s in &scored {
            let c = &s.c;
            let legacy = c.polarity == Polarity::Mask
                && scored.iter().any(|k| {
                    let k = &k.c;
                    k.polarity == Polarity::Keep
                        && k.start <= c.start
                        && c.end <= k.end
                        && (k.origin.is_enforced()
                            || (!c.hard && k.origin.rank() >= c.origin.rank()))
                });
            assert_eq!(index.covers(c), legacy);
        }
        // Оракул — исходный полный перебор пересечений до оптимизации.
        let mut masks: Vec<_> = (0..scored.len())
            .filter(|&i| scored[i].applied_mask(engine.policy.uncertain))
            .collect();
        masks.sort_by(|&a, &b| {
            let (x, y) = (&scored[a].c, &scored[b].c);
            y.origin
                .rank()
                .cmp(&x.origin.rank())
                .then(y.hard.cmp(&x.hard))
                .then(y.probability().total_cmp(&x.probability()))
                .then(y.len().cmp(&x.len()))
                .then(x.start.cmp(&y.start))
        });
        let (deferred, first): (Vec<_>, Vec<_>) = masks.iter().copied().partition(|&i| {
            let c = &scored[i].c;
            c.recognizer == SESSION
                && masks.iter().any(|&j| {
                    let o = &scored[j].c;
                    o.recognizer != SESSION
                        && o.start <= c.start
                        && c.end <= o.end
                        && o.len() > c.len()
                })
        });
        let mut legacy: Vec<usize> = Vec::new();
        for i in first.into_iter().chain(deferred) {
            let c = &scored[i].c;
            if !legacy
                .iter()
                .any(|&j| c.start < scored[j].c.end && scored[j].c.start < c.end)
            {
                legacy.push(i);
            }
        }
        legacy.sort_by_key(|&i| scored[i].c.start);
        assert_eq!(
            Engine::resolve_conflicts(&scored, engine.policy.uncertain),
            legacy
        );
    }
}
