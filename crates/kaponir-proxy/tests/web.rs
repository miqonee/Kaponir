//! Web UI API: предпросмотр без записи, «только в этом тексте», правила с пересборкой движка,
//! решения по журналу, защита от DNS rebinding.

use kaponir_core::rules::{Rule, RuleFile, ScopedRules};
use kaponir_core::{Engine, Lexicon, Scope};
use kaponir_proxy::{Control, ProxyConfig, RuleSet, State};
use kaponir_vault::SqliteVault;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};

/// Правила в памяти вместо файлов `KAPONIR_HOME/rules`.
#[derive(Default)]
struct MemControl {
    sets: Mutex<Vec<(Scope, RuleFile)>>,
    s1: Mutex<(kaponir_s1::Settings, Option<String>)>,
}

impl MemControl {
    fn scoped(&self) -> Vec<ScopedRules> {
        self.sets
            .lock()
            .unwrap()
            .iter()
            .map(|(scope, file)| ScopedRules {
                scope: *scope,
                file: file.clone(),
            })
            .collect()
    }

    fn file<T>(&self, scope: Scope, f: impl FnOnce(&mut RuleFile) -> T) -> T {
        let mut sets = self.sets.lock().unwrap();
        if !sets.iter().any(|(s, _)| *s == scope) {
            sets.push((scope, RuleFile::default()));
        }
        f(&mut sets.iter_mut().find(|(s, _)| *s == scope).unwrap().1)
    }
}

impl Control for MemControl {
    fn rules(&self) -> anyhow::Result<Vec<RuleSet>> {
        Ok(self
            .scoped()
            .into_iter()
            .map(|s| RuleSet {
                scope: s.scope,
                location: "mem".into(),
                file: s.file,
            })
            .collect())
    }

    fn s1_settings(&self) -> anyhow::Result<kaponir_s1::Settings> {
        Ok(self.s1.lock().unwrap().0.clone())
    }

    fn s1_local_model_available(&self) -> anyhow::Result<bool> {
        Ok(false)
    }

    fn set_s1(&self, settings: &kaponir_s1::Settings, key: Option<&str>) -> anyhow::Result<()> {
        let mut settings = settings.clone();
        settings.http.token = key.map(str::to_owned);
        kaponir_s1::build(&settings, std::path::Path::new(""), &self.scoped())?;
        *self.s1.lock().unwrap() = (settings, key.map(str::to_owned));
        Ok(())
    }

    fn add_rule(&self, scope: Scope, rule: Rule) -> anyhow::Result<()> {
        kaponir_recognizers::RulesRecognizer::new(&[ScopedRules {
            scope,
            file: RuleFile {
                rules: vec![rule.clone()],
                ..RuleFile::default()
            },
        }])?;
        self.file(scope, |f| f.rules.push(rule));
        Ok(())
    }

    fn remove_rule(&self, scope: Scope, index: usize) -> anyhow::Result<Rule> {
        self.file(scope, |f| {
            (index < f.rules.len()).then(|| f.rules.remove(index))
        })
        .ok_or_else(|| anyhow::anyhow!("no rule #{index}"))
    }

    fn set_rule_disabled(
        &self,
        scope: Scope,
        index: usize,
        revision: &str,
        disabled: bool,
    ) -> anyhow::Result<()> {
        self.file(scope, |file| {
            anyhow::ensure!(
                blake3::hash(&serde_json::to_vec(file)?).to_hex().as_str() == revision,
                "rules changed"
            );
            file.rules
                .get_mut(index)
                .ok_or_else(|| anyhow::anyhow!("rule missing"))?
                .disabled = disabled;
            Ok(())
        })
    }

    fn set_corp_domains(&self, scope: Scope, domains: &[String]) -> anyhow::Result<()> {
        self.file(scope, |f| f.corp.domains = domains.to_vec());
        Ok(())
    }

    fn build_engine(&self) -> anyhow::Result<Engine> {
        let rules = self.scoped();
        let settings = self.s1_settings()?;
        let s1 = kaponir_s1::build(&settings, std::path::Path::new(""), &rules)?;
        Ok(engine(&rules, s1))
    }
}

fn engine(rules: &[ScopedRules], s1: Option<kaponir_core::s1::S1Config>) -> Engine {
    let mut b = Engine::builder([9; 32]);
    for id in ["lang-en", "domain-tech", "domain-web"] {
        b = b.pack(kaponir_packs::builtin(id).unwrap());
    }
    for r in kaponir_recognizers::builtin() {
        b = b.recognizer(r);
    }
    b = b.lexicon(&Lexicon::default());
    if let Some(s1) = s1 {
        b = b.s1(s1);
    }
    for set in rules {
        b = b.lexicon(&set.file.lexicon());
    }
    if !rules.is_empty() {
        b = b.recognizer(Box::new(
            kaponir_recognizers::RulesRecognizer::new(rules).unwrap(),
        ));
    }
    b.build().unwrap()
}

async fn spawn(token: Option<&str>) -> String {
    let cfg = ProxyConfig {
        api_token: token.map(Into::into),
        ..ProxyConfig::default()
    };
    let st = State::new(
        engine(&[], None),
        SqliteVault::open_in_memory(&[1; 32]).unwrap(),
        "me",
        cfg,
    )
    .unwrap()
    .with_control(Box::<MemControl>::default());
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(l, kaponir_proxy::router(Arc::new(st)))
            .await
            .unwrap()
    });
    format!("http://{addr}")
}

async fn call(method: &str, url: String, body: Option<Value>) -> (u16, Value) {
    let c = reqwest::Client::new();
    let mut req = match method {
        "GET" => c.get(url),
        "POST" => c.post(url),
        "PUT" => c.put(url),
        "DELETE" => c.delete(url),
        _ => unreachable!(),
    };
    if let Some(b) = body {
        req = req
            .header("content-type", "application/json")
            .body(b.to_string());
    }
    let r = req.send().await.unwrap();
    let status = r.status().as_u16();
    let text = r.text().await.unwrap();
    (
        status,
        serde_json::from_str(&text).unwrap_or(Value::String(text)),
    )
}

async fn mask(base: &str, text: &str, extra: Value) -> Value {
    let mut body = json!({"text": text});
    body.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    let (s, v) = call("POST", format!("{base}/api/v1/ui/mask"), Some(body)).await;
    assert_eq!(s, 200, "{v}");
    v
}

async fn entries(base: &str) -> i64 {
    call("GET", format!("{base}/api/v1/ui/info"), None).await.1["stats"]["entries"]
        .as_i64()
        .unwrap()
}

const PATH_TEXT: &str = "deploy to /srv/zorgbilling/releases";

#[tokio::test]
async fn preview_does_not_persist_commit_does() {
    let base = spawn(None).await;
    let v = mask(&base, PATH_TEXT, json!({})).await;
    assert_eq!(v["text"], "deploy to /srv/ANON_PROJ_1/releases");
    assert!(!v["explain"].as_array().unwrap().is_empty());
    assert_eq!(
        entries(&base).await,
        0,
        "preview must not write to the vault"
    );
    let (_, j) = call("GET", format!("{base}/api/v1/journal"), None).await;
    assert_eq!(
        j.as_array().unwrap().len(),
        0,
        "preview must not write to the journal"
    );

    let v = mask(&base, PATH_TEXT, json!({"commit": true})).await;
    assert_eq!(v["text"], "deploy to /srv/ANON_PROJ_1/releases");
    assert_eq!(entries(&base).await, 1);
    let (_, r) = call(
        "POST",
        format!("{base}/api/v1/ui/restore"),
        Some(json!({"text": "see /srv/anon_proj_1/x"})),
    )
    .await;
    assert_eq!(r["text"], "see /srv/zorgbilling/x");
    assert_eq!(r["restored"], 1);
}

#[tokio::test]
async fn adhoc_values_apply_to_one_request() {
    let base = spawn(None).await;
    let text = "billing is slow; billing-api-7f9 restarted";
    let v = mask(
        &base,
        text,
        json!({"adhoc": [{"value": "billing", "kind": "svc"}]}),
    )
    .await;
    assert_eq!(
        v["text"],
        "ANON_SVC_1 is slow; anon-svc-1-api-7f9 restarted"
    );
    assert_eq!(v["replacements"][0]["recognizer"], "session");
    let v = mask(&base, text, json!({})).await;
    assert_eq!(v["text"], text);
}

#[tokio::test]
async fn rules_reload_engine() {
    let base = spawn(None).await;
    let text = "billing talks to api.acme-corp.io";
    let (s, v) = call(
        "POST",
        format!("{base}/api/v1/rules"),
        Some(json!({"scope": "team", "rule": {"kind": "svc", "value": "billing"}})),
    )
    .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(
        mask(&base, text, json!({})).await["replacements"][0]["scope"],
        "team"
    );

    let (s, _) = call(
        "PUT",
        format!("{base}/api/v1/rules/personal/domains"),
        Some(json!({"domains": ["acme-corp.io"]})),
    )
    .await;
    assert_eq!(s, 200);
    let v = mask(&base, text, json!({})).await;
    assert!(!v["text"].as_str().unwrap().contains("acme"), "{v}");

    let (_, sets) = call("GET", format!("{base}/api/v1/rules"), None).await;
    let team = sets
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["scope"] == "team")
        .unwrap();
    assert_eq!(team["file"]["rule"][0]["kind"], "SVC");

    let (s, _) = call("DELETE", format!("{base}/api/v1/rules/team/0"), None).await;
    assert_eq!(s, 200);
    assert!(
        mask(&base, text, json!({})).await["text"]
            .as_str()
            .unwrap()
            .starts_with("billing talks")
    );

    // Невалидные правила отклоняются, не попадая в контур.
    for bad in [
        json!({"scope": "personal", "rule": {"kind": "X", "regex": "("}}),
        json!({"scope": "personal", "rule": {"kind": "X", "value": "a", "regex": "b"}}),
        json!({"scope": "team", "rule": {"kind": "X", "value": "abc", "enforced": true}}),
        json!({"scope": "personal", "rule": {"kind": "a b", "value": "abc"}}),
    ] {
        assert_eq!(
            call("POST", format!("{base}/api/v1/rules"), Some(bad.clone()))
                .await
                .0,
            400,
            "{bad}"
        );
    }
    assert_eq!(
        call("DELETE", format!("{base}/api/v1/rules/team/5"), None)
            .await
            .0,
        400
    );
}

#[tokio::test]
async fn journal_decisions_become_rules() {
    let base = spawn(None).await;
    mask(&base, PATH_TEXT, json!({"commit": true})).await;
    mask(&base, "cd /srv/quuxledger/bin", json!({"commit": true})).await;
    let (_, rows) = call("GET", format!("{base}/api/v1/journal"), None).await;
    let rows = rows.as_array().unwrap();
    assert_eq!(rows.len(), 2, "{rows:?}");
    let id = |v: &str| {
        rows.iter().find(|r| r["value"] == v).unwrap()["id"]
            .as_i64()
            .unwrap()
    };

    // «Никогда не маскировать» → keep-правило, значение остаётся как есть.
    let (s, v) = call(
        "POST",
        format!("{base}/api/v1/journal/decide"),
        Some(json!({"action": "keep", "ids": [id("quuxledger")]})),
    )
    .await;
    assert_eq!((s, v["closed"].as_i64()), (200, Some(1)), "{v}");
    let v = mask(&base, "cd /srv/quuxledger/bin", json!({"commit": true})).await;
    assert_eq!(v["text"], "cd /srv/quuxledger/bin");
    assert_eq!(
        v["uncertain"], 0,
        "a value kept by a rule is decided, not uncertain"
    );

    // «Маскировать» с другим типом → значение маскируется и вне пути.
    let (s, _) = call(
        "POST",
        format!("{base}/api/v1/journal/decide"),
        Some(json!({"action": "mask", "ids": [id("zorgbilling")], "kind": "svc"})),
    )
    .await;
    assert_eq!(s, 200);
    assert_eq!(
        mask(&base, "zorgbilling is down", json!({})).await["text"],
        "ANON_SVC_1 is down"
    );

    let (_, open) = call("GET", format!("{base}/api/v1/journal?status=open"), None).await;
    assert_eq!(open.as_array().unwrap().len(), 0);
    let (_, all) = call("GET", format!("{base}/api/v1/journal?status=all"), None).await;
    let statuses: Vec<&str> = all
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["status"].as_str().unwrap())
        .collect();
    assert!(
        statuses.contains(&"kept") && statuses.contains(&"masked"),
        "{statuses:?}"
    );

    // Шаблон закрывает подходящие записи.
    mask(&base, "ls /srv/zzqledger", json!({"commit": true})).await;
    let (s, v) = call(
        "POST",
        format!("{base}/api/v1/journal/template"),
        Some(json!({"template": "{PROJ}", "scope": "personal"})),
    )
    .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["closed"], 1);
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/journal/template"),
            Some(json!({"template": "{oops"}))
        )
        .await
        .0,
        400
    );
}

#[tokio::test]
async fn discover_preview_and_apply_use_only_fresh_server_suggestions() {
    let base = spawn(None).await;
    let documents =
        json!([{"name":"budget.txt","text":"Бюджет проекта: 1 250 000 ₽","format":"text"}]);
    let (status, preview) = call(
        "POST",
        format!("{base}/api/v1/ui/discover"),
        Some(json!({"documents": documents})),
    )
    .await;
    assert_eq!(status, 200, "{preview}");
    let suggestions = preview["suggestions"].as_array().unwrap();
    let amount = suggestions.iter().find(|s| s["kind"] == "AMOUNT").unwrap();
    let id = amount["id"].as_str().unwrap();
    assert_eq!(amount["value"], "1 250 000 ₽");
    assert_eq!(
        entries(&base).await,
        0,
        "discover preview must not write to the vault"
    );
    let (_, sets) = call("GET", format!("{base}/api/v1/rules"), None).await;
    assert!(
        sets.as_array()
            .unwrap()
            .iter()
            .all(|s| s["file"]["rule"].as_array().unwrap().is_empty())
    );

    // Пустой выбор — успешный no-op.
    let (status, empty) = call(
        "POST",
        format!("{base}/api/v1/ui/discover/apply"),
        Some(json!({"documents": documents, "ids": [], "scope": "personal"})),
    )
    .await;
    assert_eq!((status, empty["applied"].as_i64()), (200, Some(0)));

    for ids in [json!(["forged"]), json!([id, id])] {
        let (status, _) = call(
            "POST",
            format!("{base}/api/v1/ui/discover/apply"),
            Some(json!({"documents": documents, "ids": ids, "scope": "personal"})),
        )
        .await;
        assert_eq!(status, 400);
    }

    // Произвольные клиентские значения/отчёт не участвуют в решении сервера.
    let (status, applied) = call(
        "POST",
        format!("{base}/api/v1/ui/discover/apply"),
        Some(json!({
            "documents": documents,
            "ids": [id],
            "scope": "team",
            "report": {"suggestions": [{"id": id, "kind": "ORG", "value": "forged"}]},
            "value": "also forged"
        })),
    )
    .await;
    assert_eq!(
        (status, applied["applied"].as_i64()),
        (200, Some(1)),
        "{applied}"
    );
    assert!(
        applied["report"]["suggestions"]
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["id"] != id),
        "apply response must contain the report rebuilt with the new rules: {applied}"
    );
    let (_, sets) = call("GET", format!("{base}/api/v1/rules"), None).await;
    let team = sets
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["scope"] == "team")
        .unwrap();
    let rules = team["file"]["rule"].as_array().unwrap();
    assert_eq!(rules.len(), 1);
    assert_eq!(rules[0]["value"], "1 250 000 ₽");
    assert_eq!(rules[0]["action"], "mask");
    assert!(
        mask(&base, "Бюджет: 1 250 000 ₽", json!({})).await["text"]
            .as_str()
            .unwrap()
            .contains("ANON_AMOUNT_1")
    );

    // После применения повторный предпросмотр не предлагает уже закрытое значение.
    let (_, next) = call(
        "POST",
        format!("{base}/api/v1/ui/discover"),
        Some(json!({"documents": documents})),
    )
    .await;
    assert!(
        next["suggestions"]
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["id"] != id)
    );
}

#[tokio::test]
async fn assets_and_host_guard() {
    let base = spawn(None).await;
    let r = reqwest::get(format!("{base}/ui/")).await.unwrap();
    assert_eq!(r.status(), 200);
    assert!(
        r.headers()["content-security-policy"]
            .to_str()
            .unwrap()
            .contains("script-src 'self'")
    );
    assert!(r.text().await.unwrap().contains("i18n.js"));
    let js = reqwest::get(format!("{base}/ui/app.js")).await.unwrap();
    assert!(
        js.headers()["content-type"]
            .to_str()
            .unwrap()
            .starts_with("text/javascript")
    );

    // Чужое имя хоста (DNS rebinding) без токена — отказ, даже для статики и прокси.
    let c = reqwest::Client::new();
    for path in ["/ui/", "/api/v1/ui/info", "/v1/chat/completions"] {
        let r = c
            .get(format!("{base}{path}"))
            .header("host", "evil.example:8787")
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), 403, "{path}");
    }
    let r = c
        .get(format!("{base}/api/v1/ui/info"))
        .header("host", "localhost")
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);

    // С токеном имя хоста не проверяется, но API требует Bearer.
    let base = spawn(Some("s3cret")).await;
    let r = c
        .get(format!("{base}/api/v1/ui/info"))
        .header("host", "kaponir.internal")
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 401);
    let r = c
        .get(format!("{base}/api/v1/ui/info"))
        .bearer_auth("s3cret")
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    // Статика без секретов — без токена, иначе UI не смог бы спросить токен.
    assert_eq!(
        c.get(format!("{base}/ui/")).send().await.unwrap().status(),
        200
    );
}

#[tokio::test]
async fn metrics_are_authenticated_and_do_not_expose_text() {
    let base = spawn(Some("metrics-secret")).await;
    let client = reqwest::Client::new();
    for path in ["/metrics", "/api/v1/metrics"] {
        let r = client.get(format!("{base}{path}")).send().await.unwrap();
        assert_eq!(r.status(), 401);
        let r = client
            .get(format!("{base}{path}"))
            .bearer_auth("metrics-secret")
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.headers()["cache-control"], "no-store");
        assert!(!r.text().await.unwrap().contains("metrics-secret"));
    }
}

#[tokio::test]
async fn metrics_count_stream_restore_and_survive_rule_reload() {
    let base = spawn(None).await;
    let masked = mask(&base, PATH_TEXT, json!({"commit":true})).await;
    let tokenized = masked["text"].as_str().unwrap();
    let (status, _) = call("POST", format!("{base}/api/v1/deanonymize/stream"),
        Some(json!({"session":"metrics", "stream":"s", "chunk":format!("{tokenized} ANON_UNKNOWN_999"), "done":true}))).await;
    assert_eq!(status, 200);
    let (_, before) = call("GET", format!("{base}/api/v1/metrics"), None).await;
    assert_eq!(before["engine"]["mask"]["calls"], 1);
    assert_eq!(before["engine"]["replacements"], 1);
    assert_eq!(before["engine"]["restored"], 1);
    assert_eq!(before["engine"]["unknown_tokens"], 1);
    let (status, _) = call(
        "POST",
        format!("{base}/api/v1/rules"),
        Some(json!({"scope":"team", "rule":{"kind":"PROJ", "value":"new-private-project"}})),
    )
    .await;
    assert_eq!(status, 200);
    let (_, after) = call("GET", format!("{base}/api/v1/metrics"), None).await;
    assert_eq!(before["engine"], after["engine"]);
    assert_eq!(
        after["http"]["calls"].as_u64().unwrap(),
        before["http"]["calls"].as_u64().unwrap() + 1
    );
    let (_, body) = call("GET", format!("{base}/metrics"), None).await;
    let body = body.as_str().unwrap();
    assert!(body.contains("kaponir_replacements_total 1"));
    assert!(body.contains("kaponir_unknown_tokens_total 1"));
    assert!(body.contains("kaponir_restore_duration_seconds_bucket{le=\"+Inf\"}"));
    assert!(!body.contains("zorgbilling") && !body.contains("ANON_"));
}

#[tokio::test]
async fn setup_previews_and_applies_additively_without_duplicate_rules() {
    let base = spawn(None).await;
    let answers = json!({"domains":["corp.example"],"projects":["Vega Ledger"]});
    let (status, result) = call(
        "POST",
        format!("{base}/api/v1/ui/setup"),
        Some(answers.clone()),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(result["applied"], false);
    assert_eq!(entries(&base).await, 0);
    let mut apply = answers;
    apply["apply"] = json!(true);
    for _ in 0..2 {
        let (status, _) = call(
            "POST",
            format!("{base}/api/v1/ui/setup"),
            Some(apply.clone()),
        )
        .await;
        assert_eq!(status, 200);
    }
    let (_, rules) = call("GET", format!("{base}/api/v1/rules"), None).await;
    let rules = rules.as_array().unwrap();
    let personal = rules.iter().find(|x| x["scope"] == "personal").unwrap();
    assert_eq!(personal["file"]["rule"].as_array().unwrap().len(), 1);
    let result = mask(&base, "Vega Ledger", json!({})).await;
    assert!(!result["text"].as_str().unwrap().contains("Vega Ledger"));
    let (status, _) = call(
        "POST",
        format!("{base}/api/v1/ui/setup"),
        Some(json!({"domains":["https://bad.example"],"projects":["Another Secret"],"apply":true})),
    )
    .await;
    assert_eq!(status, 400);
    let protected = spawn(Some("guard")).await;
    assert_eq!(
        call(
            "POST",
            format!("{protected}/api/v1/ui/setup"),
            Some(json!({}))
        )
        .await
        .0,
        401
    );
}

#[tokio::test]
async fn s1_settings_apply_without_exposing_or_losing_the_vault_key() {
    let base = spawn(None).await;
    let (status, initial) = call("GET", format!("{base}/api/v1/ui/s1"), None).await;
    assert_eq!(status, 200);
    assert_eq!(initial["mode"], "off");
    assert_eq!(initial["key_saved"], false);
    assert_eq!(initial["local_model_available"], false);
    assert!(initial["settings"]["http"].get("token").is_none());

    let mut settings = initial["settings"].clone();
    settings["provider"] = json!("http");
    settings["shadow"] = json!(true);
    settings["http"]["url"] = json!("http://127.0.0.1:9/score");
    let (status, saved) = call(
        "PUT",
        format!("{base}/api/v1/ui/s1"),
        Some(json!({"settings":settings,"key":"vault-secret-never-return"})),
    )
    .await;
    assert_eq!(status, 200, "{saved}");
    assert_eq!(saved["mode"], "shadow");
    assert_eq!(saved["key_saved"], true);
    assert!(!saved.to_string().contains("vault-secret-never-return"));
    assert!(saved["settings"]["http"].get("token").is_none());
    let (_, info) = call("GET", format!("{base}/api/v1/ui/info"), None).await;
    assert_eq!(info["s1_mode"], "shadow");

    settings["http"]["timeout_ms"] = json!(500);
    let (status, updated) = call(
        "PUT",
        format!("{base}/api/v1/ui/s1"),
        Some(json!({"settings":settings.clone()})),
    )
    .await;
    assert_eq!(status, 200, "{updated}");
    assert_eq!(updated["key_saved"], true);
    assert_eq!(updated["settings"]["http"]["timeout_ms"], 500);

    let (status, cleared) = call(
        "PUT",
        format!("{base}/api/v1/ui/s1"),
        Some(json!({"settings":settings,"clear_key":true})),
    )
    .await;
    assert_eq!(status, 200, "{cleared}");
    assert_eq!(cleared["key_saved"], false);
}

#[tokio::test]
async fn s1_rejects_invalid_settings_and_local_model_without_a_file() {
    let base = spawn(None).await;
    let (_, initial) = call("GET", format!("{base}/api/v1/ui/s1"), None).await;
    let mut settings = initial["settings"].clone();
    settings["provider"] = json!("ngram");
    let (status, _) = call(
        "PUT",
        format!("{base}/api/v1/ui/s1"),
        Some(json!({"settings":settings})),
    )
    .await;
    assert_eq!(status, 400);
    let mut settings = initial["settings"].clone();
    settings["provider"] = json!("http");
    settings["http"]["url"] = json!("file:///tmp/s1");
    let (status, _) = call(
        "PUT",
        format!("{base}/api/v1/ui/s1"),
        Some(json!({"settings":settings})),
    )
    .await;
    assert_eq!(status, 400);
    let (_, current) = call("GET", format!("{base}/api/v1/ui/s1"), None).await;
    assert_eq!(current["mode"], "off");
}

#[tokio::test]
async fn settings_transfer_requires_authorization() {
    let base = spawn(Some("private-token")).await;
    for path in ["export", "import"] {
        let (status, _) = call(
            "POST",
            format!("{base}/api/v1/ui/config/{path}"),
            Some(json!({})),
        )
        .await;
        assert_eq!(status, 401);
    }
}

#[tokio::test]
async fn discovery_collection_persists_and_rejection_is_a_label_not_a_rule() {
    let base = spawn(None).await;
    let documents = json!([{"name":"a","text":"Цена: 250 USD"}]);
    let (status, preview) = call(
        "POST",
        format!("{base}/api/v1/ui/discover"),
        Some(json!({"documents":documents,"remember":true,"use_s1":true})),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(preview["s1_status"], "off");
    let id = preview["suggestions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["kind"] == "AMOUNT")
        .unwrap()["id"]
        .clone();
    call(
        "POST",
        format!("{base}/api/v1/ui/discover"),
        Some(json!({"documents":documents,"remember":true})),
    )
    .await;
    let (_, stored) = call("GET", format!("{base}/api/v1/ui/discover/profile"), None).await;
    let suggestion = stored["report"]["suggestions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["id"] == id)
        .unwrap();
    assert_eq!(suggestion["count"], 1);
    assert_eq!(stored["labels"], 0);
    let (status, _) = call(
        "POST",
        format!("{base}/api/v1/ui/discover/apply"),
        Some(json!({"saved":true,"action":"not_sensitive","ids":[id,"forged"]})),
    )
    .await;
    assert_eq!(status, 400);
    let (_, stored) = call("GET", format!("{base}/api/v1/ui/discover/profile"), None).await;
    assert_eq!(stored["labels"], 0);
    let (status, done) = call(
        "POST",
        format!("{base}/api/v1/ui/discover/apply"),
        Some(json!({"saved":true,"action":"not_sensitive","ids":[id]})),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(done["applied"], 1);
    let (_, rules) = call("GET", format!("{base}/api/v1/rules"), None).await;
    assert!(
        rules
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["file"]["rule"].as_array().unwrap().is_empty())
    );
    let (_, stored) = call("GET", format!("{base}/api/v1/ui/discover/profile"), None).await;
    assert_eq!(stored["labels"], 1);
}

#[tokio::test]
async fn worker_management_is_authenticated_and_optional() {
    let protected = spawn(Some("private-token")).await;
    let (status, _) = call("GET", format!("{protected}/api/v1/ui/worker"), None).await;
    assert_eq!(status, 401);
    let base = spawn(None).await;
    let (status, value) = call("GET", format!("{base}/api/v1/ui/worker"), None).await;
    assert_eq!(status, 200);
    assert_eq!(value["configured"], false);
    let (status, _) = call(
        "POST",
        format!("{base}/api/v1/ui/worker/jobs"),
        Some(json!({"source":"."})),
    )
    .await;
    assert_eq!(status, 503);
}

#[tokio::test]
async fn model_candidates_require_source_evidence_and_manual_confirmation() {
    let base = spawn(None).await;
    let document = json!({"name":"model.txt", "text":"Наш движок называется Лунная Слива.",
        "model_candidates":[{"value":"Лунная Слива","kind":"CUSTOM","quote":"Наш движок называется Лунная Слива."}]});
    let (status, report) = call(
        "POST",
        format!("{base}/api/v1/ui/discover"),
        Some(json!({"documents":[document.clone()],"remember":true})),
    )
    .await;
    assert_eq!(status, 200, "{report}");
    let candidate = report["suggestions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["value"] == "Лунная Слива")
        .unwrap();
    assert!(candidate["reason"].as_str().unwrap().contains("Xiaomi"));
    let id = candidate["id"].clone();
    let (_, profile) = call("GET", format!("{base}/api/v1/ui/discover/profile"), None).await;
    assert_eq!(profile["labels"], 0);
    let mut forged = document.clone();
    forged["model_candidates"][0]["value"] = json!("выдумка");
    let (status, _) = call(
        "POST",
        format!("{base}/api/v1/ui/discover"),
        Some(json!({"documents":[forged],"remember":true})),
    )
    .await;
    assert_eq!(status, 400);
    let (_, after) = call("GET", format!("{base}/api/v1/ui/discover/profile"), None).await;
    assert_eq!(after, profile);
    let (status, result) = call(
        "POST",
        format!("{base}/api/v1/ui/discover/apply"),
        Some(json!({"saved":true,"ids":[id],"action":"mask"})),
    )
    .await;
    assert_eq!(status, 200, "{result}");
    let (_, profile) = call("GET", format!("{base}/api/v1/ui/discover/profile"), None).await;
    assert_eq!(profile["labels"], 1);
}

#[tokio::test]
async fn audit_disables_only_selected_rule_preserving_restore_and_rejects_stale_revision() {
    let base = spawn(None).await;
    let added = call("POST", format!("{base}/api/v1/rules"), Some(json!({"scope":"personal","rule":{"kind":"PROJ","value":"questions","note":"kaponir discover"}}))).await;
    assert_eq!(added.0, 200);
    let before = mask(&base, "questions", json!({"commit":true})).await;
    assert_ne!(before["text"], "questions");
    let (_, memory) = call(
        "POST",
        format!("{base}/api/v1/anonymize"),
        Some(json!({"text":"questions","session":"audit-chat"})),
    )
    .await;
    assert_ne!(memory["text"], "questions");
    let audit = call("GET", format!("{base}/api/v1/ui/rules/audit"), None)
        .await
        .1;
    let item = &audit["items"][0];
    let change = json!({"scope":item["scope"],"index":item["index"],"revision":item["revision"],"disabled":true});
    assert_eq!(
        call(
            "PUT",
            format!("{base}/api/v1/ui/rules/audit"),
            Some(change.clone())
        )
        .await
        .0,
        200
    );
    assert_eq!(
        mask(&base, "questions", json!({"commit":true})).await["text"],
        "questions"
    );
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/anonymize"),
            Some(json!({"text":"questions","session":"audit-chat"}))
        )
        .await
        .1["text"],
        "questions"
    );
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/deanonymize"),
            Some(json!({"text":memory["text"],"session":"audit-chat"}))
        )
        .await
        .1["text"],
        "questions"
    );
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/anonymize"),
            Some(json!({"text":"questions","session":"audit-chat"}))
        )
        .await
        .1["text"],
        "questions"
    );
    assert_eq!(
        call("PUT", format!("{base}/api/v1/ui/rules/audit"), Some(change))
            .await
            .0,
        409
    );
    let (status, restored) = call(
        "POST",
        format!("{base}/api/v1/ui/restore"),
        Some(json!({"text":before["text"]})),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(restored["text"], "questions");
    let audit = call("GET", format!("{base}/api/v1/ui/rules/audit"), None)
        .await
        .1;
    let item = &audit["items"][0];
    assert_eq!(call("PUT",format!("{base}/api/v1/ui/rules/audit"),Some(json!({"scope":item["scope"],"index":item["index"],"revision":item["revision"],"disabled":false}))).await.0,200);
    assert_ne!(
        mask(&base, "questions", json!({})).await["text"],
        "questions"
    );
}

#[tokio::test]
async fn low_evidence_words_stay_readable_but_exact_rules_still_apply() {
    let base = spawn(None).await;
    let prose = "password: ...\nType::Call namespace::f e:: ::C ::f\nproject character\nproject questions\nпроект или\nmodel gemini-3.8-flash\nSee s1.md\n/tmp/randomunknownword/file.txt";
    assert_eq!(mask(&base, prose, json!({})).await["text"], prose);
    let secret = "password: Cedar-47!Plum";
    assert!(
        !mask(&base, secret, json!({})).await["text"]
            .as_str()
            .unwrap()
            .contains("Cedar-47!Plum")
    );
}

#[tokio::test]
async fn legacy_session_memory_can_be_excluded_without_rules_or_deleting_tokens() {
    let base = spawn(None).await;
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/rules"),
            Some(json!({"scope":"personal","rule":{"kind":"PROJ","value":"questions"}}))
        )
        .await
        .0,
        200
    );
    let original = call(
        "POST",
        format!("{base}/api/v1/anonymize"),
        Some(json!({"text":"questions","session":"legacy-memory"})),
    )
    .await
    .1;
    assert_eq!(
        call("DELETE", format!("{base}/api/v1/rules/personal/0"), None)
            .await
            .0,
        200
    );
    assert_ne!(
        call(
            "POST",
            format!("{base}/api/v1/anonymize"),
            Some(json!({"text":"questions","session":"legacy-memory"}))
        )
        .await
        .1["text"],
        "questions"
    );
    let audit = call("GET", format!("{base}/api/v1/ui/rules/audit"), None)
        .await
        .1;
    let memory = audit["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["scope"] == "memory")
        .unwrap();
    assert_eq!(
        call(
            "PUT",
            format!("{base}/api/v1/ui/rules/audit/memory"),
            Some(json!({"ids":[memory["index"]],"revision":memory["revision"],"disabled":true}))
        )
        .await
        .0,
        200
    );
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/anonymize"),
            Some(json!({"text":"questions","session":"legacy-memory"}))
        )
        .await
        .1["text"],
        "questions"
    );
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/deanonymize"),
            Some(json!({"text":original["text"],"session":"legacy-memory"}))
        )
        .await
        .1["text"],
        "questions"
    );
    assert_eq!(
        call(
            "POST",
            format!("{base}/api/v1/anonymize"),
            Some(json!({"text":"questions","session":"legacy-memory"}))
        )
        .await
        .1["text"],
        "questions"
    );
}
