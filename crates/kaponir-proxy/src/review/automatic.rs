use super::*;
use kaponir_vault::SqliteVault;
use std::{sync::Arc, time::Duration};

pub(super) fn start(st: &Shared) {
    if st.control.is_none() {
        return;
    }
    let weak = Arc::downgrade(st);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_millis(250)).await;
            let Some(st) = weak.upgrade() else {
                break;
            };
            if !*st.learning.automatic_review_signal.borrow() {
                continue;
            }
            let settings = st.learning.snapshot();
            if settings.analysis.provider == "local"
                && matches!(
                    st.learning.queue_wait(),
                    Some(
                        learning::QueueWait::WorkerOffline
                            | learning::QueueWait::WorkerPaused
                            | learning::QueueWait::DetectionModel
                    )
                )
            {
                continue;
            }
            if let Ok(Some((job, targets))) = next_job(&st).await {
                run(st, job, targets).await;
            }
        }
    });
}

pub(super) fn finding(v: &SqliteVault, key: &str) -> anyhow::Result<Option<Value>> {
    match key.split_once(':') {
        Some((origin, id)) => raw_finding(v, origin, id),
        None => Ok(None),
    }
}

async fn next_job(st: &Shared) -> anyhow::Result<Option<(Value, Vec<Target>)>> {
    let _settings = st.learning_changes.lock().await;
    let settings = st.learning.snapshot();
    if !settings.analysis.enabled
        || !settings.analysis.auto_review
        || settings.analysis.provider == "none"
    {
        return Ok(None);
    }
    let keys = st.with_vault(|_, v| Ok(v.pending_finding_reviews(32)?))?;
    if keys.is_empty() {
        return Ok(None);
    }
    let source = learning::automatic_review_source(&settings)?;
    let _rules = st.rule_changes.lock().await;
    let rules = active_rules(st)?;
    st.with_vault(|_, v| {
        learning::transaction(v, || {
            let mut targets = Vec::new();
            for key in &keys {
                if let Some(mut item) = finding(v, key)? {
                    rules.project(&mut item);
                    if needs_decision(&item) && item["protected"] != true {
                        targets.push(Target {
                            key: key.clone(),
                            fingerprint: fingerprint(&item),
                        });
                    }
                }
            }
            let job = if targets.is_empty() {
                None
            } else {
                let job = json!({
                    "id":random_id()?,"state":"queued","created":now_ms(),"updated":now_ms(),
                    "revision":source["revision"],"source":source,"automatic":true,"applied":0,
                    "total":targets.len(),"skipped":keys.len() - targets.len(),"processed":0,
                    "completed":0,"failed":0,"manual":0,"active":0,"error":""
                });
                store_queued_job(v, &job, &targets)?;
                Some((job, targets))
            };
            v.dequeue_finding_reviews(&keys)?;
            Ok(job)
        })
    })
}

pub(super) fn apply_mask(
    st: &Shared,
    key: &str,
    rec: &Value,
    threshold: f64,
) -> anyhow::Result<bool> {
    if rec["state"] != "succeeded"
        || rec["action"] != "mask"
        || rec["redacted_git_revision"] == true
        || !rec["confidence"]
            .as_f64()
            .is_some_and(|score| score > threshold)
    {
        return Ok(false);
    }
    let Some((origin, id)) = key.split_once(':') else {
        return Ok(false);
    };
    let Some(item) = action_item(st, origin, id)? else {
        return Ok(false);
    };
    if !needs_decision(&item)
        || rec["fingerprint"] != fingerprint(&item)
        || item["protected"] == true
        || item["proposal"]["declined"] == true
        || !item["unmatched_rule"].is_null()
        || item["reason"] == "analysis_partial"
        || matches!(
            item["validation_reason"].as_str(),
            Some("numeric_type_corrected" | "type_format_conflict" | "type_rechecked")
        )
        || item["extraction_confidence"]
            .as_f64()
            .is_some_and(|score| score <= threshold)
    {
        return Ok(false);
    }

    let kind = rec["kind"].as_str().unwrap_or_default();
    let value = item["value"].as_str().unwrap_or_default();
    let context = item["context"].as_str().unwrap_or_default();
    if !learning::verified_mask_candidate(context, value, kind) {
        return Ok(false);
    }
    let span: Option<[usize; 2]> = serde_json::from_value(item["context_span"].clone())?;
    let matches_source = match span {
        Some([start, end]) => context.get(start..end) == Some(value),
        None => context.match_indices(value).take(2).count() == 1,
    };
    if !matches_source {
        return Ok(false);
    }
    let held = st.with_vault(|_, v| {
        Ok(learning::rejected_value(v, value)?
            || v.app_record(&format!("review-deferred:{key}"))?.is_some())
    })?;
    if held {
        return Ok(false);
    }

    let scope = serde_json::from_value(rec["scope"].clone())?;
    let Json(result) = apply_effect(st, &item, "mask", kind, scope, false, true)
        .map_err(|_| anyhow::anyhow!("automatic_rule_failed"))?;
    Ok(matches!(
        result["state"].as_str(),
        Some("active" | "masked")
    ))
}
