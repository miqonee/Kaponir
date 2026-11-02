//! Прогон размеченного корпуса пакета: что должно быть замаскировано и что должно остаться.

use kaponir_core::{Engine, Input, MaskOptions, MemoryVault};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Case {
    pub text: String,
    /// Подстроки, которых не должно остаться в результате.
    #[serde(default)]
    pub mask: Vec<String>,
    /// Подстроки, которые должны остаться без изменений.
    #[serde(default)]
    pub keep: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Report {
    pub cases: usize,
    pub mask_total: usize,
    pub mask_ok: usize,
    pub keep_total: usize,
    pub keep_ok: usize,
    /// (номер строки, описание ошибки, результат маскирования).
    pub failures: Vec<(usize, String, String)>,
}

impl Report {
    /// Доля чувствительных значений, которые были скрыты.
    pub fn recall(&self) -> f64 {
        if self.mask_total == 0 {
            1.0
        } else {
            self.mask_ok as f64 / self.mask_total as f64
        }
    }

    /// Доля безопасных значений, которые остались (обратное к «лишнему» маскированию).
    pub fn keep_rate(&self) -> f64 {
        if self.keep_total == 0 {
            1.0
        } else {
            self.keep_ok as f64 / self.keep_total as f64
        }
    }

    pub fn passed(&self) -> bool {
        self.failures.is_empty()
    }
}

pub fn parse_jsonl(s: &str) -> Result<Vec<(usize, Case)>, String> {
    s.lines()
        .enumerate()
        .filter(|(_, l)| !l.trim().is_empty() && !l.trim_start().starts_with("//"))
        .map(|(i, l)| {
            serde_json::from_str(l)
                .map(|c| (i + 1, c))
                .map_err(|e| format!("line {}: {e}", i + 1))
        })
        .collect()
}

pub fn run(engine: &Engine, cases: &[(usize, Case)]) -> Report {
    let mut r = Report::default();
    let opts = MaskOptions {
        journal: false,
        owner: "corpus".into(),
        ..MaskOptions::default()
    };
    for (line, case) in cases {
        let mut vault = MemoryVault::new();
        r.cases += 1;
        let out = match engine.mask(&Input::text(&case.text), &opts, &mut vault) {
            Ok(m) => m.text,
            Err(e) => {
                r.failures
                    .push((*line, format!("error: {e}"), String::new()));
                continue;
            }
        };
        for m in &case.mask {
            r.mask_total += 1;
            if out.contains(m.as_str()) {
                r.failures
                    .push((*line, format!("leaked `{m}`"), out.clone()));
            } else {
                r.mask_ok += 1;
            }
        }
        for k in &case.keep {
            r.keep_total += 1;
            if out.contains(k.as_str()) {
                r.keep_ok += 1;
            } else {
                r.failures
                    .push((*line, format!("over-masked `{k}`"), out.clone()));
            }
        }
    }
    r
}
