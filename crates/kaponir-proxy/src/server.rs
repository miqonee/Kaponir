//! HTTP: прокси Anthropic/OpenAI и sidecar REST.
//!
//! Принцип «сбой = не отправлять»: если запрос не удалось замаскировать, он не уходит
//! провайдеру. Сбой восстановления ответа не опасен — клиент получит токены вместо оригиналов.

use crate::traffic::Span;
use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{DefaultBodyLimit, Json, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, Uri, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use futures_util::{Stream, StreamExt};
use kaponir_core::{Escape, Format, Input, Scope, StreamRestorer};
use serde::Deserialize;
use serde_json::{Value, json};
use std::pin::Pin;
use std::sync::{Arc, atomic::Ordering::Relaxed};

use crate::sse::{Rewriter, SseParser};
use crate::{Shared, anthropic, openai};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Api {
    Anthropic,
    OpenAi,
}

pub fn router(state: Shared) -> Router {
    Router::new()
        .route("/v1/messages", post(anthropic_messages))
        .route("/v1/messages/count_tokens", post(anthropic_messages))
        .route("/v1/chat/completions", post(openai_chat))
        .route("/v1/models", get(models))
        .route("/p/{id}/v1/messages", post(anthropic_messages))
        .route("/p/{id}/v1/messages/count_tokens", post(anthropic_messages))
        .route("/p/{id}/v1/chat/completions", post(openai_chat))
        .route("/p/{id}/v1/models", get(models))
        .route("/api/v1/anonymize", post(api_anonymize))
        .route("/api/v1/deanonymize", post(api_deanonymize))
        .route("/api/v1/deanonymize/stream", post(api_deanonymize_stream))
        .route("/api/v1/metrics", get(crate::metrics::snapshot))
        .route("/metrics", get(crate::metrics::prometheus))
        .route("/healthz", get(|| async { "ok" }))
        .merge(crate::web::routes())
        .merge(crate::worker::routes())
        .merge(crate::providers::routes())
        .layer(DefaultBodyLimit::max(64 << 20))
        .layer(middleware::from_fn_with_state(state.clone(), guard_host))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            observe_request,
        ))
        .with_state(state)
}

// Время до заголовков ответа. Для SSE это НЕ полная длительность потока.
async fn observe_request(State(st): State<Shared>, req: Request, next: Next) -> Response {
    let path = req.uri().path();
    let observe = path != "/api/v1/metrics"
        && (path.starts_with("/api/v1/") || path.starts_with("/v1/") || path.starts_with("/p/"));
    let started = std::time::Instant::now();
    let response = next.run(req).await;
    if observe {
        st.requests.observe(
            started.elapsed(),
            response.status().is_client_error() || response.status().is_server_error(),
        );
    }
    response
}

/// Защита от DNS rebinding: без токена сервер доверяет только запросам на loopback-имя.
/// Иначе страница чужого сайта, чей домен перепривязан на 127.0.0.1, могла бы читать vault
/// через API восстановления.
async fn guard_host(State(st): State<Shared>, req: Request, next: Next) -> Response {
    if st.cfg.api_token.is_none()
        && let Some(host) = req.headers().get(header::HOST)
        && !host.to_str().is_ok_and(loopback_host)
    {
        return error(
            StatusCode::FORBIDDEN,
            "host is not loopback: set api_token to serve other hosts",
        )
        .into_response();
    }
    next.run(req).await
}

fn loopback_host(host: &str) -> bool {
    let name = match host.strip_prefix('[') {
        Some(rest) => rest.split(']').next().unwrap_or_default(),
        None => host.rsplit_once(':').map_or(host, |(h, _)| h),
    };
    let name = name.trim_end_matches('.').to_ascii_lowercase();
    name == "localhost"
        || name.ends_with(".localhost")
        || name == "::1"
        || name
            .parse::<std::net::Ipv4Addr>()
            .is_ok_and(|ip| ip.is_loopback())
}

/// HTTP-ответ создаётся только на границе Axum, а не хранится в Result::Err.
pub(crate) struct ApiError {
    status: StatusCode,
    message: String,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({"type": "error", "error": {
                "type": "kaponir_proxy_error", "message": self.message
            }})),
        )
            .into_response()
    }
}

pub(crate) fn error(status: StatusCode, msg: impl Into<String>) -> ApiError {
    ApiError {
        status,
        message: msg.into(),
    }
}

pub(crate) fn internal(e: anyhow::Error) -> ApiError {
    error(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}"))
}

/// API accepts the Kaponir Bearer token. Proxy routes also accept x-kaponir-token
/// for clients forwarding their own provider credential. A local Bearer token is
/// stripped before forwarding; managed routes inject the encrypted provider key.
pub(crate) fn authorized(st: &Shared, headers: &HeaderMap, api: bool) -> Result<(), ApiError> {
    let Some(expected) = &st.cfg.api_token else {
        return Ok(());
    };
    let got = if api {
        headers
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
    } else {
        headers
            .get("x-kaponir-token")
            .and_then(|v| v.to_str().ok())
            .or_else(|| {
                headers
                    .get(header::AUTHORIZATION)
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.strip_prefix("Bearer "))
            })
    };
    // Сравнение хешей blake3 — за постоянное время.
    match got {
        Some(g) if blake3::hash(g.as_bytes()) == blake3::hash(expected.as_bytes()) => Ok(()),
        _ => Err(error(
            StatusCode::UNAUTHORIZED,
            "missing or invalid kaponir token",
        )),
    }
}

/// Сессия: `X-Kaponir-Session`, иначе хеш первого сообщения разговора — он одинаков на всех ходах.
fn session_id(headers: &HeaderMap, api: Api, body: &Value) -> String {
    if let Some(h) = headers
        .get("x-kaponir-session")
        .and_then(|v| v.to_str().ok())
        .filter(|h| !h.is_empty())
    {
        return named(h);
    }
    let first = body
        .get("messages")
        .and_then(Value::as_array)
        .and_then(|m| {
            m.iter().find(|m| {
                !matches!(
                    m.get("role").and_then(Value::as_str),
                    Some("system" | "developer")
                )
            })
        });
    let mut h = blake3::Hasher::new();
    h.update(if api == Api::Anthropic {
        b"anthropic\0"
    } else {
        b"openai\0"
    });
    h.update(first.map(Value::to_string).unwrap_or_default().as_bytes());
    format!("c:{}", &h.finalize().to_hex()[..32])
}

/// Явные сессии общие для прокси и REST: шлюз может маскировать через API, а восстанавливать
/// через прокси, и наоборот.
fn named(s: &str) -> String {
    format!("s:{s}")
}

const HOP: &[&str] = &[
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "accept-encoding",
    "x-kaponir-token",
    "x-kaponir-session",
];

fn forward_headers(h: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::new();
    for (k, v) in h {
        if !HOP.contains(&k.as_str()) {
            out.append(k.clone(), v.clone());
        }
    }
    // Тело ответа нужно читать: без сжатия.
    out.insert(
        header::ACCEPT_ENCODING,
        HeaderValue::from_static("identity"),
    );
    out
}

fn strip_local_token(st: &Shared, headers: &mut HeaderMap) {
    if let Some(expected) = &st.cfg.api_token
        && headers
            .get(header::AUTHORIZATION)
            .and_then(|h| h.to_str().ok())
            .and_then(|h| h.strip_prefix("Bearer "))
            .is_some_and(|v| blake3::hash(v.as_bytes()) == blake3::hash(expected.as_bytes()))
    {
        headers.remove(header::AUTHORIZATION);
    }
}

fn response_headers(h: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::new();
    for (k, v) in h {
        if !matches!(
            k.as_str(),
            "content-length"
                | "transfer-encoding"
                | "connection"
                | "keep-alive"
                | "content-encoding"
        ) {
            out.append(k.clone(), v.clone());
        }
    }
    out
}

fn connection_id(uri: &Uri) -> Option<&str> {
    uri.path()
        .strip_prefix("/p/")
        .and_then(|rest| rest.split_once('/'))
        .map(|(id, _)| id)
}
fn upstream_url(base: &str, uri: &Uri) -> String {
    let path = uri.path_and_query().map_or(uri.path(), |pq| pq.as_str());
    let path = if let Some(rest) = path.strip_prefix("/p/") {
        format!("/{}", rest.split_once('/').map_or("", |(_, path)| path))
    } else {
        path.to_owned()
    };
    format!("{}{path}", base.trim_end_matches('/'))
}

fn respond(status: StatusCode, headers: HeaderMap, body: Body) -> Response {
    let mut r = Response::new(body);
    *r.status_mut() = status;
    *r.headers_mut() = headers;
    r
}

async fn anthropic_messages(
    State(st): State<Shared>,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    proxy(st, Api::Anthropic, uri, headers, body)
        .await
        .unwrap_or_else(IntoResponse::into_response)
}

async fn openai_chat(
    State(st): State<Shared>,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    proxy(st, Api::OpenAi, uri, headers, body)
        .await
        .unwrap_or_else(IntoResponse::into_response)
}

async fn proxy(
    st: Shared,
    api: Api,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    let span = Span::new(
        st.traffic.clone(),
        if api == Api::Anthropic {
            "anthropic"
        } else {
            "openai"
        },
    );
    let result = proxy_request(st, api, uri, headers, body, span.clone()).await;
    span.status.store(
        match &result {
            Ok(r) => r.status().as_u16(),
            Err(e) => e.status.as_u16(),
        },
        Relaxed,
    );
    if !span.streaming.load(Relaxed) {
        span.complete.store(true, Relaxed);
    }
    result
}

async fn proxy_request(
    st: Shared,
    api: Api,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
    span: Arc<Span>,
) -> Result<Response, ApiError> {
    authorized(&st, &headers, false)?;
    let mut json: Value = serde_json::from_slice(&body).map_err(|e| {
        error(
            StatusCode::BAD_REQUEST,
            format!("request body is not JSON: {e}"),
        )
    })?;
    let session = session_id(&headers, api, &json);
    let hint = st
        .cfg
        .system_hint
        .then(|| crate::system_hint(st.engine().codec().prefix()));
    let (_, usage) = st
        .with_usage(&session, true, |cx| match api {
            Api::Anthropic => anthropic::mask_request(&mut json, cx, hint.as_deref()),
            Api::OpenAi => openai::mask_request(&mut json, cx, hint.as_deref()),
        })
        .map_err(|e| {
            error(
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("masking failed, request was not sent: {e:#}"),
            )
        })?;
    span.add(&usage);
    let body = serde_json::to_vec(&json).map_err(|e| internal(e.into()))?;

    let base = match api {
        Api::Anthropic => &st.cfg.anthropic_upstream,
        Api::OpenAi => &st.cfg.openai_upstream,
    };
    let kind = if api == Api::Anthropic {
        "anthropic"
    } else {
        "openai"
    };
    crate::providers::refresh(&st, connection_id(&uri), false).await?;
    let model = json.get("model").and_then(Value::as_str).unwrap_or("");
    let mut attempted = Vec::new();
    let mut provider = crate::providers::select_for_session(
        &st,
        kind,
        connection_id(&uri),
        model,
        &session,
        &attempted,
        None,
    )?;
    // Reuse exactly the same masked bytes. Never retry after forwarding response data,
    // on transport errors, or on ambiguous upstream failures (e.g. HTTP 5xx).
    let resp = loop {
        let base = provider
            .as_ref()
            .map_or(base.as_str(), |p| p.base_url.as_str());
        let mut outgoing = forward_headers(&headers);
        strip_local_token(&st, &mut outgoing);
        if let Some(p) = &provider {
            outgoing = crate::providers::headers(outgoing, kind, p);
            attempted.push(p.id.clone());
        }
        *span
            .connection_id
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) =
            provider.as_ref().map(|p| p.id.clone());
        span.sent.store(true, Relaxed);
        let resp = st
            .http
            .post(upstream_url(base, &uri))
            .headers(outgoing)
            .header(header::CONTENT_TYPE, "application/json")
            .body(body.clone())
            .send()
            .await
            .map_err(|e| error(StatusCode::BAD_GATEWAY, format!("upstream: {e}")))?;
        if resp.status() != StatusCode::TOO_MANY_REQUESTS
            || connection_id(&uri).is_some()
            || provider.as_ref().is_none_or(|p| !p.retry_on_429)
            || attempted.len() >= 64
        {
            break resp;
        }
        let Some(next) = crate::providers::select_for_session(
            &st,
            kind,
            None,
            model,
            &session,
            &attempted,
            provider.as_ref(),
        )?
        else {
            break resp;
        };
        provider = Some(next);
    };
    let status = resp.status();
    let out_headers = response_headers(resp.headers());
    let content_type = resp
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    let is_sse = content_type.starts_with("text/event-stream");
    let is_json = content_type.contains("json");

    if is_sse && status.is_success() {
        let rw: Box<dyn Rewriter> = match api {
            Api::Anthropic => Box::<anthropic::StreamRewriter>::default(),
            Api::OpenAi => Box::<openai::StreamRewriter>::default(),
        };
        span.streaming.store(true, Relaxed);
        let stream = restore_stream(st, session, rw, Box::pin(resp.bytes_stream()), span);
        return Ok(respond(status, out_headers, Body::from_stream(stream)));
    }

    let bytes = resp
        .bytes()
        .await
        .map_err(|e| error(StatusCode::BAD_GATEWAY, format!("upstream: {e}")))?;
    if !(status.is_success() && is_json) {
        return Ok(respond(status, out_headers, Body::from(bytes)));
    }
    let Ok(mut v) = serde_json::from_slice::<Value>(&bytes) else {
        return Ok(respond(status, out_headers, Body::from(bytes)));
    };
    let restored = st.with_usage(&session, false, |cx| match api {
        Api::Anthropic => anthropic::restore_response(&mut v, cx),
        Api::OpenAi => openai::restore_response(&mut v, cx),
    });
    let body = match restored.and_then(|((), usage)| {
        span.add(&usage);
        Ok(serde_json::to_vec(&v)?)
    }) {
        Ok(b) => Body::from(b),
        Err(e) => {
            span.restore_failed.store(true, Relaxed);
            eprintln!("kaponir: restore failed, response passed with placeholders: {e:#}");
            Body::from(bytes)
        }
    };
    Ok(respond(status, out_headers, body))
}

type Upstream = Pin<Box<dyn Stream<Item = reqwest::Result<Bytes>> + Send>>;

struct StreamState {
    span: Arc<Span>,
    up: Upstream,
    parser: SseParser,
    rw: Box<dyn Rewriter>,
    st: Shared,
    session: String,
    done: bool,
}

impl StreamState {
    fn process(&mut self, events: Vec<crate::sse::Event>, end: bool) -> String {
        let raw: String = events.iter().map(|e| e.raw.as_str()).collect();
        let st = self.st.clone();
        let rw = &mut self.rw;
        let r = st.with_usage(&self.session, false, |cx| {
            let mut out = String::new();
            for ev in events {
                for s in rw.event(ev, cx)? {
                    out.push_str(&s);
                }
            }
            if end {
                for s in rw.end(cx)? {
                    out.push_str(&s);
                }
            }
            Ok(out)
        });
        let r = r.map(|(out, usage)| {
            self.span.add(&usage);
            out
        });
        if end {
            self.span.complete.store(true, Relaxed);
        }
        r.unwrap_or_else(|e| {
            self.span.restore_failed.store(true, Relaxed);
            eprintln!("kaponir: stream restore failed, events passed with placeholders: {e:#}");
            raw
        })
    }
}

/// Поток событий провайдера → поток с восстановленными значениями. Придерживается только
/// хвост, похожий на начало токена.
fn restore_stream(
    st: Shared,
    session: String,
    rw: Box<dyn Rewriter>,
    up: Upstream,
    span: Arc<Span>,
) -> impl Stream<Item = Result<Bytes, std::io::Error>> + Send + 'static {
    let state = StreamState {
        span,
        up,
        parser: SseParser::default(),
        rw,
        st,
        session,
        done: false,
    };
    futures_util::stream::unfold(state, |mut s| async move {
        loop {
            if s.done {
                return None;
            }
            let (events, end) = match s.up.next().await {
                Some(Ok(b)) => (s.parser.push(&b), false),
                Some(Err(e)) => {
                    s.span.failed.store(true, Relaxed);
                    s.st.stream_errors
                        .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    s.done = true;
                    return Some((Err(std::io::Error::other(e)), s));
                }
                None => {
                    s.done = true;
                    (s.parser.finish().into_iter().collect(), true)
                }
            };
            let out = s.process(events, end);
            if !out.is_empty() {
                return Some((Ok(Bytes::from(out)), s));
            }
        }
    })
}

/// Список моделей: без данных пользователя, проксируется как есть.
async fn models(
    State(st): State<Shared>,
    uri: Uri,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    authorized(&st, &headers, false)?;
    let explicit = connection_id(&uri)
        .map(|id| crate::providers::by_id(&st, id))
        .transpose()?;
    let anthropic = explicit.as_ref().map_or_else(
        || headers.contains_key("anthropic-version") || headers.contains_key("x-api-key"),
        |p| p.kind == "anthropic",
    );
    let kind = if anthropic { "anthropic" } else { "openai" };
    if explicit.is_none() {
        crate::providers::refresh(&st, None, false).await?;
    }
    if explicit.is_none()
        && let Some(catalog) = crate::providers::catalog(&st, kind)?
    {
        return Ok(Json(catalog).into_response());
    }
    let base = if anthropic {
        &st.cfg.anthropic_upstream
    } else {
        &st.cfg.openai_upstream
    };
    let provider = crate::providers::select(&st, kind, connection_id(&uri), "")?;
    let base = provider
        .as_ref()
        .map_or(base.as_str(), |p| p.base_url.as_str());
    let mut outgoing = forward_headers(&headers);
    strip_local_token(&st, &mut outgoing);
    if let Some(p) = &provider {
        outgoing = crate::providers::headers(outgoing, kind, p);
    }
    let resp = st
        .http
        .get(upstream_url(base, &uri))
        .headers(outgoing)
        .send()
        .await
        .map_err(|e| error(StatusCode::BAD_GATEWAY, format!("upstream: {e}")))?;
    let (status, h) = (resp.status(), response_headers(resp.headers()));
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| error(StatusCode::BAD_GATEWAY, format!("upstream: {e}")))?;
    Ok(respond(status, h, Body::from(bytes)))
}

// ---------- sidecar REST ----------

#[derive(Deserialize)]
struct AnonymizeReq {
    text: String,
    #[serde(default)]
    format: Format,
    session: Option<String>,
    scope: Option<Scope>,
}

async fn api_anonymize(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<AnonymizeReq>,
) -> Result<Response, ApiError> {
    authorized(&st, &headers, true)?;
    let session = req.session.unwrap_or_else(|| "default".into());
    let m = st
        .with(&named(&session), true, |cx| {
            if let Some(s) = req.scope {
                cx.opts.scope = s;
            }
            cx.mask_full(&Input {
                text: &req.text,
                format: req.format,
            })
        })
        .map_err(internal)?;
    Ok(Json(json!({
        "text": m.text,
        "session": session,
        "replacements": m.replacements,
        "uncertain": m.uncertain,
        "literals": m.literals,
    }))
    .into_response())
}

#[derive(Deserialize)]
struct DeanonymizeReq {
    text: String,
    session: Option<String>,
    #[serde(default)]
    json_escape: bool,
}

fn escape(json_escape: bool) -> Escape {
    if json_escape {
        Escape::Json
    } else {
        Escape::None
    }
}

async fn api_deanonymize(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<DeanonymizeReq>,
) -> Result<Response, ApiError> {
    authorized(&st, &headers, true)?;
    let session = req.session.unwrap_or_else(|| "default".into());
    let r = st
        .with(&named(&session), false, |cx| {
            cx.restore_full(&req.text, escape(req.json_escape))
        })
        .map_err(internal)?;
    Ok(Json(
        json!({"text": r.text, "restored": r.restored, "unknown": r.unknown, "denied": r.denied}),
    )
    .into_response())
}

#[derive(Deserialize)]
struct StreamReq {
    session: Option<String>,
    /// Id потока внутри сессии: шлюз может восстанавливать несколько потоков параллельно.
    stream: String,
    #[serde(default)]
    chunk: String,
    /// Последний чанк: отдать придержанный хвост и забыть поток.
    #[serde(default)]
    done: bool,
    #[serde(default)]
    json_escape: bool,
}

async fn api_deanonymize_stream(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<StreamReq>,
) -> Result<Response, ApiError> {
    authorized(&st, &headers, true)?;
    let session = req.session.unwrap_or_else(|| "default".into());
    let (text, pending) = st
        .with(&named(&session), false, |cx| {
            let mut r = cx
                .streams()
                .remove(&req.stream)
                .unwrap_or_else(|| StreamRestorer::new(escape(req.json_escape)));
            let mut out = cx.push(&mut r, &req.chunk)?;
            if req.done {
                out.push_str(&cx.flush(&mut r)?);
                return Ok((out, 0));
            }
            let pending = r.pending();
            cx.streams().insert(req.stream.clone(), r);
            Ok((out, pending))
        })
        .map_err(internal)?;
    Ok(Json(json!({"text": text, "pending": pending})).into_response())
}

#[cfg(test)]
mod tests {
    use super::loopback_host;

    #[test]
    fn loopback_hosts() {
        for h in [
            "127.0.0.1:8787",
            "localhost",
            "LOCALHOST:1",
            "app.localhost:3000",
            "[::1]:8787",
            "127.1.2.3",
        ] {
            assert!(loopback_host(h), "{h}");
        }
        for h in [
            "evil.example:8787",
            "10.0.0.1",
            "127.0.0.1.evil.com",
            "[2001:db8::1]:80",
            "localhost.evil.com",
        ] {
            assert!(!loopback_host(h), "{h}");
        }
    }
}
