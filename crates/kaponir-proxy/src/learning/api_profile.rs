use super::*;
use futures_util::StreamExt;

#[derive(Deserialize)]
pub(super) struct ProfileRequest {
    role: String,
    model: String,
    url: String,
    key: Option<String>,
    #[serde(default)]
    clear_key: bool,
}

fn recommendation(row: &Value) -> Value {
    let limit = |paths: &[&str]| {
        paths
            .iter()
            .filter_map(|p| row.pointer(p)?.as_u64())
            .filter(|n| *n > 0)
            .min()
    };
    let context = limit(&[
        "/top_provider/context_length",
        "/context_length",
        "/context_window",
        "/max_context_length",
        "/limits/context",
    ]);
    let output = limit(&[
        "/top_provider/max_completion_tokens",
        "/max_output_tokens",
        "/max_completion_tokens",
        "/limits/output",
    ]);
    let window = context.unwrap_or(8192).clamp(4, 262144);
    let response = (window / 4).min(output.unwrap_or(8192)).clamp(1, 8192);
    let reserve = (window / 32).clamp(1, 512);
    json!({"reported_context":context,"reported_output":output,
        "inference":{"context_tokens":window,"max_output_tokens":response,"reserve_tokens":reserve,
        "batch_size":(response/200).clamp(1,32),"response_format":"auto"},
        "max_candidates":(response/64).clamp(1,128),"timeout_seconds":120})
}

pub(super) async fn profile(
    AxumState(st): AxumState<Shared>,
    h: HeaderMap,
    Json(req): Json<ProfileRequest>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    validate_url(&req.url).map_err(bad)?;
    if !matches!(req.role.as_str(), "analysis" | "categorization") {
        return Err(bad(anyhow::anyhow!("invalid_review_source")));
    }
    let fallback = || Json(recommendation(&Value::Null));
    let endpoint = req.url.trim_end_matches('/');
    let Some(base) = endpoint
        .strip_suffix("/chat/completions")
        .or_else(|| endpoint.ends_with("/v1").then_some(endpoint))
    else {
        return Ok(fallback());
    };
    let settings = st.learning.snapshot();
    let saved_url = if req.role == "analysis" {
        settings.analysis.url
    } else {
        settings.categorization.http.url
    };
    let mut request = st
        .http
        .get(format!("{base}/models"))
        .timeout(Duration::from_secs(4));
    let token = if req.key.as_deref().is_some_and(|k| !k.is_empty()) {
        req.key
    } else if !req.clear_key && req.url == saved_url {
        st.with_vault(|_, v| key(v, &req.role)).map_err(internal)?
    } else {
        None
    };
    if let Some(token) = token {
        request = request.bearer_auth(token);
    }
    let Ok(response) = request.send().await else {
        return Ok(fallback());
    };
    if !response.status().is_success() {
        return Ok(fallback());
    }
    let mut data = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(part) = stream.next().await {
        let Ok(part) = part else {
            return Ok(fallback());
        };
        if data.len() + part.len() > 4 * 1024 * 1024 {
            return Ok(fallback());
        }
        data.extend_from_slice(&part);
    }
    let catalog: Value = serde_json::from_slice(&data).unwrap_or(Value::Null);
    let row = catalog["data"]
        .as_array()
        .and_then(|rows| rows.iter().find(|r| r["id"] == req.model));
    Ok(Json(recommendation(row.unwrap_or(&Value::Null))))
}
