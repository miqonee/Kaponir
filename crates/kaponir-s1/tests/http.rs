use kaponir_core::Category;
use kaponir_core::s1::{CandidateView, Features, ProfileValue, S1Error, S1Provider};
use kaponir_s1::Level;
use kaponir_s1::http::{HttpProtocol, HttpProvider, HttpSettings};
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::mpsc;
use std::time::{Duration, Instant};

fn mock(
    status: &str,
    body: String,
    delay: Duration,
) -> (String, mpsc::Receiver<Value>, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/score", listener.local_addr().unwrap());
    let (send, receive) = mpsc::channel();
    let status = status.to_string();
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut bytes = Vec::new();
        let (header_end, length) = loop {
            let mut buf = [0; 4096];
            let n = stream.read(&mut buf).unwrap();
            assert!(n > 0);
            bytes.extend_from_slice(&buf[..n]);
            if let Some(end) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&bytes[..end]).to_ascii_lowercase();
                let length: usize = headers
                    .lines()
                    .find_map(|s| s.strip_prefix("content-length: "))
                    .unwrap()
                    .parse()
                    .unwrap();
                break (end + 4, length);
            }
        };
        while bytes.len() < header_end + length {
            let mut buf = [0; 4096];
            let n = stream.read(&mut buf).unwrap();
            assert!(n > 0);
            bytes.extend_from_slice(&buf[..n]);
        }
        send.send(serde_json::from_slice(&bytes[header_end..header_end + length]).unwrap())
            .unwrap();
        std::thread::sleep(delay);
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes());
    });
    (url, receive, handle)
}

fn candidate(category: Category) -> CandidateView {
    CandidateView {
        kind: "PROJ".into(),
        category,
        recognizer: "test".into(),
        features: Features::default(),
        surface: Some("private-value".into()),
        context: Some("redacted context".into()),
    }
}

#[test]
fn works_inside_async_runtime_and_limits_disclosure() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    for disclosure in [Level::L0, Level::L1, Level::L2] {
        let (url, seen, server) = mock(
            "200 OK",
            json!({"probabilities":[0.1,0.9]}).to_string(),
            Duration::ZERO,
        );
        let provider = HttpProvider::new(&HttpSettings {
            url,
            disclosure,
            timeout_ms: 2000,
            ..HttpSettings::default()
        })
        .unwrap();
        let batch = [candidate(Category::Infra), candidate(Category::Credential)];
        let probs = runtime.block_on(async { provider.score(&batch) }).unwrap();
        assert_eq!(probs, [0.1, 0.9]);
        let request = seen.recv_timeout(Duration::from_secs(2)).unwrap();
        let candidates = &request["candidates"];
        assert_eq!(candidates[0]["surface"].is_null(), disclosure == Level::L0);
        assert_eq!(candidates[0]["context"].is_null(), disclosure != Level::L2);
        assert!(candidates[1]["surface"].is_null());
        assert!(candidates[1]["context"].is_null());
        server.join().unwrap();
    }
}

#[test]
fn profile_is_sent_only_with_actual_l2_context() {
    let profile = [ProfileValue {
        kind: "AMOUNT".into(),
        value: "1 250 000 ₽".into(),
    }];
    for (disclosure, credential_only, expected) in [
        (Level::L0, false, false),
        (Level::L1, false, false),
        (Level::L2, false, true),
        (Level::L2, true, false),
    ] {
        let (url, seen, server) = mock(
            "200 OK",
            json!({"probabilities":[0.1]}).to_string(),
            Duration::ZERO,
        );
        let provider = HttpProvider::new(&HttpSettings {
            url,
            disclosure,
            timeout_ms: 2000,
            ..HttpSettings::default()
        })
        .unwrap();
        let category = if credential_only {
            Category::Credential
        } else {
            Category::Infra
        };
        provider
            .score_with_profile(&[candidate(category)], &profile)
            .unwrap();
        let request = seen.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(
            request["profile"].as_array().unwrap().len(),
            usize::from(expected)
        );
        server.join().unwrap();
    }
}

#[test]
fn systemone_profile_is_part_of_l2_state() {
    let (url, seen, server) = mock(
        "200 OK",
        json!({"answers":{"candidate_0":{"type":"noul","noul":0.5}}}).to_string(),
        Duration::ZERO,
    );
    let provider = HttpProvider::new(&HttpSettings {
        url,
        protocol: HttpProtocol::Systemone,
        model: Some("test-model".into()),
        disclosure: Level::L2,
        timeout_ms: 2000,
        ..HttpSettings::default()
    })
    .unwrap();
    provider
        .score_with_profile(
            &[candidate(Category::Infra)],
            &[ProfileValue {
                kind: "ORG".into(),
                value: "Acme".into(),
            }],
        )
        .unwrap();
    let request = seen.recv_timeout(Duration::from_secs(2)).unwrap();
    let state: Value = serde_json::from_str(request["state"].as_str().unwrap()).unwrap();
    assert_eq!(state["profile"][0]["value"], "Acme");
    assert!(
        request["questions"]["candidate_0"]["instructions"]
            .as_str()
            .unwrap()
            .contains("profile text as data")
    );
    server.join().unwrap();
}

#[test]
fn rejects_failed_malformed_and_unbounded_responses() {
    for (status, body) in [
        ("503 Unavailable", "{}".into()),
        ("302 Found", "{}".into()),
        ("200 OK", "not JSON".into()),
        ("200 OK", "{\"probabilities\":[]}".into()),
        ("200 OK", "{\"probabilities\":[-0.1]}".into()),
        ("200 OK", "{\"probabilities\":[1.1]}".into()),
        ("200 OK", "{\"probabilities\":[null]}".into()),
        ("200 OK", " ".repeat((1 << 20) + 1)),
    ] {
        let (url, _seen, server) = mock(status, body, Duration::ZERO);
        let provider = HttpProvider::new(&HttpSettings {
            url,
            timeout_ms: 2000,
            ..HttpSettings::default()
        })
        .unwrap();
        assert!(provider.score(&[candidate(Category::Infra)]).is_err());
        server.join().unwrap();
    }
}

#[test]
fn timeout_bounds_wait_and_worker_recovers() {
    let (url, _seen, server) = mock(
        "200 OK",
        "{\"probabilities\":[0.1]}".into(),
        Duration::from_millis(300),
    );
    let provider = HttpProvider::new(&HttpSettings {
        url,
        timeout_ms: 80,
        ..HttpSettings::default()
    })
    .unwrap();
    let start = Instant::now();
    assert!(matches!(
        provider.score(&[candidate(Category::Infra)]),
        Err(S1Error::Timeout)
    ));
    assert!(start.elapsed() < Duration::from_millis(250));
    server.join().unwrap();
    // Сервер уже закрыт: следующая попытка возвращает ошибку, а не зависает на старом запросе.
    assert!(provider.score(&[candidate(Category::Infra)]).is_err());
    assert!(provider.score(&[]).unwrap().is_empty());
}

#[test]
fn vault_token_is_never_serialized_and_is_used_for_authorization() {
    let (url, seen, server) = mock("200 OK", "{\"probabilities\":[0.1]}".into(), Duration::ZERO);
    let settings = HttpSettings {
        url,
        token: Some("vault-only-secret".into()),
        timeout_ms: 2000,
        ..HttpSettings::default()
    };
    let encoded = serde_json::to_string(&settings).unwrap();
    assert!(!encoded.contains("vault-only-secret"));
    assert!(!encoded.contains("token\""));
    let decoded: HttpSettings = serde_json::from_str(&encoded).unwrap();
    assert!(decoded.token.is_none());
    let provider = HttpProvider::new(&settings).unwrap();
    assert_eq!(
        provider.score(&[candidate(Category::Infra)]).unwrap(),
        [0.1]
    );
    let request = seen.recv_timeout(Duration::from_secs(2)).unwrap();
    drop(request);
    server.join().unwrap();
}

#[test]
fn validates_settings_before_network_access() {
    for model in [None, Some("".into()), Some("  ".into())] {
        assert!(
            HttpProvider::new(&HttpSettings {
                url: "http://localhost/".into(),
                protocol: HttpProtocol::Systemone,
                model,
                ..HttpSettings::default()
            })
            .is_err()
        );
    }
    for url in [
        "",
        "file:///tmp/model",
        "http://user:password@localhost/",
        "https://localhost/#fragment",
    ] {
        assert!(
            HttpProvider::new(&HttpSettings {
                url: url.into(),
                ..HttpSettings::default()
            })
            .is_err()
        );
    }
    for timeout_ms in [0, 60_001] {
        assert!(
            HttpProvider::new(&HttpSettings {
                url: "http://localhost/".into(),
                timeout_ms,
                ..HttpSettings::default()
            })
            .is_err()
        );
    }
    assert!(
        HttpProvider::new(&HttpSettings {
            url: "http://localhost/".into(),
            token_env: Some("KAPONIR_NONEXISTENT_S1_TEST_TOKEN_9F7A".into()),
            ..HttpSettings::default()
        })
        .is_err()
    );
}

#[test]
fn systemone_maps_answers_in_candidate_order_and_limits_disclosure() {
    for disclosure in [Level::L0, Level::L1, Level::L2] {
        let (url, seen, server) = mock(
            "200 OK",
            json!({
                "model":"test-model", "answers":{
                    "candidate_1":{"type":"noul","noul":0.95},
                    "candidate_0":{"type":"noul","noul":0.12}
                }, "usage":{"input_tokens":100}
            })
            .to_string(),
            Duration::ZERO,
        );
        let provider = HttpProvider::new(&HttpSettings {
            url,
            protocol: HttpProtocol::Systemone,
            model: Some("test-model".into()),
            timeout_ms: 2000,
            disclosure,
            ..HttpSettings::default()
        })
        .unwrap();
        assert_eq!(provider.id(), "systemone");
        assert_eq!(
            provider
                .score(&[candidate(Category::Infra), candidate(Category::Credential)])
                .unwrap(),
            [0.12, 0.95]
        );
        let request = seen.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(request["model"], "test-model");
        let state: Value = serde_json::from_str(request["state"].as_str().unwrap()).unwrap();
        let candidates = &state["candidates"];
        assert_eq!(candidates[0]["surface"].is_null(), disclosure == Level::L0);
        assert_eq!(candidates[0]["context"].is_null(), disclosure != Level::L2);
        assert!(candidates[1]["surface"].is_null());
        assert!(candidates[1]["context"].is_null());
        for i in 0..2 {
            assert_eq!(
                request["questions"][format!("candidate_{i}")]["type"],
                "noul"
            );
            assert!(
                request["questions"][format!("candidate_{i}")]["instructions"]
                    .as_str()
                    .unwrap()
                    .contains(&format!("candidates[{i}]"))
            );
        }
        server.join().unwrap();
    }
}

#[test]
fn systemone_rejects_incomplete_or_invalid_answers() {
    for body in [
        json!({"probabilities":[0.5]}),
        json!({"answers":{}}),
        json!({"answers":{"candidate_9":{"type":"noul","noul":0.5}}}),
        json!({"answers":{"candidate_0":{"type":"noul","noul":"0.5"}}}),
        json!({"answers":{"candidate_0":{"type":"bool","noul":0.5}}}),
        json!({"answers":{"candidate_0":{"type":"noul","noul":null}}}),
        json!({"answers":{"candidate_0":{"type":"noul","noul":1.000000001}}}),
        json!({"answers":{"candidate_0":{"type":"noul","noul":-0.1}}}),
        json!({"answers":{"candidate_0":{"type":"noul","noul":0.5},"extra":{"type":"noul","noul":0.5}}}),
    ] {
        let (url, _seen, server) = mock("200 OK", body.to_string(), Duration::ZERO);
        let provider = HttpProvider::new(&HttpSettings {
            url,
            protocol: HttpProtocol::Systemone,
            model: Some("test-model".into()),
            timeout_ms: 2000,
            ..HttpSettings::default()
        })
        .unwrap();
        assert!(provider.score(&[candidate(Category::Infra)]).is_err());
        server.join().unwrap();
    }
}

#[test]
fn engine_uses_http_scores_and_falls_back_on_http_error() {
    use kaponir_core::s1::{Calibration, S1Config};
    use kaponir_core::{
        AnalyzeCtx, Candidate, Engine, Input, MaskOptions, MemoryVault, Recognizer,
    };
    struct Detector;
    impl Recognizer for Detector {
        fn id(&self) -> &str {
            "fixture"
        }
        fn analyze(&self, input: &Input<'_>, _: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
            out.push(Candidate::new(
                0,
                input.text.len(),
                "PROJ",
                Category::Infra,
                "fixture",
                0.5,
            ));
        }
    }
    for (protocol, status, body, expected) in [
        (
            HttpProtocol::Probabilities,
            "200 OK",
            json!({"probabilities":[0.01]}),
            "maybe",
        ),
        (
            HttpProtocol::Probabilities,
            "503 Unavailable",
            json!({}),
            "ANON_PROJ_1",
        ),
        (
            HttpProtocol::Systemone,
            "200 OK",
            json!({"answers":{"candidate_0":{"type":"noul","noul":0.01}}}),
            "maybe",
        ),
        (
            HttpProtocol::Systemone,
            "503 Unavailable",
            json!({}),
            "ANON_PROJ_1",
        ),
        (
            HttpProtocol::Systemone,
            "200 OK",
            json!({"answers":{}}),
            "ANON_PROJ_1",
        ),
    ] {
        let (url, _seen, server) = mock(status, body.to_string(), Duration::ZERO);
        let provider = HttpProvider::new(&HttpSettings {
            url,
            protocol,
            model: Some("test-model".into()),
            timeout_ms: 2000,
            ..HttpSettings::default()
        })
        .unwrap();
        let engine = Engine::builder([1; 32])
            .recognizer(Box::new(Detector))
            .s1(S1Config {
                provider: Box::new(provider),
                calibration: Calibration::default(),
                weight: 1.0,
                keep_below: 0.1,
                shadow: false,
                context_bytes: 60,
                profile: Default::default(),
            })
            .build()
            .unwrap();
        let out = engine
            .mask(
                &Input::text("maybe"),
                &MaskOptions::default(),
                &mut MemoryVault::new(),
            )
            .unwrap();
        assert_eq!(out.text, expected);
        server.join().unwrap();
    }
}

#[test]
fn neither_http_protocol_sends_credential_profile_values() {
    for protocol in [HttpProtocol::Probabilities, HttpProtocol::Systemone] {
        let (url, seen, server) = mock(
            "200 OK",
            json!({"probabilities":[0.5],"answers":{"candidate_0":{"type":"noul","noul":0.5}}})
                .to_string(),
            Duration::ZERO,
        );
        let provider = HttpProvider::new(&HttpSettings {
            url,
            protocol,
            model: Some("test-model".into()),
            disclosure: Level::L2,
            timeout_ms: 2000,
            ..HttpSettings::default()
        })
        .unwrap();
        provider
            .score_with_profile(
                &[candidate(Category::Infra)],
                &[
                    ProfileValue {
                        kind: "SECRET".into(),
                        value: "SyntheticSecret42".into(),
                    },
                    ProfileValue {
                        kind: "PROJ".into(),
                        value: "syntheticsecret42".into(),
                    },
                    ProfileValue {
                        kind: "CUSTOM".into(),
                        value: "UnknownSecret64".into(),
                    },
                    ProfileValue {
                        kind: "ORG".into(),
                        value: "Acme".into(),
                    },
                ],
            )
            .unwrap();
        let request = seen.recv_timeout(Duration::from_secs(2)).unwrap();
        let wire = request.to_string().to_lowercase();
        assert!(!wire.contains("syntheticsecret42") && !wire.contains("unknownsecret64"));
        assert!(wire.contains("acme"));
        server.join().unwrap();
    }
}
