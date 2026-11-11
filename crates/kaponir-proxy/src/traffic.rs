//! Телеметрия прокси-трафика, поэтапные тайминги (Latency Waterfall), диагностика ошибок
//! и снимки Request Inspector (хранятся в зашифрованном Vault + кольцевом буфере RAM).
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const MAX_INSPECTOR_CHARS: usize = 32_768;
const MAX_ITEMS_PER_EVENT: usize = 128;

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

pub fn cap_str(s: &str, max_bytes: usize) -> String {
    if s.len() <= max_bytes {
        return s.to_string();
    }
    let mut cut = max_bytes;
    while !s.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{}… [truncated]", &s[..cut])
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ReplacementItem {
    pub token: String,
    pub kind: String,
    pub category: String,
    pub original: String,
    #[serde(default)]
    pub verdict: String,
    #[serde(default)]
    pub from_cache: bool,
    #[serde(default)]
    pub recognizer: Option<String>,
    #[serde(default)]
    pub origin: Option<kaponir_core::Origin>,
    #[serde(default)]
    pub evidence: Vec<kaponir_core::Evidence>,
    #[serde(default)]
    pub probability: Option<f32>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct StageTimings {
    /// Парсинг JSON + локальное маскирование входа (микросекунды).
    pub mask_us: u64,
    /// Классификатор / S1 (микросекунды).
    pub classifier_us: u64,
    /// Чистое время ожидания ответа от внешнего провайдера LLM (микросекунды).
    pub upstream_us: u64,
    /// Обратная демаскировка ответа / потока (микросекунды).
    pub restore_us: u64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct EventInspector {
    pub model: String,
    pub original_request: String,
    pub masked_request: String,
    #[serde(default)]
    pub masked_payload_json: String,
    pub raw_response: String,
    pub restored_response: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct EventError {
    pub upstream_url: String,
    pub status: u16,
    pub kind: String,
    pub message: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Usage {
    /// Новые замены в текущем сообщении (не раздуваются кэшем истории).
    pub replacements: u64,
    /// Повторные подстановки из кэша истории диалога.
    #[serde(default)]
    pub cached_replacements: u64,
    /// credentials, identity, infrastructure, financial, contact, other
    pub categories: [u64; 6],
    pub restored: u64,
    pub unknown: u64,
    pub denied: u64,
    #[serde(default)]
    pub uncertain: u64,
    #[serde(default)]
    pub prompt_tokens: u64,
    #[serde(default)]
    pub completion_tokens: u64,
    #[serde(default)]
    pub total_tokens: u64,
    #[serde(default)]
    pub masked_items: Vec<ReplacementItem>,
    #[serde(default)]
    pub unknown_tokens: Vec<String>,
    #[serde(default)]
    pub denied_tokens: Vec<String>,
    #[serde(skip)]
    pub raw_response_buf: String,
    #[serde(skip)]
    pub classifier_us: u64,
    #[serde(skip)]
    pub restored_response_buf: String,
}

impl Usage {
    pub fn add(&mut self, other: &Self) {
        self.classifier_us += other.classifier_us;
        self.replacements += other.replacements;
        self.cached_replacements += other.cached_replacements;
        for (a, b) in self.categories.iter_mut().zip(other.categories) {
            *a += b;
        }
        self.restored += other.restored;
        self.unknown += other.unknown;
        self.denied += other.denied;
        self.uncertain += other.uncertain;
        self.prompt_tokens += other.prompt_tokens;
        self.completion_tokens += other.completion_tokens;
        self.total_tokens += other.total_tokens;
        for item in &other.masked_items {
            if self.masked_items.len() < MAX_ITEMS_PER_EVENT {
                self.masked_items.push(item.clone());
            }
        }
        for tok in &other.unknown_tokens {
            if !self.unknown_tokens.contains(tok) && self.unknown_tokens.len() < 32 {
                self.unknown_tokens.push(tok.clone());
            }
        }
        for tok in &other.denied_tokens {
            if !self.denied_tokens.contains(tok) && self.denied_tokens.len() < 32 {
                self.denied_tokens.push(tok.clone());
            }
        }
        if !other.raw_response_buf.is_empty() && self.raw_response_buf.len() < MAX_INSPECTOR_CHARS {
            let rem = MAX_INSPECTOR_CHARS.saturating_sub(self.raw_response_buf.len());
            self.raw_response_buf
                .push_str(&cap_str(&other.raw_response_buf, rem));
        }
        if !other.restored_response_buf.is_empty()
            && self.restored_response_buf.len() < MAX_INSPECTOR_CHARS
        {
            let rem = MAX_INSPECTOR_CHARS.saturating_sub(self.restored_response_buf.len());
            self.restored_response_buf
                .push_str(&cap_str(&other.restored_response_buf, rem));
        }
    }

    /// Учитывает попадание в кэш истории диалога: не увеличивает счётчик новых скрытых значений `replacements`,
    /// а записывает подстановки в `cached_replacements`.
    pub fn add_cached(&mut self, cached: &Self) {
        self.cached_replacements += cached.replacements + cached.cached_replacements;
        for item in &cached.masked_items {
            if self.masked_items.len() < MAX_ITEMS_PER_EVENT {
                let mut cached_item = item.clone();
                cached_item.from_cache = true;
                self.masked_items.push(cached_item);
            }
        }
    }

    pub fn masked(m: &kaponir_core::Masked) -> Self {
        let mut usage = Self {
            uncertain: m.uncertain as u64,
            classifier_us: m.classifier_us,
            ..Self::default()
        };
        for replacement in &m.replacements {
            let (group, cat_str) = match replacement.category {
                kaponir_core::Category::Credential => (0, "credential"),
                kaponir_core::Category::Identity => (1, "identity"),
                kaponir_core::Category::Infra | kaponir_core::Category::Network => (2, "infra"),
                kaponir_core::Category::Financial => (3, "financial"),
                kaponir_core::Category::Contact => (4, "contact"),
                kaponir_core::Category::Custom => (5, "custom"),
            };
            usage.categories[group] += 1;
            usage.replacements += 1;
            if usage.masked_items.len() < MAX_ITEMS_PER_EVENT {
                usage.masked_items.push(ReplacementItem {
                    token: replacement.token.clone(),
                    kind: replacement.kind.clone(),
                    category: cat_str.into(),
                    original: replacement.original.clone(),
                    verdict: format!("{:?}", replacement.verdict).to_lowercase(),
                    from_cache: false,
                    recognizer: Some(replacement.recognizer.clone()),
                    origin: Some(replacement.origin.clone()),
                    evidence: replacement.evidence.clone(),
                    probability: Some(replacement.probability),
                });
            }
        }
        usage
    }

    pub fn restored(r: &kaponir_core::Restored) -> Self {
        let mut unknown_tokens = Vec::new();
        for u in &r.unknown {
            if !unknown_tokens.contains(u) && unknown_tokens.len() < 32 {
                unknown_tokens.push(u.clone());
            }
        }
        let mut denied_tokens = Vec::new();
        for d in &r.denied {
            if !denied_tokens.contains(d) && denied_tokens.len() < 32 {
                denied_tokens.push(d.clone());
            }
        }
        Self {
            restored: r.restored as u64,
            unknown: r.unknown.len() as u64,
            denied: r.denied.len() as u64,
            unknown_tokens,
            denied_tokens,
            ..Self::default()
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub id: u64,
    pub time_ms: u64,
    #[serde(default)]
    pub started_at_ms: u64,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub session_source: Option<String>,
    #[serde(default)]
    pub streaming: bool,
    #[serde(default)]
    pub request_kind: String,
    #[serde(default)]
    pub attempts: Vec<String>,
    pub provider: String,
    pub connection_id: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub upstream_url: Option<String>,
    pub status: u16,
    pub outcome: String,
    pub duration_ms: u64,
    pub sent: bool,
    pub usage: Usage,
    #[serde(default)]
    pub timings: StageTimings,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<EventError>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inspector: Option<EventInspector>,
}

#[derive(Default, Clone, Debug, Serialize, Deserialize)]
pub struct Bucket {
    pub hour: u64,
    pub requests: u64,
    pub errors: u64,
    pub blocked: u64,
    pub cancelled: u64,
    pub usage: Usage,
    #[serde(default)]
    pub timings_sum: StageTimings,
}

#[derive(Default, Clone, Serialize, Deserialize)]
pub struct Archive {
    pub last_id: u64,
    pub hours: BTreeMap<u64, Bucket>,
    pub total: Bucket,
}

type EventPersister = Arc<dyn Fn(&Event, &Archive, Option<&serde_json::Value>) + Send + Sync>;

#[derive(Default)]
struct History {
    archive: Archive,
    events: VecDeque<Event>,
    active: BTreeMap<u64, Weak<Span>>,
}

#[derive(Default)]
pub struct Traffic {
    history: Mutex<History>,
    persister: Mutex<Option<EventPersister>>,
    next: AtomicU64,
    in_flight: AtomicU64,
}

#[derive(Serialize)]
pub struct Snapshot {
    pub in_flight: u64,
    pub total: Bucket,
    pub hours: Vec<Bucket>,
    pub events: Vec<Event>,
    pub active: Vec<Event>,
}

fn add_to_bucket(bucket: &mut Bucket, event: &Event) {
    bucket.requests += 1;
    bucket.errors += u64::from(event.outcome == "error" || event.outcome == "restore_error");
    bucket.blocked += u64::from(!event.sent);
    bucket.cancelled += u64::from(event.outcome == "cancelled");
    bucket.usage.add(&event.usage);
    bucket.usage.masked_items.clear();
    bucket.usage.unknown_tokens.clear();
    bucket.usage.denied_tokens.clear();
    bucket.usage.raw_response_buf.clear();
    bucket.usage.restored_response_buf.clear();
    bucket.timings_sum.mask_us += event.timings.mask_us;
    bucket.timings_sum.classifier_us += event.timings.classifier_us;
    bucket.timings_sum.upstream_us += event.timings.upstream_us;
    bucket.timings_sum.restore_us += event.timings.restore_us;
}
fn accumulate(archive: &mut Archive, event: &Event) {
    archive.last_id = archive.last_id.max(event.id);
    let hour = event.time_ms / 3_600_000;
    let bucket = archive.hours.entry(hour).or_insert_with(|| Bucket {
        hour,
        ..Default::default()
    });
    add_to_bucket(bucket, event);
    add_to_bucket(&mut archive.total, event);
    archive
        .hours
        .retain(|hour, _| *hour + 168 > now_ms() / 3_600_000);
}

impl Traffic {
    pub fn set_persister(&self, p: EventPersister) {
        *self
            .persister
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(p);
    }

    pub fn restore_events(&self, events: Vec<Event>) {
        let mut h = self
            .history
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        for event in events {
            accumulate(&mut h.archive, &event);
            if h.events.len() == 200 {
                h.events.pop_front();
            }
            h.events.push_back(event);
        }
        self.next.fetch_max(h.archive.last_id, Relaxed);
    }
    pub fn restore_archive(&self, mut archive: Archive) {
        archive
            .hours
            .retain(|hour, _| *hour + 168 > now_ms() / 3_600_000);
        self.next.fetch_max(archive.last_id, Relaxed);
        self.history
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .archive = archive;
    }

    fn record(&self, event: Event, messages: Option<serde_json::Value>) {
        // Serialize persistence with recording so concurrent completions cannot
        // overwrite a newer aggregate with an older snapshot.
        let persister = self
            .persister
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let archive = {
            let mut h = self
                .history
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            h.active.remove(&event.id);
            accumulate(&mut h.archive, &event);
            if h.events.len() == 200 {
                h.events.pop_front();
            }
            h.events.push_back(event.clone());
            h.archive.clone()
        };
        if let Some(p) = &*persister {
            p(&event, &archive, messages.as_ref());
        }
    }

    pub fn snapshot(&self) -> Snapshot {
        let (mut snap, active) = {
            let h = self
                .history
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            (
                Snapshot {
                    in_flight: self.in_flight.load(Relaxed),
                    total: h.archive.total.clone(),
                    hours: h.archive.hours.values().cloned().collect(),
                    events: h.events.iter().rev().cloned().collect(),
                    active: Vec::new(),
                },
                h.active.values().cloned().collect::<Vec<_>>(),
            )
        };
        // Never drop the last strong Span reference while holding History's lock.
        snap.active = active
            .into_iter()
            .filter_map(|s| s.upgrade())
            .map(|s| s.event(false))
            .collect();
        snap
    }

    /// Dashboard/Prometheus never need message bodies or replacement originals.
    pub fn summary_snapshot(&self) -> Snapshot {
        let mut snapshot = self.snapshot();
        for event in snapshot.events.iter_mut().chain(snapshot.active.iter_mut()) {
            event.inspector = None;
            event.usage.masked_items.clear();
            event.usage.unknown_tokens.clear();
            event.usage.denied_tokens.clear();
            event.upstream_url = None;
            if let Some(error) = &mut event.error {
                error.message.clear();
                error.upstream_url.clear();
            }
        }
        snapshot
    }

    pub fn event_by_id(&self, id: u64) -> Option<Event> {
        let snap = self.snapshot();
        snap.events
            .into_iter()
            .chain(snap.active)
            .find(|e| e.id == id)
    }
}

pub struct Span {
    history: Arc<Traffic>,
    pub usage: Mutex<Usage>,
    pub connection_id: Mutex<Option<String>>,
    pub model: Mutex<Option<String>>,
    pub upstream_url: Mutex<Option<String>>,
    pub session: Mutex<Option<(String, String)>>,
    pub request_kind: Mutex<String>,
    pub attempts: Mutex<Vec<String>>,
    pub mask_us: AtomicU64,
    pub classifier_us: AtomicU64,
    pub upstream_us: AtomicU64,
    pub restore_us: AtomicU64,
    pub error: Mutex<Option<EventError>>,
    pub inspector: Mutex<Option<EventInspector>>,
    pub learning_input: Mutex<Option<serde_json::Value>>,
    pub status: AtomicU16,
    pub sent: AtomicBool,
    pub streaming: AtomicBool,
    pub complete: AtomicBool,
    pub failed: AtomicBool,
    pub restore_failed: AtomicBool,
    provider: &'static str,
    id: u64,
    started_at_ms: u64,
    start: Instant,
}

impl Span {
    pub fn new(history: Arc<Traffic>, provider: &'static str) -> Arc<Self> {
        history.in_flight.fetch_add(1, Relaxed);
        let id = history.next.fetch_add(1, Relaxed) + 1;
        let span = Arc::new(Self {
            history: history.clone(),
            provider,
            id,
            start: Instant::now(),
            started_at_ms: now_ms(),
            usage: Default::default(),
            connection_id: Default::default(),
            model: Default::default(),
            upstream_url: Default::default(),
            session: Default::default(),
            request_kind: Default::default(),
            attempts: Default::default(),
            mask_us: AtomicU64::new(0),
            classifier_us: AtomicU64::new(0),
            upstream_us: AtomicU64::new(0),
            restore_us: AtomicU64::new(0),
            error: Default::default(),
            inspector: Default::default(),
            learning_input: Default::default(),
            status: AtomicU16::new(0),
            sent: AtomicBool::new(false),
            streaming: AtomicBool::new(false),
            complete: AtomicBool::new(false),
            failed: AtomicBool::new(false),
            restore_failed: AtomicBool::new(false),
        });
        history
            .history
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .active
            .insert(id, Arc::downgrade(&span));
        span
    }
    pub fn add(&self, usage: &Usage) {
        let mut current = self
            .usage
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let prompt = current.prompt_tokens.max(usage.prompt_tokens);
        let completion = current.completion_tokens.max(usage.completion_tokens);
        let total = current
            .total_tokens
            .max(usage.total_tokens)
            .max(prompt + completion);
        current.add(usage);
        current.prompt_tokens = prompt;
        current.completion_tokens = completion;
        current.total_tokens = total;
    }
    pub fn finish(&self) {
        self.complete.store(true, Relaxed);
    }
    pub fn set_error(
        &self,
        upstream_url: impl Into<String>,
        status: Option<u16>,
        kind: impl Into<String>,
        message: impl Into<String>,
    ) {
        self.failed.store(true, Relaxed);
        *self
            .error
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(EventError {
            upstream_url: upstream_url.into(),
            status: status.unwrap_or(0),
            kind: kind.into(),
            message: cap_str(&message.into(), 1024),
        });
    }
    fn event(&self, final_event: bool) -> Event {
        let status = self.status.load(Relaxed);
        let outcome = if !final_event {
            "in_progress"
        } else if self.failed.load(Relaxed) || status >= 400 {
            "error"
        } else if !self.complete.load(Relaxed) {
            "cancelled"
        } else if self.restore_failed.load(Relaxed) {
            "restore_error"
        } else {
            "ok"
        };
        let usage = self
            .usage
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone();
        let mut inspector = self
            .inspector
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone();
        if let Some(insp) = &mut inspector {
            if insp.raw_response.is_empty() {
                insp.raw_response = usage.raw_response_buf.clone();
            }
            if insp.restored_response.is_empty() {
                insp.restored_response = usage.restored_response_buf.clone();
            }
        }
        let session = self
            .session
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone();
        Event {
            id: self.id,
            time_ms: now_ms(),
            started_at_ms: self.started_at_ms,
            session_id: session.as_ref().map(|s| s.0.clone()),
            session_source: session.map(|s| s.1),
            streaming: self.streaming.load(Relaxed),
            request_kind: self
                .request_kind
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            attempts: self
                .attempts
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            provider: self.provider.to_string(),
            connection_id: self
                .connection_id
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            model: self
                .model
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            upstream_url: self
                .upstream_url
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            status,
            outcome: outcome.to_string(),
            sent: self.sent.load(Relaxed),
            duration_ms: self.start.elapsed().as_millis() as u64,
            usage,
            timings: StageTimings {
                mask_us: self.mask_us.load(Relaxed),
                classifier_us: self.classifier_us.load(Relaxed),
                upstream_us: self.upstream_us.load(Relaxed),
                restore_us: self.restore_us.load(Relaxed),
            },
            error: self
                .error
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            inspector,
        }
    }
}
impl Drop for Span {
    fn drop(&mut self) {
        self.history.record(
            self.event(true),
            self.learning_input
                .get_mut()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take(),
        );
        self.history.in_flight.fetch_sub(1, Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stream_has_one_record_and_cumulative_usage_is_not_added_twice() {
        let traffic = Arc::new(Traffic::default());
        let span = Span::new(traffic.clone(), "openai");
        span.streaming.store(true, Relaxed);
        span.sent.store(true, Relaxed);
        span.status.store(200, Relaxed);
        for output in [1, 2, 5, 5] {
            span.add(&Usage {
                prompt_tokens: 10,
                completion_tokens: output,
                total_tokens: 10 + output,
                ..Default::default()
            });
        }
        assert_eq!(traffic.snapshot().active.len(), 1);
        assert_eq!(traffic.snapshot().total.requests, 0);
        span.finish();
        span.finish();
        drop(span);
        let snapshot = traffic.snapshot();
        assert!(snapshot.active.is_empty());
        assert_eq!(snapshot.in_flight, 0);
        assert_eq!(snapshot.events.len(), 1);
        assert_eq!(snapshot.total.requests, 1);
        assert_eq!(snapshot.total.usage.total_tokens, 15);
        assert_eq!(snapshot.events[0].outcome, "ok");
    }

    #[test]
    fn dashboard_is_metadata_only_and_inspector_retains_details() {
        let traffic = Arc::new(Traffic::default());
        let span = Span::new(traffic.clone(), "anthropic");
        *span.inspector.lock().unwrap() = Some(EventInspector {
            original_request: "private-value".into(),
            ..Default::default()
        });
        span.add(&Usage {
            masked_items: vec![ReplacementItem {
                original: "private-value".into(),
                ..Default::default()
            }],
            ..Default::default()
        });
        let id = span.id;
        assert!(
            !serde_json::to_string(&traffic.summary_snapshot())
                .unwrap()
                .contains("private-value")
        );
        span.finish();
        drop(span);
        assert!(
            !serde_json::to_string(&traffic.summary_snapshot())
                .unwrap()
                .contains("private-value")
        );
        assert!(
            traffic
                .event_by_id(id)
                .unwrap()
                .inspector
                .unwrap()
                .original_request
                .contains("private-value")
        );
    }

    #[test]
    fn rolling_aggregates_survive_detail_rotation_and_restart_without_recounting() {
        let traffic = Arc::new(Traffic::default());
        for _ in 0..205 {
            let span = Span::new(traffic.clone(), "openai");
            span.finish();
        }
        let snapshot = traffic.snapshot();
        assert_eq!(snapshot.events.len(), 200);
        assert_eq!(snapshot.total.requests, 205);
        let archive = traffic.history.lock().unwrap().archive.clone();
        let restored = Arc::new(Traffic::default());
        let mut events = snapshot.events;
        events.reverse();
        restored.restore_events(events);
        restored.restore_archive(archive);
        assert_eq!(restored.snapshot().total.requests, 205);
        let next = Span::new(restored.clone(), "openai");
        assert_eq!(next.id, 206);
        next.finish();
        drop(next);
        assert_eq!(restored.snapshot().total.requests, 206);
    }

    #[test]
    fn aborted_and_failed_streams_are_finalized_once() {
        let traffic = Arc::new(Traffic::default());
        let cancelled = Span::new(traffic.clone(), "openai");
        drop(cancelled);
        let failed = Span::new(traffic.clone(), "openai");
        failed.set_error("", None, "stream_error", "disconnected");
        drop(failed);
        let snapshot = traffic.snapshot();
        assert_eq!(snapshot.total.requests, 2);
        assert_eq!(snapshot.total.cancelled, 1);
        assert_eq!(snapshot.total.errors, 1);
        assert_eq!(snapshot.in_flight, 0);
    }
}
