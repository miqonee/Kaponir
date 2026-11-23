//! Local examples and reversible collection receipts. Raw excerpts stay in the encrypted vault.
use crate::{
    Shared,
    learning::transaction,
    review::Selection,
    server::{ApiError, authorized, error, internal},
    traffic::now_ms,
};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use kaponir_vault::SqliteVault;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;

type Response = Result<Json<Value>, ApiError>;
fn bad(e: anyhow::Error) -> ApiError {
    error(StatusCode::BAD_REQUEST, e.to_string())
}
fn writable(st: &Shared, headers: &HeaderMap) -> Result<(), ApiError> {
    authorized(st, headers, true)?;
    if st.control.is_none() {
        return Err(error(StatusCode::FORBIDDEN, "read_only"));
    }
    Ok(())
}
fn read(v: &SqliteVault, id: &str) -> anyhow::Result<Option<Value>> {
    v.app_record(id)?
        .map(|b| Ok(serde_json::from_slice(&b)?))
        .transpose()
}
fn store(v: &SqliteVault, id: &str, kind: &str, value: &Value) -> anyhow::Result<()> {
    Ok(v.put_app_record(id, kind, &serde_json::to_vec(value)?)?)
}
pub(crate) fn finding_id(item: &Value) -> String {
    let material = json!([item["key"], item["source"], item["value"], item["context"]]);
    format!(
        "example/{}",
        blake3::hash(material.to_string().as_bytes()).to_hex()
    )
}
pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/api/v1/ui/dataset", get(list))
        .route("/api/v1/ui/dataset/collect", post(collect))
        .route(
            "/api/v1/ui/dataset/import",
            post(import).layer(DefaultBodyLimit::max(32 * 1024 * 1024)),
        )
        .route("/api/v1/ui/dataset/collections/{id}/undo", post(undo))
}

#[derive(Deserialize, Serialize)]
struct Collect {
    #[serde(flatten)]
    selection: Selection,
    request_id: String,
}
async fn collect(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<Collect>,
) -> Response {
    writable(&st, &headers)?;
    if !(8..=128).contains(&req.request_id.len()) {
        return Err(error(StatusCode::BAD_REQUEST, "invalid_request_id"));
    }
    let receipt = blake3::hash(req.request_id.as_bytes()).to_hex().to_string();
    let signature = blake3::hash(&serde_json::to_vec(&req).map_err(|e| internal(e.into()))?)
        .to_hex()
        .to_string();
    let id = format!("collection:{receipt}");
    let summary = |saved: &Value| {
        Json(json!({"id":receipt,"added":saved["added"],"existing":saved["existing"]}))
    };
    if let Some(saved) = st.with_vault(|_, v| read(v, &id)).map_err(internal)? {
        if saved["signature"] != signature {
            return Err(error(StatusCode::CONFLICT, "request_id_conflict"));
        }
        return Ok(summary(&saved));
    }
    let items = req.selection.resolve(&st).map_err(bad)?;
    let result=st.with_vault(|_,v|transaction(v,||{
        if let Some(saved)=read(v,&id)? {
            anyhow::ensure!(saved["signature"]==signature,"request_id_conflict");
            return Ok(saved);
        }
        let mut added=Vec::new();
        let mut members=Vec::new();
        for item in &items {
            let key=finding_id(item);
            members.push(key.clone());
            if v.app_record(&key)?.is_some(){continue;}
            let value=item["value"].as_str().unwrap_or_default();
            let context=item["context"].as_str().filter(|s|!s.is_empty()).unwrap_or(value);
            let mut spans=Vec::new();
            if let Some(start)=context.find(value).filter(|_|!value.is_empty()&&context.matches(value).count()==1) {
                spans.push(json!({"value":value,"start_byte":start,"end_byte":start+value.len(),"kind":item["kind"],"action":"review"}));
            }
            let row=json!({"id":key,"source":item["source"].as_str().unwrap_or_else(||item["origin"].as_str().unwrap_or("detector")),
                "finding":item["key"],"value":value,"kind":item["kind"],"text":context,"status":"draft","revision":1,
                "created":now_ms(),"updated":now_ms(),"collection":receipt,"group":"field-feedback","language":if context.chars().any(|c|('А'..='я').contains(&c)){"ru"}else{"en"},"format":"text","spans":spans});
            store(v,&key,"dataset-example",&row)?;
            added.push(key);
        }
        let result=json!({"id":receipt,"added":added.len(),"existing":members.len()-added.len(),"members":members,"inserted":added,"created":now_ms(),"signature":signature});
        store(v,&id,"dataset-collection",&result)?;
        Ok(result)
    })).map_err(bad)?;
    Ok(summary(&result))
}
async fn undo(State(st): State<Shared>, headers: HeaderMap, Path(id): Path<String>) -> Response {
    writable(&st, &headers)?;
    let removed = st
        .with_vault(|_, v| {
            transaction(v, || {
                let key = format!("collection:{id}");
                let mut receipt =
                    read(v, &key)?.ok_or_else(|| anyhow::anyhow!("collection_not_found"))?;
                let mut removed = 0;
                for member in receipt["inserted"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                {
                    if let Some(row) = read(v, member)?
                        && row["collection"] == id
                        && row["revision"] == 1
                    {
                        v.delete_app_record(member)?;
                        removed += 1;
                    }
                }
                receipt["inserted"] = json!([]);
                store(v, &key, "dataset-collection", &receipt)?;
                Ok(removed)
            })
        })
        .map_err(bad)?;
    Ok(Json(json!({"removed":removed})))
}
#[derive(Default, Deserialize)]
struct List {
    status: Option<String>,
    collection: Option<String>,
    offset: Option<usize>,
    limit: Option<usize>,
    export: Option<bool>,
}
async fn list(State(st): State<Shared>, headers: HeaderMap, Query(q): Query<List>) -> Response {
    authorized(&st, &headers, true)?;
    let result = st
        .with_vault(|_, v| {
            let members = match &q.collection {
                Some(id) => {
                    let record = read(v, &format!("collection:{id}"))?
                        .ok_or_else(|| anyhow::anyhow!("collection_not_found"))?;
                    Some(serde_json::from_value::<HashSet<String>>(
                        record["members"].clone(),
                    )?)
                }
                None => None,
            };
            let mut rows = Vec::new();
            let mut checked = 0;
            let mut drafts = 0;
            for (id, bytes) in v.app_records_recent("dataset-example", i64::MAX as usize)? {
                if members.as_ref().is_some_and(|m| !m.contains(&id)) {
                    continue;
                }
                let row: Value = serde_json::from_slice(&bytes)?;
                if row["status"] == "checked" {
                    checked += 1;
                } else {
                    drafts += 1;
                }
                if q.status
                    .as_ref()
                    .is_none_or(|status| status == "all" || row["status"] == *status)
                {
                    rows.push(row);
                }
            }
            let total = rows.len();
            if q.export != Some(true) {
                rows = rows
                    .into_iter()
                    .skip(q.offset.unwrap_or(0))
                    .take(q.limit.unwrap_or(100).clamp(1, 250))
                    .collect();
            }
            Ok(json!({"items":rows,"total":total,"checked":checked,"drafts":drafts}))
        })
        .map_err(bad)?;
    Ok(Json(result))
}

fn validate(row: &Value) -> anyhow::Result<()> {
    let text = row["text"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("invalid_example"))?;
    anyhow::ensure!(
        !text.is_empty() && text.len() <= 128 * 1024,
        "invalid_excerpt"
    );
    let spans = row["spans"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid_spans"))?;
    let mut spans = spans.iter().collect::<Vec<_>>();
    spans.sort_by_key(|s| s["start_byte"].as_u64());
    let mut previous = 0;
    for s in spans {
        let start = s["start_byte"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("invalid_span"))? as usize;
        let end = s["end_byte"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("invalid_span"))? as usize;
        anyhow::ensure!(
            start >= previous
                && start < end
                && text
                    .get(start..end)
                    .is_some_and(|v| Some(v) == s["value"].as_str())
                && s["kind"]
                    .as_str()
                    .is_some_and(|k| kaponir_core::kinds::STANDARD.contains(&k)
                        || matches!(k, "ADDRESS" | "ID"))
                && matches!(s["action"].as_str(), Some("mask" | "keep" | "review")),
            "invalid_span"
        );
        previous = end;
    }
    Ok(())
}
#[derive(Deserialize)]
struct Import {
    items: Vec<Value>,
    #[serde(default)]
    legacy: bool,
}
async fn import(State(st): State<Shared>, headers: HeaderMap, Json(req): Json<Import>) -> Response {
    writable(&st, &headers)?;
    let count = st
        .with_vault(|_, v| {
            transaction(v, || {
                let mut count = 0;
                let mut seen = HashSet::new();
                for row in &req.items {
                    validate(row)?;
                    let id = row["id"].as_str().unwrap_or("");
                    anyhow::ensure!(
                        id.len() <= 160
                            && (id.starts_with("example/") || id.starts_with("feedback/"))
                            && seen.insert(id),
                        "invalid_example_id"
                    );
                    let saved = read(v, id)?;
                    let mut next = match saved {
                        Some(old) if req.legacy => {
                            let _ = old;
                            continue;
                        }
                        Some(mut old) => {
                            anyhow::ensure!(old["text"] == row["text"], "original_excerpt_changed");
                            if old["spans"] == row["spans"] && old["status"] == row["status"] {
                                continue;
                            }
                            anyhow::ensure!(old["revision"] == row["revision"], "example_changed");
                            anyhow::ensure!(
                                matches!(row["status"].as_str(), Some("draft" | "checked")),
                                "invalid_status"
                            );
                            old["spans"] = row["spans"].clone();
                            old["status"] = row["status"].clone();
                            old["revision"] = json!(old["revision"].as_u64().unwrap_or(0) + 1);
                            old
                        }
                        None => {
                            anyhow::ensure!(
                                req.legacy && id.starts_with("feedback/"),
                                "unknown_example"
                            );
                            let mut next = row.clone();
                            next["status"] = json!("checked");
                            next["revision"] = json!(1);
                            next["created"] = json!(now_ms());
                            next
                        }
                    };
                    next["updated"] = json!(now_ms());
                    store(v, id, "dataset-example", &next)?;
                    count += 1;
                }
                Ok(count)
            })
        })
        .map_err(bad)?;
    Ok(Json(json!({"updated":count})))
}
