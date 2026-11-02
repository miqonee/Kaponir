//! Журнал решений: спорные случаи для разбора и будущего обучения.

use serde::{Deserialize, Serialize};

use crate::score::Verdict;
use crate::types::{Category, Evidence};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct JournalEntry {
    pub kind: String,
    pub category: Category,
    pub recognizer: String,
    pub value: String,
    pub shape: String,
    /// Окно вокруг значения; остальные найденные значения в нём уже заменены на `[KIND]`.
    pub context: String,
    pub probability: f32,
    /// Что применено по политике.
    pub applied: Verdict,
    pub evidence: Vec<Evidence>,
}

/// Форма строки в духе Drain: `prod-billing-7d9f8b` → `x-x-dxdx`.
/// Буквы → `x`/`X`, цифры → `d`, серии одного класса схлопываются.
pub fn shape(s: &str) -> String {
    let mut out = String::new();
    let mut last: Option<char> = None;
    for c in s.chars() {
        let cls = if c.is_ascii_digit() {
            'd'
        } else if c.is_uppercase() {
            'X'
        } else if c.is_alphabetic() {
            'x'
        } else {
            c
        };
        if last != Some(cls) || !matches!(cls, 'd' | 'x' | 'X') {
            out.push(cls);
        }
        last = Some(cls);
    }
    out
}

#[cfg(test)]
mod tests {
    #[test]
    fn shapes() {
        assert_eq!(super::shape("prod-billing-7d9f8b"), "x-x-dxdxdx");
        assert_eq!(super::shape("SberPay"), "XxXx");
        assert_eq!(super::shape("10.0.0.1"), "d.d.d.d");
    }
}
