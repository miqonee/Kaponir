//! Телеметрия прокси-трафика, поэтапные тайминги (Latency Waterfall), диагностика ошибок
//! и снимки Request Inspector (хранятся в зашифрованном Vault + кольцевом буфере RAM).
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
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
    pub restored_response_buf: String,
}

impl Usage {
    pub fn add(&mut self, other: &Self) {
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

type EventPersister = Arc<dyn Fn(&Event) + Send + Sync>;

#[derive(Default)]
struct History {
    hours: BTreeMap<u64, Bucket>,
    events: VecDeque<Event>,
    total: Bucket,
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
}

fn add_to_bucket(bucket: &mut Bucket, event: &Event) {
    bucket.requests += 1;
    bucket.errors += u64::from(event.outcome == "error" || event.outcome == "restore_error");
    bucket.blocked += u64::from(!event.sent);
    bucket.cancelled += u64::from(event.outcome == "cancelled");
    bucket.usage.add(&event.usage);
    // В агрегированных бакетах не храним массивы отдельных элементов: только счётчики.
    bucket.usage.masked_items.clear();
    bucket.usage.unknown_tokens.clear();
    bucket.usage.denied_tokens.clear();
    bucket.timings_sum.mask_us = bucket
        .timings_sum
        .mask_us
        .saturating_add(event.timings.mask_us);
    bucket.timings_sum.classifier_us = bucket
        .timings_sum
        .classifier_us
        .saturating_add(event.timings.classifier_us);
    bucket.timings_sum.upstream_us = bucket
        .timings_sum
        .upstream_us
        .saturating_add(event.timings.upstream_us);
    bucket.timings_sum.restore_us = bucket
        .timings_sum
        .restore_us
        .saturating_add(event.timings.restore_us);
}

impl Traffic {
    pub fn set_persister(&self, p: EventPersister) {
        *self
            .persister
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(p);
    }

    /// Восстанавливает историю событий из зашифрованного `vault.sqlite` при старте прокси.
    pub fn restore_events(&self, events: Vec<Event>) {
        let mut h = self
            .history
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let mut max_id = 0u64;
        for event in events {
            max_id = max_id.max(event.id);
            let hour = event.time_ms / 3_600_000;
            let bucket = h.hours.entry(hour).or_insert_with(|| Bucket {
                hour,
                ..Default::default()
            });
            add_to_bucket(bucket, &event);
            add_to_bucket(&mut h.total, &event);
            if h.events.len() == 200 {
                h.events.pop_front();
            }
            h.events.push_back(event);
        }
        h.hours.retain(|hour, _| *hour + 168 > now_ms() / 3_600_000);
        if max_id > 0 {
            self.next.fetch_max(max_id, Relaxed);
        }
    }

    fn record(&self, event: Event) {
        {
            let mut h = self
                .history
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let hour = event.time_ms / 3_600_000;
            let bucket = h.hours.entry(hour).or_insert_with(|| Bucket {
                hour,
                ..Default::default()
            });
            add_to_bucket(bucket, &event);
            add_to_bucket(&mut h.total, &event);
            h.hours.retain(|hour, _| *hour + 168 > now_ms() / 3_600_000);
            if h.events.len() == 200 {
                h.events.pop_front();
            }
            h.events.push_back(event.clone());
        }
        let persister = self
            .persister
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone();
        if let Some(p) = persister {
            p(&event);
        }
    }

    pub fn snapshot(&self) -> Snapshot {
        let h = self
            .history
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        Snapshot {
            in_flight: self.in_flight.load(Relaxed),
            total: h.total.clone(),
            hours: h.hours.values().cloned().collect(),
            events: h.events.iter().rev().cloned().collect(),
        }
    }

    pub fn event_by_id(&self, id: u64) -> Option<Event> {
        let h = self
            .history
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        h.events.iter().find(|e| e.id == id).cloned()
    }
}

pub struct Span {
    history: Arc<Traffic>,
    pub usage: Mutex<Usage>,
    pub connection_id: Mutex<Option<String>>,
    pub model: Mutex<Option<String>>,
    pub upstream_url: Mutex<Option<String>>,
    pub timings: Mutex<StageTimings>,
    pub error: Mutex<Option<EventError>>,
    pub inspector: Mutex<Option<EventInspector>>,
    pub status: AtomicU16,
    pub sent: AtomicBool,
    pub streaming: AtomicBool,
    pub complete: AtomicBool,
    pub failed: AtomicBool,
    pub restore_failed: AtomicBool,
    provider: &'static str,
    id: u64,
    start: Instant,
}

impl Span {
    pub fn new(history: Arc<Traffic>, provider: &'static str) -> Arc<Self> {
        history.in_flight.fetch_add(1, Relaxed);
        let id = history.next.fetch_add(1, Relaxed) + 1;
        Arc::new(Self {
            history,
            provider,
            id,
            start: Instant::now(),
            usage: Default::default(),
            connection_id: Default::default(),
            model: Default::default(),
            upstream_url: Default::default(),
            timings: Default::default(),
            error: Default::default(),
            inspector: Default::default(),
            status: AtomicU16::new(0),
            sent: AtomicBool::new(false),
            streaming: AtomicBool::new(false),
            complete: AtomicBool::new(false),
            failed: AtomicBool::new(false),
            restore_failed: AtomicBool::new(false),
        })
    }

    pub fn add(&self, usage: &Usage) {
        self.usage
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .add(usage);
    }

    pub fn set_error(&self, upstream_url: impl Into<String>, status: u16, kind: impl Into<String>, message: impl Into<String>) {
        *self
            .error
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(EventError {
            upstream_url: upstream_url.into(),
            status,
            kind: kind.into(),
            message: cap_str(&message.into(), 1024),
        });
    }
}

impl Drop for Span {
    fn drop(&mut self) {
        let status = self.status.load(Relaxed);
        let outcome = if self.failed.load(Relaxed) || status >= 400 {
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
            if insp.raw_response.is_empty() && !usage.raw_response_buf.is_empty() {
                insp.raw_response = usage.raw_response_buf.clone();
            }
            if insp.restored_response.is_empty() && !usage.restored_response_buf.is_empty() {
                insp.restored_response = usage.restored_response_buf.clone();
            }
        }
        self.history.record(Event {
            id: self.id,
            time_ms: now_ms(),
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
            timings: self
                .timings
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            error: self
                .error
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            inspector,
        });
        self.history.in_flight.fetch_sub(1, Relaxed);
    }
}
