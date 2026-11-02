//! Региональный пакет РФ: ИНН, СНИЛС, ОГРН/ОГРНИП, БИК, расчётные и корр. счета, паспорт, телефоны.
//! Контрольные суммы проверяются математически; вес повышают слова контекста («ИНН», «р/с»).

use kaponir_core::{
    AnalyzeCtx, Candidate, Category, Input, Lexicon, Pack, PackInfo, PackKind, Recognizer,
};
use regex::Regex;
use std::sync::Arc;

const ID: &str = "region-ru";

pub fn inn_valid(d: &[u8]) -> bool {
    let check = |w: &[u32], n: usize| -> bool {
        let s: u32 = w.iter().zip(d).map(|(w, &x)| w * u32::from(x)).sum();
        ((s % 11) % 10) as u8 == d[n]
    };
    match d.len() {
        10 => check(&[2, 4, 10, 3, 5, 9, 4, 6, 8], 9),
        12 => {
            check(&[7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 10)
                && check(&[3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 11)
        }
        _ => false,
    }
}

pub fn snils_valid(d: &[u8]) -> bool {
    if d.len() != 11 {
        return false;
    }
    let num: u32 = d[..9].iter().fold(0, |a, &x| a * 10 + u32::from(x));
    if num <= 1_001_998 {
        return false;
    }
    let sum: u32 = d[..9]
        .iter()
        .enumerate()
        .map(|(i, &x)| u32::from(x) * (9 - i as u32))
        .sum();
    let check = match sum {
        s if s < 100 => s,
        100 | 101 => 0,
        s => {
            let r = s % 101;
            if r == 100 { 0 } else { r }
        }
    };
    check == u32::from(d[9]) * 10 + u32::from(d[10])
}

pub fn ogrn_valid(d: &[u8]) -> bool {
    let num = |k: usize| d[..k].iter().fold(0u64, |a, &x| a * 10 + u64::from(x));
    match d.len() {
        13 => (num(12) % 11 % 10) as u8 == d[12],
        15 => (num(14) % 13 % 10) as u8 == d[14],
        _ => false,
    }
}

/// Контрольный ключ счёта по БИК (Положение ЦБ № 579-П): `prefix` — 3 цифры, затем 20 цифр счёта.
pub fn account_valid(bik: &[u8], account: &[u8], correspondent: bool) -> bool {
    if bik.len() != 9 || account.len() != 20 {
        return false;
    }
    let mut seq: Vec<u8> = if correspondent {
        vec![0, bik[4], bik[5]]
    } else {
        bik[6..9].to_vec()
    };
    seq.extend_from_slice(account);
    let w = [7u32, 1, 3];
    seq.iter()
        .enumerate()
        .map(|(i, &x)| (u32::from(x) * w[i % 3]) % 10)
        .sum::<u32>()
        % 10
        == 0
}

/// Балансовые счета, которые почти всегда означают реквизиты.
const ACCOUNT_PREFIXES: &[&str] = &[
    "40702", "40701", "40703", "40802", "40817", "40820", "42301", "42305", "30101", "30109",
    "30110", "40807",
];

fn digits(s: &str) -> Vec<u8> {
    s.bytes()
        .filter(u8::is_ascii_digit)
        .map(|b| b - b'0')
        .collect()
}

pub struct RuRecognizer {
    number: Regex,
    snils: Regex,
    passport: Regex,
    phone: Regex,
}

impl Default for RuRecognizer {
    fn default() -> Self {
        Self::new()
    }
}

impl RuRecognizer {
    pub fn new() -> Self {
        Self {
            number: Regex::new(r"\d{9,20}").expect("valid regex"),
            snils: Regex::new(r"\d{3}-\d{3}-\d{3}[ \-]\d{2}").expect("valid regex"),
            passport: Regex::new(r"\d{2} ?\d{2} \d{6}").expect("valid regex"),
            phone: Regex::new(r"(?:\+7|\b8)[ \-]?\(?\d{3}\)?[ \-]?\d{3}[ \-]?\d{2}[ \-]?\d{2}")
                .expect("valid regex"),
        }
    }
}

fn isolated(text: &str, s: usize, e: usize) -> bool {
    !text[..s]
        .chars()
        .next_back()
        .is_some_and(|c| c.is_alphanumeric())
        && !text[e..]
            .chars()
            .next()
            .is_some_and(|c| c.is_alphanumeric())
}

impl Recognizer for RuRecognizer {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        let numbers: Vec<(usize, usize, Vec<u8>)> = self
            .number
            .find_iter(text)
            .filter(|m| isolated(text, m.start(), m.end()))
            .map(|m| (m.start(), m.end(), digits(m.as_str())))
            .collect();
        let biks: Vec<&Vec<u8>> = numbers
            .iter()
            .filter(|(_, _, d)| d.len() == 9 && d[0] == 0 && d[1] == 4)
            .map(|(_, _, d)| d)
            .collect();

        for (s, e, d) in &numbers {
            let (s, e) = (*s, *e);
            let c = |kind: &str, cat, prior, ev: &str| {
                Candidate::new(s, e, kind, cat, ID, prior).with_evidence(ev, 0.0)
            };
            match d.len() {
                9 if d[0] == 0 && d[1] == 4 => {
                    out.push(c("BIK", Category::Financial, 0.4, "bik-format"))
                }
                10 if inn_valid(d) => out.push(c("INN", Category::Identity, 0.35, "inn-checksum")),
                12 if inn_valid(d) => out.push(c("INN", Category::Identity, 0.6, "inn-checksum")),
                11 if snils_valid(d) => {
                    out.push(c("SNILS", Category::Identity, 0.4, "snils-checksum"))
                }
                13 | 15 if ogrn_valid(d) => {
                    out.push(c("OGRN", Category::Financial, 0.45, "ogrn-checksum"))
                }
                20 => {
                    let s20: String = d.iter().map(|x| char::from(b'0' + x)).collect();
                    let corr = s20.starts_with("301");
                    if biks.iter().any(|b| account_valid(b, d, corr)) {
                        out.push(c("ACCOUNT", Category::Financial, 0.95, "account-bik-key").hard());
                    } else if ACCOUNT_PREFIXES.iter().any(|p| s20.starts_with(p)) {
                        out.push(c("ACCOUNT", Category::Financial, 0.75, "account-prefix"));
                    }
                }
                _ => {}
            }
        }
        for m in self.snils.find_iter(text) {
            if isolated(text, m.start(), m.end()) && snils_valid(&digits(m.as_str())) {
                out.push(
                    Candidate::new(m.start(), m.end(), "SNILS", Category::Identity, ID, 0.9)
                        .hard()
                        .with_evidence("snils-checksum", 0.0),
                );
            }
        }
        for m in self.passport.find_iter(text) {
            if isolated(text, m.start(), m.end()) {
                out.push(Candidate::new(
                    m.start(),
                    m.end(),
                    "PASSPORT",
                    Category::Identity,
                    ID,
                    0.5,
                ));
            }
        }
        for m in self.phone.find_iter(text) {
            if isolated(text, m.start(), m.end()) || m.as_str().starts_with('+') {
                out.push(Candidate::new(
                    m.start(),
                    m.end(),
                    "PHONE",
                    Category::Contact,
                    ID,
                    0.85,
                ));
            }
        }
    }
}

pub struct RegionRu {
    info: PackInfo,
    lexicon: Lexicon,
}

impl Default for RegionRu {
    fn default() -> Self {
        Self::new()
    }
}

impl RegionRu {
    pub fn new() -> Self {
        let mut lexicon = Lexicon::default();
        let ctx: &[(&str, &[&str])] = &[
            ("INN", &["инн", "inn", "налогоплательщика", "кпп"]),
            ("SNILS", &["снилс", "snils", "страховой"]),
            ("OGRN", &["огрн", "огрнип", "ogrn"]),
            ("BIK", &["бик", "bik", "банк"]),
            (
                "ACCOUNT",
                &[
                    "р/с",
                    "к/с",
                    "р/сч",
                    "счет",
                    "счёт",
                    "расчетный",
                    "расчётный",
                    "корреспондентский",
                    "реквизиты",
                ],
            ),
            ("PASSPORT", &["паспорт", "выдан", "серия", "паспорта"]),
            ("PHONE", &["тел", "телефон", "моб"]),
        ];
        for (kind, words) in ctx {
            lexicon.context.insert(
                (*kind).into(),
                words.iter().map(|w| (*w).to_string()).collect(),
            );
        }
        for (k, v) in [
            ("инн", "INN"),
            ("inn", "INN"),
            ("снилс", "SNILS"),
            ("snils", "SNILS"),
            ("огрн", "OGRN"),
            ("бик", "BIK"),
        ] {
            lexicon.keys.insert(k.into(), v.into());
        }
        Self {
            info: PackInfo {
                id: ID.into(),
                kind: PackKind::Region,
                version: "0.1.0".into(),
                api_version: kaponir_core::PACK_API_VERSION,
                scripts: vec![],
                license: "Apache-2.0".into(),
                description: "РФ: ИНН, СНИЛС, ОГРН, БИК, счета, паспорт, телефоны".into(),
            },
            lexicon,
        }
    }

    pub fn arc() -> Arc<dyn Pack> {
        Arc::new(Self::new())
    }
}

impl Pack for RegionRu {
    fn info(&self) -> &PackInfo {
        &self.info
    }

    fn lexicon(&self) -> &Lexicon {
        &self.lexicon
    }

    fn recognizers(&self) -> Vec<Box<dyn Recognizer>> {
        vec![Box::new(RuRecognizer::new())]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksums() {
        assert!(inn_valid(&digits("7707083893"))); // ПАО Сбербанк
        assert!(!inn_valid(&digits("7707083894")));
        assert!(inn_valid(&digits("500100732259")));
        assert!(snils_valid(&digits("112-233-445 95")));
        assert!(!snils_valid(&digits("112-233-445 96")));
        assert!(ogrn_valid(&digits("1027700132195"))); // ОГРН Сбербанка
        assert!(ogrn_valid(&digits("304500116000157")));
        // Корр. счёт Сбербанка и его БИК.
        assert!(account_valid(
            &digits("044525225"),
            &digits("30101810400000000225"),
            true
        ));
        assert!(!account_valid(
            &digits("044525225"),
            &digits("30101810400000000226"),
            true
        ));
    }

    #[test]
    fn finds_requisites() {
        let l = Lexicon::default();
        let text = "ИНН 7707083893, БИК 044525225, к/с 30101810400000000225, СНИЛС 112-233-445 95, тел. 8 (912) 345-67-89";
        let mut out = Vec::new();
        RuRecognizer::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        let kinds: Vec<&str> = out.iter().map(|c| c.kind.as_str()).collect();
        for k in ["INN", "BIK", "ACCOUNT", "SNILS", "PHONE"] {
            assert!(kinds.contains(&k), "{k} not in {kinds:?}");
        }
        assert!(out.iter().find(|c| c.kind == "ACCOUNT").unwrap().hard);
    }
}
