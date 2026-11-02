//! cargo run --release -p kaponir-cli --example benchmark -- [iterations]
//! Локальное ядро, MemoryVault, S1 выключен; вывод JSON, исходники не выводятся.
use kaponir_cli::{Config, build_engine, load_packs};
use kaponir_core::{Escape, Input, MaskOptions, MemoryVault, Principal, StreamRestorer};
use serde_json::json;
use std::time::Instant;

fn summary(mut samples: Vec<f64>) -> serde_json::Value {
    samples.sort_by(f64::total_cmp);
    let percentile =
        |p: f64| samples[((samples.len() as f64 * p).ceil() as usize).saturating_sub(1)];
    json!({"samples":samples.len(), "p50_ms":percentile(0.5), "p95_ms":percentile(0.95),
        "min_ms": samples[0], "max_ms":samples[samples.len()-1]})
}

fn main() -> anyhow::Result<()> {
    let iterations = std::env::args()
        .nth(1)
        .map(|s| s.parse::<usize>())
        .transpose()?
        .unwrap_or(10);
    anyhow::ensure!(
        (1..=1000).contains(&iterations),
        "iterations must be 1..=1000"
    );
    let cfg = Config::default();
    let engine = build_engine(&cfg, load_packs(&cfg)?, &[], [7; 32], None)?;
    let principal = Principal::local("benchmark");
    let opts = MaskOptions {
        owner: "benchmark".into(),
        journal: false,
        ..Default::default()
    };
    let line =
        "deploy /srv/vegaledger/releases; password: private_fixture_425; Kubernetes nginx HTTP\n";
    let mut results = Vec::new();
    for bytes in [10_000, 100_000, 1_000_000] {
        let text = line.repeat(bytes / line.len() + 1);
        let mut mask_times = Vec::new();
        let mut restore_times = Vec::new();
        let mut chunk_times = Vec::new();
        for run in 0..=iterations {
            let mut vault = MemoryVault::new();
            let start = Instant::now();
            let masked = engine.mask(&Input::text(&text), &opts, &mut vault)?;
            let mask_ms = start.elapsed().as_secs_f64() * 1000.0;
            let start = Instant::now();
            let restored = engine.restore(&masked.text, &principal, &vault, Escape::None)?;
            let restore_ms = start.elapsed().as_secs_f64() * 1000.0;
            anyhow::ensure!(restored.text == text, "roundtrip failed");
            let mut stream = StreamRestorer::new(Escape::None);
            let mut output = String::new();
            for chunk in masked.text.as_bytes().chunks(256) {
                let start = Instant::now();
                output.push_str(&stream.push(
                    &engine,
                    &principal,
                    &vault,
                    std::str::from_utf8(chunk)?,
                )?);
                if run > 0 {
                    chunk_times.push(start.elapsed().as_secs_f64() * 1000.0);
                }
            }
            output.push_str(&stream.flush(&engine, &principal, &vault)?);
            anyhow::ensure!(output == text, "stream roundtrip failed");
            if run > 0 {
                mask_times.push(mask_ms);
                restore_times.push(restore_ms);
            }
        }
        results.push(json!({"input_bytes":text.len(), "mask":summary(mask_times),
            "restore":summary(restore_times), "stream_chunk_256_bytes":summary(chunk_times)}));
    }
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({"version":env!("CARGO_PKG_VERSION"),
        "profile":"release recommended", "s1":false, "vault":"memory", "warmup":1, "results":results}))?
    );
    Ok(())
}
