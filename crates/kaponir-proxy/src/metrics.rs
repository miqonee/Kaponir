//! JSON для дашборда и Prometheus; метки фиксированы, секретов и session id нет.
use crate::Shared;
use crate::server::{ApiError, authorized};
use axum::Json;
use axum::extract::State;
use axum::http::{HeaderMap, header};
use axum::response::{IntoResponse, Response};
use kaponir_core::metrics::OperationSnapshot;
use serde_json::json;
use std::fmt::Write;
use std::sync::atomic::Ordering::Relaxed;

pub(crate) async fn snapshot(
    State(st): State<Shared>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    authorized(&st, &headers, true)?;
    Ok((
        [(header::CACHE_CONTROL, "no-store")],
        Json(json!({
            "uptime_seconds": st.started.elapsed().as_secs(),
            "http": st.requests.snapshot(),
            "traffic": st.traffic.snapshot(),
            "stream_errors": st.stream_errors.load(Relaxed),
            "engine": st.engine().metrics().snapshot(),
        })),
    )
        .into_response())
}

fn counter(out: &mut String, name: &str, value: u64) {
    writeln!(out, "# TYPE kaponir_{name} counter\nkaponir_{name} {value}").unwrap();
}

fn operation(out: &mut String, name: &str, s: &OperationSnapshot) {
    counter(out, &format!("{name}_calls_total"), s.calls);
    counter(out, &format!("{name}_errors_total"), s.errors);
    let metric = format!("kaponir_{name}_duration_seconds");
    writeln!(out, "# TYPE {metric} histogram").unwrap();
    for (bound, count) in s.bounds_us.iter().zip(s.buckets) {
        writeln!(
            out,
            "{metric}_bucket{{le=\"{}\"}} {count}",
            *bound as f64 / 1e6
        )
        .unwrap();
    }
    writeln!(
        out,
        "{metric}_bucket{{le=\"+Inf\"}} {}\n{metric}_count {}\n{metric}_sum {}",
        s.calls,
        s.calls,
        s.duration_us as f64 / 1e6
    )
    .unwrap();
}

pub(crate) async fn prometheus(
    State(st): State<Shared>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    authorized(&st, &headers, true)?;
    let s = st.engine().metrics().snapshot();
    let t = st.traffic.snapshot();
    let mut out = String::new();
    operation(&mut out, "http", &st.requests.snapshot());
    operation(&mut out, "mask", &s.mask);
    operation(&mut out, "restore", &s.restore);
    operation(&mut out, "s1", &s.s1);
    for (name, value) in [
        ("replacements_total", s.replacements),
        ("cached_replacements_total", t.totals.cached_replacements),
        ("restored_total", s.restored),
        ("unknown_tokens_total", s.unknown_tokens),
        ("denied_tokens_total", s.denied_tokens),
        ("s1_candidates_total", s.s1_candidates),
        ("prompt_tokens_total", t.totals.prompt_tokens),
        ("completion_tokens_total", t.totals.completion_tokens),
        ("total_tokens_total", t.totals.total_tokens),
        ("stream_errors_total", st.stream_errors.load(Relaxed)),
    ] {
        counter(&mut out, name, value);
    }
    Ok((
        [
            (
                header::CONTENT_TYPE,
                "text/plain; version=0.0.4; charset=utf-8",
            ),
            (header::CACHE_CONTROL, "no-store"),
        ],
        out,
    )
        .into_response())
}

pub(crate) struct StderrDiagnostics;
impl kaponir_core::metrics::S1FailureSink for StderrDiagnostics {
    fn record(&self, event: &kaponir_core::metrics::S1Failure) {
        // Только стабильный код и время; сообщения ошибок могут содержать URL и данные.
        use std::io::Write;
        let line = json!({"event":"kaponir_s1_failure", "detail":event});
        let _ = writeln!(std::io::stderr().lock(), "{line}");
    }
}
