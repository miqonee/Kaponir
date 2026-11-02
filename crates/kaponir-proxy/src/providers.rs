//! Encrypted connection registry, model-aware pools and compatibility with single-provider settings.
use crate::{
    Shared,
    server::{ApiError, authorized, error, internal},
};
use axum::{
    Router,
    extract::{Json, Path, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    routing::{get, post, put},
};
use futures_util::{StreamExt, stream};
use kaponir_vault::SqliteVault;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::{Duration, Instant};

pub(crate) struct Binding {
    id: String,
    last_used: Instant,
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Provider {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub base_url: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub api_key: String,
    pub enabled: bool,
    pub pool: bool,
    /// Explicit consent to replay full-history requests using another key on this API URL.
    #[serde(default)]
    pub retry_on_429: bool,
    pub models: Vec<String>,
    pub all_models: bool,
    #[serde(default)]
    pub auto_models: bool,
    #[serde(default)]
    pub models_checked_at: u64,
    #[serde(default)]
    pub models_error: Option<String>,
}
#[derive(Default, Serialize, Deserialize)]
pub struct Registry {
    pub connections: Vec<Provider>,
    // Tombstones prevent removed/disabled managed routes falling back to config credentials.
    pub managed: Vec<String>,
}
#[derive(Deserialize)]
struct Legacy {
    base_url: String,
    api_key: String,
}
#[derive(Deserialize)]
struct Draft {
    base_url: String,
    api_key: Option<String>,
    name: Option<String>,
    enabled: Option<bool>,
    pool: Option<bool>,
    retry_on_429: Option<bool>,
    models: Option<Vec<String>>,
    all_models: Option<bool>,
    auto_models: Option<bool>,
}
#[derive(Deserialize)]
struct ConnectionDraft {
    kind: String,
    id: Option<String>,
    #[serde(flatten)]
    draft: Draft,
}
fn bad(code: &str) -> ApiError {
    error(StatusCode::BAD_REQUEST, code)
}
fn valid_kind(kind: &str) -> Result<(), ApiError> {
    if matches!(kind, "openai" | "anthropic") {
        Ok(())
    } else {
        Err(bad("provider_invalid_protocol"))
    }
}
fn load(v: &SqliteVault) -> anyhow::Result<Registry> {
    if let Some(bytes) = v.private_setting("providers:v2")? {
        return Ok(serde_json::from_slice(&bytes)?);
    }
    let mut registry = Registry::default();
    for kind in ["openai", "anthropic"] {
        if let Some(bytes) = v.private_setting(&format!("provider:{kind}"))? {
            let p: Legacy = serde_json::from_slice(&bytes)?;
            registry.connections.push(Provider {
                id: kind.into(),
                kind: kind.into(),
                name: kind.into(),
                base_url: p.base_url,
                api_key: p.api_key,
                enabled: true,
                pool: true,
                retry_on_429: false,
                models: vec![],
                all_models: true,
                auto_models: false,
                models_checked_at: 0,
                models_error: None,
            });
            registry.managed.push(kind.into());
        }
    }
    Ok(registry)
}
/// Portable connections. Keys are omitted unless explicitly requested.
pub fn export_settings(v: &SqliteVault, include_secrets: bool) -> anyhow::Result<Registry> {
    let mut registry = load(v)?;
    for p in &mut registry.connections {
        if !include_secrets {
            p.api_key.clear();
        }
        p.models_error = None;
    }
    Ok(registry)
}

/// Validate the complete registry before any writes. Missing keys are retained only
/// for the same id, protocol and URL; otherwise the connection is disabled.
pub fn prepare_import(v: &SqliteVault, mut incoming: Registry) -> anyhow::Result<Vec<u8>> {
    let old = load(v)?;
    let mut ids = std::collections::HashSet::new();
    anyhow::ensure!(incoming.connections.len() <= 1000, "too many connections");
    for kind in &incoming.managed {
        anyhow::ensure!(
            matches!(kind.as_str(), "openai" | "anthropic"),
            "invalid managed protocol"
        );
    }
    for p in &mut incoming.connections {
        anyhow::ensure!(
            !p.id.is_empty()
                && p.id.len() <= 80
                && p.id
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
                && ids.insert(p.id.clone()),
            "invalid or duplicate connection id"
        );
        p.base_url =
            base_url(&p.base_url).map_err(|_| anyhow::anyhow!("invalid connection URL"))?;
        if p.api_key.is_empty()
            && let Some(previous) = old
                .connections
                .iter()
                .find(|o| o.id == p.id && o.kind == p.kind && o.base_url == p.base_url)
        {
            p.api_key = previous.api_key.clone();
        }
        let missing_key = p.api_key.is_empty();
        let d = Draft {
            base_url: p.base_url.clone(),
            api_key: Some(if missing_key {
                "missing-key".into()
            } else {
                p.api_key.clone()
            }),
            name: Some(p.name.clone()),
            enabled: Some(p.enabled && !missing_key),
            pool: Some(p.pool),
            retry_on_429: Some(p.retry_on_429),
            models: Some(p.models.clone()),
            all_models: Some(p.all_models),
            auto_models: Some(p.auto_models),
        };
        *p = draft(&p.kind, &p.id, Some(p), d, true)
            .map_err(|_| anyhow::anyhow!("invalid connection settings"))?;
        if missing_key {
            p.api_key.clear();
        }
        p.models_checked_at = 0;
        p.models_error = None;
        incoming.managed.push(p.kind.clone());
    }
    // Keep tombstones for removed connections: never fall back to client credentials.
    incoming.managed.extend(old.managed);
    incoming.managed.sort();
    incoming.managed.dedup();
    Ok(serde_json::to_vec(&incoming)?)
}

fn registry(st: &Shared) -> Result<Registry, ApiError> {
    st.with_vault(|_, v| load(v)).map_err(internal)
}
pub(crate) fn saved(st: &Shared, kind: &str) -> Result<Option<Provider>, ApiError> {
    Ok(registry(st)?
        .connections
        .into_iter()
        .find(|p| p.kind == kind && p.enabled && p.pool))
}
pub(crate) fn by_id(st: &Shared, id: &str) -> Result<Provider, ApiError> {
    registry(st)?
        .connections
        .into_iter()
        .find(|p| p.id == id)
        .ok_or_else(|| error(StatusCode::NOT_FOUND, "provider_not_found"))
}
pub(crate) fn select(
    st: &Shared,
    kind: &str,
    id: Option<&str>,
    model: &str,
) -> Result<Option<Provider>, ApiError> {
    select_for_session(st, kind, id, model, "", &[], None)
}

/// Dedicated routes never fail over. Shared routes retain a session's connection,
/// rechecking eligibility on every request. Exclusions contain attempts already made.
pub(crate) fn select_for_session(
    st: &Shared,
    kind: &str,
    id: Option<&str>,
    model: &str,
    session: &str,
    excluded: &[String],
    retry_from: Option<&Provider>,
) -> Result<Option<Provider>, ApiError> {
    let registry = registry(st)?;
    if let Some(id) = id {
        let p = registry
            .connections
            .into_iter()
            .find(|p| p.id == id)
            .ok_or_else(|| error(StatusCode::NOT_FOUND, "provider_not_found"))?;
        if p.kind != kind {
            return Err(bad("provider_wrong_protocol"));
        }
        if !p.enabled {
            return Err(error(StatusCode::SERVICE_UNAVAILABLE, "provider_disabled"));
        }
        return Ok(Some(p));
    }
    if !registry.managed.iter().any(|k| k == kind) {
        return Ok(None);
    }
    let mut eligible: Vec<_> = registry
        .connections
        .into_iter()
        .filter(|p| {
            p.kind == kind
                && p.enabled
                && p.pool
                && !excluded.contains(&p.id)
                && retry_from.is_none_or(|source| {
                    source.retry_on_429 && p.retry_on_429 && p.base_url == source.base_url
                })
                && (p.all_models || p.models.iter().any(|m| m == model))
        })
        .collect();
    if eligible.is_empty() {
        if !excluded.is_empty() {
            return Ok(None); // Return the last upstream 429, including Retry-After.
        }
        return Err(error(
            StatusCode::SERVICE_UNAVAILABLE,
            "provider_no_model_route",
        ));
    }
    // Hash identifiers so the routing table does not retain client session names.
    let key = *blake3::hash(
        &serde_json::to_vec(&(kind, model, session)).map_err(|e| internal(e.into()))?,
    )
    .as_bytes();
    let mut bindings = st
        .provider_affinity
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let idle = Duration::from_secs(st.cfg.session_idle_hours.saturating_mul(3600));
    if let Some(binding) = bindings.get_mut(&key)
        && binding.last_used.elapsed() < idle
        && let Some(index) = eligible.iter().position(|p| p.id == binding.id)
    {
        binding.last_used = Instant::now();
        return Ok(Some(eligible.swap_remove(index)));
    }
    // Rendezvous hashing distributes new sessions without moving existing bindings.
    // It also preserves the initial choice across restart for an unchanged pool.
    let index = eligible
        .iter()
        .enumerate()
        .max_by_key(|(_, p)| {
            let mut hash = blake3::Hasher::new();
            hash.update(&key);
            hash.update(p.id.as_bytes());
            *hash.finalize().as_bytes()
        })
        .map(|(index, _)| index)
        .expect("nonempty eligible pool");
    let provider = eligible.swap_remove(index);
    if bindings.len() >= 8192
        && !bindings.contains_key(&key)
        && let Some(oldest) = bindings
            .iter()
            .min_by_key(|(_, b)| b.last_used)
            .map(|(k, _)| *k)
    {
        bindings.remove(&oldest);
    }
    bindings.insert(
        key,
        Binding {
            id: provider.id.clone(),
            last_used: Instant::now(),
        },
    );
    Ok(Some(provider))
}
/// A shared catalog contains the declared pool models. Legacy wildcard-only routes retain live discovery.
pub(crate) fn catalog(st: &Shared, kind: &str) -> Result<Option<Value>, ApiError> {
    let registry = registry(st)?;
    if !registry.managed.iter().any(|k| k == kind) {
        return Ok(None);
    }
    let pool: Vec<_> = registry
        .connections
        .iter()
        .filter(|p| p.kind == kind && p.enabled && p.pool)
        .collect();
    if pool.len() == 1 && pool[0].all_models && pool[0].models.is_empty() {
        return Ok(None);
    }
    let models: std::collections::BTreeSet<_> = pool.iter().flat_map(|p| p.models.iter()).collect();
    let data: Vec<_>=models.into_iter().map(|id|json!({"id":id,"object":"model","created":0,"owned_by":"kaponir","type":"model","display_name":id})).collect();
    Ok(Some(
        json!({"object":"list","data":data,"has_more":false,"first_id":data.first().map(|m|&m["id"]),"last_id":data.last().map(|m|&m["id"])}),
    ))
}
fn draft(
    kind: &str,
    id: &str,
    old: Option<&Provider>,
    d: Draft,
    validate_pool: bool,
) -> Result<Provider, ApiError> {
    valid_kind(kind)?;
    let base_url = base_url(&d.base_url)?;
    let api_key = match d.api_key.filter(|k| !k.trim().is_empty()) {
        Some(key) => key.trim().to_owned(),
        None => old
            .filter(|p| p.base_url == base_url && p.kind == kind && !p.api_key.is_empty())
            .map(|p| p.api_key.clone())
            .ok_or_else(|| bad("provider_key_required"))?,
    };
    if api_key.len() > 8192
        || HeaderValue::from_str(&api_key).is_err()
        || !api_key.is_ascii()
        || api_key.chars().any(char::is_whitespace)
    {
        return Err(bad("provider_invalid_key"));
    }
    let name = d
        .name
        .unwrap_or_else(|| old.map_or_else(|| kind.to_owned(), |p| p.name.clone()))
        .trim()
        .to_owned();
    if name.is_empty() || name.len() > 120 || name.chars().any(char::is_control) {
        return Err(bad("provider_invalid_name"));
    }
    let auto_models = d.auto_models.unwrap_or_else(|| {
        old.map_or(d.models.is_none() && d.all_models != Some(true), |p| {
            p.auto_models
        })
    });
    let changed = old.is_none_or(|p| {
        p.base_url != base_url || p.api_key != api_key || p.auto_models != auto_models
    });
    let mut models = d
        .models
        .unwrap_or_else(|| old.map_or_else(Vec::new, |p| p.models.clone()));
    if models.len() > 1000
        || models
            .iter()
            .any(|m| m.trim().is_empty() || m.len() > 256 || m.chars().any(char::is_whitespace))
    {
        return Err(bad("provider_invalid_models"));
    }
    if auto_models {
        models = if changed {
            vec![]
        } else {
            old.map_or_else(Vec::new, |p| p.models.clone())
        };
    }
    models.sort();
    models.dedup();
    let pool = d.pool.unwrap_or_else(|| old.is_some_and(|p| p.pool));
    let all_models = d
        .all_models
        .unwrap_or_else(|| old.is_some_and(|p| p.all_models));
    if validate_pool && pool && !all_models && !auto_models && models.is_empty() {
        return Err(bad("provider_pool_models_required"));
    }
    Ok(Provider {
        id: id.into(),
        kind: kind.into(),
        name,
        base_url,
        api_key,
        enabled: d.enabled.unwrap_or_else(|| old.is_none_or(|p| p.enabled)),
        pool,
        retry_on_429: d
            .retry_on_429
            .unwrap_or_else(|| old.is_some_and(|p| p.retry_on_429)),
        models,
        all_models: all_models && !auto_models,
        auto_models,
        models_checked_at: if changed {
            0
        } else {
            old.map_or(0, |p| p.models_checked_at)
        },
        models_error: if changed {
            None
        } else {
            old.and_then(|p| p.models_error.clone())
        },
    })
}
fn summary(p: &Provider) -> Value {
    json!({"id":p.id,"kind":p.kind,"name":p.name,"base_url":p.base_url,"key_saved":!p.api_key.is_empty(),"enabled":p.enabled,"pool":p.pool,"retry_on_429":p.retry_on_429,"models":p.models,"all_models":p.all_models,"auto_models":p.auto_models,"models_checked_at":p.models_checked_at,"models_error":p.models_error,"endpoint":format!("/p/{}",p.id)})
}
// The vault lock spans read-modify-write, so concurrent edits cannot lose another connection.
fn edit(
    st: &Shared,
    kind: &str,
    id: &str,
    d: Draft,
    create: bool,
    legacy: bool,
) -> Result<Json<Value>, ApiError> {
    st.with_vault(|_, v| {
        let mut r = load(v)?;
        let old = r.connections.iter().find(|p| p.id == id);
        if !create && old.is_none() {
            return Ok(Err(error(StatusCode::NOT_FOUND, "provider_not_found")));
        }
        if old.is_some_and(|p| p.kind != kind) {
            return Ok(Err(bad("provider_wrong_protocol")));
        }
        if old.is_none() && r.connections.len() >= 64 {
            return Ok(Err(bad("provider_limit")));
        }
        let mut d = d;
        if legacy && old.is_none() {
            d.pool = Some(true);
            d.all_models = Some(true);
            d.auto_models = Some(false);
        }
        let p = match draft(kind, id, old, d, true) {
            Ok(p) => p,
            Err(e) => return Ok(Err(e)),
        };
        if let Some(existing) = r.connections.iter_mut().find(|p| p.id == id) {
            *existing = p.clone();
        } else {
            r.connections.push(p.clone());
        }
        if !r.managed.iter().any(|k| k == kind) {
            r.managed.push(kind.into());
        }
        v.set_private_setting("providers:v2", &serde_json::to_vec(&r)?)?;
        Ok(Ok(Json(summary(&p))))
    })
    .map_err(internal)?
}
pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/api/v1/providers", get(list))
        .route("/api/v1/providers/{kind}", put(save_legacy))
        .route("/api/v1/providers/{kind}/test", post(test_legacy))
        .route("/api/v1/connections", get(connections).post(create))
        .route("/api/v1/connections/test", post(test_connection))
        .route(
            "/api/v1/connections/{id}/models/refresh",
            post(refresh_connection),
        )
        .route("/api/v1/connections/{id}", put(update).delete(remove))
}
async fn list(State(st): State<Shared>, h: HeaderMap) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    Ok(Json(
        json!({"openai":saved(&st,"openai")?.as_ref().map(summary),"anthropic":saved(&st,"anthropic")?.as_ref().map(summary)}),
    ))
}
async fn connections(State(st): State<Shared>, h: HeaderMap) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    refresh(&st, None, false).await?;
    let r = registry(&st)?;
    Ok(Json(
        json!({"connections":r.connections.iter().map(summary).collect::<Vec<_>>(),"managed":r.managed}),
    ))
}
async fn create(
    State(st): State<Shared>,
    h: HeaderMap,
    Json(d): Json<ConnectionDraft>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    valid_kind(&d.kind)?;
    let mut bytes = [0u8; 12];
    getrandom::fill(&mut bytes)
        .map_err(|_| internal(anyhow::anyhow!("random generation failed")))?;
    let id = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    let _ = edit(&st, &d.kind, &id, d.draft, true, false)?;
    refresh(&st, Some(&id), false).await?;
    Ok(Json(summary(&by_id(&st, &id)?)))
}
async fn update(
    State(st): State<Shared>,
    Path(id): Path<String>,
    h: HeaderMap,
    Json(d): Json<ConnectionDraft>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    let _ = edit(&st, &d.kind, &id, d.draft, false, false)?;
    refresh(&st, Some(&id), false).await?;
    Ok(Json(summary(&by_id(&st, &id)?)))
}
async fn remove(
    State(st): State<Shared>,
    Path(id): Path<String>,
    h: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    st.with_vault(|_, v| {
        let mut r = load(v)?;
        let len = r.connections.len();
        r.connections.retain(|p| p.id != id);
        if len == r.connections.len() {
            return Ok(Err(error(StatusCode::NOT_FOUND, "provider_not_found")));
        }
        v.set_private_setting("providers:v2", &serde_json::to_vec(&r)?)?;
        Ok(Ok(Json(json!({"removed":true}))))
    })
    .map_err(internal)?
}
async fn save_legacy(
    State(st): State<Shared>,
    Path(kind): Path<String>,
    h: HeaderMap,
    Json(d): Json<Draft>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    edit(&st, &kind, &kind, d, true, true)
}
async fn test_legacy(
    State(st): State<Shared>,
    Path(kind): Path<String>,
    h: HeaderMap,
    Json(d): Json<Draft>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    let old = registry(&st)?
        .connections
        .into_iter()
        .find(|p| p.id == kind);
    let p = draft(&kind, &kind, old.as_ref(), d, false)?;
    check(&st, &kind, p).await
}
async fn test_connection(
    State(st): State<Shared>,
    h: HeaderMap,
    Json(d): Json<ConnectionDraft>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    let old = d.id.as_deref().map(|id| by_id(&st, id)).transpose()?;
    let p = draft(
        &d.kind,
        d.id.as_deref().unwrap_or("new"),
        old.as_ref(),
        d.draft,
        false,
    )?;
    check(&st, &d.kind, p).await
}
fn base_url(raw: &str) -> Result<String, ApiError> {
    let u = reqwest::Url::parse(raw.trim())
        .map_err(|_| error(StatusCode::BAD_REQUEST, "provider_invalid_url"))?;
    let local = u.host_str().is_some_and(|h| {
        h == "localhost"
            || h == "[::1]"
            || h.parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback())
    });
    if (u.scheme() != "https" && !(u.scheme() == "http" && local))
        || u.host_str().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
    {
        return Err(error(StatusCode::BAD_REQUEST, "provider_invalid_url"));
    }
    let base = u.as_str().trim_end_matches('/');
    Ok(base.strip_suffix("/v1").unwrap_or(base).to_owned())
}

pub(crate) fn headers(mut headers: HeaderMap, kind: &str, p: &Provider) -> HeaderMap {
    headers.remove(header::AUTHORIZATION);
    headers.remove("x-api-key");
    // Credentials associated with a previous client/provider must not cross routes.
    headers.remove("openai-organization");
    headers.remove("openai-project");
    headers.remove(header::COOKIE);
    let (name, value) = if kind == "anthropic" {
        ("x-api-key", p.api_key.clone())
    } else {
        ("authorization", format!("Bearer {}", p.api_key))
    };
    headers.insert(
        name,
        HeaderValue::from_str(&value).expect("validated provider key"),
    );
    if kind == "anthropic" && !headers.contains_key("anthropic-version") {
        headers.insert("anthropic-version", HeaderValue::from_static("2023-06-01"));
    }
    headers
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

pub(crate) async fn refresh(st: &Shared, id: Option<&str>, force: bool) -> Result<(), ApiError> {
    let _guard = st.provider_refresh.lock().await;
    let pending = registry(st)?
        .connections
        .into_iter()
        .filter(|p| {
            p.enabled
                && p.auto_models
                && id.is_none_or(|id| p.id == id)
                && (force
                    || now().saturating_sub(p.models_checked_at)
                        >= if p.models_error.is_some() { 60 } else { 300 })
        })
        .collect::<Vec<_>>();
    let mut results = stream::iter(pending.into_iter().map(|p| async move {
        let result = fetch_models(st, &p).await;
        (p, result)
    }))
    .buffer_unordered(4);
    while let Some((snapshot, result)) = results.next().await {
        st.with_vault(|_, v| {
            let mut r = load(v)?;
            let Some(current) = r.connections.iter_mut().find(|p| p.id == snapshot.id) else {
                return Ok(());
            };
            // Never apply discovery from old credentials or overwrite a concurrent edit.
            if serde_json::to_vec(current)? != serde_json::to_vec(&snapshot)? {
                return Ok(());
            }
            current.models_checked_at = now();
            match result {
                Ok(models) => {
                    current.models = models;
                    current.models_error = None;
                }
                Err(code) => {
                    current.models_error = Some(code.into());
                }
            }
            v.set_private_setting("providers:v2", &serde_json::to_vec(&r)?)?;
            Ok(())
        })
        .map_err(internal)?;
    }
    Ok(())
}
async fn refresh_connection(
    State(st): State<Shared>,
    Path(id): Path<String>,
    h: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    by_id(&st, &id)?;
    refresh(&st, Some(&id), true).await?;
    Ok(Json(summary(&by_id(&st, &id)?)))
}
async fn fetch_models(st: &Shared, p: &Provider) -> Result<Vec<String>, &'static str> {
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        let mut models = std::collections::BTreeSet::new();
        let mut after = None::<String>;
        let mut total = 0usize;
        for _ in 0..20 {
            let mut url = reqwest::Url::parse(&format!("{}/v1/models", p.base_url))
                .map_err(|_| "provider_invalid_url")?;
            if let Some(cursor) = &after {
                url.query_pairs_mut().append_pair("after_id", cursor);
            }
            let mut resp = st
                .http
                .get(url)
                .headers(headers(HeaderMap::new(), &p.kind, p))
                .send()
                .await
                .map_err(|_| "provider_unreachable")?;
            if !resp.status().is_success() {
                return Err(match resp.status().as_u16() {
                    401 | 403 => "provider_auth_failed",
                    404 | 405 => "provider_models_unsupported",
                    _ => "provider_check_failed",
                });
            }
            let mut bytes = Vec::new();
            while let Some(chunk) = resp.chunk().await.map_err(|_| "provider_check_failed")? {
                total += chunk.len();
                if total > 2 * 1024 * 1024 {
                    return Err("provider_catalog_limit");
                }
                bytes.extend_from_slice(&chunk);
            }
            let value: Value =
                serde_json::from_slice(&bytes).map_err(|_| "provider_check_failed")?;
            let data = value
                .get("data")
                .and_then(Value::as_array)
                .ok_or("provider_models_unsupported")?;
            for model in data {
                let id = model
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or("provider_check_failed")?;
                if id.is_empty() || id.len() > 256 || id.chars().any(char::is_whitespace) {
                    return Err("provider_check_failed");
                }
                models.insert(id.to_owned());
                if models.len() > 1000 {
                    return Err("provider_catalog_limit");
                }
            }
            if value.get("has_more").and_then(Value::as_bool) != Some(true) {
                return Ok(models.into_iter().collect());
            }
            let cursor = value
                .get("last_id")
                .and_then(Value::as_str)
                .ok_or("provider_catalog_incomplete")?;
            if after.as_deref() == Some(cursor) || data.is_empty() {
                return Err("provider_catalog_incomplete");
            }
            after = Some(cursor.to_owned());
        }
        Err("provider_catalog_limit")
    })
    .await
    .map_err(|_| "provider_unreachable")?
}
async fn check(st: &Shared, _kind: &str, p: Provider) -> Result<Json<Value>, ApiError> {
    let models = fetch_models(st, &p)
        .await
        .map_err(|code| error(StatusCode::BAD_GATEWAY, code))?;
    Ok(Json(json!({"models":models,"has_more":false})))
}
