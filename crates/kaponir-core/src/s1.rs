//! Подключаемый провайдер вероятностей (System 1). По умолчанию отсутствует:
//! система полностью детерминирована и работает без него.

use serde::{Deserialize, Serialize};
use std::sync::Arc;

use crate::rules::ScopedRules;
use crate::score::{logit, sigmoid};
use crate::types::Category;

/// Что разрешено видеть провайдеру.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Disclosure {
    /// Только признаки, без исходной строки.
    L0,
    /// Строка-кандидат без контекста.
    L1,
    /// Кандидат + окно контекста, где остальные найденные значения уже замаскированы.
    L2,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Features {
    pub len: u32,
    pub entropy: f32,
    pub shape: String,
    pub digit_ratio: f32,
    pub upper_ratio: f32,
    pub is_word: bool,
    /// Хеши символьных триграмм (blake3, 32 бита).
    pub trigrams: Vec<u32>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProfileValue {
    pub kind: String,
    pub value: String,
}

/// Only explicitly recognized business/infrastructure entity kinds may be shared
/// as additional L2 profile values. Unknown kinds fail closed.
fn profile_kind_allowed(kind: &str) -> bool {
    matches!(
        kind.trim().to_ascii_uppercase().as_str(),
        "ORG" | "PROJ" | "AMOUNT" | "CLIENT" | "SVC" | "HOST" | "DOM" | "MODULE" | "ID"
    )
}

/// Defense at provider boundaries, including manually constructed S1Config values.
/// An excluded value must not reappear under a different, permitted kind.
pub fn disclosure_profile(profile: &[ProfileValue]) -> Vec<ProfileValue> {
    let excluded: Vec<_> = profile
        .iter()
        .filter(|p| !profile_kind_allowed(&p.kind))
        .map(|p| p.value.trim().to_lowercase())
        .filter(|v| !v.is_empty())
        .collect();
    profile
        .iter()
        .filter(|p| {
            let value = p.value.to_lowercase();
            profile_kind_allowed(&p.kind)
                && !value.trim().is_empty()
                && !excluded.iter().any(|secret| value.contains(secret))
        })
        .cloned()
        .collect()
}

/// Exact MASK rules for the L2 profile, excluding credentials and unknown kinds.
pub fn confirmed_profile(rules: &[ScopedRules]) -> Vec<ProfileValue> {
    let excluded: Vec<_> = rules
        .iter()
        .flat_map(|s| &s.file.rules)
        .filter(|r| r.category == Some(Category::Credential) || !profile_kind_allowed(&r.kind))
        .filter_map(|r| r.value.as_ref())
        .map(|v| v.trim().to_lowercase())
        .filter(|v| !v.is_empty())
        .collect();
    let mut values = std::collections::BTreeMap::new();
    for set in rules {
        for rule in &set.file.rules {
            if rule.action != crate::rules::RuleAction::Mask
                || rule.category == Some(Category::Credential)
                || !profile_kind_allowed(&rule.kind)
            {
                continue;
            }
            let Some(value) = rule
                .value
                .as_ref()
                .map(|v| v.trim())
                .filter(|v| !v.is_empty())
            else {
                continue;
            };
            let lower = value.to_lowercase();
            if excluded.iter().any(|secret| lower.contains(secret)) {
                continue;
            }
            let kind = rule.kind.trim().to_ascii_uppercase();
            values
                .entry((kind.clone(), lower))
                .or_insert_with(|| ProfileValue {
                    kind,
                    value: value.to_string(),
                });
        }
    }
    values.into_values().collect()
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CandidateView {
    pub kind: String,
    pub category: Category,
    pub recognizer: String,
    pub features: Features,
    pub surface: Option<String>,
    pub context: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum S1Error {
    #[error("timeout")]
    Timeout,
    #[error("{0}")]
    Other(String),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RuleClassification {
    pub action: crate::rules::RuleAction,
    pub scope: crate::types::Scope,
    pub kind: String,
    pub confidence: f32,
    pub source: String,
}

/// Маршрутизация контура правил по умолчанию на основе типа сущности (`kind`) и контекста.
pub fn default_scope_for_kind(kind: &str) -> crate::types::Scope {
    match kind.trim().to_ascii_uppercase().as_str() {
        "DOM" | "ORG" | "CLIENT" => crate::types::Scope::Global,
        "PROJ" | "SVC" | "HOST" | "MODULE" | "PATH" | "SUB" => crate::types::Scope::Team,
        _ => crate::types::Scope::Personal,
    }
}

/// Эвристическая классификация кандидата (для режима No-LLM и как базовая оценка).
pub fn classify_from_score(
    view: &CandidateView,
    probability: f32,
    source: impl Into<String>,
) -> RuleClassification {
    let p = probability.clamp(0.0, 1.0);
    let action = if p >= 0.5 {
        crate::rules::RuleAction::Mask
    } else {
        crate::rules::RuleAction::Keep
    };
    let confidence = if p >= 0.5 { p } else { 1.0 - p };
    let mut kind = view.kind.trim().to_ascii_uppercase();
    if kind.is_empty() {
        kind = "CUSTOM".into();
    }
    let mut scope = default_scope_for_kind(&kind);
    if let Some(ctx) = &view.context {
        let low = ctx.to_lowercase();
        if low.contains("корпоратив")
            || low.contains("компани")
            || low.contains("заказчик")
            || low.contains("клиент")
            || low.contains("домен")
        {
            scope = crate::types::Scope::Global;
        } else if low.contains("команд")
            || low.contains("сервис")
            || low.contains("репозитор")
            || low.contains("кластер")
            || low.contains("деплой")
        {
            scope = crate::types::Scope::Team;
        } else if low.contains("личн") || low.contains("мой ") || low.contains("пароль") {
            scope = crate::types::Scope::Personal;
        }
    }
    RuleClassification {
        action,
        scope,
        kind,
        confidence,
        source: source.into(),
    }
}

pub trait S1Provider: Send + Sync {
    fn id(&self) -> &str;
    fn max_disclosure(&self) -> Disclosure;
    /// Вероятность чувствительности для каждого кандидата батча.
    fn score(&self, batch: &[CandidateView]) -> Result<Vec<f32>, S1Error>;
    /// Профиль доступен только при L2; провайдеры без его поддержки сохраняют прежнее поведение.
    fn score_with_profile(
        &self,
        batch: &[CandidateView],
        _profile: &[ProfileValue],
    ) -> Result<Vec<f32>, S1Error> {
        self.score(batch)
    }
    /// Классификация правила (действие, контур `personal`/`team`/`global`, тип `kind`, уверенность).
    fn classify(
        &self,
        batch: &[CandidateView],
        profile: &[ProfileValue],
    ) -> Result<Vec<RuleClassification>, S1Error> {
        let scores = self.score_with_profile(batch, profile)?;
        Ok(batch
            .iter()
            .zip(scores)
            .map(|(v, p)| classify_from_score(v, p, self.id()))
            .collect())
    }
}

/// Калибровка Платта: `p' = σ(a·logit(p) + b)`.
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct Calibration {
    pub a: f32,
    pub b: f32,
}

impl Default for Calibration {
    fn default() -> Self {
        Self { a: 1.0, b: 0.0 }
    }
}

impl Calibration {
    pub fn apply(&self, p: f32) -> f32 {
        sigmoid(self.a * logit(p) + self.b)
    }
}

pub struct S1Config {
    pub provider: Box<dyn S1Provider>,
    pub calibration: Calibration,
    /// Вес слагаемого S1 в log-odds.
    pub weight: f32,
    /// Ослаблять (не маскировать) S1 может только при `p ≤ keep_below`.
    pub keep_below: f32,
    /// Теневой режим: оценка пишется в свидетельства с нулевым весом и решение не меняет.
    /// Так проверяют новую версию модели перед включением.
    pub shadow: bool,
    /// Байты UTF-8 до и после кандидата на L2.
    pub context_bytes: usize,
    /// Явные MASK-правила; движок передаёт их только провайдеру L2.
    pub profile: Arc<Vec<ProfileValue>>,
}

pub fn shannon_entropy(s: &str) -> f32 {
    let mut counts = std::collections::HashMap::new();
    let mut n = 0f32;
    for c in s.chars() {
        *counts.entry(c).or_insert(0usize) += 1;
        n += 1.0;
    }
    if n == 0.0 {
        return 0.0;
    }
    // Одинаковые частоты должны давать одинаковые биты f32, независимо от порядка HashMap.
    let mut frequencies: Vec<_> = counts.into_values().collect();
    frequencies.sort_unstable();
    frequencies
        .into_iter()
        .map(|k| {
            let p = k as f32 / n;
            -p * p.log2()
        })
        .sum()
}

pub fn features(surface: &str, is_word: bool) -> Features {
    let chars: Vec<char> = surface.chars().collect();
    let n = chars.len().max(1) as f32;
    let lower = surface.to_lowercase();
    let lc: Vec<char> = lower.chars().collect();
    let trigrams = lc
        .windows(3)
        .map(|w| {
            let s: String = w.iter().collect();
            let h = blake3::hash(s.as_bytes());
            u32::from_le_bytes(h.as_bytes()[..4].try_into().expect("4 bytes"))
        })
        .collect();
    Features {
        len: chars.len() as u32,
        entropy: shannon_entropy(surface),
        shape: crate::journal::shape(surface),
        digit_ratio: chars.iter().filter(|c| c.is_ascii_digit()).count() as f32 / n,
        upper_ratio: chars.iter().filter(|c| c.is_uppercase()).count() as f32 / n,
        is_word,
        trigrams,
    }
}

#[cfg(test)]
mod tests {
    use super::shannon_entropy;

    #[test]
    fn entropy_is_repeatable_and_depends_only_on_character_counts() {
        for text in [
            "polar-ledger",
            "kedrledger",
            "Анонимизация 123",
            "abcabcdddd",
        ] {
            let expected = shannon_entropy(text);
            let reversed: String = text.chars().rev().collect();
            for _ in 0..128 {
                assert_eq!(shannon_entropy(text), expected, "repeated input: {text}");
                assert_eq!(
                    shannon_entropy(&reversed),
                    expected,
                    "same character frequencies: {text}"
                );
            }
        }
        assert_eq!(shannon_entropy(""), 0.0);
        assert_eq!(shannon_entropy("яяяя"), 0.0);
        assert_eq!(shannon_entropy("ab"), 1.0);
        assert_eq!(shannon_entropy("abcd"), 2.0);
    }
}
