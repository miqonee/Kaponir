//! Only model proxy traffic. No request text, model names, credentials or session identifiers.
use serde::Serialize;
use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[derive(Clone, Default, Serialize)]
pub struct Usage {
    pub replacements: u64,
    /// credentials, identity, infrastructure, financial, contact, other
    pub categories: [u64; 6],
    pub restored: u64,
    pub unknown: u64,
    pub denied: u64,
}
impl Usage {
    pub fn add(&mut self, other: &Self) {
        self.replacements += other.replacements;
        for (a, b) in self.categories.iter_mut().zip(other.categories) {
            *a += b;
        }
        self.restored += other.restored;
        self.unknown += other.unknown;
        self.denied += other.denied;
    }
    pub fn masked(m: &kaponir_core::Masked) -> Self {
        let mut usage = Self::default();
        for replacement in &m.replacements {
            let group = match replacement.category {
                kaponir_core::Category::Credential => 0,
                kaponir_core::Category::Identity => 1,
                kaponir_core::Category::Infra | kaponir_core::Category::Network => 2,
                kaponir_core::Category::Financial => 3,
                kaponir_core::Category::Contact => 4,
                kaponir_core::Category::Custom => 5,
            };
            usage.categories[group] += 1;
            usage.replacements += 1;
        }
        usage
    }
    pub fn restored(r: &kaponir_core::Restored) -> Self {
        Self {
            restored: r.restored as u64,
            unknown: r.unknown.len() as u64,
            denied: r.denied.len() as u64,
            ..Self::default()
        }
    }
}

#[derive(Clone, Serialize)]
pub struct Event {
    pub id: u64,
    pub time_ms: u64,
    pub provider: &'static str,
    pub connection_id: Option<String>,
    pub status: u16,
    pub outcome: &'static str,
    pub duration_ms: u64,
    pub sent: bool,
    pub usage: Usage,
}
#[derive(Default, Clone, Serialize)]
pub struct Bucket {
    pub hour: u64,
    pub requests: u64,
    pub errors: u64,
    pub blocked: u64,
    pub cancelled: u64,
    pub usage: Usage,
}
#[derive(Default)]
struct History {
    hours: BTreeMap<u64, Bucket>,
    events: VecDeque<Event>,
    total: Bucket,
}
#[derive(Default)]
pub struct Traffic {
    history: Mutex<History>,
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
impl Traffic {
    fn record(&self, event: Event) {
        let mut h = self
            .history
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let hour = event.time_ms / 3_600_000;
        let bucket = h.hours.entry(hour).or_insert_with(|| Bucket {
            hour,
            ..Default::default()
        });
        fn add(bucket: &mut Bucket, event: &Event) {
            bucket.requests += 1;
            bucket.errors +=
                u64::from(event.outcome == "error" || event.outcome == "restore_error");
            bucket.blocked += u64::from(!event.sent);
            bucket.cancelled += u64::from(event.outcome == "cancelled");
            bucket.usage.add(&event.usage);
        }
        add(bucket, &event);
        add(&mut h.total, &event);
        h.hours.retain(|hour, _| *hour + 168 > now_ms() / 3_600_000);
        if h.events.len() == 200 {
            h.events.pop_front();
        }
        h.events.push_back(event);
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
}

pub struct Span {
    history: Arc<Traffic>,
    pub usage: Mutex<Usage>,
    pub connection_id: Mutex<Option<String>>,
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
        self.history.record(Event {
            id: self.id,
            time_ms: now_ms(),
            provider: self.provider,
            connection_id: self
                .connection_id
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
            status,
            outcome,
            sent: self.sent.load(Relaxed),
            duration_ms: self.start.elapsed().as_millis() as u64,
            usage: self
                .usage
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone(),
        });
        self.history.in_flight.fetch_sub(1, Relaxed);
    }
}
