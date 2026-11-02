//! Объединённый словарь, который собирается из пакетов и правил.
//! Ядро не знает ни одного языка: все слова, суффиксы и контекст приходят извне.

use std::collections::{HashMap, HashSet};

use crate::pack::Gender;

#[derive(Clone, Debug, Default)]
pub struct Lexicon {
    /// Безопасные слова в нижнем регистре (allowlist для структурных слотов).
    pub words: HashSet<String>,
    /// Окончания словоформ: `s`, `ing`, `ies>y` (отрезать `ies`, добавить `y`).
    pub inflections: Vec<String>,
    pub tlds: HashSet<String>,
    /// TLD, совпадающие с расширениями файлов и словами (`rs`, `py`, `me`).
    pub ambiguous_tlds: HashSet<String>,
    /// Публичные суффиксы из нескольких меток (`co.uk`, `github.io`).
    pub suffixes: HashSet<String>,
    /// Внутренние зоны без регистрируемого домена (`cluster.local`, `corp`).
    pub internal_suffixes: HashSet<String>,
    /// Регистрируемые домены, которые не маскируются (`github.com`).
    pub public_domains: HashSet<String>,
    /// Корпоративные домены — маскируются всегда.
    pub corp_domains: HashSet<String>,
    /// KIND → слова контекста, повышающие вес кандидата.
    pub context: HashMap<String, HashSet<String>>,
    /// Слово в имени ключа конфига → KIND значения (`password` → `SECRET`).
    pub keys: HashMap<String, String>,
    /// Признаки имён людей.
    pub names: NameLexicon,
}

/// Признаки имён людей из языковых пакетов. Детектор имён общий; пакет поставляет списки и веса.
#[derive(Clone, Debug, Default)]
pub struct NameLexicon {
    /// Имена в начальной форме, нижний регистр (`ё` → `е`) → род, если известен.
    pub given: HashMap<String, Option<Gender>>,
    /// Окончания фамилий (`ов`, `ская`, `enko`) с родом, если он из окончания следует.
    pub surname_suffixes: Vec<(String, Option<Gender>)>,
    /// Окончания отчеств (`ович`, `овна`).
    pub patronymic_suffixes: Vec<(String, Option<Gender>)>,
    /// Обращения и должности перед именем (`mr`, `dr`, `господин`).
    pub titles: HashSet<String>,
    /// Частицы внутри имени в нижнем регистре (`van`, `de`, `фон`, `оглы`).
    pub particles: HashSet<String>,
    /// Имена и фамилии, совпадающие с обычными словами и названиями (`Will`, `Вера`, `Ростов`).
    pub collisions: HashSet<String>,
    /// Веса признаков в log-odds: `given`, `patronymic`, `surname`, `title`, `initials`, …
    pub weights: HashMap<String, f32>,
}

impl NameLexicon {
    pub fn merge(&mut self, o: &NameLexicon) {
        for (k, g) in &o.given {
            let e = self.given.entry(k.clone()).or_insert(*g);
            if e.is_none() {
                *e = *g;
            }
        }
        for s in &o.surname_suffixes {
            if !self.surname_suffixes.contains(s) {
                self.surname_suffixes.push(s.clone());
            }
        }
        for s in &o.patronymic_suffixes {
            if !self.patronymic_suffixes.contains(s) {
                self.patronymic_suffixes.push(s.clone());
            }
        }
        self.titles.extend(o.titles.iter().cloned());
        self.particles.extend(o.particles.iter().cloned());
        self.collisions.extend(o.collisions.iter().cloned());
        for (k, w) in &o.weights {
            self.weights.entry(k.clone()).or_insert(*w);
        }
    }

    pub fn weight(&self, feature: &str, default: f32) -> f32 {
        self.weights.get(feature).copied().unwrap_or(default)
    }

    /// Род имени из списка; `None` — имени нет в списке. `Some(None)` — род неизвестен.
    pub fn given(&self, lemma: &str) -> Option<Option<Gender>> {
        self.given.get(&name_key(lemma)).copied()
    }

    /// Самое длинное подходящее окончание из списка (основа — не короче двух букв).
    fn suffix_match(list: &[(String, Option<Gender>)], lemma: &str) -> Option<Option<Gender>> {
        let l = name_key(lemma);
        let n = l.chars().count();
        list.iter()
            .filter(|(s, _)| l.ends_with(s.as_str()) && n >= s.chars().count() + 2)
            .max_by_key(|(s, _)| s.chars().count())
            .map(|(_, g)| *g)
    }

    pub fn surname(&self, lemma: &str) -> Option<Option<Gender>> {
        Self::suffix_match(&self.surname_suffixes, lemma)
    }

    pub fn patronymic(&self, lemma: &str) -> Option<Option<Gender>> {
        Self::suffix_match(&self.patronymic_suffixes, lemma)
    }

    pub fn is_collision(&self, word: &str) -> bool {
        self.collisions.contains(&name_key(word))
    }

    pub fn is_empty(&self) -> bool {
        self.given.is_empty()
            && self.surname_suffixes.is_empty()
            && self.patronymic_suffixes.is_empty()
    }
}

/// Ключ имени: нижний регистр, `ё` → `е` (пишут по-разному).
pub fn name_key(s: &str) -> String {
    s.to_lowercase().replace('ё', "е")
}

/// Класс части составного идентификатора.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PartClass {
    /// Слово из словаря.
    Known,
    /// Числа, хеши, короткие технические части — сохраняются.
    Opaque,
    Unknown,
}

impl Lexicon {
    pub fn merge(&mut self, o: &Lexicon) {
        self.words.extend(o.words.iter().cloned());
        for inf in &o.inflections {
            if !self.inflections.contains(inf) {
                self.inflections.push(inf.clone());
            }
        }
        self.tlds.extend(o.tlds.iter().cloned());
        self.ambiguous_tlds.extend(o.ambiguous_tlds.iter().cloned());
        self.suffixes.extend(o.suffixes.iter().cloned());
        self.internal_suffixes
            .extend(o.internal_suffixes.iter().cloned());
        self.public_domains.extend(o.public_domains.iter().cloned());
        self.corp_domains.extend(o.corp_domains.iter().cloned());
        for (kind, words) in &o.context {
            self.context
                .entry(kind.clone())
                .or_default()
                .extend(words.iter().cloned());
        }
        for (k, v) in &o.keys {
            self.keys.entry(k.clone()).or_insert_with(|| v.clone());
        }
        self.names.merge(&o.names);
    }

    pub fn is_word(&self, w: &str) -> bool {
        let l = w.to_lowercase();
        if self.words.contains(&l) {
            return true;
        }
        for inf in &self.inflections {
            let (cut, add) = inf.split_once('>').unwrap_or((inf.as_str(), ""));
            if let Some(stem) = l.strip_suffix(cut) {
                let stem = format!("{stem}{add}");
                if stem.chars().count() >= 3 && self.words.contains(&stem) {
                    return true;
                }
            }
        }
        false
    }

    pub fn context_for(&self, kind: &str) -> Option<&HashSet<String>> {
        self.context.get(kind)
    }

    pub fn key_kind(&self, word: &str) -> Option<&str> {
        self.keys.get(&word.to_lowercase()).map(String::as_str)
    }

    /// Классифицирует одну часть идентификатора (без разделителей `-_.`).
    pub fn classify(&self, part: &str) -> PartClass {
        let len = part.chars().count();
        if len <= 2 {
            return PartClass::Opaque;
        }
        let digits = part.chars().filter(|c| c.is_ascii_digit()).count();
        if digits == len {
            return PartClass::Opaque;
        }
        if digits > 0 {
            if len <= 6 && part.is_ascii() {
                return PartClass::Opaque;
            }
            if len >= 5 && part.chars().all(|c| c.is_ascii_hexdigit()) {
                return PartClass::Opaque;
            }
            if digits >= 2 && len <= 16 && max_letter_run(part) < 4 {
                return PartClass::Opaque;
            }
            let letters: String = part.trim_matches(|c: char| c.is_ascii_digit()).to_string();
            if letters.chars().any(|c| c.is_ascii_digit()) {
                return PartClass::Unknown;
            }
            return self.classify(&letters);
        }
        if self.is_word(part) {
            return PartClass::Known;
        }
        let sub = split_camel(part);
        if sub.len() > 1
            && sub
                .iter()
                .all(|s| s.chars().count() <= 2 || self.is_word(s))
        {
            return PartClass::Known;
        }
        PartClass::Unknown
    }
}

fn max_letter_run(s: &str) -> usize {
    let (mut best, mut cur) = (0, 0);
    for c in s.chars() {
        if c.is_alphabetic() {
            cur += 1;
            best = best.max(cur);
        } else {
            cur = 0;
        }
    }
    best
}

/// `SberPayCore` → `[Sber, Pay, Core]`, `HTTPServer` → `[HTTP, Server]`.
pub fn split_camel(s: &str) -> Vec<&str> {
    let chars: Vec<(usize, char)> = s.char_indices().collect();
    let mut out = Vec::new();
    let mut start = 0;
    for i in 1..chars.len() {
        let (pos, c) = chars[i];
        let prev = chars[i - 1].1;
        let next_lower = chars.get(i + 1).is_some_and(|(_, n)| n.is_lowercase());
        if c.is_uppercase() && (prev.is_lowercase() || (prev.is_uppercase() && next_lower)) {
            out.push(&s[start..pos]);
            start = pos;
        }
    }
    out.push(&s[start..]);
    out
}

/// Части строки, разделённые `-_.:@+~` и т.п., с байтовыми смещениями.
pub fn split_parts(s: &str, base: usize) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut start: Option<usize> = None;
    for (i, c) in s.char_indices() {
        if c.is_alphanumeric() {
            start.get_or_insert(i);
        } else if let Some(st) = start.take() {
            out.push((base + st, base + i));
        }
    }
    if let Some(st) = start {
        out.push((base + st, base + s.len()));
    }
    out
}

/// Подряд идущие неизвестные части (разделённые только разделителями) сливаются в один слот.
/// Возвращает байтовые диапазоны слотов относительно `base`.
pub fn unknown_slots(lex: &Lexicon, s: &str, base: usize) -> Vec<(usize, usize)> {
    let mut slots: Vec<(usize, usize)> = Vec::new();
    let mut prev_unknown = false;
    for (a, b) in split_parts(s, base) {
        let part = &s[a - base..b - base];
        if lex.classify(part) == PartClass::Unknown {
            if prev_unknown {
                slots.last_mut().expect("previous slot").1 = b;
            } else {
                slots.push((a, b));
            }
            prev_unknown = true;
        } else {
            prev_unknown = false;
        }
    }
    slots
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex() -> Lexicon {
        let mut l = Lexicon::default();
        for w in [
            "prod", "auth", "values", "yaml", "service", "pay", "core", "billing", "policy",
        ] {
            l.words.insert(w.into());
        }
        l.inflections = vec!["s".into(), "ies>y".into()];
        l
    }

    #[test]
    fn classifies_parts() {
        let l = lex();
        assert_eq!(l.classify("prod"), PartClass::Known);
        assert_eq!(l.classify("services"), PartClass::Known);
        assert_eq!(l.classify("policies"), PartClass::Known);
        assert_eq!(l.classify("7d9f8b"), PartClass::Opaque);
        assert_eq!(l.classify("x2v"), PartClass::Opaque);
        assert_eq!(l.classify("x7f3k9q2"), PartClass::Opaque);
        assert_eq!(l.classify("billing2"), PartClass::Known);
        assert_eq!(l.classify("sber"), PartClass::Unknown);
        assert_eq!(l.classify("PayCore"), PartClass::Known);
        assert_eq!(l.classify("SberPay"), PartClass::Unknown);
    }

    #[test]
    fn merges_unknown_runs() {
        let l = lex();
        let s = "prod-sber-zorg-auth-7d9f8b-x2v";
        let slots = unknown_slots(&l, s, 0);
        assert_eq!(slots.len(), 1);
        assert_eq!(&s[slots[0].0..slots[0].1], "sber-zorg");
    }

    #[test]
    fn splits_camel() {
        assert_eq!(split_camel("SberPayCore"), vec!["Sber", "Pay", "Core"]);
        assert_eq!(split_camel("HTTPServer"), vec!["HTTP", "Server"]);
    }
}
