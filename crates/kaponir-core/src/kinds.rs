//! Shared semantics for standard token kinds. Custom kinds remain supported.
use crate::Category;
use regex::Regex;
use std::sync::OnceLock;
// The first 16 kinds preserve the compact discovery schema. Append fine-grained
// categories after AMOUNT; changing the discovery vocabulary requires quality evaluation.
pub const STANDARD: &[&str] = &[
    "PROJ", "SVC", "HOST", "SECRET", "DOM", "ORG", "CLIENT", "PERSON", "USER", "MODULE", "CUSTOM",
    "EMAIL", "PHONE", "IP", "PATH", "AMOUNT", "SUB", "FNAME", "MNAME", "LNAME", "INN", "SNILS",
    "OGRN", "BIK", "ACCT", "PASS",
];
pub fn category(kind: &str) -> Category {
    match kind {
        "SECRET" | "PASSWORD" | "TOKEN" | "KEY" => Category::Credential,
        "PERSON" | "FNAME" | "MNAME" | "LNAME" | "USER" | "PASS" | "SNILS" => Category::Identity,
        "EMAIL" | "PHONE" => Category::Contact,
        "AMOUNT" | "ACCT" | "BIK" => Category::Financial,
        "IP" | "HOST" | "DOM" | "SUB" => Category::Network,
        "PROJ" | "SVC" | "MODULE" | "PATH" | "ORG" | "CLIENT" => Category::Infra,
        _ => Category::Custom,
    }
}
pub fn financial_value(value: &str) -> bool {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN
        .get_or_init(|| {
            Regex::new(r"(?i)^[+-]?\d(?:[\d\s,.]*\d)?\s*(?:₽|руб\.?|RUB|USD|EUR|[$€%])$")
                .expect("financial format")
        })
        .is_match(value.trim())
}
pub fn type_conflict(kind: &str, value: &str) -> bool {
    (financial_value(value) && !matches!(kind, "AMOUNT" | "CUSTOM"))
        || matches!(kind, "PERSON" | "FNAME" | "LNAME" | "MNAME")
            && value.chars().any(|c| c.is_ascii_digit())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn amounts_and_percentages_cannot_be_people_or_services() {
        for value in ["40%", "1 320,00 ₽", "3\u{a0}292\u{a0}699,92 ₽"] {
            assert!(financial_value(value));
            assert!(type_conflict("PERSON", value));
            assert!(type_conflict("SVC", value));
            assert!(!type_conflict("AMOUNT", value));
        }
        assert!(!financial_value("billing-api-7"));
        assert!(!type_conflict("PROJ", "Atlas 7"));
        assert!(type_conflict("PERSON", "96 линий"));
        assert!(!type_conflict("PERSON", "Thu Lan Nguyen"));
    }
}
