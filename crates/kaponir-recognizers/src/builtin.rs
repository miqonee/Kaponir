//! Параллельный поиск в одном документе. Формат, словарь и координаты общие для всех проходов.
use std::sync::{LazyLock, Mutex};
use std::thread;

use kaponir_core::{AnalyzeCtx, Candidate, Input, Recognizer};
use rayon_core::{ThreadPool, ThreadPoolBuilder};

use crate::{composite, kv, names, network, paths, secrets, tabular, users, validators, web};

const MIN_PARALLEL_BYTES: usize = 32 * 1024;
const MAX_SCAN_THREADS: usize = 3;

struct ScanPool {
    helpers: ThreadPool,
    // Другой запрос может сразу сканировать на своём потоке, не ожидая свободных помощников.
    slot: Mutex<()>,
}

static POOL: LazyLock<Option<ScanPool>> = LazyLock::new(|| {
    let available = thread::available_parallelism().map_or(1, |n| n.get());
    let configured = std::env::var("KAPONIR_DETECTOR_THREADS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(MAX_SCAN_THREADS);
    let threads = available.min(configured).min(MAX_SCAN_THREADS);
    if threads < 2 {
        return None;
    }
    let helpers = ThreadPoolBuilder::new()
        .num_threads(threads - 1)
        .thread_name(|i| format!("kaponir-detector-{i}"))
        .build()
        .ok()?;
    Some(ScanPool {
        helpers,
        slot: Mutex::new(()),
    })
});

type Detector = Box<dyn Recognizer>;

/// Встроенные детекторы в штатном порядке. Правила пользователя и детекторы пакетов
/// остаются отдельными этапами движка и видят все результаты этого прохода.
pub struct BuiltinDetectors {
    before_web: [Detector; 5],
    web: web::Web,
    after_web: [Detector; 3],
    names: names::Names,
}

impl Default for BuiltinDetectors {
    fn default() -> Self {
        Self {
            before_web: [
                Box::new(secrets::Secrets::new()),
                Box::new(kv::KeyValue::new()),
                Box::new(users::Users::new()),
                Box::new(tabular::Tabular::new()),
                Box::new(network::Network::new()),
            ],
            web: web::Web::new(),
            after_web: [
                Box::new(paths::Paths::new()),
                Box::new(composite::Composite::new()),
                Box::new(validators::Validators::new()),
            ],
            names: names::Names::new(),
        }
    }
}

/// Отдельные распознаватели для вызывающих, собирающих свой конвейер.
pub fn builtin() -> Vec<Box<dyn Recognizer>> {
    let detectors = BuiltinDetectors::default();
    let mut ordered = Vec::from(detectors.before_web);
    ordered.push(Box::new(detectors.web));
    ordered.extend(detectors.after_web);
    ordered.push(Box::new(detectors.names));
    ordered
}

impl BuiltinDetectors {
    fn serial(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        for d in &self.before_web {
            d.analyze(input, ctx, out);
        }
        self.web.analyze(input, ctx, out);
        for d in &self.after_web {
            d.analyze(input, ctx, out);
        }
        self.names.analyze(input, ctx, out);
    }

    fn parallel(
        &self,
        pool: &ThreadPool,
        input: &Input<'_>,
        ctx: &AnalyzeCtx<'_>,
        out: &mut Vec<Candidate>,
    ) {
        let mut web = Vec::new();
        let mut after_web = Vec::new();
        let mut names = Vec::new();
        pool.in_place_scope(|scope| {
            scope.spawn(|_| self.names.analyze(input, ctx, &mut names));
            scope.spawn(|_| self.web.analyze(input, ctx, &mut web));
            // secrets и config-keys читают уже добавленные совпадения, включая входной out.
            for d in &self.before_web {
                d.analyze(input, ctx, out);
            }
            // Эти три детектора зависят только от текста, словаря и собственных совпадений.
            for d in &self.after_web {
                d.analyze(input, ctx, &mut after_web);
            }
        });
        out.append(&mut web);
        out.append(&mut after_web);
        out.append(&mut names);
    }
}

impl Recognizer for BuiltinDetectors {
    fn id(&self) -> &str {
        "builtin"
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        if input.text.len() >= MIN_PARALLEL_BYTES
            && let Some(pool) = POOL.as_ref()
            && let Ok(_slot) = pool.slot.try_lock()
        {
            self.parallel(&pool.helpers, input, ctx, out);
            return;
        }
        self.serial(input, ctx, out);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kaponir_core::{Category, Format, Lexicon};

    #[test]
    fn scoped_workers_keep_candidate_order_format_and_current_lexicon() {
        let scanners = BuiltinDetectors::default();
        let reference = builtin();
        let text =
            "password=opaque-value\ncc John Smith at https://glacier.example/path or 10.20.30.40";
        let start = text.find("opaque-value").unwrap();
        let mut existing = Candidate::new(
            start,
            start + "opaque-value".len(),
            "SECRET",
            Category::Credential,
            "earlier",
            0.99,
        );
        existing.hard = true;
        let mut first = Lexicon::default();
        first.keys.insert("password".into(), "SECRET".into());
        let mut second = first.clone();
        second.names.given.insert("john".into(), None);
        second.corp_domains.insert("glacier.example".into());
        for helpers in [1, 2] {
            let pool = ThreadPoolBuilder::new()
                .num_threads(helpers)
                .build()
                .unwrap();
            for lexicon in [&first, &second, &first] {
                let ctx = AnalyzeCtx::new(lexicon);
                for format in [Format::Auto, Format::Env, Format::Json, Format::Csv] {
                    let input = Input { text, format };
                    let mut expected = vec![existing.clone()];
                    let mut actual = expected.clone();
                    for d in &reference {
                        d.analyze(&input, &ctx, &mut expected);
                    }
                    scanners.parallel(&pool, &input, &ctx, &mut actual);
                    assert_eq!(
                        serde_json::to_value(&actual).unwrap(),
                        serde_json::to_value(&expected).unwrap()
                    );
                    assert_eq!(
                        actual.iter().any(|c| c.kind == "FNAME"),
                        !lexicon.names.is_empty()
                    );
                }
            }
        }
    }
}
