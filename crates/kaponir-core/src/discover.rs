//! Локальное обнаружение повторяющихся значений для первоначальной настройки.
//!
//! По умолчанию анализ локальный. S1 включается явно и только оценивает предложения;
//! создание правил и обучающих меток всегда требует решения человека.

use crate::rules::{Rule, RuleAction};
use crate::{Category, Engine, Input, MemoryVault};
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::LazyLock;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Document {
    pub name: String,
    pub text: String,
    #[serde(default)]
    pub format: crate::Format,
    /// Untrusted model proposals. Only exact, source-backed values are considered.
    #[serde(default)]
    pub model_candidates: Vec<ModelCandidate>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct ModelCandidate {
    pub value: String,
    pub kind: String,
    pub quote: String,
}

impl ModelCandidate {
    fn valid_for(&self, text: &str) -> bool {
        (3..=512).contains(&self.value.len())
            && !self.value.chars().any(char::is_control)
            && self.value.trim() == self.value
            && self.quote.len() <= 2048
            && self.quote.contains(&self.value)
            && text.contains(&self.quote)
            && profile_categories().contains_key(self.kind.as_str())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Suggestion {
    /// Детерминированный идентификатор значения, типа и действия.
    pub id: String,
    pub value: String,
    pub kind: String,
    pub count: usize,
    pub documents: usize,
    pub reason: String,
    pub sample: String,
    #[serde(default)]
    pub examples: Vec<Example>,
    /// Digest of document name + text → number of distinct occurrences.
    #[serde(default)]
    pub occurrences: BTreeMap<String, usize>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Example {
    pub document: String,
    pub document_id: String,
    pub start: usize,
    pub end: usize,
    /// Bounded fragment; other detected values are replaced locally.
    pub context: String,
    pub s1_score: Option<f32>,
}

#[derive(Clone, Debug, Default)]
pub struct Options {
    pub use_s1: bool,
}

pub const MAX_DOCUMENTS: usize = 64;
pub const MAX_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_SUGGESTIONS: usize = 256;
const MAX_EXAMPLES: usize = 3;
const MAX_S1_VIEWS: usize = 96;

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Report {
    pub suggestions: Vec<Suggestion>,
    pub documents: usize,
    pub bytes: usize,
    #[serde(default)]
    pub truncated: bool,
    #[serde(default)]
    pub s1_status: String,
    #[serde(default)]
    pub s1_scored: usize,
}

impl Suggestion {
    pub fn mask_rule(&self) -> Rule {
        Rule {
            kind: self.kind.clone(),
            action: RuleAction::Mask,
            value: Some(self.value.clone()),
            category: profile_categories().get(self.kind.as_str()).copied(),
            note: format!("kaponir discover: {}", self.reason),
            ..Rule::default()
        }
    }
}

/// Анализ документов без записи в vault, журнал и без сетевых вызовов.
pub fn analyze(engine: &Engine, documents: &[Document]) -> Report {
    analyze_with_options(engine, documents, &Options::default())
}

pub fn validate_documents(documents: &[Document]) -> Result<(), String> {
    if documents.iter().any(|d| {
        d.model_candidates.len() > MAX_SUGGESTIONS
            || d.model_candidates.iter().any(|c| !c.valid_for(&d.text))
    }) {
        return Err("invalid model candidates: require exact source quote, known kind, bounded values/count".into());
    }
    if documents.iter().any(|d| d.name.len() > 1024) {
        return Err("discover document name limit: 1024 bytes".into());
    }
    if documents.len() > MAX_DOCUMENTS
        || documents.iter().map(|d| d.text.len()).sum::<usize>() > MAX_BYTES
    {
        return Err("discover input limit: 64 documents / 2 MiB".into());
    }
    Ok(())
}

pub fn analyze_with_options(engine: &Engine, documents: &[Document], options: &Options) -> Report {
    let mut report = Report {
        s1_status: "not_requested".into(),
        ..Report::default()
    };
    let mut found: BTreeMap<(String, String), Suggestion> = BTreeMap::new();
    let mut views = BTreeMap::new();
    let mut seen_documents = HashSet::new();
    let s1 = engine.discovery_s1();
    for doc in documents.iter().take(MAX_DOCUMENTS) {
        if report.bytes.saturating_add(doc.text.len()) > MAX_BYTES {
            report.truncated = true;
            break;
        }
        report.bytes += doc.text.len();
        report.documents += 1;
        let input = Input {
            text: &doc.text,
            format: doc.format,
        };
        let candidates = engine.analyze(&input);
        let masked = engine.mask_without_s1(
            &input,
            &crate::MaskOptions {
                explain: true,
                journal: false,
                ..Default::default()
            },
            &mut MemoryVault::new(),
        );
        let covered: Vec<_> = masked
            .map(|m| {
                m.explain
                    .into_iter()
                    .filter(|e| e.applied)
                    .map(|e| (e.start, e.end))
                    .collect()
            })
            .unwrap_or_default();
        let mut spans = business_spans(&doc.text);
        let business = spans.clone();
        for c in &candidates {
            if c.polarity != crate::Polarity::Mask
                || c.origin != crate::Origin::Builtin
                || covered.iter().any(|&(a, b)| a < c.end && c.start < b)
                || business
                    .iter()
                    .any(|s| s.start <= c.start && c.end <= s.end)
            {
                continue;
            }
            spans.push(Span {
                start: c.start,
                end: c.end,
                kind: c.kind.clone(),
                category: c.category,
                reason: format!("кандидат детектора {}", c.recognizer),
                recognizer: c.recognizer.clone(),
            });
        }
        // Do not trust model offsets, categories or quotations without checking the source.
        // All occurrences are rediscovered locally, retaining UTF-8 byte offsets.
        let mut model_occurrences = 0;
        'model: for hint in doc.model_candidates.iter().take(MAX_SUGGESTIONS) {
            if !hint.valid_for(&doc.text) || weak_business_value(&hint.kind, &hint.value) {
                continue;
            }
            for (start, value) in doc.text.match_indices(&hint.value) {
                if model_occurrences >= 4096 {
                    report.truncated = true;
                    break 'model;
                }
                model_occurrences += 1;
                let end = start + value.len();
                if let Some(existing) = spans.iter_mut().find(|s| s.start == start && s.end == end)
                {
                    if hint.kind == "SECRET" {
                        existing.kind = hint.kind.clone();
                        existing.category = Category::Credential;
                    }
                    if !model_proposed(&existing.reason) {
                        existing.reason.push_str("; также предложено моделью");
                    }
                } else {
                    spans.push(Span {
                        start,
                        end,
                        kind: hint.kind.clone(),
                        category: profile_categories()[hint.kind.as_str()],
                        reason: "кандидат модели, значение проверено по источнику".into(),
                        recognizer: "model".into(),
                    });
                }
            }
        }
        spans.sort_by_key(|s| (s.start, s.end, s.kind.clone()));
        spans.dedup_by(|a, b| a.start == b.start && a.end == b.end && a.kind == b.kind);
        let model_secrets: Vec<_> = spans
            .iter()
            .filter(|s| s.category == Category::Credential)
            .map(|s| (s.start, s.end))
            .collect();
        let all_masks: Vec<_> = candidates
            .iter()
            .filter(|c| c.polarity == crate::Polarity::Mask)
            .map(|c| (c.start, c.end, c.kind.clone()))
            .chain(spans.iter().map(|s| (s.start, s.end, s.kind.clone())))
            .collect();
        let document_id = blake3::hash(format!("{}\0{}", doc.name, doc.text).as_bytes())
            .to_hex()
            .to_string();
        if !seen_documents.insert(document_id.clone()) {
            continue;
        }
        for span in spans {
            let value = doc.text[span.start..span.end].trim();
            if value.len() < 3
                || (span.category != Category::Credential
                    && model_secrets
                        .iter()
                        .any(|&(a, b)| a < span.end && span.start < b))
                || value.chars().any(char::is_control)
                || engine.lexicon().is_word(value)
                || covered
                    .iter()
                    .any(|&(a, b)| a <= span.start && span.end <= b)
                || candidates.iter().any(|c| {
                    (c.category == Category::Credential && c.start < span.end && span.start < c.end)
                        || (c.polarity == crate::Polarity::Keep
                            && c.start <= span.start
                            && span.end <= c.end)
                })
            {
                continue;
            }
            let key = (span.kind.clone(), value.to_lowercase());
            if found.len() >= MAX_SUGGESTIONS && !found.contains_key(&key) {
                report.truncated = true;
                continue;
            }
            let suggestion = found.entry(key.clone()).or_insert_with(|| Suggestion {
                id: suggestion_id(&span.kind, value),
                value: value.into(),
                kind: span.kind.clone(),
                count: 0,
                documents: 0,
                reason: span.reason.clone(),
                sample: doc.name.clone(),
                examples: vec![],
                occurrences: BTreeMap::new(),
            });
            *suggestion
                .occurrences
                .entry(document_id.clone())
                .or_default() += 1;
            if suggestion.examples.len() < MAX_EXAMPLES {
                let context = safe_context(&doc.text, span.start, span.end, &all_masks, 160);
                let index = suggestion.examples.len();
                suggestion.examples.push(Example {
                    document: doc.name.clone(),
                    document_id: document_id.clone(),
                    start: span.start,
                    end: span.end,
                    context,
                    s1_score: None,
                });
                if options.use_s1
                    && span.category != Category::Credential
                    && let Some(s1) = s1
                {
                    use crate::s1::{CandidateView, Disclosure, features};
                    let level = s1.provider.max_disclosure();
                    views.insert(
                        (key, index),
                        CandidateView {
                            kind: span.kind.clone(),
                            category: span.category,
                            recognizer: span.recognizer,
                            features: features(value, engine.lexicon().is_word(value)),
                            surface: (level >= Disclosure::L1).then(|| value.to_owned()),
                            context: (level >= Disclosure::L2).then(|| {
                                safe_context(
                                    &doc.text,
                                    span.start,
                                    span.end,
                                    &all_masks,
                                    s1.context_bytes,
                                )
                            }),
                        },
                    );
                }
            }
        }
    }
    if report.documents < documents.len() {
        report.truncated = true;
    }
    if options.use_s1 {
        if let Some(s1) = s1 {
            let limited = views.len() > MAX_S1_VIEWS;
            let (keys, batch): (Vec<_>, Vec<_>) = views.into_iter().take(MAX_S1_VIEWS).unzip();
            if batch.is_empty() {
                report.s1_status = "no_candidates".into();
            } else {
                // Discovery never sends a whole document or unfiltered confirmed profile.
                let started = std::time::Instant::now();
                engine
                    .metrics()
                    .s1_candidates
                    .fetch_add(batch.len() as u64, std::sync::atomic::Ordering::Relaxed);
                let result = s1.provider.score(&batch);
                match result {
                    Ok(scores)
                        if scores.len() == batch.len()
                            && scores
                                .iter()
                                .all(|p| p.is_finite() && (0.0..=1.0).contains(p)) =>
                    {
                        engine.metrics().s1.observe(started.elapsed(), false);
                        report.s1_scored = scores.len();
                        report.s1_status = if limited { "partial" } else { "scored" }.into();
                        for ((key, index), score) in keys.into_iter().zip(scores) {
                            found.get_mut(&key).unwrap().examples[index].s1_score =
                                Some(s1.calibration.apply(score));
                        }
                    }
                    _ => {
                        engine.metrics().s1.observe(started.elapsed(), true);
                        report.s1_status = "unavailable".into();
                    }
                }
            }
        } else {
            report.s1_status = "off".into();
        }
    }
    report.suggestions = found
        .into_values()
        .map(|mut s| {
            recount(&mut s);
            s
        })
        .collect();
    sort_suggestions(&mut report.suggestions);
    report
}

fn recount(s: &mut Suggestion) {
    s.count = s.occurrences.values().sum();
    s.documents = s.occurrences.len();
}
fn sort_suggestions(suggestions: &mut [Suggestion]) {
    suggestions.sort_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.kind.cmp(&b.kind))
            .then_with(|| a.value.cmp(&b.value))
    });
}

/// Context offsets are UTF-8 byte offsets. Mask complete intersecting spans,
/// including those crossing the fragment boundary; only the selected value is visible.
fn safe_context(
    text: &str,
    start: usize,
    end: usize,
    masks: &[(usize, usize, String)],
    radius: usize,
) -> String {
    let mut left = start.saturating_sub(radius.min(4096));
    while !text.is_char_boundary(left) {
        left += 1;
    }
    let mut right = end.saturating_add(radius.min(4096)).min(text.len());
    while !text.is_char_boundary(right) {
        right -= 1;
    }
    let mut ranges: Vec<_> = masks
        .iter()
        .filter(|(a, b, _)| *a < right && left < *b && !(*a >= start && *b <= end))
        .flat_map(|(a, b, kind)| {
            [
                (left.max(*a), start.min(*b), kind),
                (end.max(*a), right.min(*b), kind),
            ]
        })
        .filter(|(a, b, _)| a < b)
        .collect();
    ranges.sort_by_key(|(a, b, _)| (*a, *b));
    let mut out = String::new();
    let mut cursor = left;
    for (a, b, kind) in ranges {
        if b <= cursor {
            continue;
        }
        if a > cursor {
            out.push_str(&text[cursor..a]);
        }
        out.push_str(&format!("[{kind}]"));
        cursor = b;
    }
    out.push_str(&text[cursor..right]);
    out
}

#[derive(Clone)]
struct Span {
    start: usize,
    end: usize,
    kind: String,
    category: Category,
    reason: String,
    recognizer: String,
}

fn model_proposed(reason: &str) -> bool {
    reason.contains("Xiaomi")
        || reason.contains("кандидат модели")
        || reason.contains("предложено моделью")
}

/// Values which provide no evidence of a private business entity. Exact user rules
/// may still intentionally protect these values; this only filters suggestions.
pub fn weak_business_value(kind: &str, value: &str) -> bool {
    let v = value.trim().to_lowercase();
    if v.is_empty() || v.chars().all(|c| !c.is_alphanumeric()) {
        return true;
    }
    if !matches!(kind, "PROJ" | "ORG" | "CLIENT" | "CUSTOM") {
        return false;
    }
    matches!(
        v.as_str(),
        "или"
            | "и"
            | "как"
            | "это"
            | "для"
            | "the"
            | "and"
            | "or"
            | "project"
            | "character"
            | "questions"
            | "name"
            | "value"
            | "example"
            | "null"
            | "none"
    ) || [
        "gemini-",
        "gpt-",
        "claude-",
        "qwen3-",
        "qwen3.5-",
        "llama-",
        "deepseek-",
    ]
    .iter()
    .any(|p| v.starts_with(p))
}

fn business_spans(text: &str) -> Vec<Span> {
    let mut spans = Vec::new();
    for (re, kind, category, reason) in business_patterns() {
        for caps in re.captures_iter(text) {
            let m = caps.name("value").unwrap();
            if m.as_str().chars().count() > 240 || weak_business_value(kind, m.as_str()) {
                continue;
            }
            if *kind == "ID" && !m.as_str().chars().any(|c| c.is_ascii_digit()) {
                continue;
            }
            spans.push(Span {
                start: m.start(),
                end: m.end(),
                kind: (*kind).into(),
                category: *category,
                reason: (*reason).into(),
                recognizer: "discover".into(),
            });
        }
    }
    for m in amount_re().find_iter(text) {
        spans.push(Span {
            start: m.start(),
            end: m.end(),
            kind: "AMOUNT".into(),
            category: Category::Financial,
            reason: "число с валютой или денежной единицей".into(),
            recognizer: "discover".into(),
        });
    }
    spans
}

type BusinessPattern = (Regex, &'static str, Category, &'static str);
fn business_patterns() -> &'static Vec<BusinessPattern> {
    static PATTERNS: LazyLock<Vec<BusinessPattern>> = LazyLock::new(|| {
        let mut out = Vec::new();
        for (label, kind, category, reason) in [
            (
                "проект|project",
                "PROJ",
                Category::Infra,
                "название после явной метки проекта",
            ),
            (
                "компания|организация|company|organization|ООО|АО|ПАО",
                "ORG",
                Category::Custom,
                "название после метки организации",
            ),
            (
                "клиент|заказчик|client|customer",
                "CLIENT",
                Category::Custom,
                "название после метки клиента",
            ),
        ] {
            let prefix = format!(r#"(?i:\b(?:{label}))["]?(?:[ \t]*[:=][ \t]*|[ \t]+)"#);
            out.push((
                Regex::new(&format!(
                    r#"{prefix}["«'](?P<value>[^"»'\r\n]{{3,120}})["»']"#
                ))
                .unwrap(),
                kind,
                category,
                reason,
            ));
            out.push((Regex::new(&format!(r#"{prefix}(?P<value>[\p{{Lu}}][\p{{L}}\p{{N}}_-]+(?:[ \t]+[\p{{Lu}}][\p{{L}}\p{{N}}_-]+){{0,5}}|[\p{{L}}][\p{{L}}\p{{N}}_-]+)"#)).unwrap(),kind,category,reason));
        }
        out.push((Regex::new(r#"(?i)\b(?:contract(?:[ _]id)?|customer_id|client_id|ticket|номер договора|договор|идентификатор)["]?[ \t]*[:=#][ \t]*["']?(?P<value>[\p{L}\p{N}][\p{L}\p{N}_/-]{2,79})"#).unwrap(),"ID",Category::Custom,"идентификатор после явной метки"));
        // Естественные подписи с уточнением: «Пароль базы данных: …». Это локальный
        // кандидат для подтверждения; значения Credential никогда не передаются в S1.
        out.push((Regex::new(r#"(?i)\b(?:пароль|password|passwd|passphrase)(?:[ \t]+[\p{L}][\p{L}-]*){0,5}["]?[ \t]*[:=][ \t]*["']?(?P<value>[^\s,;"']+)"#).unwrap(),"SECRET",Category::Credential,"значение после явной метки пароля"));
        out
    });
    &PATTERNS
}

fn suggestion_id(kind: &str, value: &str) -> String {
    let mut input = Vec::new();
    input.extend_from_slice(kind.as_bytes());
    input.push(0);
    input.extend_from_slice(value.to_lowercase().as_bytes());
    blake3::hash(&input).to_hex()[..16].to_string()
}

/// Применение по идентификаторам, полученным из предпросмотра. Неизвестный id — ошибка,
/// чтобы устаревший или подменённый запрос не создал правило для другого значения.
pub fn selected_rules(report: &Report, ids: &[String]) -> Result<Vec<Rule>, String> {
    let wanted: HashSet<_> = ids.iter().collect();
    if wanted.len() != ids.len() {
        return Err("duplicate suggestion ids".into());
    }
    let mut rules = Vec::new();
    for id in ids {
        let s = report
            .suggestions
            .iter()
            .find(|s| &s.id == id)
            .ok_or_else(|| format!("unknown suggestion id `{id}`"))?;
        rules.push(s.mask_rule());
    }
    Ok(rules)
}

fn amount_re() -> &'static Regex {
    static RE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)(?:\b\d+(?:[ \t\u{a0}]\d{3})*(?:[.,]\d{1,2})?[ \t\u{a0}]*(?:₽|руб(?:лей|ля)?\b\.?|(?:EUR|USD|RUB)\b|€|\$)|(?:\b(?:EUR|USD|RUB)\b|€|\$)[ \t\u{a0}]*\d+(?:[ \t\u{a0}]\d{3})*(?:[.,]\d{1,2})?\b)").unwrap()
    });
    &RE
}

/// Дополнительные признаки корпоративного профиля, не зависящие от S1.
pub fn profile_categories() -> HashMap<&'static str, Category> {
    [
        ("ORG", Category::Custom),
        ("CLIENT", Category::Custom),
        ("ID", Category::Custom),
        ("PROJ", Category::Infra),
        ("AMOUNT", Category::Financial),
        ("SECRET", Category::Credential),
        ("PERSON", Category::Identity),
        ("EMAIL", Category::Contact),
        ("PHONE", Category::Contact),
        ("ADDRESS", Category::Contact),
        ("CUSTOM", Category::Custom),
    ]
    .into_iter()
    .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn amounts_require_a_currency() {
        assert!(amount_re().is_match("budget 1 250 000,50 ₽"));
        assert!(amount_re().is_match("USD 250.00"));
        assert!(!amount_re().is_match("version 1.25 and 250 users"));
    }

    #[test]
    fn analysis_never_calls_s1() {
        use crate::s1::{Calibration, CandidateView, Disclosure, S1Config, S1Error, S1Provider};
        use crate::{AnalyzeCtx, Candidate};

        struct Detector;
        impl crate::Recognizer for Detector {
            fn id(&self) -> &str {
                "fixture"
            }

            fn analyze(&self, _: &Input<'_>, _: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
                out.push(Candidate::new(
                    0,
                    15,
                    "PROJ",
                    Category::Infra,
                    "fixture",
                    0.1,
                ));
            }
        }

        struct PanicProvider;
        impl S1Provider for PanicProvider {
            fn id(&self) -> &str {
                "must-not-be-called"
            }

            fn max_disclosure(&self) -> Disclosure {
                Disclosure::L2
            }

            fn score(&self, _: &[CandidateView]) -> Result<Vec<f32>, S1Error> {
                panic!("discover must not call S1")
            }
        }

        let engine = Engine::builder([1; 32])
            .recognizer(Box::new(Detector))
            .s1(S1Config {
                provider: Box::new(PanicProvider),
                calibration: Calibration::default(),
                weight: 1.0,
                keep_below: 0.1,
                shadow: false,
                context_bytes: 60,
                profile: Default::default(),
            })
            .build()
            .unwrap();
        let report = analyze(
            &engine,
            &[Document {
                name: "a.txt".into(),
                text: "internalproject".into(),
                format: crate::Format::Text,
                model_candidates: vec![],
            }],
        );
        assert_eq!(report.suggestions.len(), 1);
        assert_eq!(report.suggestions[0].value, "internalproject");
    }

    #[test]
    fn selection_is_explicit_and_stable() {
        let report = Report {
            suggestions: vec![Suggestion {
                id: suggestion_id("ORG", "Acme"),
                value: "Acme".into(),
                kind: "ORG".into(),
                count: 2,
                documents: 1,
                reason: "test".into(),
                sample: "a.txt".into(),
                examples: vec![],
                occurrences: BTreeMap::new(),
            }],
            ..Report::default()
        };
        assert!(selected_rules(&report, &[]).unwrap().is_empty());
        assert!(selected_rules(&report, &["bad".into()]).is_err());
        let rules = selected_rules(&report, &[report.suggestions[0].id.clone()]).unwrap();
        assert_eq!(rules[0].value.as_deref(), Some("Acme"));
        assert_eq!(rules[0].action, RuleAction::Mask);
    }
}

/// Compact, encrypted by the application: no full documents and no model-generated labels.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Profile {
    pub suggestions: Vec<Suggestion>,
    pub feedback: Vec<Feedback>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Feedback {
    pub id: String,
    pub value: String,
    pub kind: String,
    pub sensitive: bool,
}

impl Profile {
    pub fn report(&self) -> Report {
        let documents: HashSet<_> = self
            .suggestions
            .iter()
            .flat_map(|s| s.occurrences.keys())
            .collect();
        Report {
            suggestions: self.suggestions.clone(),
            documents: documents.len(),
            s1_status: "stored".into(),
            ..Report::default()
        }
    }

    pub fn merge(&mut self, report: &Report) -> Result<(), String> {
        let mut merged = self.clone();
        for incoming in &report.suggestions {
            if merged.feedback.iter().any(|f| f.id == incoming.id) {
                continue;
            }
            if let Some(current) = merged.suggestions.iter_mut().find(|s| s.id == incoming.id) {
                if model_proposed(&incoming.reason) && !model_proposed(&current.reason) {
                    current.reason.push_str("; также предложено моделью");
                }
                for (document, count) in &incoming.occurrences {
                    current
                        .occurrences
                        .entry(document.clone())
                        .and_modify(|n| *n = (*n).max(*count))
                        .or_insert(*count);
                }
                if current.occurrences.len() > 256 {
                    return Err("discovery profile limit: 256 documents per value".into());
                }
                for example in &incoming.examples {
                    if let Some(old) = current.examples.iter_mut().find(|e| {
                        e.document_id == example.document_id
                            && e.start == example.start
                            && e.end == example.end
                    }) {
                        if example.s1_score.is_some() {
                            *old = example.clone();
                        }
                    } else if current.examples.len() < MAX_EXAMPLES {
                        current.examples.push(example.clone());
                    }
                }
                recount(current);
            } else {
                merged.suggestions.push(incoming.clone());
            }
        }
        if merged.suggestions.len() > 1000 {
            return Err("discovery profile limit: 1000 suggestions".into());
        }
        sort_suggestions(&mut merged.suggestions);
        *self = merged;
        Ok(())
    }

    /// Only a human action calls this. Rejection is a training label, not a KEEP rule.
    pub fn decide(
        &mut self,
        report: &Report,
        ids: &[String],
        sensitive: bool,
    ) -> Result<(), String> {
        let selected = selected_suggestions(report, ids)?;
        let mut next = self.clone();
        for s in selected {
            next.feedback.retain(|f| f.id != s.id);
            next.feedback.push(Feedback {
                id: s.id.clone(),
                value: s.value.clone(),
                kind: s.kind.clone(),
                sensitive,
            });
            next.suggestions.retain(|p| p.id != s.id);
        }
        if next.feedback.len() > 10000 {
            return Err("discovery profile limit: 10000 labels".into());
        }
        *self = next;
        Ok(())
    }
}

pub fn selected_suggestions<'a>(
    report: &'a Report,
    ids: &[String],
) -> Result<Vec<&'a Suggestion>, String> {
    let wanted: HashSet<_> = ids.iter().collect();
    if wanted.len() != ids.len() {
        return Err("duplicate suggestion ids".into());
    }
    ids.iter()
        .map(|id| {
            report
                .suggestions
                .iter()
                .find(|s| &s.id == id)
                .ok_or_else(|| format!("unknown suggestion id `{id}`"))
        })
        .collect()
}

#[cfg(test)]
mod precision_regressions {
    use super::*;

    #[test]
    fn ordinary_words_and_public_model_ids_are_not_business_suggestions() {
        let engine = Engine::builder([0; 32]).build().unwrap();
        let report = analyze(&engine, &[Document {
            name: "prose".into(),
            text: "project character\nproject questions\nпроект или\nproject gemini-3.8-flash\npassword: ...\nПроект: Лунная Слива".into(),
            format: crate::Format::Text, model_candidates: Vec::new(),
        }]);
        assert_eq!(report.suggestions.len(), 1, "{report:?}");
        assert_eq!(report.suggestions[0].value, "Лунная Слива");
    }
}
