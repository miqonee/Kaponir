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
fn finding_id(item: &Value) -> String {
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
struct Examples {
    rows: Vec<Value>,
    primary: HashMap<String, usize>,
}
impl Examples {
    fn load(v: &SqliteVault) -> anyhow::Result<Self> {
        let rows = v
            .app_records_recent("dataset-example", i64::MAX as usize)?
            .into_iter()
            .map(|(_, bytes)| serde_json::from_slice(&bytes))
            .collect::<Result<_, _>>()?;
        Ok(Self::new(rows))
    }

    fn new(rows: Vec<Value>) -> Self {
        let mut primary = HashMap::<String, usize>::new();
        for (index, row) in rows.iter().enumerate() {
            if let Some(value) = row["value"].as_str().filter(|s| !s.is_empty()) {
                primary
                    .entry(value.to_owned())
                    .and_modify(|old| {
                        if primary_order(row) < primary_order(&rows[*old]) {
                            *old = index;
                        }
                    })
                    .or_insert(index);
            }
        }
        Self { rows, primary }
    }

    fn owner(&self, index: usize) -> usize {
        let row = &self.rows[index];
        if untouched(row)
            && let Some(primary) = row["value"].as_str().and_then(|v| self.primary.get(v))
        {
            return *primary;
        }
        index
    }

    fn list(&self, q: &List, members: Option<&HashSet<String>>) -> Value {
        let mut groups = HashMap::<usize, Vec<usize>>::new();
        let mut example = None;
        for (i, row) in self.rows.iter().enumerate() {
            let owner = self.owner(i);
            let id = row["id"].as_str().unwrap_or_default();
            if q.example.as_deref() == Some(id) {
                example = Some(owner);
            }
            if members.is_none_or(|m| m.contains(id)) {
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
            (q.example.is_none() || example == Some(*i))
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
            let displayed = if untouched(&self.rows[*owner]) && !sources.contains(owner) {
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
    by_value: HashMap<String, String>,
    annotated: HashSet<String>,
}

impl ExampleIndex {
    pub(crate) fn for_finding(&self, item: &Value) -> Option<&String> {
        if !self.annotated.is_empty()
            && let Some(id) = self.annotated.get(&finding_id(item))
        {
            return Some(id);
        }
        self.by_value.get(item["value"].as_str()?)
    }
}

pub(crate) fn example_index(v: &SqliteVault) -> anyhow::Result<ExampleIndex> {
    let examples = Examples::load(v)?;
    let annotated = examples
        .rows
        .iter()
        .filter(|row| !untouched(row))
        .filter_map(|row| row["id"].as_str().map(str::to_owned))
        .collect();
    let by_value = examples
        .primary
        .into_iter()
        .map(|(value, index)| {
            (
                value,
                examples.rows[index]["id"].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    Ok(ExampleIndex {
        by_value,
        annotated,
    })
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
    let items = req.selection.resolve(&st, true).map_err(bad)?;
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
                    values.insert(item["value"].as_str().unwrap_or_default());
                    members.push(key.clone());
                    if vault.app_record(&key)?.is_none() {
                        let row = draft_example(&key, &receipt, item);
                        store(vault, &key, "dataset-example", &row)?;
                        inserted.push(key);
                    }
                }
                let existing = values
                    .iter()
                    .filter(|value| known.by_value.contains_key(**value))
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
fn draft_example(id: &str, collection: &str, item: &Value) -> Value {
    let value = item["value"].as_str().unwrap_or_default();
    let text = item["context"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or(value);
    let span: Option<[usize; 2]> = serde_json::from_value(item["context_span"].clone()).ok();
    let start = span
        .filter(|[a, b]| text.get(*a..*b) == Some(value))
        .map(|[a, _]| a)
        .or_else(|| {
            text.find(value)
                .filter(|_| !value.is_empty() && text.matches(value).count() == 1)
        });
    let spans: Vec<_> = start.into_iter().map(|from| json!({
        "value": value, "start_byte": from, "end_byte": from + value.len(), "kind": item["kind"], "action": "review"
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
        "kind": item["kind"], "text": text, "status": "draft", "revision": 1, "created": now, "updated": now,
        "group": "field-feedback", "language": language, "format": "text", "spans": spans
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
                    .filter(|value| !remaining.by_value.contains_key(*value))
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
            Ok(Examples::load(v)?.list(&q, members.as_ref()))
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

#[cfg(test)]
mod tests {
    use super::*;

    fn draft(id: usize, value: usize) -> Value {
        json!({"id":format!("example/{id}"),"value":value.to_string(),"status":"draft","revision":1,"created":id,"text":format!("source {id}: {value}"),"spans":[]})
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
        assert_eq!(result["drafts"], 2);
        let alias = examples.list(
            &List {
                example: Some("example/0".into()),
                ..Default::default()
            },
            None,
        );
        assert_eq!(alias["items"][0]["id"], "example/1");
        assert_eq!(
            alias["items"][0]["occurrences"].as_array().unwrap().len(),
            2
        );
        let members = HashSet::from(["example/4".into()]);
        assert_eq!(
            examples.list(&List::default(), Some(&members))["items"][0]["id"],
            "example/4"
        );
    }
}
