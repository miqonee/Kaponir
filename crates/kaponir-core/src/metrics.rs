//! Ограниченная по памяти телеметрия без текстов, токенов, имён сессий и сообщений провайдеров.
//! Снимок атомарных счётчиков приблизителен при конкурентной записи.
use serde::Serialize;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const LATENCY_BOUNDS_US: [u64; 8] =
    [1000, 5000, 10000, 50000, 100000, 500000, 1000000, 10000000];

#[derive(Default)]
struct OperationState {
    snapshot: OperationSnapshot,
    samples_us: VecDeque<u64>,
}

#[derive(Default)]
pub struct OperationMetrics {
    inner: Mutex<OperationState>,
}

#[derive(Clone, Serialize)]
pub struct OperationSnapshot {
    pub calls: u64,
    pub errors: u64,
    pub duration_us: u64,
    pub p50_us: u64,
    pub p95_us: u64,
    pub p99_us: u64,
    pub bounds_us: [u64; 8],
    pub buckets: [u64; 8],
}

impl Default for OperationSnapshot {
    fn default() -> Self {
        Self {
            calls: 0,
            errors: 0,
            duration_us: 0,
            p50_us: 0,
            p95_us: 0,
            p99_us: 0,
            bounds_us: LATENCY_BOUNDS_US,
            buckets: [0; 8],
        }
    }
}

fn percentile(sorted: &[u64], pct: usize) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let idx = ((sorted.len() - 1) * pct) / 100;
    sorted[idx]
}

impl OperationMetrics {
    pub fn observe(&self, elapsed: Duration, failed: bool) {
        let micros = elapsed.as_micros().min(u64::MAX as u128) as u64;
        let mut st = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        st.snapshot.calls += 1;
        st.snapshot.errors += u64::from(failed);
        st.snapshot.duration_us = st.snapshot.duration_us.saturating_add(micros);
        for (bound, bucket) in LATENCY_BOUNDS_US.iter().zip(&mut st.snapshot.buckets) {
            if micros <= *bound {
                *bucket += 1;
            }
        }
        if st.samples_us.len() >= 512 {
            st.samples_us.pop_front();
        }
        st.samples_us.push_back(micros);
    }

    pub fn snapshot(&self) -> OperationSnapshot {
        let st = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let mut snap = st.snapshot.clone();
        if !st.samples_us.is_empty() {
            let mut sorted: Vec<u64> = st.samples_us.iter().copied().collect();
            sorted.sort_unstable();
            snap.p50_us = percentile(&sorted, 50);
            snap.p95_us = percentile(&sorted, 95);
            snap.p99_us = percentile(&sorted, 99);
        }
        snap
    }
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum S1FailureCode {
    Timeout,
    ProviderError,
    InvalidResponse,
}

#[derive(Clone, Serialize)]
pub struct S1Failure {
    pub unix_ms: u64,
    pub code: S1FailureCode,
}

/// Сборщик приложения решает, куда сохранять обезличенные события.
pub trait S1FailureSink: Send + Sync {
    fn record(&self, event: &S1Failure);
}

#[derive(Default)]
pub struct Metrics {
    sink: Mutex<Option<Arc<dyn S1FailureSink>>>,
    pub mask: OperationMetrics,
    pub restore: OperationMetrics,
    pub s1: OperationMetrics,
    pub replacements: AtomicU64,
    pub restored: AtomicU64,
    pub unknown_tokens: AtomicU64,
    pub denied_tokens: AtomicU64,
    pub s1_candidates: AtomicU64,
    failures: Mutex<VecDeque<S1Failure>>,
}

#[derive(Serialize)]
pub struct Snapshot {
    pub mask: OperationSnapshot,
    pub restore: OperationSnapshot,
    pub s1: OperationSnapshot,
    pub replacements: u64,
    pub restored: u64,
    pub unknown_tokens: u64,
    pub denied_tokens: u64,
    pub s1_candidates: u64,
    pub recent_s1_failures: Vec<S1Failure>,
}

impl Metrics {
    pub fn set_s1_failure_sink(&self, sink: Arc<dyn S1FailureSink>) {
        *self
            .sink
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(sink);
    }

    pub fn s1_failure(&self, code: S1FailureCode) {
        let event = S1Failure {
            unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis()
                .min(u64::MAX as u128) as u64,
            code,
        };
        {
            let mut events = self
                .failures
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if events.len() == 64 {
                events.pop_front();
            }
            events.push_back(event.clone());
        }
        let sink = self
            .sink
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone();
        if let Some(sink) = sink {
            sink.record(&event);
        }
    }

    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            mask: self.mask.snapshot(),
            restore: self.restore.snapshot(),
            s1: self.s1.snapshot(),
            replacements: self.replacements.load(Relaxed),
            restored: self.restored.load(Relaxed),
            unknown_tokens: self.unknown_tokens.load(Relaxed),
            denied_tokens: self.denied_tokens.load(Relaxed),
            s1_candidates: self.s1_candidates.load(Relaxed),
            recent_s1_failures: self
                .failures
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .iter()
                .cloned()
                .collect(),
        }
    }
}
