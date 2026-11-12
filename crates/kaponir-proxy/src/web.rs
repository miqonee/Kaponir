//! Web UI: встроенная страница (без сборки фронтенда, файлы вшиты в бинарник) и её API.
//!
//! - `/ui/` — split view с click-to-mask, восстановление, разбор журнала, правила контуров;
//! - `/api/v1/ui/mask` — анализ без сохранения (живой предпросмотр) или с записью в vault;
//! - `/api/v1/journal*`, `/api/v1/rules*` — решения и правила; после изменения правил движок
//!   пересобирается без перезапуска сервера.
//!
//! Доступ тот же, что у sidecar REST: `Authorization: Bearer`, если задан `api_token`.

use axum::Router;
use axum::extract::{Json, Path, Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Redirect, Response};
use axum::routing::{delete, get, post, put};
use kaponir_core::rules::{Rule, RuleAction};
use kaponir_core::{Escape, Format, Input, Known, MaskOptions, Scope, Strategy, TokenForm};
use kaponir_vault::JournalRow;
use serde::Deserialize;
use serde_json::json;

use crate::Shared;
use crate::server::{ApiError, authorized, error, internal};

const INDEX: &str = include_str!("../assets/index.html");
const APP_JS: &str = include_str!("../assets/app.js");
const APP_CSS: &str = include_str!("../assets/app.css");

pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/", get(|| async { Redirect::temporary("/ui/") }))
        .route("/ui", get(|| async { Redirect::permanent("/ui/") }))
        .route(
            "/ui/",
            get(|| async { asset(INDEX, "text/html; charset=utf-8") }),
        )
        .route(
            "/ui/app.js",
            get(|| async { asset(APP_JS, "text/javascript; charset=utf-8") }),
        )
        .route(
            "/ui/app.css",
            get(|| async { asset(APP_CSS, "text/css; charset=utf-8") }),
        )
        .route(
            "/ui/i18n.js",
            get(|| async {
                asset(
                    include_str!("../assets/i18n.js"),
                    "text/javascript; charset=utf-8",
                )
            }),
        )
        .route(
            "/ui/panel.js",
            get(|| async {
                asset(
                    include_str!("../assets/panel.js"),
                    "text/javascript; charset=utf-8",
                )
            }),
        )
        .route(
            "/ui/s1.js",
            get(|| async {
                asset(
                    include_str!("../assets/s1.js"),
                    "text/javascript; charset=utf-8",
                )
            }),
        )
        .route(
            "/ui/locales/ru.json",
            get(|| async {
                asset(
                    include_str!("../assets/locales/ru.json"),
                    "application/json; charset=utf-8",
                )
            }),
        )
        .route(
            "/ui/locales/en.json",
            get(|| async {
                asset(
                    include_str!("../assets/locales/en.json"),
                    "application/json; charset=utf-8",
                )
            }),
        )
        .route(
            "/ui/providers.js",
            get(|| async {
                asset(
                    include_str!("../assets/providers.js"),
                    "text/javascript; charset=utf-8",
                )
            }),
        )
        .route(
            "/ui/worker.js",
            get(|| async {
                asset(
                    include_str!("../assets/worker.js"),
                    "text/javascript; charset=utf-8",
                )
            }),
        )
        .route("/api/v1/ui/info", get(info))
        .route(
            "/ui/help.js",
            get(|| async {
                asset(
                    include_str!("../assets/help.js"),
                    "text/javascript; charset=utf-8",
                )
            }),
        )
        .route(
            "/ui/review.js",
            get(|| async {
                asset(
                    include_str!("../assets/review.js"),
                    "text/javascript; charset=utf-8",
                )
            }),
        )
        .route("/api/v1/ui/events", get(ui_events))
        .route("/api/v1/ui/events/{id}", get(ui_event_by_id))
        .route(
            "/api/v1/ui/rules/audit",
            get(audit_rules).put(update_audit_rule),
        )
        .route("/api/v1/ui/rules/audit/memory", put(update_memory_audit))
        .route("/api/v1/ui/config/export", post(export_settings))
        .route("/api/v1/ui/config/import", post(import_settings))
        .route("/api/v1/ui/s1", get(get_s1).put(put_s1))
        .route("/api/v1/ui/setup", post(setup))
        .route("/api/v1/ui/mask", post(mask))
        .route("/api/v1/ui/restore", post(restore))
        .route("/api/v1/ui/discover", post(discover))
        .route("/api/v1/ui/discover/sessions", post(discover_sessions))
        .route("/api/v1/ui/sessions/export", get(export_sessions))
        .route("/api/v1/ui/discover/profile", get(discovery_profile))
        .route("/api/v1/ui/discover/apply", post(apply_discover))
        .route("/api/v1/journal", get(journal))
        .route("/api/v1/journal/decide", post(decide))
        .route("/api/v1/journal/auto-review", post(auto_review))
        .route("/api/v1/ui/journal/auto-review", post(auto_review))
        .route("/api/v1/journal/template", post(template))
        .route("/api/v1/rules", get(rules).post(add_rule))
        .route("/api/v1/rules/{scope}/{index}", delete(remove_rule))
        .route("/api/v1/rules/{scope}/domains", put(set_domains))
}

fn asset(body: &'static str, content_type: &'static str) -> Response {
    let mut r = body.into_response();
    let h = r.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    h.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; \
             img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        ),
    );
    r
}

type ApiResult = Result<Response, ApiError>;

fn control(st: &Shared) -> Result<&dyn crate::Control, ApiError> {
    st.control.as_deref().ok_or_else(|| {
        error(
            StatusCode::NOT_IMPLEMENTED,
            "rules are read-only on this server",
        )
    })
}

/// Пересобирает движок после изменения правил.
fn reload(st: &Shared) -> Result<(), ApiError> {
    let engine = control(st)?.build_engine().map_err(internal)?;
    st.replace_engine(engine);
    Ok(())
}

fn bad(e: impl std::fmt::Display) -> ApiError {
    error(StatusCode::BAD_REQUEST, e.to_string())
}

#[derive(Deserialize)]
struct ExportSettings {
    #[serde(default)]
    include_secrets: bool,
}

async fn export_settings(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<ExportSettings>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let bundle = control(&st)?
        .export_settings(req.include_secrets)
        .map_err(internal)?;
    let mut response = Json(bundle).into_response();
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(response)
}

async fn import_settings(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(bundle): Json<serde_json::Value>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    // Serialize against model refresh and provider writes while replacing the registry.
    let _refresh = st.provider_refresh.lock().await;
    st.with_vault(|_, _| {
        control(&st)
            .map_err(|_| anyhow::anyhow!("settings unavailable"))?
            .import_settings(bundle)
    })
    .map_err(bad)?;
    reload(&st)?;
    st.provider_affinity
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .clear();
    Ok(Json(json!({"imported": true, "restart_required": true})).into_response())
}

// ---------- info ----------

async fn info(State(st): State<Shared>, headers: HeaderMap) -> ApiResult {
    authorized(&st, &headers, true)?;
    let engine = st.engine();
    let openai = crate::providers::saved(&st, "openai")?;
    let anthropic = crate::providers::saved(&st, "anthropic")?;
    let registry = crate::providers::registry(&st)?;
    let provider_configured = registry.connections.iter().any(|p| p.enabled)
        || (registry.managed.is_empty()
            && (st.cfg.openai_upstream != crate::ProxyConfig::default().openai_upstream
                || st.cfg.anthropic_upstream != crate::ProxyConfig::default().anthropic_upstream
                || st
                    .traffic
                    .summary_snapshot()
                    .events
                    .iter()
                    .any(|e| e.sent && e.status < 400 && e.status >= 200)));
    let (stats, learning_pending) = st
        .with_vault(|_, v| Ok((v.stats()?, v.app_record_count("learning-entry-pending")?)))
        .map_err(internal)?;
    let (rules_count, corp_domains_count) = control(&st)
        .ok()
        .and_then(|ctl| ctl.rules().ok())
        .map_or((0usize, 0usize), |sets| {
            let r = sets
                .iter()
                .map(|s| s.file.rules.iter().filter(|r| !r.disabled).count())
                .sum();
            let d = sets.iter().map(|s| s.file.corp.domains.len()).sum();
            (r, d)
        });
    let uncertain_policy = match engine.policy().uncertain {
        kaponir_core::UncertainAction::Mask => "mask",
        kaponir_core::UncertainAction::Keep => "keep",
    };
    let packs: Vec<_> = engine
        .packs()
        .iter()
        .map(|p| {
            let i = p.info();
            json!({"id": i.id, "kind": i.kind, "version": i.version, "description": i.description})
        })
        .collect();
    let m = engine.metrics();
    Ok(Json(json!({
        "version": env!("CARGO_PKG_VERSION"),
        "owner": st.owner(),
        "prefix": engine.codec().prefix(),
        "packs": packs,
        "editable": st.control.is_some(),
        "s1_mode": engine.s1_mode(),
        "uncertain_policy": uncertain_policy,
        "rules_count": rules_count,
        "provider_configured": provider_configured,
        "corp_domains_count": corp_domains_count,
        "unique_entities": stats.entries,
        "review_pending": (stats.journal_open.max(0) as usize).saturating_add(learning_pending),
        "api_token_required": st.cfg.api_token.is_some(),
        "upstreams": {
            "openai": reqwest::Url::parse(openai.as_ref().map_or(st.cfg.openai_upstream.as_str(), |p| p.base_url.as_str())).ok().and_then(|u| u.host_str().map(str::to_owned)),
            "anthropic": reqwest::Url::parse(anthropic.as_ref().map_or(st.cfg.anthropic_upstream.as_str(), |p| p.base_url.as_str())).ok().and_then(|u| u.host_str().map(str::to_owned)),
        },
        "stats": stats,
        "latency": {
            "mask": m.mask.snapshot(),
            "restore": m.restore.snapshot(),
            "s1": m.s1.snapshot(),
            "http": st.requests.snapshot(),
        },
    }))
    .into_response())
}

// ---------- events & request inspector ----------

async fn ui_events(State(st): State<Shared>, headers: HeaderMap) -> ApiResult {
    authorized(&st, &headers, true)?;
    Ok(Json(crate::review::traffic_summary(&st)).into_response())
}

async fn ui_event_by_id(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<u64>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    if let Some(ev) = st.traffic.event_by_id(id) {
        return Ok(Json(crate::review::event_detail(&st, ev)).into_response());
    }
    let from_vault: Option<crate::traffic::Event> = st
        .with_vault(|_, v| {
            for raw in v.load_traffic_events(200)? {
                if let Ok(ev) = serde_json::from_slice::<crate::traffic::Event>(&raw)
                    && ev.id == id
                {
                    return Ok(Some(ev));
                }
            }
            Ok(None)
        })
        .map_err(internal)?;
    match from_vault {
        Some(ev) => Ok(Json(crate::review::event_detail(&st, ev)).into_response()),
        None => Err(error(
            StatusCode::NOT_FOUND,
            format!("event #{id} not found"),
        )),
    }
}

// ---------- S1 ----------

const S1_TOKEN_SETTING: &str = "s1:http-token";

#[derive(Deserialize)]
struct S1Update {
    settings: kaponir_s1::Settings,
    /// Новый ключ. Пустое значение оставляет сохранённый ключ без изменений.
    key: Option<String>,
    #[serde(default)]
    clear_key: bool,
}

fn saved_s1_key(st: &Shared) -> Result<Option<String>, ApiError> {
    st.with_vault(|_, vault| {
        vault
            .private_setting(S1_TOKEN_SETTING)?
            .map(String::from_utf8)
            .transpose()
            .map_err(|_| anyhow::anyhow!("S1 token is not UTF-8"))
    })
    .map_err(internal)
}

fn store_s1_key(st: &Shared, key: Option<&str>) -> Result<(), ApiError> {
    st.with_vault(|_, vault| {
        if let Some(key) = key {
            vault.set_private_setting(S1_TOKEN_SETTING, key.as_bytes())?;
        } else {
            vault.delete_private_setting(S1_TOKEN_SETTING)?;
        }
        Ok(())
    })
    .map_err(internal)
}

async fn get_s1(State(st): State<Shared>, headers: HeaderMap) -> ApiResult {
    authorized(&st, &headers, true)?;
    let settings = control(&st)?.s1_settings().map_err(internal)?;
    let local_model_available = control(&st)?.s1_local_model_available().map_err(internal)?;
    let key_saved = saved_s1_key(&st)?.is_some();
    Ok(Json(json!({
        "settings": settings,
        "key_saved": key_saved,
        "local_model_available": local_model_available,
        "editable": st.control.is_some(),
        "mode": st.engine().s1_mode(),
    }))
    .into_response())
}

async fn put_s1(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<S1Update>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let previous = control(&st)?.s1_settings().map_err(internal)?;
    let previous_key = saved_s1_key(&st)?;
    let new_key = req
        .key
        .as_deref()
        .map(str::trim)
        .filter(|key| !key.is_empty());
    if req.clear_key && new_key.is_some() {
        return Err(bad("provide either a new S1 key or clear_key, not both"));
    }
    let validation_key = if req.clear_key {
        None
    } else {
        new_key.map(str::to_owned).or(previous_key.clone())
    };
    control(&st)?
        .set_s1(&req.settings, validation_key.as_deref())
        .map_err(bad)?;
    let effective_key = if req.clear_key {
        None
    } else {
        new_key.or(previous_key.as_deref())
    };
    if let Err(err) = store_s1_key(&st, effective_key) {
        let _ = control(&st)?.set_s1(&previous, previous_key.as_deref());
        return Err(err);
    }
    if let Err(err) = reload(&st) {
        let _ = control(&st)?.set_s1(&previous, previous_key.as_deref());
        let _ = store_s1_key(&st, previous_key.as_deref());
        let _ = reload(&st);
        return Err(err);
    }
    get_s1(State(st), headers).await
}

// ---------- mask / restore ----------

#[derive(Deserialize)]
struct AdHoc {
    value: String,
    kind: String,
}

#[derive(Deserialize)]
struct MaskReq {
    text: String,
    #[serde(default)]
    format: Format,
    #[serde(default)]
    scope: Scope,
    strategy: Option<Strategy>,
    /// «Только в этом тексте»: значения, выделенные мышкой, без правила.
    #[serde(default)]
    adhoc: Vec<AdHoc>,
    /// Записать токены в vault и спорные решения в журнал. Без него — предпросмотр,
    /// транзакция откатывается.
    #[serde(default)]
    commit: bool,
}

/// Значение «только в этом тексте» маскируется как значение из памяти сессии (форма токена
/// выбирается ядром по месту вхождения).
fn adhoc_known(a: &AdHoc, scope: Scope, strategy: Option<Strategy>) -> Option<Known> {
    let value = a.value.trim();
    let kind: String = a
        .kind
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect::<String>()
        .to_ascii_uppercase();
    if value.is_empty() || kind.is_empty() {
        return None;
    }
    Some(Known {
        text: value.to_string(),
        kind,
        canonical: value.to_string(),
        scope,
        strategy: strategy.unwrap_or(Strategy::Variable),
        form: TokenForm::Snake,
    })
}

async fn mask(State(st): State<Shared>, headers: HeaderMap, Json(req): Json<MaskReq>) -> ApiResult {
    authorized(&st, &headers, true)?;
    let known: Vec<Known> = req
        .adhoc
        .iter()
        .filter_map(|a| adhoc_known(a, req.scope, req.strategy))
        .collect();
    let opts = MaskOptions {
        scope: req.scope,
        owner: st.owner().to_string(),
        irreversible: false,
        strategy: req.strategy,
        journal: req.commit,
        explain: true,
        known,
    };
    let m = st
        .with_vault(|engine, vault| {
            vault.begin()?;
            match engine.mask(
                &Input {
                    text: &req.text,
                    format: req.format,
                },
                &opts,
                vault,
            ) {
                Ok(m) => {
                    if req.commit {
                        vault.commit()?
                    } else {
                        vault.rollback()?
                    }
                    Ok(m)
                }
                Err(e) => {
                    let _ = vault.rollback();
                    Err(e.into())
                }
            }
        })
        .map_err(internal)?;
    Ok(Json(json!({
        "text": m.text,
        "replacements": m.replacements,
        "explain": m.explain,
        "uncertain": m.uncertain,
        "literals": m.literals,
        "committed": req.commit,
    }))
    .into_response())
}

#[derive(Deserialize)]
struct RestoreReq {
    text: String,
    #[serde(default)]
    json_escape: bool,
}

async fn restore(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<RestoreReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let escape = if req.json_escape {
        Escape::Json
    } else {
        Escape::None
    };
    let r = st
        .with_vault(|engine, vault| {
            Ok(engine.restore(&req.text, &st.principal, &*vault, escape)?)
        })
        .map_err(internal)?;
    Ok(Json(
        json!({"text": r.text, "restored": r.restored, "unknown": r.unknown, "denied": r.denied}),
    )
    .into_response())
}

// ---------- discover ----------

#[derive(Deserialize)]
struct DiscoverReq {
    #[serde(default)]
    documents: Vec<kaponir_core::discover::Document>,
    #[serde(default)]
    use_s1: bool,
    #[serde(default)]
    remember: bool,
}

#[derive(Clone, Copy, Default, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum DiscoverAction {
    #[default]
    Mask,
    NotSensitive,
}

#[derive(Deserialize)]
struct ApplyDiscoverReq {
    #[serde(default)]
    documents: Vec<kaponir_core::discover::Document>,
    #[serde(default)]
    ids: Vec<String>,
    #[serde(default)]
    scope: Scope,
    #[serde(default)]
    saved: bool,
    #[serde(default)]
    action: DiscoverAction,
}

async fn discovery_profile(State(st): State<Shared>, headers: HeaderMap) -> ApiResult {
    authorized(&st, &headers, true)?;
    let profile = st
        .with_vault(|_, v| crate::discovery::load(v))
        .map_err(internal)?;
    Ok(Json(json!({"report":profile.report(), "labels":profile.feedback.len()})).into_response())
}

async fn discover(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<DiscoverReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    kaponir_core::discover::validate_documents(&req.documents).map_err(bad)?;
    let engine = st.engine();
    let mut report = tokio::task::spawn_blocking(move || {
        kaponir_core::discover::analyze_with_options(
            &engine,
            &req.documents,
            &kaponir_core::discover::Options { use_s1: req.use_s1 },
        )
    })
    .await
    .map_err(|e| internal(e.into()))?;
    if req.remember {
        report = st
            .with_vault(|_, v| {
                crate::discovery::update(v, |profile| {
                    profile.merge(&report).map_err(anyhow::Error::msg)?;
                    let mut combined = profile.report();
                    combined.s1_status = report.s1_status;
                    combined.s1_scored = report.s1_scored;
                    combined.truncated = report.truncated;
                    Ok(combined)
                })
            })
            .map_err(bad)?;
    }
    Ok(Json(report).into_response())
}

async fn apply_discover(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<ApplyDiscoverReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    kaponir_core::discover::validate_documents(&req.documents).map_err(bad)?;
    // Client supplies IDs, never values, scores or training labels. Applying does not call S1.
    let fresh = (!req.saved).then(|| kaponir_core::discover::analyze(&st.engine(), &req.documents));
    let applied = st
        .with_vault(|_, v| {
            crate::discovery::update(v, |profile| {
                let report = fresh.as_ref().cloned().unwrap_or_else(|| profile.report());
                let rules = kaponir_core::discover::selected_rules(&report, &req.ids)
                    .map_err(anyhow::Error::msg)?;
                profile
                    .decide(&report, &req.ids, req.action == DiscoverAction::Mask)
                    .map_err(anyhow::Error::msg)?;
                if req.action == DiscoverAction::Mask {
                    let ctl = control(&st).map_err(|_| anyhow::anyhow!("rules are read-only"))?;
                    for rule in &rules {
                        ctl.add_rule(req.scope, rule.clone())?;
                    }
                }
                Ok(rules.len())
            })
        })
        .map_err(bad)?;
    if applied > 0 && req.action == DiscoverAction::Mask {
        if let Ok(ctl) = control(&st) {
            let _ = ctl.auto_train_s1();
        }
        reload(&st)?;
    }
    let report = if req.saved {
        st.with_vault(|_, v| Ok(crate::discovery::load(v)?.report()))
            .map_err(internal)?
    } else {
        let mut report = kaponir_core::discover::analyze(&st.engine(), &req.documents);
        report.suggestions.retain(|s| !req.ids.contains(&s.id));
        report
    };
    Ok(Json(json!({"applied":applied,"scope":req.scope,"report":report})).into_response())
}

#[derive(Deserialize, Default)]
struct DiscoverSessionsReq {
    #[serde(default = "default_true")]
    use_s1: bool,
    #[serde(default = "default_true")]
    remember: bool,
}

fn default_true() -> bool {
    true
}

/// Export each retained session once, omitting histories already present in a
/// later request. Legacy events without correlation remain separate documents.
fn session_documents(st: &Shared) -> (Vec<kaponir_core::discover::Document>, bool) {
    let mut groups = std::collections::BTreeMap::<String, Vec<String>>::new();
    for ev in st.traffic.snapshot().events {
        if let Some(insp) = ev.inspector {
            let key = ev
                .session_id
                .unwrap_or_else(|| format!("request-{}", ev.id));
            let parts = groups.entry(key).or_default();
            for text in [insp.original_request, insp.restored_response] {
                if text.trim().is_empty() || parts.iter().any(|p| p.contains(&text)) {
                    continue;
                }
                parts.retain(|p| !text.contains(p.as_str()));
                parts.push(text);
            }
        }
    }
    let mut documents = Vec::new();
    let mut bytes = 0;
    let mut truncated = false;
    for (key, parts) in groups {
        let text = parts.join("\n\n");
        if documents.len() >= kaponir_core::discover::MAX_DOCUMENTS
            || bytes + text.len() > kaponir_core::discover::MAX_BYTES
        {
            truncated = true;
            continue;
        }
        bytes += text.len();
        documents.push(kaponir_core::discover::Document {
            name: format!("session-{key}"),
            text,
            format: Format::Auto,
            model_candidates: Vec::new(),
        });
    }
    (documents, truncated)
}
async fn export_sessions(State(st): State<Shared>, headers: HeaderMap) -> ApiResult {
    authorized(&st, &headers, true)?;
    let (documents, truncated) = session_documents(&st);
    Ok((
        [(header::CACHE_CONTROL, "no-store")],
        Json(json!({"documents":documents,"truncated":truncated})),
    )
        .into_response())
}

async fn discover_sessions(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<DiscoverSessionsReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let (documents, truncated) = session_documents(&st);
    let docs_count = documents.len();
    if documents.is_empty() {
        let report = st
            .with_vault(|_, v| Ok(crate::discovery::load(v)?.report()))
            .map_err(internal)?;
        return Ok(
            Json(json!({"documents_analyzed": 0, "sessions_analyzed":0,"report": report}))
                .into_response(),
        );
    }
    let engine = st.engine();
    let use_s1 = req.use_s1;
    let mut report = tokio::task::spawn_blocking(move || {
        kaponir_core::discover::analyze_with_options(
            &engine,
            &documents,
            &kaponir_core::discover::Options { use_s1 },
        )
    })
    .await
    .map_err(|e| internal(e.into()))?;
    report.truncated |= truncated;
    if req.remember {
        report = st
            .with_vault(|_, v| {
                crate::discovery::update(v, |profile| {
                    profile.merge(&report).map_err(anyhow::Error::msg)?;
                    let mut combined = profile.report();
                    combined.s1_status = report.s1_status;
                    combined.s1_scored = report.s1_scored;
                    combined.truncated = report.truncated;
                    Ok(combined)
                })
            })
            .map_err(bad)?;
    }
    Ok(Json(
        json!({"documents_analyzed": docs_count, "sessions_analyzed":docs_count,"report": report}),
    )
    .into_response())
}

// ---------- journal ----------

#[derive(Deserialize)]
struct JournalQuery {
    /// open | masked | kept | dismissed | all
    status: Option<String>,
    limit: Option<usize>,
}

async fn journal(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(q): Query<JournalQuery>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let status = q.status.unwrap_or_else(|| "open".into());
    let filter = (status != "all").then_some(status.as_str());
    let rows = st
        .with_vault(|_, v| Ok(v.journal(filter, q.limit.unwrap_or(500))?))
        .map_err(internal)?;
    Ok(Json(rows).into_response())
}

#[derive(Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum Decision {
    Mask,
    Keep,
    Dismiss,
}

#[derive(Deserialize)]
struct DecideReq {
    action: Decision,
    #[serde(default)]
    ids: Vec<i64>,
    /// Все открытые записи с этой формой (и типом, если задан `of_kind`).
    shape: Option<String>,
    of_kind: Option<String>,
    /// Тип токена для «маскировать» (по умолчанию — найденный детектором).
    kind: Option<String>,
    #[serde(default)]
    scope: Scope,
}

fn selected(rows: Vec<JournalRow>, req: &DecideReq) -> Vec<JournalRow> {
    rows.into_iter()
        .filter(|r| {
            req.ids.contains(&r.id)
                || (r.status == "open"
                    && req.shape.as_ref().is_some_and(|s| *s == r.shape)
                    && req.of_kind.as_ref().is_none_or(|k| *k == r.kind))
        })
        .collect()
}

async fn decide(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<DecideReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    if req.ids.is_empty() && req.shape.is_none() {
        return Err(bad("specify journal ids or shape"));
    }
    let rows = st
        .with_vault(|_, v| Ok(v.journal(None, usize::MAX >> 1)?))
        .map_err(internal)?;
    let rows = selected(rows, &req);
    if rows.is_empty() {
        return Err(error(StatusCode::NOT_FOUND, "no matching journal entries"));
    }
    let (status, rules_changed) = match req.action {
        Decision::Mask | Decision::Keep => {
            let ctl = control(&st)?;
            // Одно правило на значение: одинаковые значения в разных записях не дублируются.
            let mut seen = std::collections::HashSet::new();
            for r in &rows {
                if seen.insert(r.value.clone()) {
                    let rule = if req.action == Decision::Mask {
                        r.mask_rule(req.kind.as_deref())
                    } else {
                        r.keep_rule()
                    };
                    ctl.add_rule(req.scope, rule).map_err(bad)?;
                }
            }
            (
                if req.action == Decision::Mask {
                    "masked"
                } else {
                    "kept"
                },
                true,
            )
        }
        Decision::Dismiss => ("dismissed", false),
    };
    st.with_vault(|_, v| {
        for r in &rows {
            v.set_journal_status(r.id, status)?;
        }
        Ok(())
    })
    .map_err(internal)?;
    if rules_changed {
        if let Ok(ctl) = control(&st) {
            let _ = ctl.auto_train_s1();
        }
        reload(&st)?;
    }
    Ok(Json(json!({"closed": rows.len(), "status": status})).into_response())
}

#[derive(Deserialize, Default)]
struct AutoReviewReq {
    #[serde(default)]
    ids: Vec<i64>,
    #[serde(default)]
    auto_save: bool,
    #[serde(default = "default_auto_save_threshold")]
    auto_save_threshold: f32,
}

fn default_auto_save_threshold() -> f32 {
    0.85
}

async fn auto_review(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<AutoReviewReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    let rows = st
        .with_vault(|_, v| Ok(v.journal(Some("open"), 500)?))
        .map_err(internal)?;
    let rows: Vec<JournalRow> = if req.ids.is_empty() {
        rows
    } else {
        rows.into_iter()
            .filter(|r| req.ids.contains(&r.id))
            .collect()
    };
    if rows.is_empty() {
        return Ok(Json(json!({"items": [], "auto_saved": 0, "total": 0})).into_response());
    }
    let engine = st.engine();
    let rows_for_cls = rows.clone();
    let classifications = tokio::task::spawn_blocking(move || {
        use kaponir_core::s1::{CandidateView, Disclosure, features};
        let lex = engine.lexicon();
        let views: Vec<CandidateView> = rows_for_cls
            .iter()
            .map(|r| {
                let cat = serde_json::from_value(json!(r.category))
                    .unwrap_or(kaponir_core::Category::Custom);
                let disc = engine
                    .s1_config()
                    .map_or(Disclosure::L0, |s| s.provider.max_disclosure());
                let disc = if cat == kaponir_core::Category::Credential {
                    Disclosure::L0
                } else {
                    disc
                };
                CandidateView {
                    features: features(&r.value, lex.is_word(&r.value)),
                    kind: r.kind.clone(),
                    category: cat,
                    recognizer: r.recognizer.clone(),
                    surface: (disc >= Disclosure::L1).then(|| r.value.clone()),
                    context: (disc >= Disclosure::L2).then(|| r.context.clone()),
                }
            })
            .collect();
        let s1 = engine
            .s1_config()
            .ok_or_else(|| anyhow::anyhow!("categorization_disabled"))?;
        let result = s1
            .provider
            .classify(&views, &kaponir_core::s1::disclosure_profile(&s1.profile))
            .map_err(|_| anyhow::anyhow!("categorization_failed"))?;
        anyhow::ensure!(
            result.len() == rows_for_cls.len(),
            "invalid_classification_response"
        );
        Ok::<_, anyhow::Error>(result)
    })
    .await
    .map_err(|e| internal(e.into()))?
    .map_err(bad)?;

    let mut items = Vec::with_capacity(rows.len());
    let mut auto_saved = 0usize;
    let mut seen_values = std::collections::HashSet::new();
    let threshold = req.auto_save_threshold.clamp(0.5, 0.99);
    let ctl = control(&st).ok();

    for (r, cls) in rows.iter().zip(classifications) {
        let mut saved_this = false;
        if req.auto_save
            && cls.source != "heuristic"
            && cls.confidence.is_finite()
            && cls.confidence > threshold
            && let Some(ctl) = ctl
        {
            if seen_values.insert((cls.scope, r.value.clone())) {
                let rule = if cls.action == RuleAction::Mask {
                    r.mask_rule(Some(&cls.kind))
                } else {
                    r.keep_rule()
                };
                if ctl.add_rule(cls.scope, rule).is_ok() {
                    saved_this = true;
                }
            } else {
                saved_this = true;
            }
            if saved_this {
                let status = if cls.action == RuleAction::Mask {
                    "masked"
                } else {
                    "kept"
                };
                let _ = st.with_vault(|_, v| Ok(v.set_journal_status(r.id, status)?));
                auto_saved += 1;
            }
        }
        items.push(json!({
            "id": r.id,
            "value": r.value,
            "sensitive": (cls.action == RuleAction::Mask),
            "action": if cls.action == RuleAction::Mask { "mask" } else { "keep" },
            "kind": cls.kind,
            "scope": cls.scope,
            "confidence": cls.confidence,
            "auto_saved": saved_this,
        }));
    }
    if auto_saved > 0 {
        if let Some(ctl) = ctl {
            let _ = ctl.auto_train_s1();
        }
        reload(&st)?;
    }
    Ok(Json(json!({
        "items": items,
        "auto_saved": auto_saved,
        "total": rows.len(),
        "reviewed": rows.len(),
    }))
    .into_response())
}

#[derive(Deserialize)]
struct TemplateReq {
    template: String,
    #[serde(default)]
    scope: Scope,
}

async fn template(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<TemplateReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    let ctl = control(&st)?;
    let (re, _) = kaponir_recognizers::rules::compile_template(&req.template)
        .map_err(|e| bad(format!("template: {e}")))?;
    let re = regex::RegexBuilder::new(&format!("^(?:{re})$"))
        .case_insensitive(true)
        .build()
        .map_err(bad)?;
    let rule = Rule {
        template: Some(req.template.clone()),
        note: "kaponir ui template".into(),
        ..Rule::default()
    };
    ctl.add_rule(req.scope, rule).map_err(bad)?;
    let closed = st
        .with_vault(|_, v| {
            let mut n = 0;
            for r in v.journal(Some("open"), usize::MAX >> 1)? {
                if r.covered_by(|w| re.is_match(w)) {
                    v.set_journal_status(r.id, "masked")?;
                    n += 1;
                }
            }
            Ok(n)
        })
        .map_err(internal)?;
    reload(&st)?;
    Ok(Json(json!({"closed": closed})).into_response())
}

// ---------- rules ----------

fn memory_audit_items(st: &Shared) -> anyhow::Result<(Vec<serde_json::Value>, String)> {
    let (values, ignored) = st.with_vault(|_, v| {
        let ignored: Vec<(String, String)> = v
            .private_setting(crate::state::MEMORY_AUDIT_SETTING)?
            .map(|b| serde_json::from_slice(&b))
            .transpose()?
            .unwrap_or_default();
        Ok((v.session_values(10_000)?, ignored))
    })?;
    let mut candidates = std::collections::BTreeMap::new();
    for value in values {
        if kaponir_core::discover::weak_business_value(&value.kind, &value.text)
            || kaponir_core::kinds::type_conflict(&value.kind, &value.text)
            || (value.kind == "DOM" && value.text.chars().count() <= 3)
            || (value.kind == "IP"
                && (value.text.parse::<std::net::IpAddr>().is_err()
                    || value.text.replace(':', "").len() < 2))
        {
            candidates.insert((value.kind, value.text.to_lowercase()), (value.text, false));
        }
    }
    for (kind, value) in ignored {
        candidates.insert((kind, value.to_lowercase()), (value, true));
    }
    let mut items: Vec<serde_json::Value> = candidates.into_iter().map(|((kind,_),(value,disabled))| {
        let id = blake3::hash(format!("{kind}\0{}", value.to_lowercase()).as_bytes()).to_hex().to_string();
        json!({"scope":"memory","index":id,"rule":{"kind":kind,"value":value,"disabled":disabled,"note":"Память сессии · источник не сохранён"},"reason":"session_memory"})
    }).collect();
    let revision = blake3::hash(&serde_json::to_vec(&items)?)
        .to_hex()
        .to_string();
    for item in &mut items {
        item["revision"] = json!(revision);
    }
    Ok((items, revision))
}
#[derive(Deserialize)]
struct MemoryAuditReq {
    ids: Vec<String>,
    revision: String,
    disabled: bool,
}
async fn update_memory_audit(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<MemoryAuditReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    control(&st)?;
    let _rules = st.rule_changes.lock().await;
    let (items, revision) = memory_audit_items(&st).map_err(internal)?;
    if req.ids.is_empty() || req.ids.len() > 10_000 {
        return Err(bad("select at least one memory value"));
    }
    if req.revision != revision
        || req
            .ids
            .iter()
            .any(|id| !items.iter().any(|i| i["index"] == *id))
    {
        return Err(error(
            StatusCode::CONFLICT,
            "memory changed; refresh the audit",
        ));
    }
    st.with_vault(|_, v| {
        let mut ignored: Vec<(String, String)> = v
            .private_setting(crate::state::MEMORY_AUDIT_SETTING)?
            .map(|b| serde_json::from_slice(&b))
            .transpose()?
            .unwrap_or_default();
        for item in items
            .iter()
            .filter(|i| req.ids.iter().any(|id| i["index"] == *id))
        {
            let kind = item["rule"]["kind"].as_str().unwrap_or_default();
            let value = item["rule"]["value"].as_str().unwrap_or_default();
            ignored.retain(|(k, x)| k != kind || !x.eq_ignore_ascii_case(value));
            if req.disabled {
                ignored.push((kind.to_string(), value.to_string()));
            }
        }
        v.set_private_setting(
            crate::state::MEMORY_AUDIT_SETTING,
            &serde_json::to_vec(&ignored)?,
        )?;
        Ok(())
    })
    .map_err(internal)?;
    st.refresh_suppressed();
    drop(_rules);
    audit_rules(State(st), headers).await
}

async fn audit_rules(State(st): State<Shared>, headers: HeaderMap) -> ApiResult {
    authorized(&st, &headers, true)?;
    let mut items = Vec::new();
    for set in control(&st)?.rules().map_err(internal)? {
        let revision =
            blake3::hash(&serde_json::to_vec(&set.file).map_err(|e| internal(e.into()))?)
                .to_hex()
                .to_string();
        for (index, rule) in set.file.rules.iter().enumerate() {
            let value = rule.value.as_deref().unwrap_or_default();
            let reason = if rule.disabled {
                "disabled"
            } else if kaponir_core::kinds::type_conflict(&rule.kind, value) {
                "type_format_conflict"
            } else if matches!(rule.kind.as_str(), "FNAME" | "LNAME")
                && [
                    "thu", "nov", "mon", "tue", "wed", "fri", "sat", "sun", "jan", "feb", "mar",
                    "apr", "sep", "oct", "dec",
                ]
                .contains(&value.to_lowercase().as_str())
            {
                "ambiguous_calendar_name"
            } else if rule.kind == "PROJ" && rule.note.contains("paths") {
                "unconfirmed_path_project"
            } else if kaponir_core::discover::weak_business_value(&rule.kind, value)
                && rule.value.is_some()
            {
                "weak_value"
            } else if rule.kind == "IP"
                && (value.parse::<std::net::IpAddr>().is_err() || value.replace(':', "").len() < 2)
                && rule.value.is_some()
            {
                "invalid_ip"
            } else if rule.note.contains("auto") || rule.note.contains("discover") {
                "generated_rule"
            } else {
                ""
            };
            if !reason.is_empty() {
                items.push(json!({"scope":set.scope,"index":index,"revision":revision,"rule":rule,"reason":reason}));
            }
        }
    }
    items.extend(memory_audit_items(&st).map_err(internal)?.0);
    Ok(Json(json!({"items":items})).into_response())
}
#[derive(Deserialize)]
struct AuditRuleReq {
    scope: Scope,
    index: Option<usize>,
    #[serde(default)]
    indices: Vec<usize>,
    revision: String,
    disabled: bool,
}
async fn update_audit_rule(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<AuditRuleReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    let indices = if req.indices.is_empty() {
        vec![req.index.ok_or_else(|| bad("select at least one rule"))?]
    } else {
        req.indices
    };
    if indices.len() > 10_000 {
        return Err(bad("too many selected rules"));
    }
    control(&st)?
        .set_rules_disabled(req.scope, &indices, &req.revision, req.disabled)
        .map_err(|e| error(StatusCode::CONFLICT, e.to_string()))?;
    reload(&st)?;
    drop(_rules);
    audit_rules(State(st), headers).await
}

async fn rules(State(st): State<Shared>, headers: HeaderMap) -> ApiResult {
    authorized(&st, &headers, true)?;
    let sets = control(&st)?.rules().map_err(internal)?;
    Ok(Json(sets).into_response())
}

#[derive(Deserialize)]
struct AddRuleReq {
    scope: Scope,
    rule: Rule,
}

async fn add_rule(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<AddRuleReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    let mut rule = req.rule;
    let patterns = [&rule.value, &rule.template, &rule.regex]
        .iter()
        .filter(|p| p.as_ref().is_some_and(|s| !s.trim().is_empty()))
        .count();
    if patterns != 1 {
        return Err(bad("exactly one of value, template, regex is required"));
    }
    if rule.enforced && req.scope != Scope::Global {
        return Err(bad("enforced applies only to global rules"));
    }
    rule.kind = if rule.action == RuleAction::Keep {
        "KEEP".into()
    } else {
        rule.kind.trim().to_ascii_uppercase()
    };
    if rule.kind.is_empty() || !rule.kind.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err(bad("kind must be latin letters and digits, e.g. SVC"));
    }
    control(&st)?.add_rule(req.scope, rule).map_err(bad)?;
    reload(&st)?;
    Ok(Json(json!({"ok": true})).into_response())
}

async fn remove_rule(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path((scope, index)): Path<(Scope, usize)>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    let removed = control(&st)?.remove_rule(scope, index).map_err(bad)?;
    reload(&st)?;
    Ok(Json(json!({"removed": removed})).into_response())
}

#[derive(Deserialize)]
struct DomainsReq {
    domains: Vec<String>,
}

async fn set_domains(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path(scope): Path<Scope>,
    Json(req): Json<DomainsReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    control(&st)?
        .set_corp_domains(scope, &req.domains)
        .map_err(bad)?;
    reload(&st)?;
    Ok(Json(json!({"ok": true})).into_response())
}

#[derive(Deserialize)]
struct SetupReq {
    #[serde(flatten)]
    answers: kaponir_core::onboarding::Answers,
    #[serde(default)]
    apply: bool,
}

async fn setup(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<SetupReq>,
) -> ApiResult {
    authorized(&st, &headers, true)?;
    let _rules = st.rule_changes.lock().await;
    let plan = kaponir_core::onboarding::plan(&req.answers).map_err(bad)?;
    if req.apply {
        let ctl = control(&st)?;
        let sets = ctl.rules().map_err(internal)?;
        let existing = sets.iter().find(|s| s.scope == Scope::Personal);
        let mut domains = existing
            .map(|s| s.file.corp.domains.clone())
            .unwrap_or_default();
        for domain in &plan.domains {
            if !domains.iter().any(|d| d.eq_ignore_ascii_case(domain)) {
                domains.push(domain.clone());
            }
        }
        for rule in &plan.rules {
            if !existing.is_some_and(|s| {
                s.file.rules.iter().any(|r| {
                    r.action == RuleAction::Mask
                        && r.kind == rule.kind
                        && r.value
                            .as_ref()
                            .zip(rule.value.as_ref())
                            .is_some_and(|(a, b)| a.to_lowercase() == b.to_lowercase())
                })
            }) {
                ctl.add_rule(Scope::Personal, rule.clone()).map_err(bad)?;
            }
        }
        if !plan.domains.is_empty() {
            ctl.set_corp_domains(Scope::Personal, &domains)
                .map_err(bad)?;
        }
        reload(&st)?;
    }
    Ok(Json(json!({"plan":plan,"applied":req.apply})).into_response())
}
