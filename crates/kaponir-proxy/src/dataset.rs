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
    routing::{get, post, put},
};
use kaponir_vault::SqliteVault;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

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
// Source-specific records retain original excerpts, revisions and collection receipts.
// Only untouched drafts can be folded into another example. Human annotations always stand alone.
fn untouched(row: &Value) -> bool {
    row["status"] == "draft" && row["revision"] == 1
}
fn primary_order(row: &Value) -> (bool, bool, u64, &str) {
    (
        row["status"] != "checked",
        untouched(row),
        row["created"].as_u64().unwrap_or(0),
        row["id"].as_str().unwrap_or_default(),
    )
}
fn draft_group(row: &Value) -> Option<(&str, &str, &str)> {
    if row["status"] != "draft" || !(untouched(row) || row["label_source"] == "finding") {
        return None;
    }
    let value = row["value"].as_str().filter(|s| !s.is_empty())?;
    let span = &row["spans"][0];
    Some(match span["action"].as_str() {
        Some(action @ ("mask" | "keep")) => {
            (value, action, span["kind"].as_str().unwrap_or_default())
        }
        _ => (value, "review", ""),
    })
}
fn example_rows(v: &SqliteVault) -> anyhow::Result<Vec<Value>> {
    v.app_records_recent("dataset-example", i64::MAX as usize)?
        .into_iter()
        .map(|(_, bytes)| Ok(serde_json::from_slice(&bytes)?))
        .collect()
}
struct Examples {
    rows: Vec<Value>,
    owners: Vec<usize>,
}
impl Examples {
    fn load(v: &SqliteVault) -> anyhow::Result<Self> {
        Ok(Self::new(example_rows(v)?))
    }
    fn new(rows: Vec<Value>) -> Self {
        let mut groups = HashMap::new();
        for (index, row) in rows.iter().enumerate() {
            if let Some(key) = draft_group(row) {
                let owner = groups.entry(key).or_insert(index);
                if primary_order(row) < primary_order(&rows[*owner]) {
                    *owner = index;
                }
            }
        }
        let owners = rows
            .iter()
            .enumerate()
            .map(|(index, row)| {
                draft_group(row)
                    .and_then(|key| groups.get(&key).copied())
                    .unwrap_or(index)
            })
            .collect();
        Self { rows, owners }
    }
    fn list(&self, q: &List, members: Option<&HashSet<String>>) -> Value {
        let mut groups = HashMap::<usize, Vec<usize>>::new();
        let mut example = None;
        for (i, row) in self.rows.iter().enumerate() {
            let owner = self.owners[i];
            let id = row["id"].as_str().unwrap_or_default();
            if members.is_none_or(|m| m.contains(id)) {
                if q.example.as_deref() == Some(id) {
                    example = Some(i);
                }
                groups.entry(owner).or_default().push(i);
            }
        }
        let mut indices: Vec<_> = groups.keys().copied().collect();
        indices.sort_unstable(); // retain the vault listing order
        let checked = indices
            .iter()
            .filter(|i| self.rows[**i]["status"] == "checked")
            .count();
        let drafts = indices.len() - checked;
        indices.retain(|i| {
            (q.example.is_none() || example.is_some_and(|row| self.owners[row] == *i))
                && q.status
                    .as_ref()
                    .is_none_or(|s| s == "all" || self.rows[*i]["status"] == *s)
        });
        let total = indices.len();
        let page = if q.export == Some(true) {
            indices.as_slice()
        } else {
            let offset = q.offset.unwrap_or_default().min(total);
            &indices[offset..total.min(offset + q.limit.unwrap_or(100).clamp(1, 250))]
        };
        let mut items = Vec::with_capacity(page.len());
        for owner in page {
            let sources = &groups[owner];
            // Keep the excerpt inside the requested collection where it has its own untouched draft.
            let displayed = if let Some(requested) = example {
                requested
            } else if !sources.contains(owner) {
                *sources
                    .iter()
                    .min_by_key(|i| primary_order(&self.rows[**i]))
                    .unwrap()
            } else {
                *owner
            };
            let mut row = self.rows[displayed].clone();
            if sources.iter().any(|index| *index != displayed) {
                let mut occurrences = Vec::with_capacity(sources.len());
                for i in sources {
                    let source = &self.rows[*i];
                    occurrences.push(json!({
                        "id": source["id"], "finding": source["finding"], "source": source["source"],
                        "kind": source["kind"], "text": source["text"], "spans": source["spans"]
                    }));
                }
                row["occurrences"] = Value::Array(occurrences);
            }
            items.push(row);
        }
        json!({"items":items,"total":total,"checked":checked,"drafts":drafts})
    }
}

pub(crate) struct ExampleIndex {
    by_value: HashSet<String>,
    by_finding: HashMap<String, String>,
}

impl ExampleIndex {
    pub(crate) fn status(&self, id: &str) -> Option<&String> {
        self.by_finding.get(id)
    }
}
pub(crate) fn example_index(v: &SqliteVault) -> anyhow::Result<ExampleIndex> {
    let mut by_value = HashSet::new();
    let mut by_finding = HashMap::new();
    for row in example_rows(v)? {
        if let Some(value) = row["value"].as_str() {
            by_value.insert(value.to_owned());
        }
        if let (Some(id), Some(status)) = (row["id"].as_str(), row["status"].as_str()) {
            by_finding.insert(id.to_owned(), status.to_owned());
        }
    }
    Ok(ExampleIndex {
        by_value,
        by_finding,
    })
}
pub(crate) fn capture_enabled(v: &SqliteVault) -> anyhow::Result<bool> {
    Ok(v.private_setting("dataset:capture-review")?.as_deref() == Some(b"true"))
}
#[derive(Deserialize)]
struct Preferences {
    capture_review: bool,
}
async fn preferences(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<Preferences>,
) -> Response {
    writable(&st, &headers)?;
    st.with_vault(|_, v| {
        Ok(v.set_private_setting(
            "dataset:capture-review",
            if req.capture_review {
                b"true"
            } else {
                b"false"
            },
        )?)
    })
    .map_err(internal)?;
    Ok(Json(json!({"capture_review":req.capture_review})))
}

pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/api/v1/ui/dataset", get(list))
        .route("/api/v1/ui/dataset/collect", post(collect))
        .route("/api/v1/ui/dataset/preferences", put(preferences))
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    from: Option<CollectFrom>,
}
#[derive(Deserialize, Serialize, Clone, Copy)]
#[serde(rename_all = "snake_case")]
enum CollectFrom {
    All,
    Resolved,
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
        Json(
            json!({"id":receipt,"added":saved["added"],"existing":saved["existing"],"new_occurrences":saved["new_occurrences"]}),
        )
    };
    if let Some(saved) = st.with_vault(|_, v| read(v, &id)).map_err(internal)? {
        if saved["signature"] != signature {
            return Err(error(StatusCode::CONFLICT, "request_id_conflict"));
        }
        return Ok(summary(&saved));
    }
    let items = if let Some(source) = req.from {
        if req.selection.has_explicit_selection() {
            return Err(bad(anyhow::anyhow!("invalid_selection")));
        }
        crate::review::items(&st)
            .map_err(internal)?
            .into_iter()
            .filter(|item| {
                matches!(source, CollectFrom::All) || !crate::review::needs_decision(item)
            })
            .collect()
    } else {
        req.selection.resolve(&st, true).map_err(bad)?
    };
    let result = st
        .with_vault(|_, vault| {
            transaction(vault, || {
                if let Some(saved) = read(vault, &id)? {
                    anyhow::ensure!(saved["signature"] == signature, "request_id_conflict");
                    return Ok(saved);
                }
                let known = example_index(vault)?;
                let mut values = HashSet::new();
                let mut inserted = Vec::new();
                let mut members = Vec::new();
                for item in &items {
                    let key = finding_id(item);
                    values.insert(training_value(item));
                    members.push(key.clone());
                    match read(vault, &key)? {
                        None => {
                            let row = draft_example(&key, &receipt, item);
                            store(vault, &key, "dataset-example", &row)?;
                            inserted.push(key);
                        }
                        Some(old) if req.from.is_some() && draft_group(&old).is_some() => {
                            let draft = draft_example(&key, &receipt, item);
                            if matches!(draft["spans"][0]["action"].as_str(), Some("mask" | "keep"))
                                && old["spans"] != draft["spans"]
                            {
                                let mut row = old;
                                row["spans"] = draft["spans"].clone();
                                row["value"] = draft["value"].clone();
                                row["kind"] = draft["kind"].clone();
                                row["updated"] = json!(now_ms());
                                row["revision"] = json!(row["revision"].as_u64().unwrap_or(1) + 1);
                                row["label_source"] = json!("finding");
                                store(vault, &key, "dataset-example", &row)?;
                            }
                        }
                        Some(_) => {}
                    }
                }
                let existing = values
                    .iter()
                    .filter(|value| known.by_value.contains(**value))
                    .count();
                let result = json!({
                    "id": receipt, "added": values.len() - existing, "existing": existing,
                    "new_occurrences": inserted.len(), "members": members, "inserted": inserted,
                    "created": now_ms(), "signature": signature
                });
                store(vault, &id, "dataset-collection", &result)?;
                Ok(result)
            })
        })
        .map_err(bad)?;
    Ok(summary(&result))
}
// Ground the full original first so two filenames with the same directory remain distinguishable.
fn training_value(item: &Value) -> &str {
    let value = item["value"].as_str().unwrap_or_default();
    item["document_rule"]["path_prefix"]
        .as_str()
        .or(item["dataset_prefix"].as_str())
        .filter(|prefix| item["kind"] == "PATH" && value.starts_with(prefix))
        .map(|p| p.trim_end_matches(['/', '\\']))
        .unwrap_or(value)
}

fn draft_example(id: &str, collection: &str, item: &Value) -> Value {
    let value = item["value"].as_str().unwrap_or_default();
    let text = item["context"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or(value);
    let span: Option<[usize; 2]> = serde_json::from_value(item["context_span"].clone()).ok();
    let range = span
        .filter(|[start, end]| {
            start < end
                && text.get(*start..*end).is_some_and(|surface| {
                    surface == value
                        || surface.to_lowercase() == value.to_lowercase()
                        || surface == value.replace(['\r', '\n'], " ")
                })
        })
        .or_else(|| {
            let start = text.find(value)?;
            (!value.is_empty() && text.matches(value).count() == 1)
                .then_some([start, start + value.len()])
        });
    let value = training_value(item);
    let range = range.and_then(|[start, end]| {
        if item["document_rule"]["path_prefix"].is_string() || item["dataset_prefix"].is_string() {
            text.get(start..end)?
                .starts_with(value)
                .then_some([start, start + value.len()])
        } else {
            Some([start, end])
        }
    });
    let decision = &item["decision"];
    let rule = &item["document_rule"];
    let resolved = match item["state"].as_str() {
        Some("masked" | "active") => Some("mask"),
        Some("kept") => Some("keep"),
        Some("covered") => rule["action"].as_str(),
        Some("rejected") if item["reason"] == "human_keep" || decision["action"] == "keep" => {
            Some("keep")
        }
        _ => None,
    };
    let kind = if resolved.is_some() {
        decision["kind"]
            .as_str()
            .or(rule["kind"].as_str().filter(|kind| *kind != "KEEP"))
            .or(item["kind"].as_str())
    } else {
        item["kind"].as_str()
    }
    .unwrap_or("CUSTOM");
    let spans: Vec<_> = range.into_iter().map(|[start,end]| json!({
        "value": &text[start..end], "start_byte": start, "end_byte": end, "kind": kind, "action": resolved.unwrap_or("review")
    })).collect();
    let source = item["source"]
        .as_str()
        .or(item["origin"].as_str())
        .unwrap_or("detector");
    let language = if text.chars().any(|c| ('А'..='я').contains(&c)) {
        "ru"
    } else {
        "en"
    };
    let now = now_ms();
    json!({
        "id": id, "collection": collection, "source": source, "finding": item["key"], "value": value,
        "kind": kind, "text": text, "status": "draft", "revision": 1, "created": now, "updated": now,
        "group": "field-feedback", "language": language, "format": "text", "spans": spans
    })
}

pub(crate) enum Capture {
    Checked,
    Draft,
    Unchanged,
}

/// Capture only the shown, grounded occurrence as checked. Related excerpts stay drafts.
pub(crate) fn capture_finding(
    v: &SqliteVault,
    item: &Value,
    label: Option<(&str, &str)>,
    seen: bool,
) -> anyhow::Result<Capture> {
    let id = finding_id(item);
    let existing = read(v, &id)?;
    if let Some(row) = existing.as_ref() {
        let may_update = untouched(row)
            || (row["label_source"] == "finding" && (seen || row["status"] == "draft"));
        if !may_update {
            return Ok(Capture::Unchanged);
        }
    }
    let grounded = draft_example(&id, "", item);
    let mut row = existing.clone().unwrap_or_else(|| grounded.clone());
    if let Some((action, kind)) = label
        && let Some(mut span) = grounded["spans"]
            .as_array()
            .and_then(|s| s.first())
            .cloned()
    {
        span["kind"] = json!(kind);
        span["action"] = json!(action);
        row["spans"] = json!([span]);
        row["kind"] = json!(kind);
        row["value"] = grounded["value"].clone();
        row["label_source"] = json!("finding");
        if seen && item["context"].as_str().is_some_and(|s| !s.is_empty()) && validate(&row).is_ok()
        {
            row["status"] = json!("checked");
        }
    }
    if existing.as_ref() == Some(&row) {
        return Ok(Capture::Unchanged);
    }
    if existing.is_some() {
        row["revision"] = json!(row["revision"].as_u64().unwrap_or(1) + 1);
    }
    row["updated"] = json!(now_ms());
    store(v, &id, "dataset-example", &row)?;
    Ok(if row["status"] == "checked" {
        Capture::Checked
    } else {
        Capture::Draft
    })
}

async fn undo(State(st): State<Shared>, headers: HeaderMap, Path(id): Path<String>) -> Response {
    writable(&st, &headers)?;
    let removed = st
        .with_vault(|_, v| {
            transaction(v, || {
                let key = format!("collection:{id}");
                let mut receipt =
                    read(v, &key)?.ok_or_else(|| anyhow::anyhow!("collection_not_found"))?;
                let mut removed_values = HashSet::new();
                let mut removed_occurrences = 0;
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
                        if let Some(value) = row["value"].as_str() {
                            removed_values.insert(value.to_owned());
                        }
                        removed_occurrences += 1;
                    }
                }
                receipt["inserted"] = json!([]);
                store(v, &key, "dataset-collection", &receipt)?;
                let remaining = example_index(v)?;
                let removed = removed_values
                    .iter()
                    .filter(|value| !remaining.by_value.contains(*value))
                    .count();
                Ok(json!({"removed":removed,"removed_occurrences":removed_occurrences}))
            })
        })
        .map_err(bad)?;
    Ok(Json(removed))
}
#[derive(Default, Deserialize)]
struct List {
    status: Option<String>,
    collection: Option<String>,
    example: Option<String>,
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
            let mut result = Examples::load(v)?.list(&q, members.as_ref());
            result["editable"] = json!(st.control.is_some());
            result["capture_review"] = json!(capture_enabled(v)?);
            Ok(result)
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
                    .is_some_and(|k| crate::review::supported_kinds().any(|known| known == k)
                        || k == "ADDRESS")
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
                            old.as_object_mut().unwrap().remove("label_source");
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

#[cfg(test)]
mod tests {
    use super::*;

    fn draft(id: usize, value: usize) -> Value {
        json!({"id":format!("example/{id}"),"value":value.to_string(),"status":"draft","revision":1,"created":id,"text":format!("source {id}: {value}"),"spans":[]})
    }

    #[test]
    fn historical_drafts_use_the_actual_span_and_the_effective_action() {
        let mut finding = json!({"key":"test","value":"cedar","kind":"PROJ","context":"CEDAR / cedar",
            "context_span":[0,5],"state":"covered","document_rule":{"action":"keep","kind":"KEEP"}});
        let draft = draft_example("example/1", "collection", &finding);
        assert_eq!(draft["status"], "draft");
        assert_eq!(
            draft["spans"][0],
            json!({"value":"CEDAR", "kind":"PROJ", "action":"keep","start_byte":0,"end_byte":5})
        );
        finding["state"] = json!("rejected");
        finding["context"] = json!("cedar / cedar");
        finding["context_span"] = Value::Null;
        assert_eq!(draft_example("example/1", "", &finding)["spans"], json!([]));
        finding["context"] = json!("cedar");
        assert_eq!(
            draft_example("example/1", "", &finding)["spans"][0]["action"],
            "review"
        );
    }

    #[test]
    fn legacy_draft_export_groups_1104_records_into_645_values_and_keeps_every_excerpt() {
        let examples = Examples::new((0..1104).map(|id| draft(id, id % 645)).collect());
        let result = examples.list(
            &List {
                export: Some(true),
                status: Some("draft".into()),
                ..Default::default()
            },
            None,
        );
        assert_eq!(result["total"], 645);
        assert_eq!(result["drafts"], 645);
        let rows = result["items"].as_array().unwrap();
        let contexts: HashSet<_> = rows
            .iter()
            .flat_map(|row| match row["occurrences"].as_array() {
                Some(sources) => sources
                    .iter()
                    .map(|r| r["text"].as_str().unwrap())
                    .collect(),
                None => vec![row["text"].as_str().unwrap()],
            })
            .collect();
        assert_eq!(contexts.len(), 1104);
        let page = examples.list(
            &List {
                offset: Some(600),
                limit: Some(50),
                ..Default::default()
            },
            None,
        );
        assert_eq!(page["items"].as_array().unwrap().len(), 45);
    }

    #[test]
    fn previous_edits_legacy_links_and_collection_excerpts_survive_grouping() {
        let mut checked = draft(1, 7);
        checked["status"] = json!("checked");
        checked["revision"] = json!(2);
        let mut edited = draft(2, 7);
        edited["revision"] = json!(3);
        edited["spans"] = json!([]);
        let legacy =
            json!({"id":"feedback/1","text":"independent","revision":1,"status":"checked"});
        let examples = Examples::new(vec![
            draft(0, 7),
            checked,
            edited,
            legacy,
            draft(3, 8),
            draft(4, 8),
        ]);
        let result = examples.list(&List::default(), None);
        assert_eq!(result["checked"], 2);
        assert_eq!(result["drafts"], 3);
        let alias = examples.list(
            &List {
                example: Some("example/0".into()),
                ..Default::default()
            },
            None,
        );
        assert_eq!(alias["items"][0]["id"], "example/0");
        assert_eq!(alias["items"][0]["status"], "draft");
        let exact = List {
            example: Some("example/4".into()),
            ..Default::default()
        };
        assert_eq!(examples.list(&exact, None)["items"][0]["id"], "example/4");
        let members = HashSet::from(["example/4".into()]);
        assert_eq!(
            examples.list(&List::default(), Some(&members))["items"][0]["id"],
            "example/4"
        );
    }
}
