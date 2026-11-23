//! Explicit, durable model review. Recommendations never mutate rules until the user applies them.
use crate::{
    Shared, learning,
    server::{ApiError, authorized, error, internal},
    traffic::now_ms,
};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use futures_util::{StreamExt, stream};
use kaponir_core::Scope;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

type ResultJson = Result<Json<Value>, ApiError>;
fn bad(code: &str) -> ApiError {
    error(StatusCode::BAD_REQUEST, code)
}
fn read(st: &Shared, id: &str) -> anyhow::Result<Option<Value>> {
    st.with_vault(|_, v| {
        v.app_record(id)?
            .map(|b| Ok(serde_json::from_slice(&b)?))
            .transpose()
    })
}
fn write(st: &Shared, id: &str, kind: &str, value: &Value) -> anyhow::Result<()> {
    st.with_vault(|_, v| {
        v.put_app_record(id, kind, &serde_json::to_vec(value)?)?;
        Ok(())
    })
}
fn fingerprint(item: &Value) -> String {
    blake3::hash(
        serde_json::to_string(&json!([
            item["value"],
            item["kind"],
            item["context"],
            item["protected"]
        ]))
        .unwrap()
        .as_bytes(),
    )
    .to_hex()
    .to_string()
}
pub(crate) fn items(st: &Shared) -> anyhow::Result<Vec<Value>> {
    let rows = st.with_vault(|_, v| Ok(v.journal(None, i64::MAX as usize)?))?;
    let mut events = HashMap::new();
    for event in st.traffic.summary_snapshot().events {
        for id in event.usage.review_ids {
            events
                .entry(id)
                .or_insert((event.id, event.session_id.clone()));
        }
    }
    let mut all = Vec::new();
    for row in rows {
        let mut item = serde_json::to_value(&row)?;
        item["key"] = json!(format!("detector:{}", row.id));
        item["origin"] = json!("detector");
        item["id"] = json!(row.id.to_string());
        item["state"] = json!(row.status);
        item["updated"] = json!(row.last_seen.saturating_mul(1000));
        item["protected"] = json!(row.category == "credential");
        if let Some((event, session)) = events.get(&row.id) {
            item["event_id"] = json!(event);
            item["session"] = json!(session);
            item["source_type"] = json!("session");
        }
        all.push(item);
    }
    let mut document_rules = HashMap::new();
    if let Some(ctl) = &st.control {
        for set in ctl.rules()? {
            for (index, rule) in set.file.rules.iter().enumerate() {
                if !rule.disabled
                    && rule.note.starts_with("kaponir discover:")
                    && let Some(value) = &rule.value
                {
                    document_rules
                        .entry((value.clone(), rule.kind.clone()))
                        .or_insert(json!({"id":rule.id,"scope":set.scope,"index":index}));
                }
            }
        }
    }
    for mut entry in learning::review_entries(st)? {
        entry["key"] = json!(format!(
            "learning:{}",
            entry["id"].as_str().unwrap_or_default()
        ));
        entry["origin"] = json!("learning");
        let job_id = entry["job_id"].as_str().unwrap_or_default();
        let session = job_id.starts_with("learning:job:turn:")
            || (job_id.starts_with("document/")
                && entry["source"]
                    .as_str()
                    .unwrap_or_default()
                    .starts_with("session-"));
        if let Some(event) = job_id
            .strip_prefix("learning:job:turn:")
            .and_then(|s| s.parse::<u64>().ok())
        {
            entry["event_id"] = json!(event);
        }
        entry["source_type"] = json!(if session { "session" } else { "document" });
        let pair = (
            entry["value"].as_str().unwrap_or_default().to_owned(),
            entry["kind"].as_str().unwrap_or_default().to_owned(),
        );
        if let Some(rule) = document_rules.get(&pair) {
            entry["document_rule"] = rule.clone();
            if entry["state"] == "pending" {
                entry["state"] = json!("covered");
            }
        }
        entry["context"] = entry["quote"].clone();
        entry["protected"] = json!(entry["kind"] == "SECRET");
        all.push(entry);
    }
    let revision = st.learning.snapshot().revision;
    let (mut recommendations, mut decisions, dataset) = st.with_vault(|_, v| {
        let records = |kind: &str| -> anyhow::Result<HashMap<String, Value>> {
            v.app_records(kind, i64::MAX as usize)?
                .into_iter()
                .map(|(id, data)| Ok((id, serde_json::from_slice(&data)?)))
                .collect()
        };
        Ok((
            records("review-recommendation")?,
            records("review-decision")?,
            v.app_record_ids("dataset-example")?
                .into_iter()
                .collect::<HashSet<_>>(),
        ))
    })?;
    for item in &mut all {
        let key = item["key"].as_str().unwrap().to_owned();
        if let Some(mut rec) = recommendations.remove(&format!("review-rec:{key}")) {
            if rec["revision"].as_u64() != Some(revision) || rec["fingerprint"] != fingerprint(item)
            {
                rec["state"] = json!("stale");
            }
            item["recommendation"] = rec;
        }
        if let Some(decision) = decisions.remove(&format!("review-decision:{key}")) {
            item["decision"] = decision;
        }
        item["in_dataset"] = json!(dataset.contains(&crate::dataset::finding_id(item)));
    }
    Ok(all)
}
pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/api/v1/ui/review", get(list))
        .route("/api/v1/ui/review/selection", post(selection))
        .route("/api/v1/ui/jobs", get(jobs))
        .route("/api/v1/ui/review/jobs/{id}/retry", post(retry))
        .route("/api/v1/ui/review/jobs", post(submit))
        .route("/api/v1/ui/review/from-event", post(from_event))
        .route("/api/v1/ui/review/jobs/{id}", get(job))
        .route("/api/v1/ui/review/{origin}/{id}/{action}", post(apply))
}

#[derive(Deserialize)]
struct FromEvent {
    event_id: u64,
    indexes: Vec<usize>,
}
async fn from_event(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<FromEvent>,
) -> ResultJson {
    use kaponir_core::Vault;
    authorized(&st, &headers, true)?;
    if st.control.is_none() {
        return Err(bad("read_only"));
    }
    if req.indexes.is_empty() || req.indexes.len() > 64 {
        return Err(bad("select_up_to_64"));
    }
    let event = st
        .traffic
        .event_by_id(req.event_id)
        .or_else(|| {
            st.with_vault(|_, v| {
                Ok(v.load_traffic_events(200)?
                    .into_iter()
                    .filter_map(|raw| serde_json::from_slice::<crate::traffic::Event>(&raw).ok())
                    .find(|event| event.id == req.event_id))
            })
            .ok()
            .flatten()
        })
        .ok_or_else(|| bad("event_not_found"))?;
    let mut selected = Vec::new();
    for index in req.indexes {
        let item = event
            .usage
            .masked_items
            .get(index)
            .ok_or_else(|| bad("entry_not_found"))?;
        if item.protected {
            return Err(bad("protected_credential"));
        }
        if item.from_cache || !matches!(item.origin, Some(kaponir_core::Origin::Builtin)) {
            return Err(bad("existing_rule"));
        }
        let source = event
            .inspector
            .as_ref()
            .map(|i| i.original_request.as_str())
            .unwrap_or_default();
        let mut context = if let Some(reference) = event
            .usage
            .review_items
            .iter()
            .find(|r| r.entry.value == item.original)
        {
            reference.entry.context.clone()
        } else if let Some(position) = source.find(&item.original) {
            let lo = kaponir_core::engine::floor_boundary(source, position.saturating_sub(100));
            let hi = kaponir_core::engine::ceil_boundary(
                source,
                (position + item.original.len() + 100).min(source.len()),
            );
            source[lo..hi].to_string()
        } else {
            String::new()
        };
        for other in &event.usage.masked_items {
            if other.original != item.original && !other.original.is_empty() {
                context = context.replace(&other.original, "[REDACTED]");
            }
        }
        selected.push(kaponir_core::journal::JournalEntry {
            kind: item.kind.clone(),
            category: serde_json::from_value(json!(item.category))
                .map_err(|_| bad("invalid_kind"))?,
            recognizer: item.recognizer.clone().unwrap_or_default(),
            value: item.original.clone(),
            shape: kaponir_core::journal::shape(&item.original),
            context: redacted_context(&st, &context, &item.original),
            probability: item.probability.unwrap_or(0.5),
            applied: kaponir_core::score::Verdict::Mask,
            evidence: item.evidence.clone(),
        });
    }
    let keys = st
        .with_vault(|_, v| {
            let mut keys = Vec::new();
            for entry in selected {
                v.record(&entry)?;
                if let Some(id) = v.journal_id(&entry.kind, &entry.value)? {
                    let status = v.journal_states()?.get(&id).cloned();
                    if status.as_deref() == Some("dismissed") {
                        v.set_journal_status(id, "open")?;
                    }
                    keys.push(format!("detector:{id}"));
                }
            }
            Ok(keys)
        })
        .map_err(internal)?;
    Ok(Json(json!({"keys":keys})))
}

fn decorate_event(event: &mut Value, states: &std::collections::HashMap<i64, String>) {
    let usage = &event["usage"];
    let mut reasons = Vec::new();
    for (field, code) in [
        ("unknown", "unknown_restore_token"),
        ("denied", "restore_access_denied"),
    ] {
        if let Some(count) = usage[field].as_u64().filter(|n| *n > 0) {
            reasons.push(json!({"code":code,"count":count,"state":"open","keys":[]}));
        }
    }
    let ids = usage["review_ids"].as_array().cloned().unwrap_or_default();
    if usage["uncertain"].as_u64().unwrap_or(0) > 0 {
        if ids.is_empty() {
            reasons.push(json!({"code":"legacy_uncertain","count":usage["uncertain"],"state":"unlinked","keys":[]}));
        } else {
            for state in ["open", "resolved", "unlinked"] {
                let keys: Vec<_> = ids
                    .iter()
                    .filter_map(Value::as_i64)
                    .filter(|id| {
                        let actual = match states.get(id) {
                            Some(status) if status == "open" => "open",
                            Some(_) => "resolved",
                            None => "unlinked",
                        };
                        actual == state
                    })
                    .map(|id| format!("detector:{id}"))
                    .collect();
                if !keys.is_empty() {
                    reasons.push(json!({"code":"detector_uncertain","count":keys.len(),"state":state,"keys":keys}));
                }
            }
        }
    }
    event["review_reasons"] = json!(reasons);
}
pub(crate) fn traffic_summary(st: &Shared) -> Value {
    let mut snapshot = json!(st.traffic.summary_snapshot());
    let states = st
        .with_vault(|_, v| Ok(v.journal_states()?))
        .unwrap_or_default();
    for field in ["events", "active"] {
        if let Some(events) = snapshot[field].as_array_mut() {
            for event in events {
                decorate_event(event, &states);
            }
        }
    }
    snapshot
}
pub(crate) fn event_detail(st: &Shared, event: crate::traffic::Event) -> Value {
    let mut value = json!(event);
    let states = st
        .with_vault(|_, v| Ok(v.journal_states()?))
        .unwrap_or_default();
    decorate_event(&mut value, &states);
    value
}
fn cleanup(st: &Shared) {
    let _ = st.with_vault(|_, v| {
        for kind in [
            "review-job-succeeded",
            "review-job-failed",
            "review-recommendation",
            "review-selection",
            "dataset-collection",
        ] {
            for (id, bytes) in v.app_records(kind, 4000)? {
                let record: Value = serde_json::from_slice(&bytes)?;
                if now_ms().saturating_sub(record["created"].as_u64().unwrap_or(0))
                    > 30 * 24 * 60 * 60 * 1000
                {
                    if let Some(job) = id.strip_prefix("review-job:") {
                        for packet in v.app_record_ids(&format!("review-results:{job}"))? {
                            v.delete_app_record(&packet)?;
                        }
                        v.delete_app_record(&format!("review-targets:{job}"))?;
                    }
                    v.delete_app_record(&id)?;
                }
            }
        }
        Ok(())
    });
}
pub(crate) fn start(st: &Shared) {
    let _ = st.with_vault(|_, v| {
        learning::transaction(v, || {
            if v.private_setting("workspace:discovery-migrated")?.is_none() {
                learning::retain_findings(v, &crate::discovery::load(v)?.report())?;
                v.set_private_setting("workspace:discovery-migrated", b"1")?;
            }
            Ok(())
        })
    });
    cleanup(st);
    // A restart must not silently send old work to another connection.
    let _ = st.with_vault(|_, v| {
        for state in ["queued", "running"] {
            for (id, bytes) in v.app_records(&format!("review-job-{state}"), i64::MAX as usize)? {
                let mut job: Value = serde_json::from_slice(&bytes)?;
                job["state"] = json!("failed");
                job["error"] = json!("interrupted_review");
                job["active"] = json!(0);
                job["updated"] = json!(now_ms());
                v.put_app_record(&id, "review-job-failed", &serde_json::to_vec(&job)?)?;
            }
        }
        Ok(())
    });
    let state = st.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
            cleanup(&state);
        }
    });
}
#[derive(Default, Deserialize, Serialize)]
pub(crate) struct Filters {
    status: Option<String>,
    source: Option<String>,
    q: Option<String>,
    kind: Option<String>,
    action: Option<String>,
    sort: Option<String>,
    keys: Option<String>,
    snapshot: Option<String>,
    job_id: Option<String>,
    learning_job: Option<String>,
    rule_id: Option<String>,
    rule_scope: Option<String>,
    family: Option<String>,
    id: Option<String>,
    outcome: Option<String>,
    stable: Option<bool>,
    offset: Option<usize>,
    limit: Option<usize>,
}
fn filtered(st: &Shared, q: &Filters) -> anyhow::Result<(usize, Vec<Value>)> {
    let all = items(st)?;
    let snapshot = if let Some(id) = &q.snapshot {
        Some(selection_keys(st, id)?)
    } else {
        None
    };
    let allowed = snapshot
        .as_ref()
        .map(|keys| keys.iter().map(String::as_str).collect::<HashSet<_>>());
    let mut historical = Value::Null;
    let job_keys = if let Some(id) = &q.job_id {
        let job = full_job(st, id)?.ok_or_else(|| anyhow::anyhow!("job_not_found"))?;
        historical = job["recommendations"].clone();
        Some(
            job["targets"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|target| {
                    let key = target["key"].as_str()?;
                    let outcome = job["results"][key].as_str().unwrap_or("unfinished");
                    q.outcome
                        .as_ref()
                        .is_none_or(|s| s.is_empty() || s == outcome)
                        .then(|| key.to_owned())
                })
                .collect::<HashSet<_>>(),
        )
    } else {
        None
    };
    let keys = q
        .keys
        .as_ref()
        .map(|s| s.split(',').collect::<HashSet<_>>());
    let pending = all
        .iter()
        .filter(|i| matches!(i["state"].as_str(), Some("open" | "pending")))
        .count();
    let mut selected: Vec<_> = all
        .into_iter()
        .filter(|item| {
            let is_pending = matches!(item["state"].as_str(), Some("open" | "pending"));
            let key = item["key"].as_str().unwrap_or_default();
            allowed.as_ref().is_none_or(|set| set.contains(key))
                && q.rule_id.as_ref().is_none_or(|id| {
                    [
                        (&item["rule_id"], &item["scope"]),
                        (&item["decision"]["rule_id"], &item["decision"]["scope"]),
                        (
                            &item["document_rule"]["id"],
                            &item["document_rule"]["scope"],
                        ),
                    ]
                    .iter()
                    .any(|(rule, scope)| {
                        **rule == *id && q.rule_scope.as_ref().is_none_or(|s| **scope == *s)
                    })
                })
                && q.learning_job
                    .as_ref()
                    .is_none_or(|id| item["job_id"] == *id)
                && job_keys.as_ref().is_none_or(|set| set.contains(key))
                && (q.status.as_deref() == Some("all")
                    || if q.status.as_deref() == Some("history") {
                        !is_pending
                    } else {
                        is_pending
                    })
                && q.source
                    .as_deref()
                    .is_none_or(|s| s == "all" || item["origin"] == s || item["source_type"] == s)
                && q.kind
                    .as_deref()
                    .is_none_or(|s| s.is_empty() || item["kind"] == s)
                && q.action
                    .as_deref()
                    .is_none_or(|s| s.is_empty() || item["applied"] == s)
                && keys.as_ref().is_none_or(|set| set.contains(key))
                && q.q.as_ref().is_none_or(|s| {
                    format!(
                        "{} {} {}",
                        item["source"].as_str().unwrap_or_default(),
                        item["value"].as_str().unwrap_or_default(),
                        item["context"].as_str().unwrap_or_default()
                    )
                    .to_lowercase()
                    .contains(&s.to_lowercase())
                })
        })
        .collect();
    selected.sort_by(|a, b| {
        let field = match q.sort.as_deref() {
            Some("frequency") => "count",
            Some("detector_score") => "probability",
            Some("review_score") => "recommendation",
            _ => "updated",
        };
        let score = |i: &Value| {
            if field == "recommendation" {
                if i["recommendation"]["state"] == "succeeded" {
                    i["recommendation"]["confidence"].as_f64().unwrap_or(-1.0)
                } else {
                    -1.0
                }
            } else {
                i[field].as_f64().unwrap_or(-1.0)
            }
        };
        score(b)
            .total_cmp(&score(a))
            .then_with(|| b["key"].as_str().cmp(&a["key"].as_str()))
    });
    if !historical.is_null() {
        for item in &mut selected {
            let key = item["key"].as_str().unwrap_or_default();
            if let Some(recommendation) = historical.get(key) {
                item["job_recommendation"] = recommendation.clone();
            }
        }
    }
    if let Some(keys) = snapshot {
        let order: HashMap<_, _> = keys.into_iter().enumerate().map(|(i, k)| (k, i)).collect();
        selected.sort_by_key(|item| order.get(item["key"].as_str().unwrap()).copied());
    }
    Ok((pending, selected))
}

async fn list(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(q): Query<Filters>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let (pending, selected) = filtered(&st, &q).map_err(internal)?;
    let total = selected.len();
    let snapshot =
        if q.stable == Some(true) && q.snapshot.is_none() && total > q.limit.unwrap_or(100) {
            let keys: Vec<_> = selected
                .iter()
                .map(|i| i["key"].as_str().unwrap())
                .collect();
            let id = blake3::hash(&serde_json::to_vec(&keys).map_err(|e| internal(e.into()))?)
                .to_hex()
                .to_string();
            write(
                &st,
                &format!("selection:{id}"),
                "review-selection",
                &json!({"created":now_ms(),"keys":keys}),
            )
            .map_err(internal)?;
            Some(id)
        } else {
            q.snapshot.clone()
        };
    let offset = q.offset.unwrap_or(0).min(total);
    let limit = q.limit.unwrap_or(100).clamp(1, 250);
    let page = selected
        .into_iter()
        .skip(offset)
        .take(limit)
        .collect::<Vec<_>>();
    let sources = ["categorization", "analysis"].map(|role| {
        learning::review_configuration(&st, role, None)
            .unwrap_or(json!({"role":role,"provider":"none","model":""}))
    });
    let jobs = st
        .with_vault(|_, v| {
            v.app_records_recent("review-job-running", 2)?
                .into_iter()
                .chain(v.app_records_recent("review-job-queued", 2)?)
                .map(|(_, bytes)| Ok(serde_json::from_slice::<Value>(&bytes)?))
                .collect::<anyhow::Result<Vec<_>>>()
        })
        .map_err(internal)?;
    Ok(Json(
        json!({"snapshot":snapshot,"items":page,"total":total,"pending":pending,"offset":offset,"limit":limit,"sources":sources,"jobs":jobs,"editable":st.control.is_some()}),
    ))
}
pub(crate) fn random_id() -> anyhow::Result<String> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|e| anyhow::anyhow!(e))?;
    Ok(blake3::hash(&bytes).to_hex().to_string())
}

fn selection_keys(st: &Shared, id: &str) -> anyhow::Result<Vec<String>> {
    let saved = read(st, &format!("selection:{id}"))?
        .ok_or_else(|| anyhow::anyhow!("selection_expired"))?;
    Ok(serde_json::from_value(saved["keys"].clone())?)
}

async fn selection(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(q): Json<Filters>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let (_, items) = filtered(&st, &q).map_err(internal)?;
    let keys: Vec<_> = items.iter().map(|i| &i["key"]).collect();
    let id = random_id().map_err(internal)?;
    write(
        &st,
        &format!("selection:{id}"),
        "review-selection",
        &json!({"keys":keys,"created":now_ms()}),
    )
    .map_err(internal)?;
    Ok(Json(json!({"snapshot":id,"total":keys.len()})))
}

#[derive(Default, Serialize, Deserialize)]
pub(crate) struct Selection {
    #[serde(default)]
    pub keys: Vec<String>,
    pub snapshot: Option<String>,
    #[serde(default)]
    pub excluded: Vec<String>,
}

impl Selection {
    pub(crate) fn resolve(&self, st: &Shared) -> anyhow::Result<Vec<Value>> {
        let keys = match self.snapshot.as_deref() {
            Some(id) => selection_keys(st, id)?,
            None => self.keys.clone(),
        };
        let excluded: HashSet<_> = self.excluded.iter().collect();
        let mut all: HashMap<_, _> = items(st)?
            .into_iter()
            .map(|i| (i["key"].as_str().unwrap().to_owned(), i))
            .collect();
        let mut seen = HashSet::new();
        keys.into_iter()
            .filter(|key| !excluded.contains(key) && seen.insert(key.clone()))
            .map(|key| {
                all.remove(&key)
                    .ok_or_else(|| anyhow::anyhow!("entry_not_found"))
            })
            .collect()
    }
}

#[derive(Clone, Serialize, Deserialize)]
struct Target {
    key: String,
    fingerprint: String,
}
fn model_id(target: &Target) -> String {
    blake3::hash(format!("{}\0{}", target.key, target.fingerprint).as_bytes()).to_hex()[..24]
        .to_owned()
}
#[derive(Default, Serialize, Deserialize)]
struct Submit {
    #[serde(flatten)]
    selection: Selection,
    #[serde(default = "auto_source")]
    model_source: String,
    scenario: Option<String>,
    request_id: Option<String>,
}
fn auto_source() -> String {
    "auto".into()
}

fn full_job(st: &Shared, id: &str) -> anyhow::Result<Option<Value>> {
    let Some(mut job) = read(st, &format!("review-job:{id}"))? else {
        return Ok(None);
    };
    if job["targets"].is_null() {
        job["targets"] = read(st, &format!("review-targets:{id}"))?.unwrap_or_else(|| json!([]));
    }
    let mut results = serde_json::Map::new();
    st.with_vault(|_, v| {
        for (_, bytes) in v.app_records(&format!("review-results:{id}"), i64::MAX as usize)? {
            let packet: HashMap<String, Value> = serde_json::from_slice(&bytes)?;
            for (key, rec) in packet {
                if rec.is_object() {
                    results.insert(key.clone(), rec["state"].clone());
                    job["recommendations"][&key] = rec;
                } else {
                    results.insert(key, rec);
                }
            }
        }
        Ok(())
    })?;
    // Older jobs kept only the completed prefix and the latest individual recommendations.
    if results.is_empty() && job["total"].is_null() {
        for t in job["targets"].as_array().into_iter().flatten() {
            if let Some(key) = t["key"].as_str()
                && let Some(rec) = read(st, &format!("review-rec:{key}"))?
                && rec["job_id"] == id
            {
                results.insert(key.to_owned(), rec["state"].clone());
            }
        }
    }
    job["results"] = Value::Object(results);
    Ok(Some(job))
}

async fn submit(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<Submit>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    if st.control.is_none() {
        return Err(bad("read_only"));
    }
    let signature = blake3::hash(&serde_json::to_vec(&req).map_err(|e| internal(e.into()))?)
        .to_hex()
        .to_string();
    let id = match &req.request_id {
        Some(key) if (8..=128).contains(&key.len()) => {
            blake3::hash(key.as_bytes()).to_hex().to_string()
        }
        Some(_) => return Err(bad("invalid_request_id")),
        None => random_id().map_err(internal)?,
    };
    let record_id = format!("review-job:{id}");
    if let Some(job) = read(&st, &record_id).map_err(internal)? {
        if job["signature"] != signature {
            return Err(bad("request_id_conflict"));
        }
        return Ok(Json(job));
    }
    let source = learning::review_configuration(&st, &req.model_source, req.scenario.as_deref())
        .map_err(|e| bad(&learning::safe_error(&e)))?;
    let selected = req
        .selection
        .resolve(&st)
        .map_err(|e| bad(&e.to_string()))?;
    let mut targets = Vec::new();
    let mut skipped = 0;
    let mut protected = 0;
    for item in selected {
        if item["protected"] == true || !matches!(item["state"].as_str(), Some("open" | "pending"))
        {
            if item["protected"] == true {
                protected += 1;
            }
            skipped += 1;
            continue;
        }
        targets.push(Target {
            key: item["key"].as_str().unwrap().into(),
            fingerprint: fingerprint(&item),
        });
    }
    if targets.is_empty() {
        return Err(bad(if protected > 0 {
            "protected_credential"
        } else if skipped > 0 {
            "entry_not_pending"
        } else {
            "empty_selection"
        }));
    }
    let job = json!({"id":id,"state":"queued","created":now_ms(),"updated":now_ms(),"revision":source["revision"],"source":source,
        "total":targets.len(),"skipped":skipped,"processed":0,"completed":0,"failed":0,"manual":0,"active":0,"signature":signature,"error":""});
    let started = st
        .with_vault(|_, v| {
            learning::transaction(v, || {
                if v.app_record(&record_id)?.is_some() {
                    return Ok(false);
                }
                v.put_app_record(
                    &format!("review-targets:{id}"),
                    "review-targets",
                    &serde_json::to_vec(&targets)?,
                )?;
                v.put_app_record(&record_id, "review-job-queued", &serde_json::to_vec(&job)?)?;
                Ok(true)
            })
        })
        .map_err(internal)?;
    if !started {
        let saved = read(&st, &record_id)
            .map_err(internal)?
            .ok_or_else(|| bad("job_not_found"))?;
        return if saved["signature"] == signature {
            Ok(Json(saved))
        } else {
            Err(bad("request_id_conflict"))
        };
    }
    let runner = st.clone();
    let queued = job.clone();
    let response_targets = serde_json::to_value(&targets).map_err(|e| internal(e.into()))?;
    tokio::spawn(async move {
        run(runner, queued, targets).await;
    });
    let mut response = job;
    response["targets"] = response_targets;
    Ok(Json(response))
}

async fn job(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(query): Query<HashMap<String, String>>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let result = if query.contains_key("brief") {
        read(&st, &format!("review-job:{id}"))
    } else {
        full_job(&st, &id)
    };
    Ok(Json(
        result
            .map_err(internal)?
            .ok_or_else(|| bad("job_not_found"))?,
    ))
}
#[derive(Deserialize)]
struct Retry {
    request_id: Option<String>,
}
async fn retry(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(req): Json<Retry>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let job = full_job(&st, &id)
        .map_err(internal)?
        .ok_or_else(|| bad("job_not_found"))?;
    if job["state"] != "failed" {
        return Err(bad("invalid_action"));
    }
    if job["revision"] != st.learning.snapshot().revision {
        return Err(bad("configuration_changed"));
    }
    let keys = job["targets"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|target| {
            let key = target["key"].as_str()?;
            (!matches!(
                job["results"][key].as_str(),
                Some("succeeded" | "needs_review")
            ))
            .then(|| key.to_owned())
        })
        .collect();
    submit(
        State(st),
        headers,
        Json(Submit {
            selection: Selection {
                keys,
                ..Default::default()
            },
            model_source: job["source"]["role"].as_str().unwrap_or("auto").into(),
            scenario: job["source"]["scenario"].as_str().map(str::to_owned),
            request_id: req.request_id,
        }),
    )
    .await
}

async fn jobs(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(q): Query<Filters>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    if q.family.as_deref() == Some("learning")
        && let Some(id) = &q.id
    {
        let mut jobs = Vec::new();
        if let Some(mut meta) = read(&st, &format!("meta:{id}")).map_err(internal)?
            && meta["id"] == *id
        {
            meta["family"] = json!("learning");
            jobs.push(meta);
        }
        return Ok(Json(json!({"jobs":jobs,"total":jobs.len()})));
    }
    let mut rows=st.with_vault(|_,v|{
        let mut rows=Vec::new();
        for kind in ["review-job-running","review-job-queued","review-job-failed","review-job-succeeded","learning-job-meta"] {
            for (_,bytes) in v.app_records_recent(kind,i64::MAX as usize)? {
                let j:Value=serde_json::from_slice(&bytes)?;
                let family=if kind=="learning-job-meta" {"learning"} else {"review"};
                rows.push(json!({"family":family,"id":j["id"],"name":j["name"],"state":j["state"],"created":j["created"],
                    "updated":j["updated"],"source":j["source"],"model":j["model"],"scenario":j["scenario"],"total":j["total"].as_u64().or_else(||j["targets"].as_array().map(|x|x.len() as u64)),
                    "completed":j.get("processed").unwrap_or(&j["completed"]),"extracted":j["extracted"],"active":j["active"],"failed":j["failed"],"retryable":j["retryable"],"error":j["error"]}));
            }
        }
        Ok(rows)
    }).map_err(internal)?;
    let worker = match crate::worker::forward(&st, "/workspace/jobs", None).await {
        Ok(result) if result["jobs"].is_array() => Some(result),
        Err(err) if err.message() == "worker unavailable" => None,
        _ => crate::worker::forward(&st, "/status", None).await.ok(),
    };
    if let Some(files) = worker.as_ref().and_then(|w| w["jobs"].as_array()) {
        for j in files {
            let download = j["kind"] == "download";
            rows.push(json!({
                "family": "file", "kind": j["kind"], "stage": j["stage"], "id": j["id"],
                "name": j["source"], "state": j["state"], "model": j["model"], "error": j["error"],
                "updated": (1000.0 * j["updated"].as_f64().unwrap_or_default()) as u64,
                "created": (1000.0 * j["created"].as_f64().unwrap_or_default()) as u64,
                "total": if download { &j["progress_total"] } else { &j["total"] },
                "completed": if download { &j["progress"] } else { &j["completed"] }
            }));
        }
    }
    rows.retain(|row| {
        q.family.as_ref().is_none_or(|f| row["family"] == *f)
            && q.id.as_ref().is_none_or(|id| row["id"] == *id)
    });
    let running = |s: &Value| {
        matches!(
            s["state"].as_str(),
            Some("running" | "processing" | "queued" | "scanning" | "downloading")
        )
    };
    rows.sort_by_key(|r| {
        (
            !running(r),
            std::cmp::Reverse(r["created"].as_u64().unwrap_or_default()),
        )
    });
    let active = rows.iter().filter(|r| running(r)).count();
    let total = rows.len();
    let offset = q.offset.unwrap_or_default();
    let rows: Vec<_> = rows
        .into_iter()
        .skip(offset)
        .take(q.limit.unwrap_or(100).clamp(1, 250))
        .collect();
    Ok(Json(
        json!({"jobs":rows,"total":total,"active":active,"api_active":st.api_slots.active(),"updated":now_ms(),"worker_online":worker.as_ref().and_then(|w|w["online"].as_bool())}),
    ))
}

fn schema(ids: &[String]) -> Value {
    let count = ids.len();
    json!({"type":"object","properties":{"decisions":{"type":"array","minItems":count,"maxItems":count,"items":{"type":"object","properties":{
        "id":{"type":"string","enum":ids},"action":{"type":"string","enum":["mask","keep","review"]},"kind":{"type":"string","enum":kaponir_core::kinds::STANDARD},
        "scope":{"type":"string","enum":["personal","team","global"]},"confidence":{"type":"number","minimum":0,"maximum":1},"reason":{"type":"string","minLength":1,"maxLength":400}},
        "required":["id","action","kind","scope","confidence","reason"],"additionalProperties":false}}},"required":["decisions"],"additionalProperties":false})
}

fn validate_decision(decision: &Value, value: &str, research: bool) -> anyhow::Result<()> {
    let kind = decision["kind"].as_str().unwrap_or_default();
    anyhow::ensure!(
        kaponir_core::kinds::STANDARD.contains(&kind)
            && matches!(
                decision["action"].as_str(),
                Some("mask" | "keep" | "review")
            )
            && ((research && decision["confidence"].is_null())
                || decision["confidence"]
                    .as_f64()
                    .is_some_and(|p| p.is_finite() && (0.0..=1.0).contains(&p)))
            && decision["scope"]
                .as_str()
                .is_some_and(|s| s.parse::<Scope>().is_ok())
            && decision["reason"]
                .as_str()
                .is_some_and(|s| !s.is_empty() && s.len() <= 1600),
        "invalid_classification_response"
    );
    anyhow::ensure!(
        decision["action"] == "review" || !kaponir_core::kinds::type_conflict(kind, value),
        "type_format_conflict"
    );
    Ok(())
}

async fn run(st: Shared, mut job: Value, targets: Vec<Target>) {
    let _queue = st.review_queue.lock().await;
    let id = job["id"].as_str().unwrap().to_owned();
    job["started"] = json!(now_ms());
    job["state"] = json!("running");
    let execution = run_packets(&st, &mut job, &targets).await;
    let error = execution.err().map(|e| learning::safe_error(&e));
    if let Some(error) = error {
        job["error"] = json!(error);
    }
    job["state"] = json!(if job["error"] == "" {
        "succeeded"
    } else {
        "failed"
    });
    job["active"] = json!(0);
    job["updated"] = json!(now_ms());
    let _ = write(
        &st,
        &format!("review-job:{id}"),
        &format!("review-job-{}", job["state"].as_str().unwrap()),
        &job,
    );
}

async fn run_packets(st: &Shared, job: &mut Value, targets: &[Target]) -> anyhow::Result<()> {
    let settings = st.learning.snapshot();
    let batch_size = if job["source"]["role"] == "analysis" {
        settings.analysis.inference.batch_size
    } else {
        settings.categorization.inference.batch_size
    };
    let slots = settings.analysis.parallelism.max(1);
    let map: HashMap<String, Value> = items(st)?
        .into_iter()
        .map(|i| (i["key"].as_str().unwrap().to_owned(), i))
        .collect();
    let source = job["source"].clone();
    let id = job["id"].as_str().unwrap().to_owned();
    let mut packets = stream::iter((0..targets.len()).step_by(batch_size.max(1)).enumerate())
        .map(|(index, start)| {
            let batch = &targets[start..targets.len().min(start + batch_size.max(1))];
            let source = &source;
            let map = &map;
            let id = &id;
            async move {
                let start = std::time::Instant::now();
                let result = infer_packet(st, source, id, map, batch).await;
                (index, batch, result, start.elapsed().as_secs_f64())
            }
        })
        .buffer_unordered(slots);
    let save = |job: &Value| write(st, &format!("review-job:{id}"), "review-job-running", job);
    job["slots"] = json!(slots);
    job["active"] = json!(targets.len().min(slots * batch_size));
    save(job)?;
    let mut first_error = None;
    while let Some((index, batch, result, seconds)) = packets.next().await {
        job["last_packet_seconds"] = json!(seconds);
        let identified = result.is_ok();
        let records=result.unwrap_or_else(|err| {
            let code=learning::safe_error(&err);
            batch.iter().map(|t|(t.key.clone(),json!({"state":"failed","error":code,"fingerprint":t.fingerprint,"job_id":id,"revision":source["revision"],"model":source["model"],"created":now_ms()}))).collect()
        });
        let mut outcomes = HashMap::new();
        for (key, rec) in &records {
            if rec["state"] == "failed" {
                first_error.get_or_insert_with(|| {
                    rec["error"].as_str().unwrap_or("review_failed").to_owned()
                });
                job["failed"] = json!(job["failed"].as_u64().unwrap_or(0) + 1);
            } else if rec["state"] == "needs_review" {
                job["manual"] = json!(job["manual"].as_u64().unwrap_or(0) + 1);
            }
            outcomes.insert(key, rec);
        }
        let completed = job["processed"].as_u64().unwrap_or(0) + batch.len() as u64;
        job["processed"] = json!(completed);
        if identified {
            job["completed"] = json!(job["completed"].as_u64().unwrap_or(0) + batch.len() as u64);
        }
        job["active"] = json!((targets.len() - completed as usize).min(slots * batch_size));
        job["updated"] = json!(now_ms());
        st.with_vault(|_, v| {
            learning::transaction(v, || {
                for (key, rec) in &records {
                    v.put_app_record(
                        &format!("review-rec:{key}"),
                        "review-recommendation",
                        &serde_json::to_vec(rec)?,
                    )?;
                }
                v.put_app_record(
                    &format!("packet:{id}:{index}"),
                    &format!("review-results:{id}"),
                    &serde_json::to_vec(&outcomes)?,
                )?;
                v.put_app_record(
                    &format!("review-job:{id}"),
                    "review-job-running",
                    &serde_json::to_vec(&job)?,
                )?;
                Ok(())
            })
        })?;
    }
    if let Some(error) = first_error {
        job["error"] = json!(if job["failed"] == job["processed"] {
            error
        } else {
            "review_partial_failure".into()
        });
    }
    Ok(())
}

async fn infer_packet(
    st: &Shared,
    source: &Value,
    job_id: &str,
    current: &HashMap<String, Value>,
    batch: &[Target],
) -> anyhow::Result<Vec<(String, Value)>> {
    anyhow::ensure!(
        source["revision"] == st.learning.snapshot().revision,
        "configuration_changed"
    );
    let mut input = Vec::new();
    for target in batch {
        let item = current
            .get(&target.key)
            .ok_or_else(|| anyhow::anyhow!("entry_not_found"))?;
        anyhow::ensure!(fingerprint(item) == target.fingerprint, "review_stale");
        let value = item["value"].as_str().unwrap_or_default();
        anyhow::ensure!(
            !st.engine()
                .analyze(&kaponir_core::Input::text(value))
                .iter()
                .any(|c| c.hard && c.category == kaponir_core::Category::Credential),
            "protected_credential"
        );
        input.push(json!({"id":model_id(target),"value":item["value"],"kind":item["kind"],"context":redacted_context(st,item["context"].as_str().unwrap_or_default(),value)}));
    }
    let reply = learning::review_infer(
        st,
        source,
        json!(input),
        schema(&batch.iter().map(model_id).collect::<Vec<_>>()),
    )
    .await?;
    anyhow::ensure!(
        source["revision"].as_u64() == Some(st.learning.snapshot().revision),
        "configuration_changed"
    );
    let decisions = reply["decisions"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid_classification_response"))?;
    anyhow::ensure!(
        decisions.len() == batch.len(),
        "invalid_classification_response"
    );
    // Uncertain identity invalidates the packet. Once every ID is unique and
    // expected, a row's type/shape error must not discard its neighbors.
    for target in batch {
        let id = model_id(target);
        anyhow::ensure!(
            decisions.iter().filter(|d| d["id"] == id).count() == 1,
            "invalid_classification_response"
        );
    }
    let mut validated = Vec::new();
    for target in batch {
        let id = model_id(target);
        let d = decisions.iter().find(|d| d["id"] == id).unwrap();
        let item = &current[&target.key];
        let mut rec = match validate_decision(
            d,
            item["value"].as_str().unwrap_or_default(),
            source["scenario"].as_str().is_some_and(|s| s != "standard"),
        ) {
            Ok(()) => {
                let mut rec = d.clone();
                rec["state"] = json!(if d["action"] == "review" {
                    "needs_review"
                } else {
                    "succeeded"
                });
                rec
            }
            Err(error) => {
                let code = learning::safe_error(&error);

                // Do not retain a malformed action, type or confidence as a
                // usable recommendation. Applying this row remains blocked.
                json!({"state":"failed","error":code})
            }
        };
        rec["job_id"] = json!(job_id);
        rec["revision"] = source["revision"].clone();
        rec["fingerprint"] = json!(target.fingerprint);
        rec["model"] = source["model"].clone();
        rec["created"] = json!(now_ms());
        validated.push((target.key.clone(), rec));
    }
    Ok(validated)
}

fn redacted_context(st: &Shared, context: &str, value: &str) -> String {
    let mut ranges: Vec<_> = st
        .engine()
        .analyze(&kaponir_core::Input::text(context))
        .into_iter()
        .filter(|c| c.category == kaponir_core::Category::Credential && c.surface(context) != value)
        .map(|c| (c.start, c.end))
        .collect();
    ranges.sort_unstable();
    let mut merged: Vec<(usize, usize)> = Vec::new();
    for (start, end) in ranges {
        if let Some(last) = merged.last_mut()
            && start <= last.1
        {
            last.1 = last.1.max(end);
        } else {
            merged.push((start, end));
        }
    }
    let mut result = context.to_string();
    for (a, b) in merged.into_iter().rev() {
        result.replace_range(a..b, "[SECRET]");
    }
    result
}
#[derive(Deserialize)]
struct Apply {
    kind: Option<String>,
    scope: Option<Scope>,
    #[serde(default)]
    use_recommendation: bool,
    expected_job: Option<String>,
}
async fn apply(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path((origin, id, mut action)): Path<(String, String, String)>,
    Json(req): Json<Apply>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let key = format!("{origin}:{id}");
    let all = items(&st).map_err(internal)?;
    let item = all
        .iter()
        .find(|i| i["key"] == key)
        .ok_or_else(|| bad("entry_not_found"))?;
    let mut kind = req
        .kind
        .unwrap_or_else(|| item["kind"].as_str().unwrap_or("CUSTOM").into());
    let mut scope = req
        .scope
        .unwrap_or_else(|| kaponir_core::s1::default_scope_for_kind(&kind));
    if req.use_recommendation {
        let rec = &item["recommendation"];
        if rec["state"] == "needs_review" {
            return Err(bad("review_inconclusive"));
        }
        if rec["state"] != "succeeded"
            || req
                .expected_job
                .as_ref()
                .is_some_and(|id| rec["job_id"] != *id)
        {
            return Err(bad("review_stale"));
        }
        kind = rec["kind"].as_str().unwrap().into();
        scope = serde_json::from_value(rec["scope"].clone()).map_err(|_| bad("invalid_scope"))?;
        action = rec["action"].as_str().unwrap().into();
    }
    if origin == "learning" {
        let mapped = match action.as_str() {
            "mask" => "approve",
            "keep" | "reject" => "reject",
            "rollback" => "rollback",
            _ => return Err(bad("invalid_action")),
        };
        return learning::action(
            State(st),
            headers,
            Path((id, mapped.into())),
            Json(learning::Action {
                kind: Some(kind),
                scope: Some(scope),
            }),
        )
        .await;
    }
    let _guard = st.rule_changes.lock().await;
    let ctl = st.control.as_ref().ok_or_else(|| bad("read_only"))?;
    let row_id = id.parse::<i64>().map_err(|_| bad("entry_not_found"))?;
    let row = st
        .with_vault(|_, v| {
            Ok(v.journal(None, i64::MAX as usize)?
                .into_iter()
                .find(|r| r.id == row_id))
        })
        .map_err(internal)?
        .ok_or_else(|| bad("entry_not_found"))?;
    if row.value != item["value"].as_str().unwrap_or_default()
        || row.context != item["context"].as_str().unwrap_or_default()
        || row.kind != item["kind"].as_str().unwrap_or_default()
        || req.use_recommendation
            && item["recommendation"]["revision"].as_u64() != Some(st.learning.snapshot().revision)
    {
        return Err(bad("review_stale"));
    }
    let mut decision = None;
    let status = match action.as_str() {
        "dismiss" if row.status == "open" => "dismissed",
        "reopen" if row.status == "dismissed" => "open",
        "mask" | "keep" => {
            if row.status != "open" {
                return Err(bad("entry_not_pending"));
            }
            if kind.is_empty()
                || kind.len() > 32
                || !kind
                    .bytes()
                    .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'_')
                || kaponir_core::kinds::type_conflict(&kind, &row.value)
            {
                return Err(bad("type_format_conflict"));
            }
            let rule = if action == "mask" {
                row.mask_rule(Some(&kind))
            } else {
                row.keep_rule()
            };
            let sets = ctl.rules().map_err(internal)?;
            if st
                .engine()
                .analyze(&kaponir_core::Input::text(&row.value))
                .iter()
                .any(|candidate| matches!(candidate.origin, kaponir_core::Origin::Rule { .. }))
            {
                return Err(bad("existing_rule"));
            }
            if sets.iter().any(|set| {
                set.file
                    .rules
                    .iter()
                    .any(|r| !r.disabled && r.value.as_deref() == Some(&row.value))
            }) {
                return Err(bad("existing_rule"));
            }
            ctl.add_rule(scope, rule.clone()).map_err(internal)?;
            decision = Some(
                json!({"action":action,"scope":scope,"rule_id":rule.id,"rule":rule,"created":now_ms()}),
            );
            if action == "mask" { "masked" } else { "kept" }
        }
        "rollback" => {
            let saved = read(&st, &format!("review-decision:{key}"))
                .map_err(internal)?
                .ok_or_else(|| bad("rule_was_edited"))?;
            let sets = ctl.rules().map_err(internal)?;
            let mut found = false;
            for set in sets {
                for (index, rule) in set.file.rules.iter().enumerate() {
                    if json!(set.scope) == saved["scope"] && rule.id == saved["rule_id"] {
                        if serde_json::to_value(rule).map_err(|e| internal(e.into()))?
                            != saved["rule"]
                        {
                            return Err(bad("rule_was_edited"));
                        }
                        let revision = blake3::hash(
                            &serde_json::to_vec(&set.file).map_err(|e| internal(e.into()))?,
                        )
                        .to_hex()
                        .to_string();
                        ctl.set_rule_disabled(set.scope, index, &revision, true)
                            .map_err(internal)?;
                        found = true;
                    }
                }
            }
            if !found {
                return Err(bad("rule_was_edited"));
            }
            "open"
        }
        _ => return Err(bad("invalid_action")),
    };
    st.with_vault(|_, v| {
        v.set_journal_status(row_id, status)?;
        Ok(())
    })
    .map_err(internal)?;
    if let Some(decision) = decision {
        write(
            &st,
            &format!("review-decision:{key}"),
            "review-decision",
            &decision,
        )
        .map_err(internal)?;
    }
    if matches!(action.as_str(), "mask" | "keep" | "rollback") {
        st.replace_engine(ctl.build_engine().map_err(internal)?);
    }
    Ok(Json(json!({"ok":true,"state":status})))
}
