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
        .route("/api/v1/ui/worker/start", post(start))
        .route("/api/v1/ui/worker/stop", post(stop))
        .route("/api/v1/ui/worker/jobs", post(submit))
        .route("/api/v1/ui/worker/models/download", post(download))
        .route("/api/v1/ui/worker/jobs/{id}/{action}", post(action))
}
pub(crate) async fn forward(
    st: &Shared,
    path: &str,
    body: Option<Value>,
) -> Result<Json<Value>, ApiError> {
    let configured = std::env::var("KAPONIR_WORKER_URL")
        .ok()
        .or_else(|| st.cfg.worker_url.clone());
    let configured = configured.or_else(|| {
        st.worker_manager
            .as_ref()
            .filter(|m| m.running())
            .map(|m| m.endpoint())
    });
    let Some(base) = configured else {
        if body.is_some() {
            return Err(error(
                StatusCode::SERVICE_UNAVAILABLE,
                "worker is not configured",
            ));
        }
        return Ok(Json(
            json!({"configured":false,"state":"disabled","enabled":false,"can_manage":st.worker_manager.is_some()}),
        ));
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
        .timeout(Duration::from_secs(if path == "/status" {
            1
        } else if path == "/evaluate" {
            300
        } else {
            10
        }))
        .send()
        .await
        .map_err(|_| error(StatusCode::BAD_GATEWAY, "worker unavailable"))?;
    let status = response.status();
    let mut value: Value = response.json().await.map_err(|e| internal(e.into()))?;
    if !status.is_success() {
        return Err(error(
            StatusCode::BAD_REQUEST,
            value["error"].as_str().unwrap_or("worker operation failed"),
        ));
    }
    if path == "/status" {
        if value.get("can_manage").is_none() {
            value["can_manage"] = json!(false);
        }
        if value.get("enabled").is_none() {
            value["enabled"] = json!(true);
        }
        if value.get("state").is_none() {
            value["state"] = json!("ready");
        }
    }
    Ok(Json(value))
}
async fn status(State(st): State<Shared>, headers: HeaderMap) -> Result<Json<Value>, ApiError> {
    authorized(&st, &headers, true)?;
    if let Some(m) = &st.worker_manager
        && !m.running()
        && std::env::var("KAPONIR_WORKER_URL").is_err()
        && st.cfg.worker_url.is_none()
    {
        return Ok(Json(json!({
            "configured": true,
            "online": false,
            "state": "disabled",
            "enabled": false,
            "can_manage": true,
        })));
    }
    match forward(&st, "/status", None).await {
        Ok(mut value) => {
            if let Some(obj) = value.as_object_mut()
                && st.worker_manager.is_some()
            {
                obj.insert("can_manage".into(), json!(true));
            }
            Ok(value)
        }
        Err(_) => Ok(Json(json!({
            "configured": true,
            "online": false,
            "state": "error",
            "enabled": false,
            "can_manage": st.worker_manager.is_some(),
            "error": "Worker is unavailable. Check its process and connection settings.",
        }))),
    }
}
async fn start(State(st): State<Shared>, headers: HeaderMap) -> Result<Json<Value>, ApiError> {
    authorized(&st, &headers, true)?;
    let _action = st.worker_actions.lock().await;
    let external_online = forward(&st, "/status", None)
        .await
        .is_ok_and(|v| v["online"] == true);
    if !external_online && let Some(manager) = st.worker_manager.clone() {
        tokio::task::spawn_blocking(move || manager.start())
            .await
            .map_err(|e| internal(e.into()))?
            .map_err(internal)?;
        let mut ready = false;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
        while tokio::time::Instant::now() < deadline {
            if forward(&st, "/status", None)
                .await
                .is_ok_and(|v| v["online"] == true)
            {
                ready = true;
                break;
            }
            if st.worker_manager.as_ref().is_none_or(|m| !m.running()) {
                return Err(error(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Worker exited during startup. Check Python 3.12+ and the worker process log.",
                ));
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        if !ready {
            return Err(error(
                StatusCode::GATEWAY_TIMEOUT,
                "Worker did not become ready",
            ));
        }
    }
    forward(&st, "/enable", Some(json!({}))).await
}
async fn stop(State(st): State<Shared>, headers: HeaderMap) -> Result<Json<Value>, ApiError> {
    authorized(&st, &headers, true)?;
    let _action = st.worker_actions.lock().await;
    if st.worker_manager.as_ref().is_some_and(|m| !m.running())
        && std::env::var("KAPONIR_WORKER_URL").is_err()
        && st.cfg.worker_url.is_none()
    {
        return status(State(st.clone()), headers).await;
    }
    let disabled = forward(&st, "/disable", Some(json!({}))).await;
    if let Some(manager) = st.worker_manager.clone().filter(|m| m.running()) {
        tokio::task::spawn_blocking(move || manager.stop())
            .await
            .map_err(|e| internal(e.into()))?
            .map_err(internal)?;
        return Ok(Json(json!({
            "configured": true,
            "online": false,
            "state": "disabled",
            "enabled": false,
            "can_manage": true,
        })));
    }
    let _ = disabled?;
    drop(_action);
    status(State(st), headers).await
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
