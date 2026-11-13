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
use kaponir_core::Scope;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

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
    let rows = st.with_vault(|_, v| Ok(v.journal(None, 2000)?))?;
    let mut all = Vec::new();
    for row in rows {
        let mut item = serde_json::to_value(&row)?;
        item["key"] = json!(format!("detector:{}", row.id));
        item["origin"] = json!("detector");
        item["id"] = json!(row.id.to_string());
        item["state"] = json!(row.status);
        item["updated"] = json!(row.last_seen.saturating_mul(1000));
        item["protected"] = json!(row.category == "credential");
        all.push(item);
    }
    for mut entry in learning::review_entries(st)? {
        entry["key"] = json!(format!(
            "learning:{}",
            entry["id"].as_str().unwrap_or_default()
        ));
        entry["origin"] = json!("learning");
        entry["context"] = entry["quote"].clone();
        entry["protected"] = json!(entry["kind"] == "SECRET");
        all.push(entry);
    }
    let revision = st.learning.snapshot().revision;
    for item in &mut all {
        let key = item["key"].as_str().unwrap().to_string();
        if let Some(mut rec) = read(st, &format!("review-rec:{key}"))? {
            if rec["revision"].as_u64() != Some(revision) || rec["fingerprint"] != fingerprint(item)
            {
                rec["state"] = json!("stale");
            }
            item["recommendation"] = rec;
        }
        if let Some(decision) = read(st, &format!("review-decision:{key}"))? {
            item["decision"] = decision;
        }
    }
    Ok(all)
}
pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/api/v1/ui/review", get(list))
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
        ] {
            for (id, bytes) in v.app_records(kind, 4000)? {
                let record: Value = serde_json::from_slice(&bytes)?;
                if now_ms().saturating_sub(record["created"].as_u64().unwrap_or(0))
                    > 30 * 24 * 60 * 60 * 1000
                {
                    v.delete_app_record(&id)?;
                }
            }
        }
        Ok(())
    });
}
pub(crate) fn start(st: &Shared) {
    cleanup(st);
    // A restart must not silently send old work to another connection.
    let _ = st.with_vault(|_, v| {
        for state in ["queued", "running"] {
            for (id, bytes) in v.app_records(&format!("review-job-{state}"), 100)? {
                let mut job: Value = serde_json::from_slice(&bytes)?;
                job["state"] = json!("failed");
                job["error"] = json!("interrupted_review");
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
#[derive(Default, Deserialize)]
struct Filters {
    status: Option<String>,
    source: Option<String>,
    q: Option<String>,
    kind: Option<String>,
    action: Option<String>,
    sort: Option<String>,
    keys: Option<String>,
    offset: Option<usize>,
    limit: Option<usize>,
}
async fn list(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(q): Query<Filters>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let all = items(&st).map_err(internal)?;
    let pending = all
        .iter()
        .filter(|i| matches!(i["state"].as_str(), Some("open" | "pending")))
        .count();
    let mut selected: Vec<_> = all
        .into_iter()
        .filter(|item| {
            let is_pending = matches!(item["state"].as_str(), Some("open" | "pending"));
            (q.status.as_deref() == Some("all")
                || if q.status.as_deref() == Some("history") {
                    !is_pending
                } else {
                    is_pending
                })
                && q.source
                    .as_deref()
                    .is_none_or(|s| s == "all" || item["origin"] == s)
                && q.kind
                    .as_deref()
                    .is_none_or(|s| s.is_empty() || item["kind"] == s)
                && q.action
                    .as_deref()
                    .is_none_or(|s| s.is_empty() || item["applied"] == s)
                && q.keys
                    .as_deref()
                    .is_none_or(|s| s.split(',').any(|key| item["key"] == key))
                && q.q.as_ref().is_none_or(|s| {
                    format!(
                        "{} {}",
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
    let total = selected.len();
    let offset = q.offset.unwrap_or(0).min(total);
    let limit = q.limit.unwrap_or(50).clamp(1, 100);
    let page = selected
        .into_iter()
        .skip(offset)
        .take(limit)
        .collect::<Vec<_>>();
    let sources = ["categorization", "analysis"].map(|role| {
        learning::review_source(&st, role)
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
        json!({"items":page,"total":total,"pending":pending,"offset":offset,"limit":limit,"sources":sources,"jobs":jobs,"editable":st.control.is_some()}),
    ))
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
#[derive(Deserialize)]
struct Submit {
    keys: Vec<String>,
    #[serde(default = "auto_source")]
    model_source: String,
}
fn auto_source() -> String {
    "auto".into()
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
    if req.keys.is_empty() || req.keys.len() > 64 {
        return Err(bad("select_up_to_64"));
    }
    let source = learning::review_source(&st, &req.model_source)
        .map_err(|e| bad(&learning::safe_error(&e)))?;
    let all = items(&st).map_err(internal)?;
    let mut targets = Vec::new();
    for key in &req.keys {
        let item = all
            .iter()
            .find(|i| i["key"] == *key)
            .ok_or_else(|| bad("entry_not_found"))?;
        if !matches!(item["state"].as_str(), Some("open" | "pending")) {
            return Err(bad("entry_not_pending"));
        }
        if item["protected"] == true {
            return Err(bad("protected_credential"));
        }
        if !targets.iter().any(|target: &Target| target.key == *key) {
            targets.push(Target {
                key: key.clone(),
                fingerprint: fingerprint(item),
            });
        }
    }
    let mut random = [0u8; 16];
    getrandom::fill(&mut random).map_err(|e| internal(anyhow::anyhow!(e)))?;
    let id = blake3::hash(&random).to_hex().to_string();
    let job = json!({"id":id,"state":"queued","created":now_ms(),"revision":source["revision"],"source":source,"targets":targets,"completed":0,"error":""});
    st.with_vault(|_, v| {
        anyhow::ensure!(
            v.app_record_count("review-job-queued")? + v.app_record_count("review-job-running")?
                < 2,
            "review_busy"
        );
        v.put_app_record(
            &format!("review-job:{id}"),
            "review-job-queued",
            &serde_json::to_vec(&job)?,
        )?;
        Ok(())
    })
    .map_err(|e| bad(&learning::safe_error(&e)))?;
    let state = st.clone();
    let job_copy = job.clone();
    tokio::spawn(async move {
        run(state, job_copy).await;
    });
    Ok(Json(job))
}
async fn job(State(st): State<Shared>, headers: HeaderMap, Path(id): Path<String>) -> ResultJson {
    authorized(&st, &headers, true)?;
    if id.len() != 64 || !id.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err(bad("job_not_found"));
    }
    Ok(Json(
        read(&st, &format!("review-job:{id}"))
            .map_err(internal)?
            .ok_or_else(|| bad("job_not_found"))?,
    ))
}
fn schema(ids: &[String]) -> Value {
    let count = ids.len();
    json!({"type":"object","properties":{"decisions":{"type":"array","minItems":count,"maxItems":count,"items":{"type":"object","properties":{
        "id":{"type":"string","enum":ids},"action":{"type":"string","enum":["mask","keep","review"]},"kind":{"type":"string","enum":kaponir_core::kinds::STANDARD},
        "scope":{"type":"string","enum":["personal","team","global"]},"confidence":{"type":"number","minimum":0,"maximum":1},"reason":{"type":"string","minLength":1,"maxLength":400}},
        "required":["id","action","kind","scope","confidence","reason"],"additionalProperties":false}}},"required":["decisions"],"additionalProperties":false})
}
async fn run(st: Shared, mut job: Value) {
    let id = job["id"].as_str().unwrap().to_string();
    job["state"] = json!("running");
    let _ = write(&st, &format!("review-job:{id}"), "review-job-running", &job);
    let result = run_inner(&st, &mut job).await;
    job["state"] = json!(if result.is_ok() {
        "succeeded"
    } else {
        "failed"
    });
    if let Err(e) = result {
        job["error"] = json!(learning::safe_error(&e));
        if let Ok(targets) = serde_json::from_value::<Vec<Target>>(job["targets"].clone()) {
            for target in targets
                .into_iter()
                .skip(job["completed"].as_u64().unwrap_or(0) as usize)
            {
                let _ = write(
                    &st,
                    &format!("review-rec:{}", target.key),
                    "review-recommendation",
                    &json!({"state":"failed","error":job["error"],"fingerprint":target.fingerprint,"revision":job["revision"],"model":job["source"]["model"],"created":now_ms(),"job_id":id}),
                );
            }
        }
    }
    let _ = write(
        &st,
        &format!("review-job:{id}"),
        &format!("review-job-{}", job["state"].as_str().unwrap()),
        &job,
    );
}
async fn run_inner(st: &Shared, job: &mut Value) -> anyhow::Result<()> {
    let targets: Vec<Target> = serde_json::from_value(job["targets"].clone())?;
    let settings = st.learning.snapshot();
    let batch_size = if job["source"]["role"] == "analysis" {
        settings.analysis.inference.batch_size
    } else {
        settings.categorization.inference.batch_size
    };
    for batch in targets.chunks(batch_size) {
        anyhow::ensure!(
            job["revision"].as_u64() == Some(st.learning.snapshot().revision),
            "configuration_changed"
        );
        let current = items(st)?;
        let mut input = Vec::new();
        for target in batch {
            let item = current
                .iter()
                .find(|i| i["key"] == target.key)
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
            job["source"]["role"].as_str().unwrap(),
            json!(input),
            schema(&batch.iter().map(model_id).collect::<Vec<_>>()),
        )
        .await?;
        anyhow::ensure!(
            job["revision"].as_u64() == Some(st.learning.snapshot().revision),
            "configuration_changed"
        );
        let decisions = reply["decisions"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("invalid_classification_response"))?;
        anyhow::ensure!(
            decisions.len() == batch.len(),
            "invalid_classification_response"
        );
        let mut validated = Vec::new();
        for target in batch {
            let id = model_id(target);
            let found: Vec<_> = decisions.iter().filter(|d| d["id"] == id).collect();
            anyhow::ensure!(found.len() == 1, "invalid_classification_response");
            let d = found[0];
            let kind = d["kind"].as_str().unwrap_or_default();
            anyhow::ensure!(
                kaponir_core::kinds::STANDARD.contains(&kind)
                    && matches!(d["action"].as_str(), Some("mask" | "keep" | "review"))
                    && d["confidence"]
                        .as_f64()
                        .is_some_and(|p| p.is_finite() && (0.0..=1.0).contains(&p))
                    && d["scope"]
                        .as_str()
                        .is_some_and(|s| s.parse::<Scope>().is_ok())
                    && d["reason"]
                        .as_str()
                        .is_some_and(|s| !s.is_empty() && s.len() <= 1600),
                "invalid_classification_response"
            );
            let item = current.iter().find(|i| i["key"] == target.key).unwrap();
            anyhow::ensure!(
                d["action"] == "review"
                    || !kaponir_core::kinds::type_conflict(
                        kind,
                        item["value"].as_str().unwrap_or_default()
                    ),
                "type_format_conflict"
            );
            let mut rec = d.clone();
            rec["state"] = json!(if d["action"] == "review" {
                "needs_review"
            } else {
                "succeeded"
            });
            rec["job_id"] = job["id"].clone();
            rec["revision"] = job["revision"].clone();
            rec["fingerprint"] = json!(target.fingerprint);
            rec["model"] = job["source"]["model"].clone();
            rec["created"] = json!(now_ms());
            validated.push((target.key.clone(), rec));
        }
        for (key, rec) in validated {
            write(
                st,
                &format!("review-rec:{key}"),
                "review-recommendation",
                &rec,
            )?;
        }
        job["completed"] = json!(job["completed"].as_u64().unwrap_or(0) + batch.len() as u64);
        write(
            st,
            &format!("review-job:{}", job["id"].as_str().unwrap()),
            "review-job-running",
            job,
        )?;
    }
    Ok(())
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
        if rec["state"] != "succeeded" {
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
        .with_vault(|_, v| Ok(v.journal(None, 2000)?.into_iter().find(|r| r.id == row_id)))
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
