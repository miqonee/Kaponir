//! Per-call counters only. Never retain prompts, generated text, or credentials here.
use super::*;
use std::{future::Future, sync::Mutex};

#[derive(Default)]
struct Calls {
    active: BTreeMap<String, Value>,
    last: Option<Value>,
}
type SharedCalls = Arc<Mutex<Calls>>;
tokio::task_local! {static CURRENT: SharedCalls;}
#[derive(Default)]
pub(crate) struct Activity(Mutex<BTreeMap<String, SharedCalls>>);
struct Registration<'a>(&'a Activity, String, SharedCalls);
impl Drop for Registration<'_> {
    fn drop(&mut self) {
        let mut jobs = self.0.0.lock().unwrap();
        if jobs
            .get(&self.1)
            .is_some_and(|current| Arc::ptr_eq(current, &self.2))
        {
            jobs.remove(&self.1);
        }
    }
}
impl Activity {
    pub(crate) async fn track<T>(
        &self,
        id: &str,
        work: impl Future<Output = T>,
    ) -> (T, Option<Value>) {
        let calls = SharedCalls::default();
        self.0.lock().unwrap().insert(id.to_owned(), calls.clone());
        let _guard = Registration(self, id.to_owned(), calls.clone());
        let outcome = CURRENT.scope(calls.clone(), work).await;
        let last = calls.lock().unwrap().last.clone();
        (outcome, last)
    }
    pub(crate) fn snapshot(&self, worker: &Value) -> BTreeMap<String, Value> {
        let now = now_ms();
        self.0
            .lock()
            .unwrap()
            .iter()
            .map(|(id, calls)| {
                let c = calls.lock().unwrap();
                let active: Vec<_> = c
                    .active
                    .iter()
                    .map(|(tag, reading)| {
                        let mut value = reading.clone();
                        value["elapsed_ms"] =
                            json!(now.saturating_sub(value["started"].as_u64().unwrap_or(now)));
                        if let Some(m) = worker.get(tag) {
                            for key in ["completion_tokens", "prompt_tokens", "tokens_per_second"] {
                                if m[key].as_f64().is_some_and(|n| n >= 0.0) {
                                    value[key] = m[key].clone();
                                }
                            }
                            if matches!(
                                m["phase"].as_str(),
                                Some("preparing" | "waiting" | "generating")
                            ) {
                                value["phase"] = m["phase"].clone();
                            }
                        }
                        value
                    })
                    .collect();
                (id.clone(), json!({"calls":active,"last":c.last}))
            })
            .collect()
    }
}
pub(super) struct Call {
    calls: SharedCalls,
    pub tag: String,
}
impl Call {
    pub fn start(phase: &str) -> Option<Self> {
        let calls = CURRENT.try_with(Clone::clone).ok()?;
        let tag = crate::review::random_id().ok()?;
        calls
            .lock()
            .unwrap()
            .active
            .insert(tag.clone(), json!({"started":now_ms(),"phase":phase}));
        Some(Self { calls, tag })
    }
    pub fn set(&self, patch: Value) {
        if let Some(row) = self.calls.lock().unwrap().active.get_mut(&self.tag) {
            row.as_object_mut()
                .unwrap()
                .extend(patch.as_object().unwrap().clone());
        }
    }
    pub fn finish(&self, telemetry: &Value) {
        let mut c = self.calls.lock().unwrap();
        if let Some(mut row) = c.active.remove(&self.tag) {
            let seconds = telemetry["seconds"].as_f64().unwrap_or_else(|| {
                now_ms().saturating_sub(row["started"].as_u64().unwrap_or_default()) as f64 / 1000.0
            });
            row["elapsed_ms"] = json!((seconds * 1000.) as u64);
            if let Some(tokens) = telemetry["completion_tokens"].as_u64() {
                row["completion_tokens"] = json!(tokens);
                if seconds > 0.0 {
                    row["average_tokens_per_second"] = json!(tokens as f64 / seconds);
                }
            }
            if telemetry["tokens_per_second"].is_number() {
                row["tokens_per_second"] = telemetry["tokens_per_second"].clone();
            }
            c.last = Some(row);
        }
    }
}
impl Drop for Call {
    fn drop(&mut self) {
        self.calls.lock().unwrap().active.remove(&self.tag);
    }
}

pub(super) async fn read_reply(
    response: reqwest::Response,
    meter: Option<&Call>,
) -> anyhow::Result<Value> {
    use futures_util::StreamExt;
    let streaming = response.status().is_success()
        && response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|s| s.starts_with("text/event-stream"));
    let mut stream = response.bytes_stream();
    if !streaming {
        let mut buf = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            anyhow::ensure!(
                buf.len() + chunk.len() <= 2 * 1024 * 1024,
                "model_response_too_large"
            );
            buf.extend_from_slice(&chunk);
        }
        return Ok(serde_json::from_slice(&buf).unwrap_or(Value::Null));
    }
    let mut parser = crate::sse::SseParser::default();
    let mut size = 0;
    let mut chars = 0;
    let mut text = String::new();
    let mut usage = Value::Null;
    let mut finish = Value::Null;
    let mut timings = Value::Null;
    let mut first_count = None;
    let mut consume = |event: crate::sse::Event| -> anyhow::Result<()> {
        if event.data.is_empty() || event.data.trim() == "[DONE]" {
            return Ok(());
        }
        let chunk: Value =
            serde_json::from_str(&event.data).map_err(|_| anyhow::anyhow!("invalid_model_json"))?;
        anyhow::ensure!(chunk["error"].is_null(), "model_stream_interrupted");
        if !chunk["usage"].is_null() {
            usage = chunk["usage"].clone();
        }
        if !chunk["timings"].is_null() {
            timings = chunk["timings"].clone();
        }
        let choice = &chunk["choices"][0];
        if !choice["finish_reason"].is_null() {
            finish = choice["finish_reason"].clone();
        }
        let content = choice["delta"]["content"].as_str().unwrap_or_default();
        chars += content.chars().count()
            + choice["delta"]["reasoning_content"]
                .as_str()
                .map_or(0, |s| s.chars().count());
        text.push_str(content);
        anyhow::ensure!(text.len() <= 2 * 1024 * 1024, "model_response_too_large");
        if let Some(call) = meter
            && chars > 0
        {
            let tokens = usage["completion_tokens"]
                .as_u64()
                .or_else(|| timings["predicted_n"].as_u64());
            let mut rate = timings["predicted_per_second"]
                .as_f64()
                .filter(|n| *n > 0.0);
            if let Some(count) = tokens {
                let (initial, started) =
                    first_count.get_or_insert_with(|| (count, std::time::Instant::now()));
                let seconds = started.elapsed().as_secs_f64();
                if rate.is_none() && seconds >= 0.3 && count > *initial {
                    rate = Some((count - *initial) as f64 / seconds);
                }
            }
            call.set(json!({"phase":"generating","characters":chars,"completion_tokens":tokens,"tokens_per_second":rate}));
        }
        Ok(())
    };
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        size += chunk.len();
        anyhow::ensure!(size <= 16 * 1024 * 1024, "model_response_too_large");
        for event in parser.push(&chunk) {
            consume(event)?;
        }
    }
    if let Some(event) = parser.finish() {
        consume(event)?;
    }
    Ok(
        json!({"choices":[{"message":{"content":text},"finish_reason":finish}],"usage":usage,"timings":timings}),
    )
}
