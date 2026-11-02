//! Универсальные валидаторы контрольных сумм: банковские карты (алгоритм Луна) и IBAN (mod 97).

use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Recognizer};
use regex::Regex;

use crate::word_bounded;

const ID: &str = "validators";

pub fn luhn(digits: &[u8]) -> bool {
    let mut sum = 0u32;
    for (i, &d) in digits.iter().rev().enumerate() {
        let mut v = u32::from(d);
        if i % 2 == 1 {
            v *= 2;
            if v > 9 {
                v -= 9;
            }
        }
        sum += v;
    }
    sum.is_multiple_of(10)
}

pub fn iban_valid(s: &str) -> bool {
    let compact: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    if compact.len() < 15 || compact.len() > 34 {
        return false;
    }
    let rearranged = format!("{}{}", &compact[4..], &compact[..4]);
    let mut rem = 0u32;
    for c in rearranged.chars() {
        let v = match c {
            '0'..='9' => c as u32 - '0' as u32,
            'A'..='Z' => c as u32 - 'A' as u32 + 10,
            _ => return false,
        };
        rem = if v >= 10 {
            (rem * 100 + v) % 97
        } else {
            (rem * 10 + v) % 97
        };
    }
    rem == 1
}

/// Известные префиксы платёжных систем (Visa, Mastercard, МИР, Amex, UnionPay, JCB, Discover).
fn card_prefix(d: &[u8]) -> bool {
    let n = |k: usize| d[..k].iter().fold(0u32, |a, &x| a * 10 + u32::from(x));
    d[0] == 4
        || (51..=55).contains(&n(2))
        || (2221..=2720).contains(&n(4))
        || (2200..=2204).contains(&n(4))
        || n(2) == 34
        || n(2) == 37
        || n(2) == 62
        || n(2) == 35
        || n(4) == 6011
        || n(2) == 65
}

pub struct Validators {
    card: Regex,
    iban: Regex,
}

impl Default for Validators {
    fn default() -> Self {
        Self::new()
    }
}

impl Validators {
    pub fn new() -> Self {
        Self {
            card: Regex::new(r"\d(?:[ \-]?\d){12,18}").expect("valid regex"),
            iban: Regex::new(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
                .expect("valid regex"),
        }
    }
}

impl Recognizer for Validators {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        for m in self.card.find_iter(text) {
            if !word_bounded(text, m.start(), m.end()) {
                continue;
            }
            let digits: Vec<u8> = m
                .as_str()
                .bytes()
                .filter(u8::is_ascii_digit)
                .map(|b| b - b'0')
                .collect();
            if !(13..=19).contains(&digits.len()) || !luhn(&digits) {
                continue;
            }
            let c = Candidate::new(m.start(), m.end(), "CARD", Category::Financial, ID, 0.6)
                .with_evidence("luhn", 0.5);
            out.push(if card_prefix(&digits) {
                c.hard().with_evidence("card-prefix", 1.5)
            } else {
                c
            });
        }
        for m in self.iban.find_iter(text) {
            if iban_valid(m.as_str()) {
                out.push(
                    Candidate::new(m.start(), m.end(), "IBAN", Category::Financial, ID, 0.95)
                        .hard()
                        .with_evidence("mod97", 1.0),
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksums() {
        assert!(luhn(&[4, 5, 3, 9, 1, 4, 8, 8, 0, 3, 4, 3, 6, 4, 6, 7]));
        assert!(!luhn(&[4, 5, 3, 9, 1, 4, 8, 8, 0, 3, 4, 3, 6, 4, 6, 8]));
        assert!(iban_valid("GB82 WEST 1234 5698 7654 32"));
        assert!(!iban_valid("GB82 WEST 1234 5698 7654 33"));
    }

    #[test]
    fn finds_cards() {
        let l = kaponir_core::Lexicon::default();
        let text = "card 4539 1488 0343 6467, order 4539148803436468";
        let mut out = Vec::new();
        Validators::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        assert_eq!(out.len(), 1);
        assert!(out[0].hard);
    }
}
