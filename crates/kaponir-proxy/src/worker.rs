//! Only short management requests are forwarded; OCR/downloads run in the worker.
use crate::{
    Shared,
    server::{ApiError, authorized, error, internal},
};
use axum::{
    Router,
    extract::{Json, Path, State},
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use serde_json::{Value, json};
use std::time::Duration;

pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/api/v1/ui/worker", get(status))
        .route("/api/v1/ui/worker/jobs", post(submit))
        .route("/api/v1/ui/worker/models/download", post(download))
        .route("/api/v1/ui/worker/jobs/{id}/{action}", post(action))
}
async fn forward(st: &Shared, path: &str, body: Option<Value>) -> Result<Json<Value>, ApiError> {
    let configured = std::env::var("KAPONIR_WORKER_URL")
        .ok()
        .or_else(|| st.cfg.worker_url.clone());
    let Some(base) = configured else {
        if body.is_some() {
            return Err(error(
                StatusCode::SERVICE_UNAVAILABLE,
                "worker is not configured",
            ));
        }
        return Ok(Json(json!({"configured":false})));
    };
    let url = format!("{}{}", base.trim_end_matches('/'), path);
    let mut request = if let Some(body) = body {
        st.http.post(url).json(&body)
    } else {
        st.http.get(url)
    };
    if let Ok(token) = std::env::var("KAPONIR_WORKER_TOKEN") {
        request = request.bearer_auth(token);
    }
    let response = request
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .map_err(|_| error(StatusCode::BAD_GATEWAY, "worker unavailable"))?;
    let status = response.status();
    let value: Value = response.json().await.map_err(|e| internal(e.into()))?;
    if !status.is_success() {
        return Err(error(
            StatusCode::BAD_REQUEST,
            value["error"].as_str().unwrap_or("worker operation failed"),
        ));
    }
    Ok(Json(value))
}
async fn status(State(st): State<Shared>, headers: HeaderMap) -> Result<Json<Value>, ApiError> {
    authorized(&st, &headers, true)?;
    forward(&st, "/status", None).await
}
async fn submit(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &headers, true)?;
    forward(&st, "/jobs", Some(body)).await
}
async fn download(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &headers, true)?;
    forward(&st, "/models/download", Some(body)).await
}
async fn action(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path((id, action)): Path<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &headers, true)?;
    if id.len() != 32
        || !id.bytes().all(|c| c.is_ascii_hexdigit())
        || !matches!(action.as_str(), "pause" | "resume" | "cancel")
    {
        return Err(error(StatusCode::BAD_REQUEST, "invalid worker action"));
    }
    forward(&st, &format!("/jobs/{id}/{action}"), Some(json!({}))).await
}
