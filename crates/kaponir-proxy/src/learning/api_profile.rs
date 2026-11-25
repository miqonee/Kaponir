use super::*;
use futures_util::StreamExt;

#[derive(Deserialize)]
pub(super) struct ProfileRequest {
    role: String,
    #[serde(default)]
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

struct Catalog {
    status: &'static str,
    models: std::collections::BTreeMap<String, Value>,
}

impl Catalog {
    fn empty(status: &'static str) -> Self {
        Self {
            status,
            models: Default::default(),
        }
    }
}

async fn fetch_catalog(st: &Shared, req: &ProfileRequest) -> Result<Catalog, ApiError> {
    let mut endpoint = model_url(&req.url).map_err(bad)?;
    if !matches!(req.role.as_str(), "analysis" | "categorization") {
        return Err(bad(anyhow::anyhow!("invalid_review_source")));
    }
    let Some(base) = chat_base(endpoint.path()) else {
        return Ok(Catalog::empty("unsupported"));
    };
    endpoint.set_path(&format!("{base}/models"));
    let token = test_key(st, &req.role, req.key.clone(), req.clear_key, &req.url).map_err(bad)?;
    let mut request = st.http.get(endpoint).timeout(Duration::from_secs(4));
    if let Some(token) = token {
        request = request.bearer_auth(token);
    }
    let Ok(response) = request.send().await else {
        return Ok(Catalog::empty("unavailable"));
    };
    if !response.status().is_success() {
        return Ok(Catalog::empty(match response.status().as_u16() {
            401 | 403 => "unauthorized",
            404 | 405 => "unsupported",
            _ => "unavailable",
        }));
    }
    let mut data = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(part) = stream.next().await {
        let Ok(part) = part else {
            return Ok(Catalog::empty("unavailable"));
        };
        if data.len() + part.len() > 4 * 1024 * 1024 {
            return Ok(Catalog::empty("too_large"));
        }
        data.extend_from_slice(&part);
    }
    let parsed: Value = serde_json::from_slice(&data).unwrap_or(Value::Null);
    let Some(rows) = parsed["data"].as_array() else {
        return Ok(Catalog::empty("unsupported"));
    };
    let mut catalog = Catalog::empty("empty");
    for row in rows {
        if let Some(id) = row["id"].as_str()
            && !id.trim().is_empty()
            && id.len() <= 1024
            && !id.chars().any(char::is_control)
        {
            if catalog.models.len() >= 8192 {
                return Ok(Catalog::empty("too_large"));
            }
            catalog
                .models
                .entry(id.to_owned())
                .or_insert_with(|| recommendation(row));
        }
    }
    if !catalog.models.is_empty() {
        catalog.status = "available";
    }
    Ok(catalog)
}

pub(super) async fn catalog(
    AxumState(st): AxumState<Shared>,
    h: HeaderMap,
    Json(req): Json<ProfileRequest>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    let catalog = fetch_catalog(&st, &req).await?;
    let models: Vec<_> = catalog
        .models
        .into_iter()
        .map(|(id, profile)| json!({"id":id,"profile":profile}))
        .collect();
    Ok(Json(
        json!({"status":catalog.status,"models":models,"fallback":recommendation(&Value::Null)}),
    ))
}

pub(super) async fn profile(
    AxumState(st): AxumState<Shared>,
    h: HeaderMap,
    Json(req): Json<ProfileRequest>,
) -> Result<Json<Value>, ApiError> {
    authorized(&st, &h, true)?;
    let mut catalog = fetch_catalog(&st, &req).await?;
    Ok(Json(
        catalog
            .models
            .remove(&req.model)
            .unwrap_or_else(|| recommendation(&Value::Null)),
    ))
}
