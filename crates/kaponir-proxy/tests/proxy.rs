//! Сквозные тесты: клиент → kaponir-proxy → имитация провайдера. Провайдер возвращает то, что
//! получил, нарезанным на мелкие чанки — так проверяется, что секреты до него не доходят,
//! а клиент получает оригиналы, в том числе в аргументах инструментов.

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{Json, State as Axs};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use kaponir_core::{Engine, Lexicon};
use kaponir_proxy::sse::SseParser;
use kaponir_proxy::{ProxyConfig, State};
use kaponir_vault::SqliteVault;
use serde_json::{Value, json};
use std::convert::Infallible;
use std::sync::{Arc, Mutex};

type Seen = Arc<Mutex<Vec<Value>>>;

const ORIGINAL: &str = "deploy /home/mgoloshanov/zorgbilling/k8s\npassword: 'p\"a\\ss'\nhost harbor.infra.mycompany.ru\nsee C:\\Users\\ivanov\\zorgproj\\main.rs\nnote password: InlineS3cret!; done\n";
const LEAKS: &[&str] = &[
    "mgoloshanov",
    "zorgbilling",
    "p\\\"a",
    "mycompany",
    "ivanov",
    "zorgproj",
    "InlineS3cret!",
];

fn engine() -> Engine {
    let mut b = Engine::builder([9; 32]);
    for id in ["lang-en", "domain-tech", "domain-web"] {
        b = b.pack(kaponir_packs::builtin(id).unwrap());
    }
    for r in kaponir_recognizers::builtin() {
        b = b.recognizer(r);
    }
    let mut lex = Lexicon::default();
    lex.corp_domains.insert("mycompany.ru".into());
    b.lexicon(&lex).build().unwrap()
}

fn state(up: &str, token: Option<&str>) -> Arc<State> {
    let cfg = ProxyConfig {
        anthropic_upstream: up.into(),
        openai_upstream: up.into(),
        api_token: token.map(Into::into),
        ..ProxyConfig::default()
    };
    Arc::new(
        State::new(
            engine(),
            SqliteVault::open_in_memory(&[1; 32]).unwrap(),
            "me",
            cfg,
        )
        .unwrap(),
    )
}

async fn spawn(router: Router) -> String {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, router).await.unwrap() });
    format!("http://{addr}")
}

fn chunks(s: &str, n: usize) -> Vec<String> {
    let chars: Vec<char> = s.chars().collect();
    chars.chunks(n).map(|c| c.iter().collect()).collect()
}

fn last_user_text(body: &Value) -> String {
    body["messages"].as_array().unwrap().last().unwrap()["content"]
        .as_str()
        .unwrap()
        .to_string()
}

/// SSE, нарезанный на куски по 7 байт: события и даже UTF-8 рвутся на границах.
fn sse_response(sse: String) -> Response {
    let pieces: Vec<Result<Bytes, Infallible>> = sse
        .into_bytes()
        .chunks(7)
        .map(|c| Ok(Bytes::copy_from_slice(c)))
        .collect();
    Response::builder()
        .header("content-type", "text/event-stream")
        .body(Body::from_stream(futures_util::stream::iter(pieces)))
        .unwrap()
}

async fn mock_anthropic(Axs(seen): Axs<Seen>, Json(body): Json<Value>) -> Response {
    seen.lock().unwrap().push(body.clone());
    let text = last_user_text(&body);
    if !body["stream"].as_bool().unwrap_or(false) {
        return Json(json!({
            "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}, {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": text}}]
        }))
        .into_response();
    }
    let mut sse = String::new();
    let mut ev = |name: &str, v: Value| sse.push_str(&format!("event: {name}\ndata: {v}\n\n"));
    ev(
        "message_start",
        json!({"type": "message_start", "message": {"id": "m", "content": []}}),
    );
    ev(
        "content_block_start",
        json!({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
    );
    ev(
        "content_block_delta",
        json!({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "ANON_PROJ_1"}}),
    );
    ev(
        "content_block_stop",
        json!({"type": "content_block_stop", "index": 0}),
    );
    ev(
        "content_block_start",
        json!({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
    );
    for c in chunks(&text, 3) {
        ev(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": c}}),
        );
    }
    ev(
        "content_block_stop",
        json!({"type": "content_block_stop", "index": 1}),
    );
    ev(
        "content_block_start",
        json!({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t1", "name": "bash", "input": {}}}),
    );
    for c in chunks(&json!({"cmd": text}).to_string(), 5) {
        ev(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": c}}),
        );
    }
    ev(
        "content_block_stop",
        json!({"type": "content_block_stop", "index": 2}),
    );
    ev("message_stop", json!({"type": "message_stop"}));
    sse_response(sse)
}

async fn mock_openai(Axs(seen): Axs<Seen>, Json(body): Json<Value>) -> Response {
    seen.lock().unwrap().push(body.clone());
    let text = last_user_text(&body);
    let args = json!({"cmd": text}).to_string();
    if !body["stream"].as_bool().unwrap_or(false) {
        return Json(json!({
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": text,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": args}}]
            }}]
        }))
        .into_response();
    }
    let mut sse = String::new();
    let mut chunk = |choice: Value| {
        sse.push_str(&format!(
            "data: {}\n\n",
            json!({"id": "x", "object": "chat.completion.chunk", "choices": [choice]})
        ))
    };
    for c in chunks(&text, 5) {
        chunk(json!({"index": 0, "delta": {"reasoning_content": c}, "finish_reason": null}));
    }
    for c in chunks(&text, 4) {
        chunk(json!({"index": 0, "delta": {"content": c}, "finish_reason": null}));
    }
    chunk(
        json!({"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "bash", "arguments": ""}}]}, "finish_reason": null}),
    );
    for c in chunks(&args, 6) {
        chunk(
            json!({"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": c}}]}, "finish_reason": null}),
        );
    }
    chunk(json!({"index": 0, "delta": {}, "finish_reason": "tool_calls"}));
    sse.push_str("data: [DONE]\n\n");
    sse_response(sse)
}

async fn setup(token: Option<&str>) -> (String, Seen) {
    let seen: Seen = Seen::default();
    let up = spawn(
        Router::new()
            .route("/v1/messages", post(mock_anthropic))
            .route("/v1/chat/completions", post(mock_openai))
            .with_state(seen.clone()),
    )
    .await;
    let proxy = spawn(kaponir_proxy::router(state(&up, token))).await;
    (proxy, seen)
}

async fn post_json(url: String, body: &Value, headers: &[(&str, &str)]) -> reqwest::Response {
    let mut req = reqwest::Client::new()
        .post(url)
        .header("content-type", "application/json")
        .body(body.to_string());
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    req.send().await.unwrap()
}

fn assert_no_leaks(sent: &Value) {
    let s = sent.to_string();
    for leak in LEAKS {
        assert!(!s.contains(leak), "`{leak}` reached the provider: {s}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn anthropic_stream_and_session_memory() {
    let (proxy, seen) = setup(None).await;
    let body = json!({"model": "m", "max_tokens": 10, "stream": true, "system": "be brief",
                      "messages": [{"role": "user", "content": ORIGINAL}]});
    let resp = post_json(format!("{proxy}/v1/messages"), &body, &[("x-api-key", "k")]).await;
    assert_eq!(resp.status(), 200);
    let raw = resp.text().await.unwrap();
    let (mut text, mut args, mut thinking) = (String::new(), String::new(), String::new());
    for e in SseParser::default().push(raw.as_bytes()) {
        let v: Value = serde_json::from_str(&e.data).unwrap();
        if v["type"] == "content_block_delta" {
            match v["delta"]["type"].as_str().unwrap() {
                "text_delta" => text += v["delta"]["text"].as_str().unwrap(),
                "input_json_delta" => args += v["delta"]["partial_json"].as_str().unwrap(),
                "thinking_delta" => thinking += v["delta"]["thinking"].as_str().unwrap(),
                _ => {}
            }
        }
    }
    assert_eq!(text, ORIGINAL);
    let args: Value = serde_json::from_str(&args).expect("tool input stays valid JSON");
    assert_eq!(args["cmd"], ORIGINAL);
    assert_eq!(
        thinking, "ANON_PROJ_1",
        "thinking is signed and must pass unchanged"
    );

    let first = seen.lock().unwrap()[0].clone();
    assert_no_leaks(&first);
    assert_eq!(first["system"], "be brief");

    // Второй ход: восстановленный ответ в истории и упоминание в прозе, которое детекторы
    // сами не ловят, — оба маскируются по памяти сессии.
    let body2 = json!({"model": "m", "max_tokens": 10, "system": "be brief", "messages": [
        {"role": "user", "content": ORIGINAL},
        {"role": "assistant", "content": [{"type": "text", "text": text}, {"type": "tool_use", "id": "t1", "name": "bash", "input": args}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "zorgbilling ok"}]},
        {"role": "user", "content": "is zorgbilling healthy? ask ivanov"}
    ]});
    let resp = post_json(
        format!("{proxy}/v1/messages"),
        &body2,
        &[("x-api-key", "k")],
    )
    .await;
    let out: Value = resp.json_or_panic().await;
    let second = seen.lock().unwrap()[1].clone();
    assert_no_leaks(&second);
    assert_eq!(
        second["messages"][0], first["messages"][0],
        "history prefix must stay byte-stable for prompt caching"
    );
    assert_eq!(
        out["content"][0]["text"],
        "is zorgbilling healthy? ask ivanov"
    );
    assert_eq!(
        out["content"][1]["input"]["cmd"],
        "is zorgbilling healthy? ask ivanov"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn openai_stream_and_buffered() {
    let (proxy, seen) = setup(None).await;
    for stream in [true, false] {
        let body = json!({"model": "m", "stream": stream, "messages": [
            {"role": "system", "content": "sys"}, {"role": "user", "content": ORIGINAL}]});
        let resp = post_json(
            format!("{proxy}/v1/chat/completions"),
            &body,
            &[("authorization", "Bearer k")],
        )
        .await;
        assert_eq!(resp.status(), 200);
        let (text, args) = if stream {
            let raw = resp.text().await.unwrap();
            let (mut text, mut args, mut reasoning) = (String::new(), String::new(), String::new());
            let mut done = false;
            for e in SseParser::default().push(raw.as_bytes()) {
                if e.data == "[DONE]" {
                    done = true;
                    continue;
                }
                let v: Value = serde_json::from_str(&e.data).unwrap();
                let d = &v["choices"][0]["delta"];
                text += d["content"].as_str().unwrap_or_default();
                reasoning += d["reasoning_content"].as_str().unwrap_or_default();
                if let Some(a) = d
                    .pointer("/tool_calls/0/function/arguments")
                    .and_then(Value::as_str)
                {
                    args += a;
                }
            }
            assert!(done);
            assert_eq!(reasoning, ORIGINAL, "reasoning_content is restored too");
            (text, args)
        } else {
            let v: Value = resp.json_or_panic().await;
            let m = &v["choices"][0]["message"];
            (
                m["content"].as_str().unwrap().to_string(),
                m["tool_calls"][0]["function"]["arguments"]
                    .as_str()
                    .unwrap()
                    .to_string(),
            )
        };
        assert_eq!(text, ORIGINAL, "stream={stream}");
        let args: Value = serde_json::from_str(&args).expect("arguments stay valid JSON");
        assert_eq!(args["cmd"], ORIGINAL, "stream={stream}");
    }
    let sent = seen.lock().unwrap().clone();
    for s in &sent {
        assert_no_leaks(s);
        assert_eq!(s["messages"][0]["content"], "sys");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn sidecar_api_with_token() {
    let (proxy, _) = setup(Some("t0ken")).await;
    let req = json!({"text": ORIGINAL, "session": "gw-1"});
    let denied = post_json(format!("{proxy}/api/v1/anonymize"), &req, &[]).await;
    assert_eq!(denied.status(), 401);
    let denied = post_json(
        format!("{proxy}/v1/messages"),
        &json!({"messages": []}),
        &[],
    )
    .await;
    assert_eq!(denied.status(), 401);

    let auth = [("authorization", "Bearer t0ken")];
    let m: Value = post_json(format!("{proxy}/api/v1/anonymize"), &req, &auth)
        .await
        .json_or_panic()
        .await;
    let masked = m["text"].as_str().unwrap().to_string();
    for leak in LEAKS {
        assert!(
            !masked.contains(&leak.replace("\\\"", "\"")),
            "{leak} in {masked}"
        );
    }

    let r: Value = post_json(
        format!("{proxy}/api/v1/deanonymize"),
        &json!({"text": masked, "session": "gw-1"}),
        &auth,
    )
    .await
    .json_or_panic()
    .await;
    assert_eq!(r["text"], ORIGINAL);

    // Потоковое восстановление чанками по 4 символа.
    let mut out = String::new();
    let parts = chunks(&masked, 4);
    for (i, c) in parts.iter().enumerate() {
        let done = i + 1 == parts.len();
        let body = json!({"session": "gw-1", "stream": "s1", "chunk": c, "done": done});
        let v: Value = post_json(format!("{proxy}/api/v1/deanonymize/stream"), &body, &auth)
            .await
            .json_or_panic()
            .await;
        out += v["text"].as_str().unwrap();
    }
    assert_eq!(out, ORIGINAL);
}

trait JsonOrPanic {
    async fn json_or_panic(self) -> Value;
}

impl JsonOrPanic for reqwest::Response {
    async fn json_or_panic(self) -> Value {
        let status = self.status();
        let text = self.text().await.unwrap();
        assert!(status.is_success(), "{status}: {text}");
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("{e}: {text}"))
    }
}

async fn traffic_snapshot(base: &str, requests: u64) -> Value {
    for _ in 0..50 {
        let response = reqwest::get(format!("{base}/api/v1/metrics"))
            .await
            .unwrap();
        let value: Value = serde_json::from_str(&response.text().await.unwrap()).unwrap();
        if value["traffic"]["total"]["requests"].as_u64() == Some(requests)
            && value["traffic"]["in_flight"] == 0
        {
            return value;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    panic!("traffic did not settle");
}

#[tokio::test(flavor = "multi_thread")]
async fn dashboard_counts_cached_proxy_traffic_and_excludes_manual_operations() {
    let (proxy, _) = setup(None).await;
    for path in ["/api/v1/anonymize", "/api/v1/ui/mask"] {
        let result = post_json(
            format!("{proxy}{path}"),
            &json!({"text":ORIGINAL,"session":"lab"}),
            &[],
        )
        .await;
        assert_eq!(result.status(), 200);
        result.bytes().await.unwrap();
    }
    assert_eq!(
        traffic_snapshot(&proxy, 0).await["traffic"]["total"]["usage"]["replacements"],
        0
    );
    let request =
        json!({"model":"m","stream":false,"messages":[{"role":"user","content":ORIGINAL}]});
    let mut replacements = 0;
    for index in 1..=2 {
        let result = post_json(
            format!("{proxy}/v1/chat/completions"),
            &request,
            &[("x-kaponir-session", "cached")],
        )
        .await;
        assert_eq!(result.status(), 200);
        result.bytes().await.unwrap();
        let m = traffic_snapshot(&proxy, index).await;
        let current = m["traffic"]["total"]["usage"]["replacements"]
            .as_u64()
            .unwrap();
        if index == 1 {
            replacements = current;
            assert!(replacements > 0);
        }
        assert_eq!(current, replacements);
        assert_eq!(
            m["traffic"]["total"]["usage"]["cached_replacements"]
                .as_u64()
                .unwrap(),
            replacements * (index - 1)
        );
        assert!(m["traffic"]["total"]["usage"]["restored"].as_u64().unwrap() > 0);
        let categories: u64 = m["traffic"]["total"]["usage"]["categories"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_u64().unwrap())
            .sum();
        assert_eq!(categories, current);
        let wire = m["traffic"].to_string();
        for secret in LEAKS {
            assert!(!wire.contains(secret));
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn streaming_traffic_includes_restoration_and_bad_requests_are_blocked() {
    let (proxy, _) = setup(None).await;
    let request =
        json!({"model":"m","stream":true,"messages":[{"role":"user","content":ORIGINAL}]});
    let result = post_json(format!("{proxy}/v1/chat/completions"), &request, &[]).await;
    assert_eq!(result.status(), 200);
    result.bytes().await.unwrap();
    let m = traffic_snapshot(&proxy, 1).await;
    assert_eq!(m["traffic"]["events"][0]["outcome"], "ok");
    assert!(m["traffic"]["total"]["usage"]["restored"].as_u64().unwrap() > 0);
    let result = reqwest::Client::new()
        .post(format!("{proxy}/v1/chat/completions"))
        .body("not-json")
        .send()
        .await
        .unwrap();
    assert_eq!(result.status(), 400);
    result.bytes().await.unwrap();
    let m = traffic_snapshot(&proxy, 2).await;
    assert_eq!(m["traffic"]["total"]["blocked"], 1);
    assert_eq!(m["traffic"]["total"]["errors"], 1);
}

#[tokio::test]
async fn managed_provider_routes_keys_models_and_preserves_masking() {
    use axum::http::HeaderMap;
    use axum::routing::get;
    let upstream = spawn(
        Router::new()
            .route(
                "/gateway/v1/models",
                get(|h: HeaderMap| async move {
                    assert_eq!(h["authorization"], "Bearer fixture-provider-key");
                    assert!(!h.contains_key("x-kaponir-token"));
                    Json(json!({"data":[{"id":"vendor/model-b"},{"id":"vendor/model-a"}]}))
                }),
            )
            .route(
                "/gateway/v1/chat/completions",
                post(|h: HeaderMap, Json(body): Json<Value>| async move {
                    assert_eq!(h["authorization"], "Bearer fixture-provider-key");
                    assert!(!h.contains_key("x-api-key"));
                    assert!(!h.contains_key("cookie"));
                    assert_eq!(body["model"], "vendor/model-a");
                    let text = last_user_text(&body);
                    assert!(!text.contains("InlineS3cret!"));
                    Json(json!({"choices":[{"message":{"role":"assistant","content":text}}]}))
                }),
            ),
    )
    .await;
    let st = state("http://127.0.0.1:1", Some("local-access"));
    let proxy = spawn(kaponir_proxy::router(st.clone())).await;
    let c = reqwest::Client::new();
    let draft =
        json!({"base_url":format!("{upstream}/gateway/v1/"),"api_key":"fixture-provider-key"});
    assert_eq!(
        c.put(format!("{proxy}/api/v1/providers/openai"))
            .json(&draft)
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let tested: Value = c
        .post(format!("{proxy}/api/v1/providers/openai/test"))
        .bearer_auth("local-access")
        .json(&draft)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        tested["models"],
        json!(["vendor/model-a", "vendor/model-b"])
    );
    let before: Value = c
        .get(format!("{proxy}/api/v1/providers"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(before["openai"].is_null(), "testing must not save");
    let saved = c
        .put(format!("{proxy}/api/v1/providers/openai"))
        .bearer_auth("local-access")
        .json(&draft)
        .send()
        .await
        .unwrap();
    assert!(saved.status().is_success());
    assert!(!saved.text().await.unwrap().contains("fixture-provider-key"));
    let list = c
        .get(format!("{proxy}/api/v1/providers"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(!list.contains("fixture-provider-key"));
    let models: Value = c
        .get(format!("{proxy}/v1/models"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(models["data"][0]["id"], "vendor/model-b");
    let response = c.post(format!("{proxy}/v1/chat/completions")).bearer_auth("local-access")
        .header("x-api-key","must-not-leak").header("cookie","must-not-leak")
        .json(&json!({"model":"vendor/model-a","messages":[{"role":"user","content":"password: InlineS3cret!"}]})).send().await.unwrap();
    assert!(response.status().is_success());
    let response: Value = response.json().await.unwrap();
    assert_eq!(
        response["choices"][0]["message"]["content"],
        "password: InlineS3cret!"
    );
    // Empty key preserves a saved credential only for the same normalized URL.
    assert!(
        c.put(format!("{proxy}/api/v1/providers/openai"))
            .bearer_auth("local-access")
            .json(&json!({"base_url":format!("{upstream}/gateway")}))
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
    assert_eq!(
        c.put(format!("{proxy}/api/v1/providers/openai"))
            .bearer_auth("local-access")
            .json(&json!({"base_url":"https://other.example/v1"}))
            .send()
            .await
            .unwrap()
            .status(),
        400
    );
}

#[tokio::test]
async fn provider_checks_validate_urls_redact_errors_and_do_not_follow_redirects() {
    use axum::http::StatusCode;
    use axum::routing::get;
    let upstream = spawn(
        Router::new()
            .route(
                "/v1/models",
                get(|| async { (StatusCode::UNAUTHORIZED, "fixture-secret-key") }),
            )
            .route(
                "/redirect/v1/models",
                get(|| async { axum::response::Redirect::temporary("/v1/models") }),
            ),
    )
    .await;
    let proxy = spawn(kaponir_proxy::router(state(&upstream, None))).await;
    let c = reqwest::Client::new();
    for url in [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com?key=secret",
        "file:///etc/passwd",
        "https://example.com/#secret",
    ] {
        assert_eq!(
            c.put(format!("{proxy}/api/v1/providers/openai"))
                .json(&json!({"base_url":url,"api_key":"fixture-secret-key"}))
                .send()
                .await
                .unwrap()
                .status(),
            400
        );
    }
    for (base, code) in [
        (upstream.clone(), "provider_auth_failed"),
        (format!("{upstream}/redirect"), "provider_check_failed"),
    ] {
        let response = c
            .post(format!("{proxy}/api/v1/providers/openai/test"))
            .json(&json!({"base_url":base,"api_key":"fixture-secret-key"}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 502);
        let text = response.text().await.unwrap();
        assert!(text.contains(code));
        assert!(!text.contains("fixture-secret-key"));
    }
}

#[tokio::test]
async fn managed_anthropic_injects_key_and_version() {
    use axum::http::HeaderMap;
    let upstream = spawn(Router::new().route(
        "/v1/messages",
        post(|h: HeaderMap, Json(body): Json<Value>| async move {
            assert_eq!(h["x-api-key"], "anthropic-fixture");
            assert_eq!(h["anthropic-version"], "2023-06-01");
            assert!(!h.contains_key("authorization"));
            Json(json!({"type":"message","content":[{"type":"text","text":last_user_text(&body)}]}))
        }),
    ))
    .await;
    let proxy = spawn(kaponir_proxy::router(state(
        "http://127.0.0.1:1",
        Some("local-access"),
    )))
    .await;
    let c = reqwest::Client::new();
    assert!(
        c.put(format!("{proxy}/api/v1/providers/anthropic"))
            .bearer_auth("local-access")
            .json(&json!({"base_url":upstream,"api_key":"anthropic-fixture"}))
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
    let r=c.post(format!("{proxy}/v1/messages")).bearer_auth("local-access").json(&json!({"model":"claude-fixture","max_tokens":20,"messages":[{"role":"user","content":"password: InlineS3cret!"}]})).send().await.unwrap();
    assert!(r.status().is_success());
    let body: Value = r.json().await.unwrap();
    assert_eq!(body["content"][0]["text"], "password: InlineS3cret!");
}

async fn add_connection(c: &reqwest::Client, proxy: &str, body: Value) -> Value {
    let r = c
        .post(format!("{proxy}/api/v1/connections"))
        .bearer_auth("local-access")
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    r.json().await.unwrap()
}

#[tokio::test]
async fn connection_pools_balance_by_model_and_explicit_routes_pin_keys() {
    use axum::http::HeaderMap;
    let seen: Arc<Mutex<Vec<(String, Value)>>> = Default::default();
    let seen_up = seen.clone();
    let up = spawn(Router::new().route(
        "/gateway/v1/chat/completions",
        post(move |h: HeaderMap, Json(body): Json<Value>| {
            let seen = seen_up.clone();
            async move {
                assert!(!h.contains_key("x-kaponir-token"));
                assert!(!h.contains_key("cookie"));
                let text = last_user_text(&body);
                assert!(!text.contains("InlineS3cret!"));
                seen.lock()
                    .unwrap()
                    .push((h["authorization"].to_str().unwrap().to_owned(), body));
                Json(json!({"choices":[{"message":{"role":"assistant","content":text}}]}))
            }
        }),
    ))
    .await;
    let proxy = spawn(kaponir_proxy::router(state(
        "http://127.0.0.1:1",
        Some("local-access"),
    )))
    .await;
    let c = reqwest::Client::new();
    let create = |key: &str, models: Value| json!({"kind":"openai","name":"Test connection","base_url":format!("{up}/gateway/v1"),"api_key":key,"pool":true,"models":models});
    let a = add_connection(&c, &proxy, create("fixture-key-a", json!(["shared"]))).await;
    let b = add_connection(
        &c,
        &proxy,
        create("fixture-key-b", json!(["shared", "exclusive-b"])),
    )
    .await;
    let isolated=add_connection(&c,&proxy,json!({"kind":"openai","name":"isolated","base_url":format!("{up}/gateway"),"api_key":"isolated-key","pool":false,"models":["hidden"]})).await;
    let catalog: Value = c
        .get(format!("{proxy}/v1/models"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        catalog["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["exclusive-b", "shared"]
    );
    let body =
        json!({"model":"shared","messages":[{"role":"user","content":"password: InlineS3cret!"}]});
    let results = futures_util::future::join_all((0..8).map(|_| {
        c.post(format!("{proxy}/v1/chat/completions"))
            .bearer_auth("local-access")
            .json(&body)
            .send()
    }))
    .await;
    for r in results {
        let r = r.unwrap();
        assert_eq!(r.status(), 200);
        let v: Value = r.json().await.unwrap();
        assert_eq!(
            v["choices"][0]["message"]["content"],
            "password: InlineS3cret!"
        );
    }
    {
        let rows = seen.lock().unwrap();
        assert_eq!(rows.len(), 8);
        assert!(rows.iter().all(|(key, _)| key == &rows[0].0));
    }
    for model in ["exclusive-b", "unknown"] {
        let r = c
            .post(format!("{proxy}/v1/chat/completions"))
            .bearer_auth("local-access")
            .json(&json!({"model":model,"messages":[{"role":"user","content":"Hello"}]}))
            .send()
            .await
            .unwrap();
        assert_eq!(
            r.status().as_u16(),
            if model == "unknown" { 503 } else { 200 }
        );
    }
    assert_eq!(
        seen.lock().unwrap().last().unwrap().0,
        "Bearer fixture-key-b"
    );
    for (p, key) in [
        (&a, "fixture-key-a"),
        (&b, "fixture-key-b"),
        (&isolated, "isolated-key"),
    ] {
        let r = c
            .post(format!(
                "{proxy}{}/v1/chat/completions?trace=test",
                p["endpoint"].as_str().unwrap()
            ))
            .bearer_auth("local-access")
            .json(&body)
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(
            seen.lock().unwrap().last().unwrap().0,
            format!("Bearer {key}")
        );
    }
    assert_eq!(seen.lock().unwrap().len(), 12);
    let list = c
        .get(format!("{proxy}/api/v1/connections"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(!list.contains("\"api_key\""));
    assert!(!list.contains("fixture-key"));
    let metrics: Value = c
        .get(format!("{proxy}/api/v1/metrics"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(
        metrics["traffic"]["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| e["sent"] == true)
            .all(
                |e| [a["id"].clone(), b["id"].clone(), isolated["id"].clone()]
                    .contains(&e["connection_id"])
            )
    );

    assert_eq!(
        c.get(format!("{proxy}/api/v1/connections"))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
}

#[tokio::test]
async fn disabling_and_deleting_connections_fail_closed() {
    let proxy = spawn(kaponir_proxy::router(state(
        "http://127.0.0.1:1",
        Some("local-access"),
    )))
    .await;
    let c = reqwest::Client::new();
    let p=add_connection(&c,&proxy,json!({"kind":"openai","name":"key","base_url":"https://example.com/v1","api_key":"fixture-secret","pool":true,"models":["shared"]})).await;
    let id = p["id"].as_str().unwrap();
    let body = json!({"model":"shared","messages":[]});
    let update = json!({"kind":"openai","base_url":"https://example.com/v1","enabled":false});
    assert_eq!(
        c.put(format!("{proxy}/api/v1/connections/{id}"))
            .bearer_auth("local-access")
            .json(&update)
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    for route in [
        "/v1/chat/completions".to_owned(),
        format!("/p/{id}/v1/chat/completions"),
    ] {
        assert_eq!(
            c.post(format!("{proxy}{route}"))
                .bearer_auth("local-access")
                .json(&body)
                .send()
                .await
                .unwrap()
                .status(),
            503
        );
    }
    assert_eq!(
        c.post(format!("{proxy}/p/{id}/v1/messages"))
            .bearer_auth("local-access")
            .json(&body)
            .send()
            .await
            .unwrap()
            .status(),
        400
    );
    assert_eq!(
        c.delete(format!("{proxy}/api/v1/connections/{id}"))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    assert_eq!(
        c.delete(format!("{proxy}/api/v1/connections/{id}"))
            .bearer_auth("local-access")
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    assert_eq!(
        c.post(format!("{proxy}/v1/chat/completions"))
            .bearer_auth("local-access")
            .json(&body)
            .send()
            .await
            .unwrap()
            .status(),
        503
    );
    assert_eq!(
        c.post(format!("{proxy}/p/{id}/v1/chat/completions"))
            .bearer_auth("local-access")
            .json(&body)
            .send()
            .await
            .unwrap()
            .status(),
        404
    );
    let models: Value = c
        .get(format!("{proxy}/v1/models"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(models["data"], json!([]));
    assert_eq!(c.post(format!("{proxy}/api/v1/connections")).bearer_auth("local-access").json(&json!({"kind":"openai","name":"empty","base_url":"https://example.com","api_key":"key","pool":true,"auto_models":false})).send().await.unwrap().status(),400);
}

#[tokio::test]
async fn legacy_connections_migrate_persist_and_never_resurrect_after_removal() {
    let path = std::env::temp_dir().join(format!(
        "kaponir-connections-{}.db",
        kaponir_vault::key_to_hex(&kaponir_vault::generate_key().unwrap())
    ));
    let vault = SqliteVault::open(&path, &[1; 32]).unwrap();
    vault
        .set_private_setting(
            "provider:openai",
            br#"{"base_url":"https://example.com","api_key":"old-fixture-secret"}"#,
        )
        .unwrap();
    let st = Arc::new(State::new(engine(), vault, "me", ProxyConfig::default()).unwrap());
    let proxy = spawn(kaponir_proxy::router(st)).await;
    let c = reqwest::Client::new();
    let list: Value = c
        .get(format!("{proxy}/api/v1/connections"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(list["connections"][0]["id"], "openai");
    assert_eq!(list["connections"][0]["pool"], true);
    let p=add_connection(&c,&proxy,json!({"kind":"openai","name":"new","base_url":"https://example.org/v1","api_key":"new-fixture-secret"})).await;
    assert_eq!(
        c.delete(format!("{proxy}/api/v1/connections/openai"))
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    let reopened = Arc::new(
        State::new(
            engine(),
            SqliteVault::open(&path, &[1; 32]).unwrap(),
            "me",
            ProxyConfig::default(),
        )
        .unwrap(),
    );
    let second = spawn(kaponir_proxy::router(reopened)).await;
    let list: Value = c
        .get(format!("{second}/api/v1/connections"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(list["connections"].as_array().unwrap().len(), 1);
    assert_eq!(list["connections"][0]["id"], p["id"]);
    let key = c
        .put(format!(
            "{second}/api/v1/connections/{}",
            p["id"].as_str().unwrap()
        ))
        .json(&json!({"kind":"openai","base_url":"https://other.example","name":"replacement"}))
        .send()
        .await
        .unwrap();
    assert_eq!(key.status(), 400);
    let bytes = std::fs::read(&path).unwrap();
    for secret in [b"old-fixture-secret", b"new-fixture-secret"] {
        assert!(!bytes.windows(secret.len()).any(|b| b == secret));
    }
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn concurrent_connection_creates_do_not_lose_updates() {
    let proxy = spawn(kaponir_proxy::router(state("http://127.0.0.1:1", None))).await;
    let c = reqwest::Client::new();
    let responses=futures_util::future::join_all((0..12).map(|i|c.post(format!("{proxy}/api/v1/connections")).json(&json!({"kind":"openai","name":format!("key-{i}"),"base_url":"https://example.com","api_key":format!("fixture-{i}")})).send())).await;
    for r in responses {
        assert_eq!(r.unwrap().status(), 200);
    }
    let list: Value = c
        .get(format!("{proxy}/api/v1/connections"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(list["connections"].as_array().unwrap().len(), 12);
}

#[tokio::test]
async fn dedicated_endpoints_keep_sse_restoration_and_anthropic_models() {
    use axum::http::HeaderMap;
    use axum::routing::get;
    let seen: Seen = Default::default();
    let upstream = spawn(
        Router::new()
            .route("/v1/chat/completions", post(mock_openai))
            .route("/v1/messages", post(mock_anthropic))
            .route(
                "/v1/models",
                get(|h: HeaderMap| async move {
                    assert_eq!(h["x-api-key"], "anthropic-secret");
                    Json(json!({"data":[{"id":"claude-fixture"}],"has_more":false}))
                }),
            )
            .with_state(seen.clone()),
    )
    .await;
    let proxy = spawn(kaponir_proxy::router(state(
        "http://127.0.0.1:1",
        Some("local-access"),
    )))
    .await;
    let c = reqwest::Client::new();
    let p = add_connection(
        &c,
        &proxy,
        json!({"kind":"openai","name":"stream","base_url":upstream,"api_key":"stream-secret"}),
    )
    .await;
    let response = c
        .post(format!(
            "{proxy}{}/v1/chat/completions",
            p["endpoint"].as_str().unwrap()
        ))
        .bearer_auth("local-access")
        .json(&json!({"model":"m","stream":true,"messages":[{"role":"user","content":ORIGINAL}]}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let text = response.text().await.unwrap();
    assert!(text.contains("[DONE]"));
    let mut parser = SseParser::default();
    let mut content = String::new();
    for event in parser.push(text.as_bytes()) {
        if let Ok(v) = serde_json::from_str::<Value>(&event.data)
            && let Some(t) = v["choices"][0]["delta"]["content"].as_str()
        {
            content.push_str(t);
        }
    }
    assert_eq!(content, ORIGINAL);
    let p=add_connection(&c,&proxy,json!({"kind":"anthropic","name":"claude","base_url":upstream,"api_key":"anthropic-secret"})).await;
    let models = c
        .get(format!(
            "{proxy}{}/v1/models",
            p["endpoint"].as_str().unwrap()
        ))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap();
    assert_eq!(models.status(), 200);
    let response = c
        .post(format!(
            "{proxy}{}/v1/messages",
            p["endpoint"].as_str().unwrap()
        ))
        .bearer_auth("local-access")
        .json(&json!({"model":"m","max_tokens":20,"messages":[{"role":"user","content":ORIGINAL}]}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let v: Value = response.json().await.unwrap();
    assert_eq!(v["content"][0]["text"], ORIGINAL);
}

#[tokio::test]
async fn automatic_model_discovery_populates_shared_catalog_after_save() {
    use axum::routing::get;
    let upstream = spawn(Router::new().route(
        "/v1/models",
        get(|h: axum::http::HeaderMap| async move {
            assert_eq!(h["authorization"], "Bearer discovery-key");
            Json(json!({"object":"list","data":[{"id":"auto/alpha"},{"id":"auto/beta"}]}))
        }),
    ))
    .await;
    let proxy = spawn(kaponir_proxy::router(state(
        "http://127.0.0.1:1",
        Some("local-access"),
    )))
    .await;
    let c = reqwest::Client::new();
    let response = c.post(format!("{proxy}/api/v1/connections")).bearer_auth("local-access").json(&json!({
        "kind":"openai","name":"Automatic discovery","base_url":upstream,"api_key":"discovery-key","pool":true
    })).send().await.unwrap();
    assert_eq!(response.status(), 200);
    let saved: Value = response.json().await.unwrap();
    assert_eq!(saved["auto_models"], true);
    assert_eq!(saved["models"], json!(["auto/alpha", "auto/beta"]));
    let catalog: Value = c
        .get(format!("{proxy}/v1/models"))
        .bearer_auth("local-access")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        catalog["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["auto/alpha", "auto/beta"]
    );
}

#[tokio::test]
async fn session_affinity_and_429_failover_preserve_masking_for_both_apis() {
    use axum::http::{HeaderMap, StatusCode};
    for kind in ["openai", "anthropic"] {
        let attempts: Arc<Mutex<Vec<(String, Value)>>> = Default::default();
        let rejection: Arc<Mutex<Option<(String, u16)>>> = Default::default();
        let path = if kind == "openai" {
            "/v1/chat/completions"
        } else {
            "/v1/messages"
        };
        let seen = attempts.clone();
        let reject = rejection.clone();
        let up = spawn(Router::new().route(
            path,
            post(move |headers: HeaderMap, Json(body): Json<Value>| {
                let seen = seen.clone();
                let reject = reject.clone();
                async move {
                    assert!(!headers.contains_key("x-kaponir-session"));
                    let key = if kind == "openai" {
                        headers["authorization"]
                            .to_str()
                            .unwrap()
                            .trim_start_matches("Bearer ")
                            .to_owned()
                    } else {
                        headers["x-api-key"].to_str().unwrap().to_owned()
                    };
                    assert!(!body.to_string().contains("InlineS3cret!"));
                    seen.lock().unwrap().push((key.clone(), body.clone()));
                    let failure = reject.lock().unwrap().clone();
                    if let Some((target, status)) = failure
                        && (target == "*" || target == key)
                    {
                        return (
                            StatusCode::from_u16(status).unwrap(),
                            [("retry-after", "17")],
                            "limited",
                        )
                            .into_response();
                    }
                    let fixture_seen = Default::default();
                    if kind == "openai" {
                        mock_openai(Axs(fixture_seen), Json(body)).await
                    } else {
                        mock_anthropic(Axs(fixture_seen), Json(body)).await
                    }
                }
            }),
        ))
        .await;
        let proxy = spawn(kaponir_proxy::router(state(
            "http://127.0.0.1:1",
            Some("local-access"),
        )))
        .await;
        let c = reqwest::Client::new();
        let mut connections = Vec::new();
        for key in ["a", "b", "isolated", "wrong-model", "disabled"] {
            let p = add_connection(
                &c,
                &proxy,
                json!({"kind":kind,"name":key,"base_url":up,"api_key":key,
                "retry_on_429":true,"pool":key!="isolated","enabled":key!="disabled","auto_models":false,
                "models":[if key=="wrong-model" {"other"} else {"shared"}]}),
            )
            .await;
            connections.push(p);
        }
        let body = json!({"model":"shared","max_tokens":128,"messages":[{"role":"user","content":"password: InlineS3cret!"}]});
        let request = |endpoint: String, data: Value| {
            c.post(endpoint)
                .bearer_auth("local-access")
                .header("x-kaponir-session", "stable-conversation")
                .json(&data)
                .send()
        };
        let url = format!("{proxy}{path}");
        let r = request(url.clone(), body.clone()).await.unwrap();
        assert_eq!(r.status(), 200);
        assert!(r.text().await.unwrap().contains("InlineS3cret!"));
        let first = attempts.lock().unwrap()[0].0.clone();
        assert!(first == "a" || first == "b");
        // A different first message must not override an explicit conversation ID.
        let mut next = body.clone();
        next["messages"][0]["content"] = json!("Second turn: password: InlineS3cret!");
        let r = request(url.clone(), next).await.unwrap();
        assert_eq!(r.status(), 200);
        let _ = r.text().await.unwrap();
        assert_eq!(attempts.lock().unwrap()[1].0, first);
        // Only 429 permits replay. Non-429 failures stay on the bound key.
        *rejection.lock().unwrap() = Some((first.clone(), 500));
        let before = attempts.lock().unwrap().len();
        assert_eq!(
            request(url.clone(), body.clone()).await.unwrap().status(),
            500
        );
        assert_eq!(attempts.lock().unwrap().len(), before + 1);
        *rejection.lock().unwrap() = Some((first.clone(), 429));
        let mut streaming = body.clone();
        streaming["stream"] = json!(true);
        let before = attempts.lock().unwrap().len();
        let r = request(url.clone(), streaming).await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.headers()["content-type"], "text/event-stream");
        let stream = r.text().await.unwrap();
        let mut parser = SseParser::default();
        let mut restored = String::new();
        for event in parser.push(stream.as_bytes()) {
            if let Ok(v) = serde_json::from_str::<Value>(&event.data) {
                let text = if kind == "openai" {
                    &v["choices"][0]["delta"]["content"]
                } else {
                    &v["delta"]["text"]
                };
                if let Some(text) = text.as_str() {
                    restored.push_str(text);
                }
            }
        }
        assert_eq!(restored, "password: InlineS3cret!");
        let second = {
            let rows = attempts.lock().unwrap();
            assert_eq!(rows.len(), before + 2);
            assert_eq!(rows[before].0, first);
            assert_ne!(rows[before + 1].0, first);
            assert_eq!(rows[before].1, rows[before + 1].1);
            rows[before + 1].0.clone()
        };
        *rejection.lock().unwrap() = None;
        let r = request(url.clone(), body.clone()).await.unwrap();
        assert_eq!(r.status(), 200);
        let _ = r.text().await.unwrap();
        assert_eq!(attempts.lock().unwrap().last().unwrap().0, second);
        // Exhaustion returns the last original 429 without cycling back to a tried key.
        *rejection.lock().unwrap() = Some(("*".into(), 429));
        let before = attempts.lock().unwrap().len();
        let r = request(url.clone(), body.clone()).await.unwrap();
        assert_eq!(r.status(), 429);
        assert_eq!(r.headers()["retry-after"], "17");
        assert_eq!(r.text().await.unwrap(), "limited");
        {
            let rows = attempts.lock().unwrap();
            assert_eq!(rows.len(), before + 2);
            assert_ne!(rows[before].0, rows[before + 1].0);
        }
        // Explicit routes never silently change credentials.
        let before = attempts.lock().unwrap().len();
        let r = request(
            format!(
                "{proxy}{}{path}",
                connections[0]["endpoint"].as_str().unwrap()
            ),
            body.clone(),
        )
        .await
        .unwrap();
        assert_eq!(r.status(), 429);
        assert_eq!(attempts.lock().unwrap().len(), before + 1);
        assert_eq!(attempts.lock().unwrap().last().unwrap().0, "a");
        // Removing a bound route sends subsequent turns to the remaining eligible key.
        *rejection.lock().unwrap() = None;
        let r = request(url.clone(), body.clone()).await.unwrap();
        assert_eq!(r.status(), 200);
        let _ = r.text().await.unwrap();
        let bound = attempts.lock().unwrap().last().unwrap().0.clone();
        let connection = connections.iter().find(|p| p["name"] == bound).unwrap();
        assert_eq!(
            c.delete(format!(
                "{proxy}/api/v1/connections/{}",
                connection["id"].as_str().unwrap()
            ))
            .bearer_auth("local-access")
            .send()
            .await
            .unwrap()
            .status(),
            200
        );
        let r = request(url, body).await.unwrap();
        assert_eq!(r.status(), 200);
        let _ = r.text().await.unwrap();
        assert_ne!(attempts.lock().unwrap().last().unwrap().0, bound);
        assert!(
            attempts
                .lock()
                .unwrap()
                .iter()
                .all(|(key, _)| key == "a" || key == "b")
        );
    }
}

#[tokio::test]
async fn default_proxy_preserves_agent_envelope_and_returns_429_without_replay() {
    use axum::http::{HeaderMap, StatusCode};
    for kind in ["openai", "anthropic"] {
        let attempts: Arc<Mutex<Vec<(HeaderMap, Value)>>> = Default::default();
        let seen = attempts.clone();
        let path = if kind == "openai" {
            "/v1/chat/completions"
        } else {
            "/v1/messages"
        };
        let upstream = spawn(Router::new().route(
            path,
            post(move |headers: HeaderMap, Json(body): Json<Value>| {
                seen.lock().unwrap().push((headers, body));
                async {
                    (
                        StatusCode::TOO_MANY_REQUESTS,
                        [("retry-after", "23"), ("x-request-id", "provider-request")],
                        "rate limited",
                    )
                }
            }),
        ))
        .await;
        let proxy = spawn(kaponir_proxy::router(state(
            "http://127.0.0.1:1",
            Some("local-access"),
        )))
        .await;
        let c = reqwest::Client::new();
        let source = add_connection(
            &c,
            &proxy,
            json!({"kind":kind,"name":"source","base_url":upstream,"api_key":"source-key",
            "pool":true,"auto_models":false,"models":["m"]}),
        )
        .await;
        assert_eq!(source["retry_on_429"], false);
        let mut body = json!({"model":"m","max_tokens":128,"temperature":0.2,"stream":false,
            "metadata":{"user_id":"agent-user","conversation_id":"agent-conversation"},
            "messages":[{"role":"user","content":"password: InlineS3cret!"}]});
        if kind == "openai" {
            body["messages"]
                .as_array_mut()
                .unwrap()
                .insert(0, json!({"role":"system","content":"be brief"}));
            body["tools"] = json!([{"type":"function","function":{"name":"lookup","parameters":{"type":"object","properties":{}}}}]);
            body["tool_choice"] = json!("auto");
        } else {
            body["system"] =
                json!([{"type":"text","text":"be brief","cache_control":{"type":"ephemeral"}}]);
            body["tools"] =
                json!([{"name":"lookup","input_schema":{"type":"object","properties":{}}}]);
            body["tool_choice"] = json!({"type":"auto"});
        }
        // No Kaponir session header. Ordinary agent correlation headers pass through.
        let send = || {
            c.post(format!("{proxy}{path}"))
                .bearer_auth("local-access")
                .header("x-session-id", "agent-session")
                .header("x-request-id", "agent-request")
                .json(&body)
                .send()
        };
        let response = send().await.unwrap();
        assert_eq!(response.status(), 429);
        assert_eq!(response.headers()["retry-after"], "23");
        assert_eq!(response.headers()["x-request-id"], "provider-request");
        assert_eq!(response.text().await.unwrap(), "rate limited");
        {
            let rows = attempts.lock().unwrap();
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].0["x-session-id"], "agent-session");
            assert_eq!(rows[0].0["x-request-id"], "agent-request");
            assert!(!rows[0].0.contains_key("x-kaponir-session"));
            assert!(!rows[0].1.to_string().contains("InlineS3cret!"));
            let mut expected = body.clone();
            let messages = expected["messages"].as_array_mut().unwrap();
            let index = messages.len() - 1;
            messages[index]["content"] = rows[0].1["messages"][index]["content"].clone();
            assert_eq!(
                rows[0].1, expected,
                "only sensitive message content may change"
            );
        }
        // A second key exists but default behavior must still deliver 429 unchanged.
        let standby = add_connection(
            &c,
            &proxy,
            json!({"kind":kind,"name":"standby","base_url":upstream,"api_key":"standby-key",
            "pool":true,"auto_models":false,"models":["m"]}),
        )
        .await;
        assert_eq!(standby["retry_on_429"], false);
        assert_eq!(send().await.unwrap().status(), 429);
        assert_eq!(attempts.lock().unwrap().len(), 2);
        // Opt-in on the source alone does not permit an unapproved target or another API URL.
        assert_eq!(
            c.put(format!(
                "{proxy}/api/v1/connections/{}",
                source["id"].as_str().unwrap()
            ))
            .bearer_auth("local-access")
            .json(&json!({"kind":kind,"base_url":upstream,"retry_on_429":true}))
            .send()
            .await
            .unwrap()
            .status(),
            200
        );
        let foreign = spawn(Router::new().route(
            path,
            post(|| async { (StatusCode::IM_A_TEAPOT, "must not retry across API URLs") }),
        ))
        .await;
        add_connection(
            &c,
            &proxy,
            json!({"kind":kind,"name":"foreign","base_url":foreign,"api_key":"foreign-key",
            "pool":true,"auto_models":false,"retry_on_429":true,"models":["m"]}),
        )
        .await;
        assert_eq!(send().await.unwrap().status(), 429);
        assert_eq!(attempts.lock().unwrap().len(), 3);
    }
}

#[tokio::test]
async fn system_hint_is_opt_in_for_both_apis() {
    assert!(!ProxyConfig::default().system_hint);
    assert!(
        !serde_json::from_value::<ProxyConfig>(json!({}))
            .unwrap()
            .system_hint
    );
    let seen: Seen = Default::default();
    let upstream = spawn(
        Router::new()
            .route("/v1/messages", post(mock_anthropic))
            .route("/v1/chat/completions", post(mock_openai))
            .with_state(seen.clone()),
    )
    .await;
    let mut st = state(&upstream, None);
    Arc::get_mut(&mut st).unwrap().cfg.system_hint = true;
    let proxy = spawn(kaponir_proxy::router(st)).await;
    for path in ["/v1/messages", "/v1/chat/completions"] {
        let body =
            json!({"model":"m","messages":[{"role":"user","content":"hello"}],"max_tokens":16});
        assert_eq!(
            post_json(format!("{proxy}{path}"), &body, &[])
                .await
                .status(),
            200
        );
    }
    let rows = seen.lock().unwrap();
    assert!(rows[0]["system"].as_str().unwrap().contains("placeholders"));
    assert_eq!(rows[1]["messages"][0]["role"], "system");
    assert!(
        rows[1]["messages"][0]["content"]
            .as_str()
            .unwrap()
            .contains("placeholders")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn session_metadata_groups_requests_and_sse_chunks_without_revealing_client_ids() {
    let (proxy, _) = setup(None).await;
    let first = json!({"model":"m","stream":true,"messages":[{"role":"user","content":"start a discussion"}]});
    let next = json!({"model":"m","stream":false,"messages":[{"role":"user","content":"start a discussion"},{"role":"assistant","content":"reply"},{"role":"user","content":"next"}]});
    for body in [&first, &next] {
        let r = post_json(
            format!("{proxy}/v1/chat/completions"),
            body,
            &[("x-kaponir-session", "private-client-id")],
        )
        .await;
        assert_eq!(r.status(), 200);
        r.bytes().await.unwrap();
    }
    for body in [&first, &next] {
        let r = post_json(format!("{proxy}/v1/chat/completions"), body, &[]).await;
        assert_eq!(r.status(), 200);
        r.bytes().await.unwrap();
    }
    let snapshot = traffic_snapshot(&proxy, 4).await;
    let events = snapshot["traffic"]["events"].as_array().unwrap();
    assert_eq!(events.len(), 4);
    assert_eq!(events[0]["session_source"], "inferred");
    assert_eq!(events[0]["session_id"], events[1]["session_id"]);
    assert_eq!(events[2]["session_source"], "explicit");
    assert_eq!(events[2]["session_id"], events[3]["session_id"]);
    assert_ne!(events[0]["session_id"], events[2]["session_id"]);
    assert!(events[1]["streaming"].as_bool().unwrap());
    assert!(!snapshot.to_string().contains("private-client-id"));
    let exported = reqwest::get(format!("{proxy}/api/v1/ui/sessions/export"))
        .await
        .unwrap()
        .json::<Value>()
        .await
        .unwrap();
    assert_eq!(exported["documents"].as_array().unwrap().len(), 2);
}
