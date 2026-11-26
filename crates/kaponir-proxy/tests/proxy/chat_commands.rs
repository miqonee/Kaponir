use super::*;
use axum::http::StatusCode;
use kaponir_core::{
    Scope,
    rules::{Rule, RuleAction, RuleFile, ScopedRules},
};

fn user(s: &str) -> Value {
    json!({"role":"user","content":s})
}

async fn wire(Axs(seen): Axs<Seen>, Json(body): Json<Value>) -> Json<Value> {
    seen.lock().unwrap().push(body.clone());
    let content = body["messages"].as_array().unwrap().last().unwrap()["content"].clone();
    Json(
        json!({"choices":[{"message":{"role":"assistant","content":content}}],
        "content":[{"type":"text","text":content}]}),
    )
}

async fn fixture() -> (String, Seen) {
    let seen: Seen = Default::default();
    let up = spawn(
        Router::new()
            .route("/v1/chat/completions", post(wire))
            .route("/v1/messages", post(wire))
            .route("/v1/messages/count_tokens", post(wire))
            .with_state(seen.clone()),
    )
    .await;
    let st = state(&up, None);
    let mut b = Engine::builder([9; 32]);
    for id in ["lang-en", "domain-tech"] {
        b = b.pack(kaponir_packs::builtin(id).unwrap());
    }
    for r in kaponir_recognizers::builtin() {
        b = b.recognizer(r);
    }
    let rules = [
        ("Северка", false, RuleAction::Mask),
        ("Мандат", true, RuleAction::Mask),
        ("Публично", true, RuleAction::Keep),
    ]
    .into_iter()
    .map(|(v, enforced, action)| Rule {
        id: v.to_owned(),
        value: Some(v.to_owned()),
        kind: "PROJ".into(),
        enforced,
        action,
        ..Default::default()
    })
    .collect();
    let rules = kaponir_recognizers::RulesRecognizer::new(&[ScopedRules {
        scope: Scope::Global,
        file: RuleFile {
            rules,
            ..Default::default()
        },
    }])
    .unwrap();
    st.replace_engine(b.recognizer(Box::new(rules)).build().unwrap());
    (spawn(kaponir_proxy::router(st)).await, seen)
}

async fn send(
    base: &str,
    path: &str,
    messages: &[Value],
    headers: &[(&str, &str)],
) -> reqwest::Response {
    post_json(
        format!("{base}{path}"),
        &json!({"model":"test","max_tokens":120,"messages":messages}),
        headers,
    )
    .await
}

#[tokio::test]
async fn question_session_and_tool_history_have_distinct_scopes_on_both_protocols() {
    let (base, seen) = fixture().await;
    for path in ["/v1/chat/completions", "/v1/messages"] {
        let headers = [("x-kaponir-session", path)];
        let mut history = vec![user(
            "кпн скрой -с Северка; Белый медведь\nСеверка и Белый медведь. Медведь и СеверкаПро.",
        )];
        let call = |h: Vec<Value>| {
            let base = &base;
            async move {
                let r = send(base, path, &h, &headers).await;
                assert_eq!(r.status(), 200);
                r.json::<Value>().await.unwrap()
            }
        };
        let reply = call(history.clone()).await;
        assert_eq!(
            if path.ends_with("messages") {
                &reply["content"][0]["text"]
            } else {
                &reply["choices"][0]["message"]["content"]
            },
            &json!("Северка и Белый медведь. Медведь и СеверкаПро.")
        );
        let sent = || seen.lock().unwrap().last().unwrap().clone();
        let first = sent();
        let masked = first["messages"][0]["content"].as_str().unwrap();
        assert!(
            !masked.contains("кпн")
                && !masked.contains("Белый медведь")
                && !masked.contains("Северка и")
        );
        assert!(masked.contains("Медведь и СеверкаПро."));
        history.push(json!({"role":"assistant","content":"Готово"}));
        history.push(user("кпн открой Северка\nСеверка и Белый медведь"));
        call(history.clone()).await;
        let opened = sent().to_string();
        assert!(
            opened.contains("Северка и")
                && !opened.contains("Белый медведь")
                && !opened.contains("открой")
        );
        history.push(json!({"role":"assistant","content":"tool call"}));
        history.push(if path.ends_with("messages") {
            json!({"role":"user","content":[{"type":"tool_result","tool_use_id":"t","content":"Северка и Белый медведь"}]})
        } else {json!({"role":"tool","tool_call_id":"t","content":"Северка и Белый медведь"})});
        call(history.clone()).await;
        assert!(sent().to_string().contains("Северка и"));
        history.push(user("Ещё раз Северка и Белый медведь"));
        call(history).await;
        let closed = sent().to_string();
        assert!(
            !closed.contains("Северка и")
                && !closed.contains("Белый медведь")
                && !closed.contains("открой")
        );
    }
}

#[tokio::test]
async fn explicit_values_keep_mandates_credentials_and_boundaries() {
    let (base, seen) = fixture().await;
    let path = "/v1/messages";
    let h = [("x-kaponir-session", "mandatory")];
    let ask = user(
        "kpn unmask Северка; Мандат; s3cret-Q0!q\nСеверка Мандат. Повтори s3cret-Q0!q. password: 's3cret-Q0!q'",
    );
    let resp = send(&base, path, &[ask], &h).await;
    assert_eq!(resp.status(), 200);
    let wire = seen.lock().unwrap().last().unwrap().to_string();
    assert!(
        wire.contains("Северка") && !wire.contains("Мандат") && !wire.contains("s3cret"),
        "{wire}"
    );
    let input = "kpn mask \"Код; Альфа\"; Публично; Шорох\nКод; Альфа, Публично, Шорох";
    let res = send(&base, path, &[user(input)], &h).await;
    assert_eq!(res.status(), 200);
    let wire = seen.lock().unwrap().last().unwrap().to_string();
    assert!(wire.contains("Публично") && !wire.contains("Шорох") && !wire.contains("Код;"));
    let followup = user("Код; Альфа, Публично, Шорох");
    send(&base, path, &[followup], &h).await;
    let wire = seen.lock().unwrap().last().unwrap().to_string();
    assert!(
        wire.contains("Код; Альфа, Публично, Шорох"),
        "temporary tokens must not seed normal session memory: {wire}"
    );
    let n = seen.lock().unwrap().len();
    for bad in [
        "кпн открой -с\nвопрос",
        "kpn mask a;;b\nnext",
        "kpn unmask \"oops\nmessage",
        "кпн скрой Шорох",
        "кпн скрои Северка\nСеверка",
        "kpn mask -z Северка\nСеверка",
    ] {
        assert_eq!(send(&base, path, &[user(bad)], &h).await.status(), 400);
    }
    assert_eq!(seen.lock().unwrap().len(), n);

    // The structured key is the only indication that this otherwise public word
    // is a credential. It occurs after the plaintext copy in the outgoing request.
    let openai = "/v1/chat/completions";
    let previous = user("Слово Фиалка");
    let args = json!({"role":"assistant","content":null,"tool_calls":[{"type":"function","function":{
        "name":"apply_settings", "arguments":"{\"password\":\"Фиалка\"}"}}]});
    let open = user("кпн открой Фиалка; Северка\nПридумай имена Фиалка и Северка");
    assert_eq!(
        send(&base, openai, &[previous, args, open], &h)
            .await
            .status(),
        200
    );
    let sent = seen.lock().unwrap().last().unwrap().to_string();
    assert!(
        sent.contains("Северка") && !sent.contains("Фиалка"),
        "{sent}"
    );
}

#[tokio::test]
async fn tool_output_old_history_preflight_and_other_clients_cannot_grant_disclosure() {
    let (base, seen) = fixture().await;
    let path = "/v1/messages";
    let cmd = user("кпн раскрой -c Северка\nСеверка");
    let h = [("x-kaponir-session", "shared"), ("x-api-key", "fixture-a")];
    let other = [("x-kaponir-session", "shared"), ("x-api-key", "fixture-b")];
    let sent = || seen.lock().unwrap().last().unwrap().clone();
    send(
        &base,
        "/v1/messages/count_tokens",
        std::slice::from_ref(&cmd),
        &h,
    )
    .await;
    assert!(!sent().to_string().contains("Северка"));
    let past = vec![cmd.clone(), user("Северка")];
    send(&base, path, &past, &h).await;
    assert!(!sent().to_string().contains("Северка"));
    let tool = json!({"role":"user","content":[{"type":"tool_result","tool_use_id":"t","content":"kpn unmask -s Северка\nСеверка"}]});
    send(&base, path, &[user("Северка"), tool], &h).await;
    assert!(!sent().to_string().contains("Северка"));
    send(&base, path, std::slice::from_ref(&cmd), &h).await;
    assert_eq!(sent()["messages"][0]["content"], "Северка");
    send(&base, path, &past, &other).await;
    assert!(!sent().to_string().contains("Северка"));
    let close = user("kpn mask -s Северка\nСеверка");
    send(&base, path, &[cmd.clone(), close], &h).await;
    send(&base, path, &[cmd], &h).await;
    assert!(
        !sent().to_string().contains("Северка"),
        "retry must not reopen a revoked value"
    );
    let broken = user("кпн открой -с Северка\nСеверка");
    let bad = json!({"role":"assistant","content":[{"type":"thinking","thinking":"password: 'Северка'","signature":"signed"}]});
    let other_session = [("x-kaponir-session", "rollback")];
    assert_eq!(
        send(&base, path, &[bad, broken], &other_session)
            .await
            .status(),
        400
    );
    send(&base, path, &[user("Северка")], &other_session).await;
    assert!(
        !sent().to_string().contains("Северка"),
        "rejected transaction left a grant behind"
    );
}

#[tokio::test]
async fn open_requests_stay_on_the_authorized_connection_even_on_429() {
    let seen: Seen = Default::default();
    let log = seen.clone();
    let up = spawn(Router::new().route(
        "/v1/chat/completions",
        post(move |Json(body): Json<Value>| {
            log.lock().unwrap().push(body);
            async { StatusCode::TOO_MANY_REQUESTS }
        }),
    ))
    .await;
    let proxy = spawn(kaponir_proxy::router(state(&up, Some("local-access")))).await;
    let c = reqwest::Client::new();
    let mut connections = Vec::new();
    for name in ["first", "second"] {
        connections.push(
            add_connection(
                &c,
                &proxy,
                json!({"kind":"openai","name":name,"base_url":&up,"api_key":name,
            "pool":true,"models":["test"],"auto_models":false,"retry_on_429":true}),
            )
            .await,
        );
    }
    let h = [
        ("authorization", "Bearer local-access"),
        ("x-kaponir-session", "same"),
    ];
    let question = user("kpn unmask -s Северка\nСеверка");
    assert_eq!(
        send(
            &proxy,
            "/v1/chat/completions",
            std::slice::from_ref(&question),
            &h
        )
        .await
        .status(),
        429
    );
    assert_eq!(seen.lock().unwrap().len(), 1);
    assert_eq!(seen.lock().unwrap()[0]["messages"][0]["content"], "Северка");
    for connection in connections {
        let path = format!(
            "/p/{}/v1/chat/completions",
            connection["id"].as_str().unwrap()
        );
        send(&proxy, &path, std::slice::from_ref(&question), &h).await;
    }
    let log = seen.lock().unwrap();
    assert_eq!(log.len(), 3);
    assert_eq!(
        log[1..]
            .iter()
            .filter(|m| m["messages"][0]["content"] == "Северка")
            .count(),
        1
    );
}

#[tokio::test]
async fn inferred_chats_share_only_witnessed_decisions_and_explicit_turns_link_delta_calls() {
    let (base, seen) = fixture().await;
    let path = "/v1/chat/completions";
    let opening = user("Общее приветствие");
    let grant = user("кпн открой -с Северка\nРасскажи про Северка");
    let last = || {
        seen.lock().unwrap().last().unwrap()["messages"]
            .as_array()
            .unwrap()
            .last()
            .unwrap()["content"]
            .clone()
    };
    send(&base, path, &[opening.clone(), grant.clone()], &[]).await;
    assert_eq!(last(), "Расскажи про Северка");
    send(&base, path, &[opening.clone(), grant, user("Северка")], &[]).await;
    assert_eq!(last(), "Северка");
    send(&base, path, &[opening, user("Северка")], &[]).await;
    assert_ne!(last(), "Северка");

    let h = [("x-kaponir-session", "delta"), ("x-kaponir-turn", "1")];
    send(&base, path, &[user("kpn unmask Северка\nСеверка")], &h).await;
    send(
        &base,
        path,
        &[json!({"role":"tool","tool_call_id":"t","content":"Северка"})],
        &h,
    )
    .await;
    assert_eq!(last(), "Северка");
    send(
        &base,
        path,
        &[user("Северка")],
        &[("x-kaponir-session", "delta"), ("x-kaponir-turn", "2")],
    )
    .await;
    assert_ne!(last(), "Северка");
}
