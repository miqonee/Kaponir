//! Model review and decisions for saved findings.
mod automatic;
pub(crate) mod coverage;
mod path_prefixes;
pub(crate) mod paths;
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
use coverage::ActiveRules;
use futures_util::{StreamExt, stream};
use kaponir_core::Scope;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

type ResultJson = Result<Json<Value>, ApiError>;
fn bad(code: &str) -> ApiError {
    error(StatusCode::BAD_REQUEST, code)
}

fn finding_excerpt(item: &Value) -> Value {
    json!({"value":item["value"], "context":item["context"], "context_span":item["context_span"]})
}

fn unmatched_rule_error(item: &Value) -> ApiError {
    bad("rule_misses_occurrence").with_details(json!({
        "rule":item["unmatched_rule"], "finding":finding_excerpt(item)
    }))
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

fn recommendation_revision(rec: &Value) -> Option<u64> {
    rec["valid_revision"]
        .as_u64()
        .or_else(|| rec["revision"].as_u64())
}

/// Keep saved decisions from a partially completed job actionable across an explicit queue change.
/// The inference revision, model and immutable packet remain available for the job history.
pub(crate) fn retain_completed(
    v: &kaponir_vault::SqliteVault,
    job: &Value,
    revision: u64,
) -> anyhow::Result<()> {
    if job["processed"].as_u64().unwrap_or(0) == 0 {
        return Ok(());
    }
    let id = job["id"].as_str().unwrap_or_default();
    for (_, bytes) in v.app_records(&format!("review-results:{id}"), i64::MAX as usize)? {
        let packet: HashMap<String, Value> = serde_json::from_slice(&bytes)?;
        for key in packet.keys() {
            let record_id = format!("review-rec:{key}");
            if let Some(data) = v.app_record(&record_id)? {
                let mut record: Value = serde_json::from_slice(&data)?;
                if record["job_id"] == id
                    && recommendation_revision(&record) == job["revision"].as_u64()
                {
                    record["valid_revision"] = json!(revision);
                    v.put_app_record(
                        &record_id,
                        "review-recommendation",
                        &serde_json::to_vec(&record)?,
                    )?;
                }
            }
        }
    }
    Ok(())
}

// A detected value or a type prediction alone does not imply a masking decision.
fn model_proposal(item: &Value) -> Option<Value> {
    let rec = &item["recommendation"];
    let mut proposal = if !rec.is_null() {
        if rec["state"] != "succeeded" {
            return None;
        }
        json!({"source":"review","job":rec["job_id"],"action":rec["action"],"kind":rec["kind"],"scope":rec["scope"],
               "model":rec["model"],"reason":rec["reason"],"confidence":rec["confidence"],"verdict_only":rec["verdict_only"]})
    } else {
        let scenario = &item["scenario_result"];
        if item["origin"] != "learning" || item["categorization_state"] == "failed" {
            return None;
        }
        let action = if !scenario.is_null() {
            scenario["verdict"].as_str()?
        } else if item["categorization_state"] == "succeeded" {
            item["classification_action"].as_str()?
        } else {
            return None;
        };
        json!({"source":"categorization","job":item["job_id"],"action":action,"kind":item["kind"],"scope":item["scope"],
               "model":item["classifier"],"reason":scenario["reason"],"confidence":item["classification_confidence"],
               "verdict_only":!scenario.is_null() && scenario["kind"].is_null()})
    };
    if !matches!(proposal["action"].as_str(), Some("mask" | "keep")) {
        return None;
    }
    let signature = json!([item["key"], fingerprint(item), proposal]);
    proposal["id"] = json!(
        blake3::hash(signature.to_string().as_bytes())
            .to_hex()
            .to_string()
    );
    Some(proposal)
}
pub(crate) fn active_rules(st: &crate::State) -> anyhow::Result<ActiveRules> {
    ActiveRules::new(
        st.control
            .as_ref()
            .map(|ctl| ctl.rules())
            .transpose()?
            .unwrap_or_default(),
    )
}

pub(crate) fn pending_count(st: &Shared, rules: &ActiveRules) -> anyhow::Result<usize> {
    st.with_vault(|_, v| {
        let mut values = learning::pending_values(v, rules)?;
        for row in v.journal(Some("open"), i64::MAX as usize)? {
            if rules
                .covering_finding(
                    &row.value,
                    &row.context,
                    row.context_span,
                    row.category == "credential",
                    &row.kind,
                )
                .is_none()
            {
                values.insert(paths::pending_id(
                    &row.kind,
                    serde_json::from_value(json!(row.recommended_scope))
                        .unwrap_or_else(|_| kaponir_core::s1::default_scope_for_kind(&row.kind)),
                    &row.value,
                ));
            }
        }
        Ok(values.len())
    })
}

fn detector_item(row: &kaponir_vault::JournalRow) -> anyhow::Result<Value> {
    let mut item = serde_json::to_value(row)?;
    item["key"] = json!(format!("detector:{}", row.id));
    item["origin"] = json!("detector");
    item["id"] = json!(row.id.to_string());
    item["state"] = json!(row.status);
    item["updated"] = json!(row.last_seen.saturating_mul(1000));
    item["protected"] = json!(row.category == "credential");
    Ok(item)
}

fn learning_item(mut entry: Value) -> Value {
    entry["key"] = json!(format!(
        "learning:{}",
        entry["id"].as_str().unwrap_or_default()
    ));
    entry["origin"] = json!("learning");
    let job_id = entry["job_id"].as_str().unwrap_or_default();
    let session = job_id.starts_with("learning:job:question:")
        || job_id.starts_with("learning:job:turn:")
        || job_id.starts_with("learning:job:session:")
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
    entry["context"] = entry["quote"].clone();
    entry["protected"] = json!(entry["kind"] == "SECRET");
    entry
}

fn attach_proposal(
    item: &mut Value,
    revision: u64,
    recommendation: Option<Value>,
    declined: Option<Value>,
) {
    if let Some(mut rec) = recommendation {
        if recommendation_revision(&rec) != Some(revision)
            || rec["fingerprint"] != fingerprint(item)
        {
            rec["state"] = json!("stale");
        }
        item["recommendation"] = rec;
    }
    if let Some(mut proposal) = model_proposal(item) {
        let previously_declined = declined.as_ref() == Some(&proposal["id"]);
        if proposal["action"] == "mask"
            && proposal["kind"] == "PATH"
            && let Some(prefix) = paths::account_directory(item)
        {
            // A tab that still shows the old whole-path action must refresh before it can accept.
            let target = json!([proposal["id"], prefix]);
            proposal["id"] = json!(
                blake3::hash(target.to_string().as_bytes())
                    .to_hex()
                    .to_string()
            );
        }
        proposal["declined"] =
            json!(previously_declined || declined.as_ref() == Some(&proposal["id"]));
        item["proposal"] = proposal;
    }
    if needs_decision(item)
        && item["unmatched_rule"].is_null()
        && let Some(prefix) = paths::account_directory(item)
    {
        item["account_directory"] = json!(prefix);
    }
}

pub(crate) fn needs_decision(item: &Value) -> bool {
    matches!(item["state"].as_str(), Some("open" | "pending"))
}

fn raw_finding(
    v: &kaponir_vault::SqliteVault,
    origin: &str,
    id: &str,
) -> anyhow::Result<Option<Value>> {
    match origin {
        "detector" => match id.parse::<i64>() {
            Ok(row) => v.journal_row(row)?.as_ref().map(detector_item).transpose(),
            Err(_) => Ok(None),
        },
        "learning" if id.starts_with("learning:entry:") => v
            .app_record(id)?
            .map(|bytes| Ok(learning_item(serde_json::from_slice(&bytes)?)))
            .transpose(),
        _ => Ok(None),
    }
}

fn excerpt_signature(item: &Value) -> String {
    blake3::hash(
        json!([
            item["key"],
            item["context"],
            item["context_span"],
            item["value"],
            item["kind"]
        ])
        .to_string()
        .as_bytes(),
    )
    .to_hex()
    .to_string()
}

// Single and bulk decisions use the same projection as browsing without rereading unrelated excerpts.
fn action_item(st: &Shared, origin: &str, id: &str) -> anyhow::Result<Option<Value>> {
    let item = st.with_vault(|_, v| raw_finding(v, origin, id))?;
    let Some(mut item) = item else {
        return Ok(None);
    };
    active_rules(st)?.project(&mut item);
    let key = format!("{origin}:{id}");
    attach_proposal(
        &mut item,
        st.learning.snapshot().revision,
        read(st, &format!("review-rec:{key}"))?,
        read(st, &format!("review-declined:{key}"))?,
    );
    Ok(Some(item))
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
        let mut item = detector_item(&row)?;
        if let Some((event, session)) = events.get(&row.id) {
            item["event_id"] = json!(event);
            item["session"] = json!(session);
            item["source_type"] = json!("session");
        }
        all.push(item);
    }
    all.extend(learning::review_entries(st)?.into_iter().map(learning_item));
    let rules = active_rules(st)?;
    let revision = st.learning.snapshot().revision;
    let (mut recommendations, mut decisions, mut declined, mut deferred, dataset) =
        st.with_vault(|_, v| {
            let records = |kind: &str| -> anyhow::Result<HashMap<String, Value>> {
                v.app_records(kind, i64::MAX as usize)?
                    .into_iter()
                    .map(|(id, data)| Ok((id, serde_json::from_slice(&data)?)))
                    .collect()
            };
            Ok((
                records("review-recommendation")?,
                records("review-decision")?,
                records("review-declined")?,
                records("review-deferred")?,
                crate::dataset::example_index(v)?,
            ))
        })?;
    for item in &mut all {
        rules.project(item);
        let key = item["key"].as_str().unwrap().to_owned();
        attach_proposal(
            item,
            revision,
            recommendations.remove(&format!("review-rec:{key}")),
            declined.remove(&format!("review-declined:{key}")),
        );
        if let Some(decision) = decisions.remove(&format!("review-decision:{key}")) {
            item["decision"] = decision;
        }
        if needs_decision(item)
            && let Some(record) = deferred.remove(&format!("review-deferred:{key}"))
        {
            item["deferred_at"] = record["created"].clone();
        }
        let dataset_id = crate::dataset::finding_id(item);
        item["excerpt_id"] = json!(excerpt_signature(item));
        let status = dataset.status(&dataset_id);
        item["in_dataset"] = json!(status.is_some());
        if let Some(status) = status {
            item["dataset_id"] = json!(dataset_id);
            item["dataset_status"] = json!(status);
        }
    }
    Ok(all)
}
pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/api/v1/ui/review", get(list))
        .route("/api/v1/ui/review/selection", post(selection))
        .route("/api/v1/ui/review/skip", post(skip_selected))
        .route("/api/v1/ui/review/path-prefix", post(paths::apply))
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
        let context = redacted_context(&st, &context, &item.original);
        let mut matches = context.match_indices(&item.original);
        let context_span = matches.next().and_then(|(at, value)| {
            (matches.next().is_none() && !value.is_empty()).then_some([at, at + value.len()])
        });
        selected.push(kaponir_core::journal::JournalEntry {
            kind: item.kind.clone(),
            category: serde_json::from_value(json!(item.category))
                .map_err(|_| bad("invalid_kind"))?,
            recognizer: item.recognizer.clone().unwrap_or_default(),
            value: item.original.clone(),
            shape: kaponir_core::journal::shape(&item.original),
            context,
            context_span,
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
    let traffic = st.traffic.summary_snapshot();
    let sessions: Vec<_> = traffic
        .events
        .iter()
        .chain(&traffic.active)
        .filter_map(|e| e.session_id.as_deref())
        .collect();
    let titles = st
        .with_vault(|_, v| Ok(v.session_titles(&sessions)?))
        .unwrap_or_default();
    let relations =
        crate::opencode::session_relations(traffic.events.iter().chain(&traffic.active));
    let mut snapshot = json!(traffic);
    snapshot["session_titles"] = json!(titles);
    snapshot["session_relations"] = json!(relations);
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
pub(crate) fn event_detail(st: &Shared, mut event: crate::traffic::Event) -> Value {
    event.mark_legacy_images();
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
            "review-job-cancelled",
            "review-recommendation",
            "review-selection",
            "dataset-collection",
            "review-deferred",
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
                learning::retain_findings(v, &crate::discovery::load(v)?.report(), false)?;
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
    automatic::start(st);
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
    let saved = q
        .snapshot
        .as_deref()
        .map(|id| saved_selection(st, id))
        .transpose()?;
    let snapshot = saved
        .as_ref()
        .map(|selection| selection.expanded(&HashSet::new()));
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
        .map(|item| {
            paths::pending_id(
                item["kind"].as_str().unwrap_or_default(),
                paths::scope(item),
                item["value"].as_str().unwrap_or_default(),
            )
        })
        .collect::<HashSet<_>>()
        .len();
    let mut selected: Vec<_> = all
        .into_iter()
        .filter(|item| {
            let is_pending = needs_decision(item);
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
                && match q.status.as_deref() {
                    Some("all" | "selection") => true,
                    Some("history") => !is_pending,
                    Some("proposals") => is_pending && item["proposal"]["declined"] == false,
                    _ => is_pending,
                }
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
    if q.status.as_deref() == Some("selection") {
        let saved = saved.ok_or_else(|| anyhow::anyhow!("selection_expired"))?;
        return Ok((pending, saved.for_masking(selected)));
    }
    if let Some(keys) = snapshot {
        let order: HashMap<_, _> = keys.into_iter().enumerate().map(|(i, k)| (k, i)).collect();
        selected.sort_by_key(|item| order.get(item["key"].as_str().unwrap()).copied());
    }
    if !matches!(q.status.as_deref(), Some("all" | "history")) {
        if q.status.as_deref() != Some("proposals")
            && q.keys.is_none()
            && q.job_id.is_none()
            && q.learning_job.is_none()
        {
            selected = paths::group(selected);
        }
        selected = group_values(selected);
        selected.sort_by_key(|item| (item["deferred_at"].is_u64(), item["deferred_at"].as_u64()));
    }
    Ok((pending, selected))
}

// Keep a deterministic representative and the exact records matched by the current filters.
fn group_values(rows: Vec<Value>) -> Vec<Value> {
    let mut positions = HashMap::<_, usize>::new();
    let mut groups: Vec<Value> = Vec::new();
    for row in rows {
        let value = row["value"].as_str().unwrap_or_default();
        if row["path_group"].is_object() {
            groups.push(row);
            continue;
        }
        let path_scope = (row["kind"] == "PATH").then(|| paths::scope(&row));
        let group_key = (value.to_owned(), path_scope);
        if let Some(index) = positions.get(&group_key) {
            let first = &mut groups[*index];
            let mut keys = match first["occurrence_keys"].take() {
                Value::Array(keys) => keys,
                _ => vec![first["key"].clone()],
            };
            keys.push(row["key"].clone());
            let proposed = |i: &Value| i["proposal"]["declined"] == false;
            let both_deferred = first["deferred_at"].is_u64() && row["deferred_at"].is_u64();
            let conflict = first["proposal_conflict"] == true
                || proposed(first)
                    && proposed(&row)
                    && ["action", "kind", "scope"]
                        .iter()
                        .any(|f| first["proposal"][*f] != row["proposal"][*f]);
            if row["protected"] == true && first["protected"] != true
                || row["protected"] == first["protected"] && !proposed(first) && proposed(&row)
            {
                *first = row;
            }
            if !both_deferred {
                first.as_object_mut().unwrap().remove("deferred_at");
            }
            if conflict {
                first["proposal_conflict"] = json!(true);
                first["proposal"] = Value::Null;
            }
            first["occurrence_keys"] = Value::Array(keys);
        } else {
            positions.insert(group_key, groups.len());
            groups.push(row);
        }
    }
    groups
}

#[derive(Default, Serialize, Deserialize)]
struct SavedSelection {
    keys: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    occurrences: BTreeMap<String, Vec<String>>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    path_targets: BTreeMap<String, Vec<String>>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    prefixes: BTreeMap<String, String>,
}

impl SavedSelection {
    fn from_items(items: &[Value]) -> Self {
        let mut saved = Self::default();
        for item in items {
            let key = item["key"].as_str().unwrap().to_owned();
            if let Some(records) = item["occurrence_keys"].as_array() {
                saved.occurrences.insert(
                    key.clone(),
                    records
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect(),
                );
            }
            if let Some(paths) = item["path_group"]["paths"].as_array() {
                saved.prefixes.insert(
                    key.clone(),
                    item["path_group"]["prefix"].as_str().unwrap().to_owned(),
                );
                saved.path_targets.insert(
                    key.clone(),
                    paths
                        .iter()
                        .filter_map(|p| p["keys"][0].as_str().map(str::to_owned))
                        .collect(),
                );
            }
            if let Some(prefix) = item["account_directory"].as_str()
                && !matches!(item["proposal"]["action"].as_str(), Some("keep"))
                && item["proposal"]["kind"]
                    .as_str()
                    .is_none_or(|k| k == "PATH")
            {
                saved.prefixes.insert(key.clone(), prefix.to_owned());
            }
            saved.keys.push(key);
        }
        saved
    }

    // A snapshot fixes both the selected rows and what each proposed rule would mask.
    fn for_masking(&self, rows: Vec<Value>) -> Vec<Value> {
        let mut by_key: HashMap<_, _> = rows
            .into_iter()
            .map(|r| (r["key"].as_str().unwrap().to_owned(), r))
            .collect();
        let mut selected = Vec::new();
        for key in &self.keys {
            let Some(first) = by_key.remove(key) else {
                continue;
            };
            let mut members = vec![first];
            if let Some(others) = self.occurrences.get(key) {
                members.extend(others.iter().filter_map(|k| by_key.remove(k)));
            }
            if let Some(prefix) = self
                .prefixes
                .get(key)
                .filter(|_| self.path_targets.contains_key(key))
            {
                if members.iter().all(needs_decision) {
                    selected.extend(
                        paths::group(members)
                            .into_iter()
                            .filter(|r| r["path_group"]["prefix"] == *prefix),
                    );
                }
            } else {
                selected.extend(group_values(members));
            }
        }
        selected
    }

    fn expanded(&self, excluded: &HashSet<&String>) -> Vec<String> {
        let mut keys = Vec::new();
        for key in &self.keys {
            if !excluded.contains(key) {
                keys.push(key.clone());
                if let Some(others) = self.occurrences.get(key) {
                    keys.extend(
                        others
                            .iter()
                            .filter(|k| *k != key && !excluded.contains(k))
                            .cloned(),
                    );
                }
            }
        }
        keys
    }
}
async fn list(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(q): Query<Filters>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let (pending, selected) = filtered(&st, &q).map_err(internal)?;
    let total = selected.len();
    let model_count = total
        + selected
            .iter()
            .filter_map(|row| row["path_group"]["paths"].as_array())
            .map(|paths| paths.len() - 1)
            .sum::<usize>();
    let snapshot =
        if q.stable == Some(true) && q.snapshot.is_none() && total > q.limit.unwrap_or(100) {
            let saved = SavedSelection::from_items(&selected);
            // Include the full membership so a snapshot cannot acquire new duplicates later.
            let id = blake3::hash(&serde_json::to_vec(&saved).map_err(|e| internal(e.into()))?)
                .to_hex()
                .to_string();
            write(
                &st,
                &format!("selection:{id}"),
                "review-selection",
                &selection_record(&saved),
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
    let mut page_keys = HashMap::new();
    for row in &page {
        let key = row["key"].as_str().unwrap();
        page_keys.insert(key, key);
        for occurrence in row["occurrence_keys"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            page_keys.insert(occurrence, key);
        }
    }
    let jobs = st
        .with_vault(|_, v| {
            v.app_records_recent("review-job-running", i64::MAX as usize)?
                .into_iter()
                .chain(v.app_records_recent("review-job-queued", i64::MAX as usize)?)
                .map(|(_, bytes)| {
                    let mut job: Value = serde_json::from_slice(&bytes)?;
                    let id = job["id"].as_str().unwrap_or_default().to_owned();
                    let targets = if job["targets"].is_array() {
                        job["targets"].take()
                    } else if let Some(bytes) = v.app_record(&format!("review-targets:{id}"))? {
                        serde_json::from_slice(&bytes)?
                    } else {
                        Value::Null
                    };
                    let mut waiting = BTreeSet::new();
                    for target in targets.as_array().into_iter().flatten() {
                        let key = target["key"].as_str().unwrap_or_default();
                        if let Some(representative) = page_keys.get(key) {
                            if job["processed"].as_u64().unwrap_or(0) > 0
                                && let Some(bytes) = v.app_record(&format!("review-rec:{key}"))?
                                && serde_json::from_slice::<Value>(&bytes)?["job_id"] == id
                            {
                                continue;
                            }
                            waiting.insert(representative);
                        }
                    }
                    job["page_keys"] = json!(waiting);
                    Ok(job)
                })
                .collect::<anyhow::Result<Vec<_>>>()
        })
        .map_err(internal)?;
    let capture_review = st
        .with_vault(|_, v| crate::dataset::capture_enabled(v))
        .map_err(internal)?;
    let engine = st.engine();
    let sample_id = if engine.policy().strategy("PATH") == kaponir_core::Strategy::Hmac {
        kaponir_core::TokenId::Hash("a1b2c3".into())
    } else {
        kaponir_core::TokenId::Num(1)
    };
    let sample = engine
        .codec()
        .format("PATH", &sample_id, kaponir_core::TokenForm::Snake);
    Ok(Json(
        json!({"path_token_example":sample,"model_count":model_count,"capture_review":capture_review,"snapshot":snapshot,"items":page,"total":total,"pending":pending,"offset":offset,"limit":limit,"sources":sources,"jobs":jobs,"editable":st.control.is_some()}),
    ))
}
pub(crate) fn random_id() -> anyhow::Result<String> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|e| anyhow::anyhow!(e))?;
    Ok(blake3::hash(&bytes).to_hex().to_string())
}

fn selection_record(saved: &SavedSelection) -> Value {
    let mut record = json!(saved);
    record["created"] = json!(now_ms());
    record
}

fn saved_selection(st: &Shared, id: &str) -> anyhow::Result<SavedSelection> {
    let saved = read(st, &format!("selection:{id}"))?
        .ok_or_else(|| anyhow::anyhow!("selection_expired"))?;
    Ok(serde_json::from_value(saved)?)
}

async fn selection(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(q): Json<Filters>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let (_, items) = filtered(&st, &q).map_err(internal)?;
    let saved = SavedSelection::from_items(&items);
    let id = random_id().map_err(internal)?;
    write(
        &st,
        &format!("selection:{id}"),
        "review-selection",
        &selection_record(&saved),
    )
    .map_err(internal)?;
    let path_sizes: BTreeMap<_, _> = saved
        .path_targets
        .iter()
        .map(|(key, paths)| (key, paths.len()))
        .collect();
    Ok(Json(
        json!({"snapshot":id,"total":saved.keys.len(),"grouped":!saved.occurrences.is_empty(),"path_sizes":path_sizes}),
    ))
}

#[derive(Default, Serialize, Deserialize)]
pub(crate) struct Selection {
    #[serde(default)]
    keys: Vec<String>,
    snapshot: Option<String>,
    #[serde(default)]
    excluded: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    prefixes: BTreeMap<String, String>,
}

impl Selection {
    pub(crate) fn has_explicit_selection(&self) -> bool {
        !self.keys.is_empty() || self.snapshot.is_some() || !self.excluded.is_empty()
    }

    pub(crate) fn resolve(
        &self,
        st: &Shared,
        include_occurrences: bool,
    ) -> anyhow::Result<Vec<Value>> {
        let excluded: HashSet<_> = self.excluded.iter().collect();
        let mut prefixes = self.prefixes.clone();
        let keys = match self.snapshot.as_deref() {
            Some(id) => {
                let saved = saved_selection(st, id)?;
                if include_occurrences {
                    for (key, prefix) in &saved.prefixes {
                        if !excluded.contains(key)
                            && let Some(records) = saved.occurrences.get(key)
                        {
                            for record in records {
                                prefixes.insert(record.clone(), prefix.clone());
                            }
                        }
                    }
                    saved.expanded(&excluded)
                } else {
                    saved
                        .keys
                        .into_iter()
                        .filter(|key| !excluded.contains(key))
                        .flat_map(|key| {
                            saved
                                .path_targets
                                .get(&key)
                                .cloned()
                                .unwrap_or_else(|| vec![key])
                        })
                        .collect()
                }
            }
            None => self.keys.clone(),
        };
        let mut all: HashMap<_, _> = items(st)?
            .into_iter()
            .map(|i| (i["key"].as_str().unwrap().to_owned(), i))
            .collect();
        let mut seen = HashSet::new();
        keys.into_iter()
            .filter(|key| !excluded.contains(key) && seen.insert(key.clone()))
            .map(|key| {
                let mut row = all
                    .remove(&key)
                    .ok_or_else(|| anyhow::anyhow!("entry_not_found"))?;
                if include_occurrences
                    && let Some(prefix) = prefixes
                        .get(&key)
                        .and_then(|p| paths::selected_prefix(&row, p))
                {
                    row["dataset_prefix"] = json!(prefix);
                }
                Ok(row)
            })
            .collect()
    }
}

#[derive(Clone, Serialize, Deserialize)]
struct Target {
    key: String,
    fingerprint: String,
}
fn store_queued_job(
    v: &kaponir_vault::SqliteVault,
    job: &Value,
    targets: &[Target],
) -> anyhow::Result<()> {
    let id = job["id"].as_str().unwrap();
    v.put_app_record(
        &format!("review-targets:{id}"),
        "review-targets",
        &serde_json::to_vec(targets)?,
    )?;
    v.put_app_record(
        &format!("review-job:{id}"),
        "review-job-queued",
        &serde_json::to_vec(job)?,
    )?;
    Ok(())
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
    let _settings = st.learning_changes.lock().await;
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
        .resolve(&st, false)
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
                store_queued_job(v, &job, &targets)?;
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

fn learning_job_source(job: &mut Value, queue_wait: Option<crate::learning::QueueWait>) {
    if job["state"] == "queued" {
        job["queue_wait"] = json!(queue_wait);
    }
    if job["question"].is_object() {
        job["event_id"] = job["question"]["event_id"].clone();
        job["session_id"] = job["question"]["session_id"].clone();
        return;
    }
    if job["kind"] == "session" {
        let name = job["name"].as_str().unwrap_or_default().to_owned();
        if let Some(id) = name.strip_prefix("session-") {
            job["session_id"] = json!(id);
        } else if let Some(id) = name
            .strip_prefix("request-")
            .and_then(|s| s.parse::<u64>().ok())
        {
            job["event_id"] = json!(id);
        }
    }
    // Retained turn jobs already encode the proxy event ID. Document names can
    // contain arbitrary numbers or separators and must never be parsed as turns.
    if job["kind"] == "turn"
        && let Some(event) = job["id"]
            .as_str()
            .and_then(|id| id.strip_prefix("learning:job:turn:"))
            .and_then(|id| id.parse::<u64>().ok())
    {
        let session = job["name"]
            .as_str()
            .and_then(|name| name.rsplit_once(" · "))
            .filter(|(_, id)| id.parse::<u64>().ok() == Some(event))
            .map(|(session, _)| session.to_owned());
        job["event_id"] = json!(event);
        job["session_id"] = json!(session);
    }
}

async fn jobs(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(q): Query<Filters>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let queue_wait = st.learning.queue_wait();
    if q.family.as_deref() == Some("learning")
        && let Some(id) = &q.id
    {
        let mut jobs = Vec::new();
        if let Some(mut meta) = read(&st, &format!("meta:{id}")).map_err(internal)?
            && meta["id"] == *id
        {
            meta["family"] = json!("learning");
            learning_job_source(&mut meta, queue_wait);
            jobs.push(meta);
        }
        return Ok(Json(json!({"jobs":jobs,"total":jobs.len()})));
    }
    let mut rows = st.with_vault(|_, v| {
        let mut rows = Vec::new();
        for kind in ["review-job-running","review-job-queued","review-job-failed","review-job-succeeded","review-job-cancelled","learning-job-meta"] {
            for (_, bytes) in v.app_records_recent(kind, i64::MAX as usize)? {
                let j: Value = serde_json::from_slice(&bytes)?;
                let learning = kind == "learning-job-meta";
                let mut row = json!({
                    "family": if learning { "learning" } else { "review" }, "id":j["id"],"kind":j["kind"],
                    "name":j["name"],"question":j["question"],"state":j["state"],"created":j["created"],"updated":j["updated"],
                    "started":j["started"],"progress":j["progress"],"last_call":j["last_call"],"source":j["source"],"model":j["model"],"scenario":j["scenario"],
                    "total":j["total"].as_u64().or_else(||j["targets"].as_array().map(|x|x.len() as u64)),
                    "completed":j.get("processed").unwrap_or(&j["completed"]),"extracted":j["extracted"],
                    "active":j["active"],"failed":j["failed"],"manual":j["manual"],"retryable":j["retryable"],"error":j["error"],
                    "automatic":j["automatic"],"applied":j["applied"]
                });
                if learning { learning_job_source(&mut row, queue_wait); }
                rows.push(row);
            }
        }
        Ok(rows)
    }).map_err(internal)?;
    let worker = match crate::worker::forward(&st, "/workspace/jobs", None).await {
        Ok(result) if result["jobs"].is_array() => Some(result),
        Err(err) if err.message() == "worker unavailable" => None,
        _ => crate::worker::forward(&st, "/status", None).await.ok(),
    };
    let activity = st.learning.activity.snapshot(
        worker
            .as_ref()
            .map(|w| &w["measurements"])
            .unwrap_or(&Value::Null),
    );
    for row in &mut rows {
        if let Some(live) = row["id"].as_str().and_then(|id| activity.get(id)) {
            row["inference_activity"] = live.clone();
        }
    }
    if let Some(files) = worker.as_ref().and_then(|w| w["jobs"].as_array()) {
        for j in files {
            let download = j["kind"] == "download";
            rows.push(json!({
                "family": "file", "kind": j["kind"], "stage": j["stage"], "id": j["id"],
                "name": j["source"], "state": j["state"], "model": j["model"], "error": j["error"],
                "current_file":j["current_file"], "error_code":j["error_code"],
                "updated": (1000.0 * j["updated"].as_f64().unwrap_or_default()) as u64,
                "created": (1000.0 * j["created"].as_f64().unwrap_or_default()) as u64,
                "total": if download { &j["progress_total"] } else { &j["total"] },
                "completed": if download { &j["progress"] } else { &j["completed"] }
            }));
        }
    }
    if q.family.as_deref().is_none_or(|f| f == "ocr") {
        rows.extend(crate::image_protection::job_rows(&st));
    }
    rows.retain(|row| {
        q.family.as_ref().is_none_or(|f| row["family"] == *f)
            && q.id.as_ref().is_none_or(|id| row["id"] == *id)
    });
    let running = |s: &Value| {
        matches!(
            s["state"].as_str(),
            Some("running" | "processing" | "queued" | "collecting" | "scanning" | "downloading")
        )
    };
    rows.sort_by_key(|r| {
        let created = r["created"].as_u64().unwrap_or_default();
        // Active work first, then queued work oldest first, then recent history.
        let rank = if matches!(r["state"].as_str(), Some("queued" | "collecting")) {
            1
        } else if running(r) {
            0
        } else if r["state"] == "paused" {
            2
        } else {
            3
        };
        let order = if rank == 1 {
            created
        } else {
            u64::MAX - r["updated"].as_u64().unwrap_or(created)
        };
        (rank, order)
    });
    let active = rows.iter().filter(|r| running(r)).count();
    let collecting = rows.iter().filter(|r| r["state"] == "collecting").count();
    let queued = rows.iter().filter(|r| r["state"] == "queued").count() + collecting;
    let in_progress = active - queued;
    let preview: Vec<_> = if active == 0 {
        rows.iter().take(3).cloned().collect()
    } else {
        let visible = in_progress.min(if queued > 0 { 3 } else { 4 });
        rows.iter()
            .take(visible)
            .chain(rows.iter().skip(in_progress).take(queued.min(4 - visible)))
            .cloned()
            .collect()
    };
    let review_activity: Vec<_> = rows
        .iter()
        .take(active)
        .filter(|j| j["family"] == "review")
        .map(|j| json!([j["id"], j["state"], j["completed"]]))
        .collect();
    let total = rows.len();
    let offset = q.offset.unwrap_or_default();
    let rows: Vec<_> = rows
        .into_iter()
        .skip(offset)
        .take(q.limit.unwrap_or(100).clamp(1, 250))
        .collect();
    Ok(Json(
        json!({"jobs":rows,"total":total,"active":active,"queued":queued,"collecting":collecting,"preview":preview,"model_activity":activity,"review_activity":review_activity,"api_active":st.api_slots.active(),"updated":now_ms(),"worker_online":worker.as_ref().and_then(|w|w["online"].as_bool())}),
    ))
}

/// Reviews also handle identifiers already present in findings and labelled datasets.
pub(crate) fn supported_kinds() -> impl Iterator<Item = &'static str> {
    kaponir_core::kinds::STANDARD.iter().copied().chain(["ID"])
}

fn schema(ids: &[String]) -> Value {
    let count = ids.len();
    json!({"type":"object","properties":{"decisions":{"type":"array","minItems":count,"maxItems":count,"items":{"type":"object","properties":{
        "id":{"type":"string","enum":ids},"action":{"type":"string","enum":["mask","keep","review"]},"kind":{"type":"string","enum":supported_kinds().collect::<Vec<_>>()},
        "scope":{"type":"string","enum":["personal","team","global"]},"confidence":{"type":"number","minimum":0,"maximum":1},"reason":{"type":"string","minLength":1,"maxLength":400}},
        "required":["id","action","kind","scope","confidence","reason"],"additionalProperties":false}}},"required":["decisions"],"additionalProperties":false})
}

fn validate_decision(decision: &Value, value: &str, research: bool) -> Result<(), &'static str> {
    let kind = decision["kind"].as_str().unwrap_or_default();
    if !supported_kinds().any(|known| known == kind) {
        return Err("review_unknown_kind");
    }
    if !matches!(
        decision["action"].as_str(),
        Some("mask" | "keep" | "review")
    ) {
        return Err("review_invalid_action");
    }
    if !(research && decision["confidence"].is_null()
        || decision["confidence"]
            .as_f64()
            .is_some_and(|p| p.is_finite() && (0.0..=1.0).contains(&p)))
    {
        return Err("review_invalid_confidence");
    }
    if !decision["scope"]
        .as_str()
        .is_some_and(|s| s.parse::<Scope>().is_ok())
    {
        return Err("review_invalid_scope");
    }
    if !decision["reason"]
        .as_str()
        .is_some_and(|s| !s.trim().is_empty() && s.len() <= 1600)
    {
        return Err("review_invalid_reason");
    }
    if decision["action"] != "review" && kaponir_core::kinds::type_conflict(kind, value) {
        return Err("type_format_conflict");
    }
    Ok(())
}

async fn run(st: Shared, submitted: Value, targets: Vec<Target>) {
    let _queue = st.review_queue.lock().await;
    let id = submitted["id"].as_str().unwrap();
    let automatic = submitted["automatic"] == true;
    loop {
        let mut changed = st.learning.revision_signal.subscribe();
        let Ok(Some(mut job)) = read(&st, &format!("review-job:{id}")) else {
            return;
        };
        if job["state"] != "queued" {
            return;
        }
        let revision = *changed.borrow_and_update();
        if job["revision"] != revision {
            let _guard = st.rule_changes.lock().await;
            if let Ok(Some(mut current)) = read(&st, &format!("review-job:{id}"))
                && current["state"] == "queued"
                && current["revision"] != *st.learning.revision_signal.borrow()
            {
                current["state"] = json!("failed");
                current["active"] = json!(0);
                current["error"] = json!("configuration_changed");
                current["updated"] = json!(now_ms());
                let _ = write(
                    &st,
                    &format!("review-job:{id}"),
                    "review-job-failed",
                    &current,
                );
                return;
            }
            continue;
        }
        let processed = job["processed"].as_u64().unwrap_or(0);
        // A partially processed job retains the scenario used for its saved packets.
        if processed == 0 && !automatic && job["source"]["provider"] == "api" {
            job["source"]["scenario"] = json!(learning::Scenario::D);
        }
        let remaining;
        let work = if processed > 0 {
            let Ok(Some(saved)) = full_job(&st, id) else {
                return;
            };
            remaining = targets
                .iter()
                .filter(|t| saved["results"][&t.key].is_null())
                .cloned()
                .collect::<Vec<_>>();
            remaining.as_slice()
        } else {
            targets.as_slice()
        };
        job["started"] = json!(now_ms());
        job["state"] = json!("running");
        let mut enabled = st.learning.automatic_review_signal.subscribe();
        let paused = automatic && !*enabled.borrow_and_update();
        let execution = if paused {
            Err(anyhow::anyhow!("automatic_review_stopped"))
        } else {
            tokio::select! {
                biased;
                _ = changed.changed() => continue,
                _ = enabled.changed(), if automatic => {
                    if let Ok(Some(saved)) = read(&st, &format!("review-job:{id}")) {
                        job = saved;
                    }
                    Err(anyhow::anyhow!("automatic_review_stopped"))
                },
                (result, last) = st.learning.activity.track(id, run_packets(&st, &mut job, work)) => {
                    job["last_call"] = json!(last);
                    result
                }
            }
        };
        let _guard = st.rule_changes.lock().await;
        if *st.learning.revision_signal.borrow() != revision {
            continue;
        }
        if let Err(e) = execution {
            job["error"] = json!(learning::safe_error(&e));
        }
        if job["error"] == "" && job["failed"].as_u64().unwrap_or(0) > 0 {
            job["error"] = json!("review_partial_failure");
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
        return;
    }
}

async fn run_packets(st: &Shared, job: &mut Value, targets: &[Target]) -> anyhow::Result<()> {
    let settings = st.learning.snapshot();
    let inference = if job["source"]["role"] == "analysis" {
        &settings.analysis.inference
    } else {
        &settings.categorization.inference
    };
    let slots = settings.analysis.parallelism.max(1);
    let is_automatic = job["automatic"] == true;
    let map: HashMap<String, Value> = if is_automatic {
        let rules = active_rules(st)?;
        st.with_vault(|_, v| {
            let mut selected = HashMap::new();
            for target in targets {
                if let Some(mut item) = automatic::finding(v, &target.key)? {
                    rules.project(&mut item);
                    if needs_decision(&item) && item["protected"] != true {
                        selected.insert(target.key.clone(), item);
                    }
                }
            }
            Ok(selected)
        })?
    } else {
        items(st)?
            .into_iter()
            .map(|i| (i["key"].as_str().unwrap().to_owned(), i))
            .collect()
    };
    let pending_targets;
    let targets = if is_automatic {
        pending_targets = targets
            .iter()
            .filter(|target| map.contains_key(&target.key))
            .cloned()
            .collect::<Vec<_>>();
        let skipped = (targets.len() - pending_targets.len()) as u64;
        job["skipped"] = json!(job["skipped"].as_u64().unwrap_or(0) + skipped);
        job["total"] = json!(job["total"].as_u64().unwrap_or(0).saturating_sub(skipped));
        pending_targets.as_slice()
    } else {
        targets
    };
    let source = job["source"].clone();
    let id = job["id"].as_str().unwrap().to_owned();
    let batches = review_batches(targets, &map, inference, &source);
    let mut scheduled = slots.min(batches.len());
    let mut active: usize = batches[..scheduled].iter().map(|b| b.len()).sum();
    let mut packets = stream::iter(0..batches.len())
        .map(|index| {
            let batch = batches[index];
            let source = &source;
            let map = &map;
            let id = &id;
            async move {
                let start = std::time::Instant::now();
                let mut effective = source.clone();
                let recheck = needs_type_check(source, map.get(&batch[0].key));
                if recheck {
                    effective["scenario"] = json!("standard");
                }
                let (mut records, identified) = infer_bounded(st, &effective, id, map, batch).await;
                if recheck {
                    for (_, row) in &mut records {
                        row["type_recheck"] = json!(true);
                    }
                }
                let result = (records, identified);
                (index, batch, result, start.elapsed().as_secs_f64())
            }
        })
        .buffer_unordered(slots);
    let save = |job: &Value| {
        st.with_vault(|_, v| {
            anyhow::ensure!(
                job["revision"] == *st.learning.revision_signal.borrow(),
                "configuration_changed"
            );
            Ok(v.put_app_record(
                &format!("review-job:{id}"),
                "review-job-running",
                &serde_json::to_vec(job)?,
            )?)
        })
    };
    job["slots"] = json!(slots);
    job["active"] = json!(active);
    save(job)?;
    let mut first_error = None;
    while let Some((index, batch, result, seconds)) = packets.next().await {
        job["last_packet_seconds"] = json!(seconds);
        let (records, identified) = result;
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
        job["completed"] = json!(job["completed"].as_u64().unwrap_or(0) + identified as u64);
        active -= batch.len();
        if let Some(next) = batches.get(scheduled) {
            scheduled += 1;
            active += next.len();
        }
        job["active"] = json!(active);
        job["updated"] = json!(now_ms());
        let _guard = st.rule_changes.lock().await;
        anyhow::ensure!(
            job["revision"] == *st.learning.revision_signal.borrow(),
            "configuration_changed"
        );
        if is_automatic {
            let policy = st.learning.snapshot().analysis;
            anyhow::ensure!(
                policy.enabled && policy.auto_review,
                "automatic_review_stopped"
            );
            if policy.auto_apply {
                let mut applied = job["applied"].as_u64().unwrap_or(0);
                for (key, rec) in &records {
                    match automatic::apply_mask(st, key, rec, policy.threshold) {
                        Ok(true) => applied += 1,
                        Ok(false) => {}
                        Err(_) => {
                            first_error.get_or_insert_with(|| "automatic_rule_failed".into());
                        }
                    }
                }
                job["applied"] = json!(applied);
            }
        }
        st.with_vault(|_, v| {
            learning::transaction(v, || {
                anyhow::ensure!(
                    job["revision"] == *st.learning.revision_signal.borrow(),
                    "configuration_changed"
                );
                for (key, rec) in &records {
                    v.put_app_record(
                        &format!("review-rec:{key}"),
                        "review-recommendation",
                        &serde_json::to_vec(rec)?,
                    )?;
                }
                v.put_app_record(
                    &format!("packet:{id}:{}:{index}", settings.revision),
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

// Scenario D supplies only a privacy verdict. An incompatible inherited label
// requires a fresh type and scope from the same model before proposing a rule.
fn needs_type_check(source: &Value, item: Option<&Value>) -> bool {
    source["scenario"] == "D"
        && item.is_some_and(|row| {
            let kind = row["kind"].as_str().unwrap_or_default();
            !supported_kinds().any(|known| known == kind)
                || source["provider"] == "api"
                    && (row["reason"] == "type_format_conflict"
                        || row["recommendation"]["reason"] == "type_format_conflict")
                || kaponir_core::kinds::type_conflict(
                    kind,
                    row["value"].as_str().unwrap_or_default(),
                )
        })
}

fn retry_individually(item: Option<&Value>) -> bool {
    item.is_some_and(|row| {
        let rec = &row["recommendation"];
        rec["state"] == "failed"
            && matches!(
                rec["error"].as_str(),
                Some(
                    "invalid_classification_response"
                        | "invalid_model_json"
                        | "model_response_truncated"
                        | "protected_credential"
                )
            )
    })
}

fn review_batches<'a>(
    targets: &'a [Target],
    items: &HashMap<String, Value>,
    inference: &learning::InferenceSettings,
    source: &Value,
) -> Vec<&'a [Target]> {
    let mut batches = Vec::new();
    let budget = inference
        .context_tokens
        .saturating_sub(inference.max_output_tokens + inference.reserve_tokens);
    let mut start = 0;
    while start < targets.len() {
        let mut end = start;
        let first = items.get(&targets[start].key);
        let recheck = needs_type_check(source, first);
        let limit = if retry_individually(first) {
            1
        } else {
            inference.batch_size.max(1)
        };
        // Reserve room for the instructions/schema and conservatively budget UTF-8 input bytes.
        let mut used = 1024;
        while end < targets.len() && end - start < limit {
            let item = items.get(&targets[end].key);
            let weight = item.map_or(0, |v| {
                v["value"].as_str().unwrap_or("").len() + v["context"].as_str().unwrap_or("").len()
            }) + 160;
            if end > start
                && (needs_type_check(source, item) != recheck
                    || retry_individually(item)
                    || source["provider"] != "local" && used + weight > budget)
            {
                break;
            }
            used += weight;
            end += 1;
        }
        batches.push(&targets[start..end]);
        start = end;
    }
    batches
}
async fn infer_bounded(
    st: &Shared,
    source: &Value,
    id: &str,
    map: &HashMap<String, Value>,
    batch: &[Target],
) -> (Vec<(String, Value)>, usize) {
    let mut pending = vec![batch];
    let mut records = Vec::new();
    let mut identified = 0;
    while let Some(part) = pending.pop() {
        match infer_packet(st, source, id, map, part).await {
            Ok(rows) => {
                identified += rows.len();
                records.extend(rows);
            }
            Err(e) => {
                let code = learning::safe_error(&e);
                let preflight = matches!(
                    code.as_str(),
                    "protected_credential" | "review_stale" | "entry_not_found"
                );
                let too_large = matches!(
                    code.as_str(),
                    "model_context_budget_exceeded" | "model_response_truncated"
                );
                if part.len() > 1 && (preflight || source["provider"] != "local" && too_large) {
                    let (left, right) = part.split_at(part.len() / 2);
                    pending.extend([right, left]);
                    continue;
                }
                for target in part {
                    let mut rec = json!({"fingerprint":target.fingerprint, "job_id":id, "revision":source["revision"],
                        "model":source["model"],"created":now_ms()});
                    if code == "protected_credential" {
                        rec["state"] = json!("needs_review");
                        rec["reason"] = json!(code);
                    } else {
                        rec["state"] = json!("failed");
                        if code == "review_response_ids" {
                            rec["detail"] = json!(code);
                            rec["error"] = json!("invalid_classification_response");
                        } else {
                            rec["error"] = json!(code);
                        }
                    }
                    records.push((target.key.clone(), rec));
                }
            }
        }
    }
    (records, identified)
}
// A truncated file URL can look like a password in isolation. Review only its
// structure when the exact occurrence is grounded, and keep the digest local.
fn git_revision_in_url<'a>(
    value: &'a str,
    context: &str,
    span: Option<[usize; 2]>,
) -> Option<&'a str> {
    let (extension, digest) = value.split_once("?ref=")?;
    if extension.is_empty()
        || extension.len() > 8
        || !extension.bytes().all(|b| b.is_ascii_alphabetic())
        || !matches!(digest.len(), 40 | 64)
        || !digest.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return None;
    }
    let (start, end) = if let Some([start, end]) = span {
        (context.get(start..end) == Some(value)).then_some((start, end))?
    } else {
        let mut matches = context.match_indices(value);
        let (start, _) = matches.next()?;
        matches
            .next()
            .is_none()
            .then_some((start, start + value.len()))?
    };
    if context[end..].starts_with(|ch: char| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-') {
        return None;
    }
    let url = context[..end]
        .rsplit(|ch: char| ch.is_whitespace() || matches!(ch, '"' | '\'' | '<' | '>' | '(' | '['))
        .next()?;
    let url = reqwest::Url::parse(url).ok()?;
    let file = url.path_segments()?.next_back()?;
    (matches!(url.scheme(), "http" | "https")
        && url.host().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && context[..start].ends_with('.')
        && file
            .rsplit_once('.')
            .is_some_and(|(name, ext)| !name.is_empty() && ext == extension)
        && url.query()?.strip_prefix("ref=") == Some(digest))
    .then_some(digest)
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
    let mut redacted_revisions = HashSet::new();
    for target in batch {
        let item = current
            .get(&target.key)
            .ok_or_else(|| anyhow::anyhow!("entry_not_found"))?;
        anyhow::ensure!(fingerprint(item) == target.fingerprint, "review_stale");
        anyhow::ensure!(item["protected"] != true, "protected_credential");
        let value = item["value"].as_str().unwrap_or_default();
        let protected = st
            .engine()
            .analyze(&kaponir_core::Input::text(value))
            .iter()
            .any(|c| c.hard && c.category == kaponir_core::Category::Credential);
        let quote = item["context"].as_str().unwrap_or_default();
        let mut context = redacted_context(st, quote, value);
        let id = model_id(target);
        let shown = if protected {
            let digest = git_revision_in_url(
                value,
                quote,
                serde_json::from_value(item["context_span"].clone()).ok(),
            )
            .ok_or_else(|| anyhow::anyhow!("protected_credential"))?;
            redacted_revisions.insert(id.clone());
            context = context.replace(digest, "[GIT_REVISION]");
            value.replace(digest, "[GIT_REVISION]")
        } else {
            value.to_owned()
        };
        let mut packet = json!({"id":id,"value":shown,"kind":item["kind"],"context":context});
        if item["path_suffix"] == true && context == quote {
            packet["verified_at"] = item["context_span"][0].clone();
        }
        input.push(packet);
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
    anyhow::ensure!(decisions.len() == batch.len(), "review_response_ids");
    // Uncertain identity invalidates the packet. Once every ID is unique and
    // expected, a row's type/shape error must not discard its neighbors.
    for target in batch {
        let id = model_id(target);
        anyhow::ensure!(
            decisions.iter().filter(|d| d["id"] == id).count() == 1,
            "review_response_ids"
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
            Err("type_format_conflict") => {
                json!({"state":"needs_review","reason":"type_format_conflict"})
            }
            // Retain only a diagnostic for malformed responses; the user can retry the finding separately.
            Err(detail) => {
                json!({"state":"failed","error":"invalid_classification_response","detail":detail})
            }
        };
        if redacted_revisions.contains(&id) {
            rec["redacted_git_revision"] = json!(true);
        }
        rec["job_id"] = json!(job_id);
        rec["revision"] = source["revision"].clone();
        rec["fingerprint"] = json!(target.fingerprint);
        rec["model"] = source["model"].clone();
        rec["created"] = json!(now_ms());
        validated.push((target.key.clone(), rec));
    }
    Ok(validated)
}

pub(crate) fn redacted_context(st: &Shared, context: &str, value: &str) -> String {
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
    #[serde(default)]
    whole_path: bool,
    expected_job: Option<String>,
    proposal_id: Option<String>,
    seen_excerpt: Option<String>,
    #[serde(default)]
    occurrences: Vec<String>,
}
async fn apply(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path((origin, id, mut action)): Path<(String, String, String)>,
    Json(req): Json<Apply>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    if st.control.is_none() {
        return Err(bad("read_only"));
    }
    let _guard = st.rule_changes.lock().await;
    let key = format!("{origin}:{id}");
    let item = action_item(&st, &origin, &id)
        .map_err(internal)?
        .ok_or_else(|| bad("entry_not_found"))?;
    let seen = req.seen_excerpt.is_some();
    if req
        .seen_excerpt
        .as_ref()
        .is_some_and(|id| *id != excerpt_signature(&item))
    {
        return Err(bad("review_stale"));
    }
    if action == "defer" {
        if !needs_decision(&item) {
            return Err(bad("entry_not_pending"));
        }
        let created =
            serde_json::to_vec(&json!({"created":now_ms()})).map_err(|e| internal(e.into()))?;
        st.with_vault(|_, v| {
            learning::transaction(v, || {
                let defer = |row: &Value| -> anyhow::Result<()> {
                    if needs_decision(row) {
                        v.put_app_record(
                            &format!("review-deferred:{}", row["key"].as_str().unwrap()),
                            "review-deferred",
                            &created,
                        )?;
                    }
                    Ok(())
                };
                defer(&item)?;
                related_occurrences(v, &item, &req.occurrences, defer)
            })
        })
        .map_err(internal)?;
        return Ok(Json(json!({"ok":true,"state":"deferred"})));
    }
    let manual_whole_path = req.whole_path && action == "mask" && !req.use_recommendation;
    let mut kind = req
        .kind
        .unwrap_or_else(|| item["kind"].as_str().unwrap_or("CUSTOM").into());
    let mut scope = req
        .scope
        .unwrap_or_else(|| kaponir_core::s1::default_scope_for_kind(&kind));
    if matches!(action.as_str(), "accept-proposal" | "decline-proposal") {
        let proposal = &item["proposal"];
        if req.proposal_id.is_none() || proposal["id"].as_str() != req.proposal_id.as_deref() {
            return Err(bad("review_stale"));
        }
        if !matches!(item["state"].as_str(), Some("open" | "pending"))
            && (action == "decline-proposal" || item["document_rule"].is_null())
        {
            return Err(bad("entry_not_pending"));
        }
        if action == "decline-proposal" {
            write(
                &st,
                &format!("review-declined:{key}"),
                "review-declined",
                &proposal["id"],
            )
            .map_err(internal)?;
            return Ok(Json(json!({"ok":true,"state":"proposal_declined"})));
        }
        if proposal["declined"] == true
            || proposal["source"] == "review"
                && recommendation_revision(&item["recommendation"])
                    != Some(st.learning.snapshot().revision)
        {
            return Err(bad("review_stale"));
        }
        kind = proposal["kind"]
            .as_str()
            .ok_or_else(|| bad("invalid_kind"))?
            .to_owned();
        scope =
            serde_json::from_value(proposal["scope"].clone()).map_err(|_| bad("invalid_scope"))?;
        action = proposal["action"]
            .as_str()
            .ok_or_else(|| bad("invalid_action"))?
            .to_owned();
    } else if req.use_recommendation {
        let rec = &item["recommendation"];
        if rec["state"] == "needs_review" {
            return Err(bad("review_inconclusive"));
        }
        if rec["state"] != "succeeded"
            || item["proposal"]["declined"] == true
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
    if matches!(action.as_str(), "mask" | "keep") && !item["unmatched_rule"].is_null() {
        return Err(unmatched_rule_error(&item));
    }
    if action == "mask"
        && kind == "PATH"
        && !manual_whole_path
        && let Some(prefix) = paths::account_directory(&item)
        && (needs_decision(&item) || item["document_rule"]["path_prefix"] == prefix)
    {
        let mut sources = vec![item.clone()];
        st.with_vault(|_, v| {
            related_occurrences(v, &item, &req.occurrences, |other| {
                if paths::scope(other) == paths::scope(&item)
                    && paths::account_directory(other) == Some(prefix)
                    && needs_decision(other)
                {
                    sources.push(other.clone());
                }
                Ok(())
            })
        })
        .map_err(internal)?;
        return paths::apply_directory(&st, sources, prefix, scope, seen.then_some(key.as_str()));
    }
    let mut result = apply_effect(
        &st,
        &item,
        &action,
        &kind,
        scope,
        req.use_recommendation,
        false,
    )?;
    if result["state"] != "conflict"
        && matches!(action.as_str(), "mask" | "keep" | "dismiss" | "reject")
    {
        let rule = &result["rule"];
        let applied_kind = if action == "mask" {
            rule["kind"].as_str().unwrap_or(&kind)
        } else {
            &kind
        };
        let confirmed = seen
            && (result["already_covered"] != true
                || (applied_kind == kind && rule["scope"] == json!(scope)));
        let label =
            matches!(action.as_str(), "mask" | "keep").then_some((action.as_str(), applied_kind));
        let captured = st.with_vault(|_, v| {
            learning::transaction(v, || {
                if !crate::dataset::capture_enabled(v)? {
                    return Ok(Value::Null);
                }
                let mut checked = 0;
                let mut drafts = 0;
                let mut record = |row: &Value, label, seen| -> anyhow::Result<()> {
                    use crate::dataset::Capture;
                    match crate::dataset::capture_finding(v, row, label, seen)? {
                        Capture::Checked => checked += 1,
                        Capture::Draft => drafts += 1,
                        Capture::Unchanged => {}
                    }
                    Ok(())
                };
                record(&item, label, confirmed)?;
                related_occurrences(v, &item, &req.occurrences, |row| record(row, None, false))?;
                Ok(json!({"checked":checked,"drafts":drafts}))
            })
        });
        // Rule application and optional collection have independent outcomes.
        result["dataset"] = captured.unwrap_or_else(|_| json!({"error":"dataset_capture_failed"}));
    }
    Ok(result)
}

fn related_occurrences(
    v: &kaponir_vault::SqliteVault,
    item: &Value,
    keys: &[String],
    mut visit: impl FnMut(&Value) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    let mut visited = HashSet::new();
    for key in keys {
        if item["key"] == *key || !visited.insert(key) {
            continue;
        }
        if let Some((origin, id)) = key.split_once(':')
            && let Some(other) = raw_finding(v, origin, id)?
            && other["value"] == item["value"]
        {
            visit(&other)?;
        }
    }
    Ok(())
}

async fn skip_selected(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(selection): Json<Selection>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    if st.control.is_none() {
        return Err(bad("read_only"));
    }
    let _guard = st.rule_changes.lock().await;
    let items = selection
        .resolve(&st, true)
        .map_err(|error| bad(&learning::safe_error(&error)))?;
    let mut closed = Vec::new();
    let mut failures = Vec::new();
    for item in items.into_iter().filter(needs_decision) {
        let action = if item["origin"] == "detector" {
            "dismiss"
        } else {
            "reject"
        };
        let kind = item["kind"].as_str().unwrap_or_default();
        match apply_effect(&st, &item, action, kind, paths::scope(&item), false, false) {
            Ok(_) => closed.push(item),
            Err(error) => failures.push(json!({"value":item["value"],"error":error.message()})),
        }
    }
    let dataset = st
        .with_vault(|_, v| {
            learning::transaction(v, || {
                let mut drafts = 0;
                if !closed.is_empty() && crate::dataset::capture_enabled(v)? {
                    for item in &closed {
                        if matches!(
                            crate::dataset::capture_finding(v, item, None, false)?,
                            crate::dataset::Capture::Draft
                        ) {
                            drafts += 1;
                        }
                    }
                }
                Ok(json!({"drafts":drafts}))
            })
        })
        .unwrap_or_else(|_| json!({"error":"dataset_capture_failed"}));
    Ok(Json(
        json!({"skipped":closed.len(),"failures":failures,"dataset":dataset}),
    ))
}

fn apply_effect(
    st: &Shared,
    item: &Value,
    action: &str,
    kind: &str,
    scope: Scope,
    use_recommendation: bool,
    automatic: bool,
) -> ResultJson {
    let origin = item["origin"].as_str().unwrap_or_default();
    let id = item["id"].as_str().unwrap_or_default();
    let key = item["key"].as_str().unwrap_or_default();
    let active = &item["document_rule"];
    if matches!(action, "mask" | "keep") && !active.is_null() {
        if active["action"] != action {
            return Err(bad("rule_action_conflict"));
        }
        return Ok(Json(
            json!({"ok":true,"state":"covered","already_covered":true,"rule":active}),
        ));
    }
    if (action == "mask" || action == "keep" && origin == "detector")
        && kaponir_core::kinds::type_conflict(kind, item["value"].as_str().unwrap_or_default())
    {
        return Err(bad("type_format_conflict"));
    }
    if origin == "learning" {
        let mapped = match action {
            "mask" => "approve",
            "keep" => "keep",
            "reject" => "reject",
            "rollback" => "rollback",
            _ => return Err(bad("invalid_action")),
        };
        if item["state"] == "rejected"
            && (mapped == "reject" || mapped == "keep" && item["reason"] == "human_keep")
        {
            return Ok(Json(json!({"ok":true,"state":"rejected"})));
        }
        if matches!(mapped, "reject" | "keep") && item["state"] != "pending" {
            return Err(bad("entry_not_pending"));
        }
        return learning::change_entry(
            st,
            id,
            mapped,
            learning::Action {
                kind: Some(kind.to_owned()),
                scope: Some(scope),
            },
            automatic,
        );
    }
    let ctl = st.control.as_ref().ok_or_else(|| bad("read_only"))?;
    let row_id = id.parse::<i64>().map_err(|_| bad("entry_not_found"))?;
    let row = st
        .with_vault(|_, v| Ok(v.journal_row(row_id)?))
        .map_err(internal)?
        .ok_or_else(|| bad("entry_not_found"))?;
    if row.value != item["value"].as_str().unwrap_or_default()
        || row.context != item["context"].as_str().unwrap_or_default()
        || row.kind != item["kind"].as_str().unwrap_or_default()
        || use_recommendation
            && recommendation_revision(&item["recommendation"])
                != Some(st.learning.snapshot().revision)
    {
        return Err(bad("review_stale"));
    }
    let mut decision = None;
    let status = match action {
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
            {
                return Err(bad("type_format_conflict"));
            }
            let mut rule = if action == "mask" {
                row.mask_rule(Some(kind))
            } else {
                row.keep_rule()
            };
            if ctl
                .rules()
                .map_err(internal)?
                .iter()
                .any(|s| s.file.rules.iter().any(|r| r.id == rule.id))
            {
                rule.id = format!("review-{row_id}-{}", random_id().map_err(internal)?);
            }
            ctl.add_rule(scope, rule.clone()).map_err(internal)?;
            decision = Some(
                json!({"action":action,"kind":kind,"scope":scope,"rule_id":rule.id,"rule":rule,"created":now_ms()}),
            );
            if action == "mask" { "masked" } else { "kept" }
        }
        "rollback" => {
            let saved = read(st, &format!("review-decision:{key}"))
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
        learning::transaction(v, || {
            v.set_journal_status(row_id, status)?;
            if let Some(decision) = decision {
                v.put_app_record(
                    &format!("review-decision:{key}"),
                    "review-decision",
                    &serde_json::to_vec(&decision)?,
                )?;
                if automatic {
                    learning::count_applied_rule(v)?;
                }
            }
            v.delete_app_record(&format!("review-deferred:{key}"))?;
            Ok(())
        })
    })
    .map_err(internal)?;
    if matches!(action, "mask" | "keep" | "rollback") {
        st.replace_engine(ctl.build_engine().map_err(internal)?);
    }
    Ok(Json(json!({"ok":true,"state":status})))
}

#[cfg(test)]
mod proposal_tests {
    use super::*;

    #[test]
    fn a_git_revision_requires_an_exact_file_url_before_redacted_review() {
        let digest = "a7acf7442f68a01bff5fbbb028ad357ac88dfb49";
        let value = format!("py?ref={digest}");
        let url = format!("https://code.example/repo/read.py?ref={digest}");
        assert_eq!(git_revision_in_url(&value, &url, None), Some(digest));
        assert_eq!(git_revision_in_url(&value, &value, None), None);
        assert_eq!(git_revision_in_url(&value, &format!("{url}f"), None), None);
        let two = format!("{url} {url}");
        assert_eq!(git_revision_in_url(&value, &two, None), None);
        let start = two.rfind(&value).unwrap();
        assert_eq!(
            git_revision_in_url(&value, &two, Some([start, start + value.len()])),
            Some(digest)
        );
        assert_eq!(git_revision_in_url(&value, &url, Some([1, 2])), None);
    }

    #[test]
    fn a_saved_path_proposal_gets_a_target_bound_action_and_preserves_old_declines() {
        let mut row = json!({"key":"learning:fixture","origin":"learning","state":"pending","value":"/home/alice/bin/tool",
            "kind":"PATH","scope":"team","categorization_state":"succeeded","classification_action":"mask"});
        let old_id = model_proposal(&row).unwrap()["id"].clone();
        attach_proposal(&mut row, 1, None, Some(old_id.clone()));
        assert_ne!(row["proposal"]["id"], old_id);
        assert_eq!(row["proposal"]["declined"], true);
        assert_eq!(row["account_directory"], "/home/alice/");
        let new_id = row["proposal"]["id"].clone();
        attach_proposal(&mut row, 1, None, Some(new_id.clone()));
        assert_eq!(row["proposal"]["declined"], true);
        assert_eq!(row["proposal"]["id"], new_id);
    }

    #[test]
    fn privacy_only_review_checks_incompatible_inherited_types_with_the_model() {
        let d = json!({"scenario":"D"});
        for (value, kind, recheck) in [
            ("40%", "PERSON", true),
            ("20 рабочих дней", "PERSON", true),
            ("1 317 079,97 ₽", "SVC", true),
            ("INC-2026-047", "ID", false),
            ("40%", "AMOUNT", false),
            ("opaque", "LEGACY_KIND", true),
        ] {
            let row = json!({"value":value,"kind":kind});
            assert_eq!(needs_type_check(&d, Some(&row)), recheck);
            assert!(!needs_type_check(
                &json!({"scenario":"standard"}),
                Some(&row)
            ));
        }
        let row = json!({"value":"project", "kind":"PROJ",
            "recommendation":{"state":"needs_review","reason":"type_format_conflict"}});
        assert!(needs_type_check(
            &json!({"provider":"api","scenario":"D"}),
            Some(&row)
        ));
    }

    #[test]
    fn grouping_preserves_disagreeing_proposals_and_protected_targets() {
        let item = |key, action, protected| {
            json!({"key":key, "value":"atlas","protected":protected,
            "proposal":{"action":action,"kind":"PROJ","scope":"team","declined":false}})
        };
        let rows = vec![
            item("a", "mask", false),
            item("b", "keep", true),
            item("c", "keep", false),
        ];
        let groups = group_values(rows);
        assert_eq!(groups.len(), 1);
        let group = &groups[0];
        assert_eq!(group["proposal_conflict"], true);
        assert!(group["proposal"].is_null());
        assert_eq!(group["key"], "b");
        let saved = SavedSelection::from_items(&groups);
        assert_eq!(saved.keys, ["b"]);
        assert_eq!(saved.expanded(&HashSet::new()), ["b", "a", "c"]);
        assert!(saved.expanded(&saved.keys.iter().collect()).is_empty());
    }
    #[test]
    fn only_explicit_current_verdicts_become_proposals() {
        let mut item = json!({"key":"learning:example", "origin":"learning", "value":"atlas",
            "kind":"PATH", "scope":"team", "job_id":"first", "classifier":"fixture",
            "categorization_state":"succeeded", "classification_confidence":0.95});
        assert!(model_proposal(&item).is_none());
        item["classification_action"] = json!("mask");
        let first = model_proposal(&item).unwrap();
        assert_eq!(first["action"], "mask");
        assert_eq!(first["source"], "categorization");
        item["scenario_result"] = json!({"verdict":"keep","reason":"public"});
        assert_eq!(model_proposal(&item).unwrap()["action"], "keep");
        item["scenario_result"]["verdict"] = json!("review");
        assert!(model_proposal(&item).is_none());
        item["recommendation"] = json!({"state":"succeeded","action":"mask","job_id":"second","kind":"PATH","scope":"team"});
        let second = model_proposal(&item).unwrap();
        assert_ne!(first["id"], second["id"]);
        assert_eq!(second["id"], model_proposal(&item).unwrap()["id"]);
        for state in ["stale", "needs_review", "failed"] {
            item["recommendation"]["state"] = json!(state);
            assert!(model_proposal(&item).is_none());
        }
    }
}
