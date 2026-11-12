//! Имена людей в прозе: «Иван Петров», «Петрову И. И.», «Mr. Smith», «Анне Ивановой».
//!
//! Детектор общий для всех языков: списки имён, окончания фамилий и отчеств, обращения и веса
//! признаков приходят из пакетов (`Lexicon::names`), склонение — из `Pack::name_forms`.
//! Каждая часть имени — отдельный токен (`FNAME`, `MNAME`, `LNAME`): одна и та же фамилия
//! в разных сочетаниях получает один токен, а окончание падежа остаётся после токена
//! (`Ивановой` → `ANON_LNAME_1ой`), чтобы модель видела грамматику.

use kaponir_core::{
    AnalyzeCtx, Candidate, Category, Gender, Input, Lexicon, NameRole, Recognizer, WordForm,
    name_key,
};

use crate::{char_after, char_before};
use regex::Regex;

const ID: &str = "names";

/// Больше слов подряд с заглавной — скорее заголовок, чем имя.
const MAX_WORDS: usize = 4;

pub struct Names {
    calendar: Regex,
}

impl Default for Names {
    fn default() -> Self {
        Self::new()
    }
}

impl Names {
    pub fn new() -> Self {
        Self { calendar: Regex::new(r"(?i)\b(?:(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?[,]?\s+)?(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}\b)").expect("calendar regex") }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Tok {
    /// Слово с заглавной буквы.
    Name,
    /// Инициал `И.` / `J.`.
    Initial,
    /// Слово в нижнем регистре (частица `van`, `фон` или обычное слово).
    Lower,
    /// Прочее: слова капсом, смешанный регистр, цифры.
    Other,
}

#[derive(Clone, Debug)]
struct Word {
    start: usize,
    end: usize,
    tok: Tok,
}

/// Вариант разбора слова.
#[derive(Clone, Debug)]
struct Opt {
    lemma: String,
    suffix: String,
    role: Option<NameRole>,
    gender: Option<Gender>,
    case: Option<u8>,
    quality: u8,
}

fn is_word_char(c: char) -> bool {
    c.is_alphabetic()
}

/// Слова и инициалы с байтовыми границами. Дефис и апостроф внутри слова — часть слова
/// (`Мамин-Сибиряк`, `O'Neil`).
fn words(text: &str) -> Vec<Word> {
    let mut out = Vec::new();
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if !is_word_char(c) {
            continue;
        }
        let mut end = i + c.len_utf8();
        while let Some(&(j, d)) = it.peek() {
            if is_word_char(d) {
                end = j + d.len_utf8();
                it.next();
            } else if matches!(d, '-' | '\'' | '’')
                && text[j + d.len_utf8()..]
                    .chars()
                    .next()
                    .is_some_and(is_word_char)
                && !possessive(&text[j..])
            {
                it.next();
            } else {
                break;
            }
        }
        let w = &text[i..end];
        let tok = if w.chars().count() == 1 && c.is_uppercase() && text[end..].starts_with('.') {
            end += 1;
            Tok::Initial
        } else if is_titlecase(w) {
            Tok::Name
        } else if w.chars().all(char::is_lowercase) {
            Tok::Lower
        } else {
            Tok::Other
        };
        out.push(Word { start: i, end, tok });
    }
    out
}

/// Английское притяжательное `'s` после слова: остаётся в тексте после токена (`ANON_LNAME_1's`).
fn possessive(rest: &str) -> bool {
    let tail = rest.strip_prefix('\'').or_else(|| rest.strip_prefix('’'));
    tail.and_then(|t| t.strip_prefix('s'))
        .is_some_and(|t| !t.chars().next().is_some_and(is_word_char))
}

/// `Иван`, `Мамин-Сибиряк`, `O'Neil`, `McDonald`, `MacLeod`. Не `IVAN`, не `SberPay`.
fn is_titlecase(w: &str) -> bool {
    if w.chars().count() < 2 {
        return false;
    }
    w.split(['-', '\'', '’']).all(|part| {
        let mut cs = part.chars();
        let Some(first) = cs.next() else { return false };
        if !first.is_uppercase() {
            return false;
        }
        let rest: String = cs.collect();
        if rest.chars().all(char::is_lowercase) {
            return true;
        }
        // Mc/Mac + имя с заглавной.
        for p in ["c", "ac"] {
            if let Some(tail) = rest.strip_prefix(p)
                && first == 'M'
                && tail.chars().next().is_some_and(char::is_uppercase)
                && tail.chars().skip(1).all(char::is_lowercase)
                && tail.chars().count() >= 2
            {
                return true;
            }
        }
        // `O'Neil`: часть из одной буквы перед апострофом.
        part.chars().count() == 1
    }) && w.chars().filter(|c| c.is_uppercase()).count() <= 3
}

/// Соседство, при котором слово — часть идентификатора, пути, адреса, а не текста.
fn glued(text: &str, start: usize, end: usize) -> bool {
    let before = char_before(text, start);
    let after = char_after(text, end);
    let glue = |c: char| {
        c.is_alphanumeric()
            || matches!(
                c,
                '_' | '/' | '\\' | '@' | '$' | '{' | '=' | '<' | '>' | '#'
            )
    };
    // Внутри JSON-строки `\n` — два обычных символа, а не разделитель Windows-пути.
    let json_escape = after == Some('\\')
        && text[end + 1..]
            .chars()
            .next()
            .is_some_and(|c| matches!(c, '"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't' | 'u'));
    before.is_some_and(|c| glue(c) || c == '.' || c == '-')
        || after.is_some_and(|c| glue(c) && !(c == '\\' && json_escape))
        || (after == Some('.')
            && text[end + 1..]
                .chars()
                .next()
                .is_some_and(char::is_alphanumeric))
        || (after == Some('-')
            && text[end + 1..]
                .chars()
                .next()
                .is_some_and(char::is_alphanumeric))
}

/// Между словами только пробелы (без перевода строки); между инициалами можно без пробела.
fn joined(text: &str, a: &Word, b: &Word) -> bool {
    let gap = &text[a.end..b.start];
    if gap.is_empty() {
        return a.tok == Tok::Initial;
    }
    gap.chars().count() <= 2 && gap.chars().all(|c| c == ' ' || c == '\u{a0}' || c == '\t')
}

fn sentence_start(text: &str, start: usize) -> bool {
    match text[..start]
        .trim_end_matches([' ', '\t', '\u{a0}', '"', '«', '(', '*', '#', '>', '-', '•'])
        .chars()
        .next_back()
    {
        None => true,
        Some(c) => matches!(c, '.' | '!' | '?' | '…' | '\n' | '\r'),
    }
}

/// Токены смыслового окна. Левая граница не должна разрезать слово: в длинной JSON-строке
/// именно это слово может быть единственным контекстом человека (`\\nпередайте …`).
fn context_words(text: &str, start: usize, end: usize) -> Vec<String> {
    let mut lo = kaponir_core::engine::floor_boundary(text, start.saturating_sub(48));
    if lo > 0
        && text[..lo]
            .chars()
            .next_back()
            .is_some_and(char::is_alphanumeric)
        && text[lo..].chars().next().is_some_and(char::is_alphanumeric)
    {
        lo = text[..lo]
            .char_indices()
            .rev()
            .find(|(_, c)| !c.is_alphanumeric())
            .map_or(0, |(i, c)| i + c.len_utf8());
    }
    let hi = kaponir_core::engine::ceil_boundary(text, (end + 24).min(text.len()));
    let window = format!("{} {}", &text[lo..start], &text[end..hi]);
    let mut normalized = String::with_capacity(window.len());
    let mut chars = window.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\\'
            && chars
                .peek()
                .is_some_and(|c| matches!(c, '"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't' | 'u'))
        {
            chars.next();
            normalized.push(' ');
        } else {
            normalized.push(c);
        }
    }
    normalized
        .to_lowercase()
        .split(|c: char| !c.is_alphanumeric() && c != '-')
        .filter(|w| !w.is_empty())
        .map(str::to_string)
        .collect()
}

impl Names {
    /// Варианты разбора слова: как есть (по словарю) и от пакетов (словоформы).
    fn options(&self, word: &str, ctx: &AnalyzeCtx<'_>) -> Vec<Opt> {
        let names = &ctx.lexicon.names;
        let mut out = Vec::new();
        let role_of = |lemma: &str| -> (Option<NameRole>, Option<Gender>, u8) {
            if let Some(g) = names.given(lemma) {
                (Some(NameRole::Given), g, 3)
            } else if lemma.contains('-') && lemma.split('-').all(|p| names.given(p).is_some()) {
                // Mary-Jane, Анна-Мария: род — по первой части.
                (
                    Some(NameRole::Given),
                    lemma
                        .split('-')
                        .next()
                        .and_then(|p| names.given(p))
                        .flatten(),
                    3,
                )
            } else if let Some(g) = names.patronymic(lemma) {
                (Some(NameRole::Patronymic), g, 3)
            } else if let Some(g) = names.surname(lemma) {
                (Some(NameRole::Surname), g, 2)
            } else {
                (None, None, 0)
            }
        };
        let (role, gender, quality) = role_of(word);
        out.push(Opt {
            lemma: word.to_string(),
            suffix: String::new(),
            role,
            gender,
            case: None,
            quality,
        });
        for p in ctx.packs {
            for WordForm {
                lemma,
                suffix,
                role,
                gender,
                case,
            } in p.name_forms(word)
            {
                let quality = match role {
                    Some(NameRole::Given | NameRole::Patronymic) => 3,
                    Some(NameRole::Surname) => 2,
                    None => role_of(&lemma).2,
                };
                out.push(Opt {
                    lemma,
                    suffix,
                    role,
                    gender,
                    case,
                    quality,
                });
            }
        }
        out
    }

    /// У слова нет ни одного признака имени (ни по словарю, ни по разбору пакета).
    fn weak(&self, text: &str, w: &Word, ctx: &AnalyzeCtx<'_>) -> bool {
        self.options(&text[w.start..w.end], ctx)
            .iter()
            .all(|o| o.quality == 0)
    }

    /// Обычное слово или слово контекста имени (`Передай`, `Ask`, `Thanks`).
    fn ordinary(&self, text: &str, w: &Word, ctx: &AnalyzeCtx<'_>) -> bool {
        let s = &text[w.start..w.end];
        let key = name_key(s);
        ctx.lexicon.is_word(s)
            || ctx
                .lexicon
                .context_for("PERSON")
                .is_some_and(|c| c.contains(&key))
    }

    fn group(
        &self,
        text: &str,
        ws: &[Word],
        title: bool,
        ctx: &AnalyzeCtx<'_>,
        out: &mut Vec<Candidate>,
    ) {
        let lex: &Lexicon = ctx.lexicon;
        let names = &lex.names;
        let name_words: Vec<&Word> = ws
            .iter()
            .filter(|w| {
                w.tok == Tok::Name
                    || (w.tok == Tok::Lower
                        && !names.particles.contains(&name_key(&text[w.start..w.end])))
            })
            .collect();
        if name_words.is_empty() || name_words.len() > MAX_WORDS {
            return;
        }
        let initials = ws.iter().any(|w| w.tok == Tok::Initial);
        let opts: Vec<Vec<Opt>> = name_words
            .iter()
            .map(|w| self.options(&text[w.start..w.end], ctx))
            .collect();

        // Согласование: род и падеж, при которых больше всего слов получают разбор.
        let compatible = |o: &Opt, g: Gender, c: u8| {
            o.gender.is_none_or(|x| x == g) && o.case.is_none_or(|x| x == c)
        };
        let mut best: Option<(u32, Gender, u8)> = None;
        for c in [0u8, 1, 2, 3, 4, 5] {
            for g in [Gender::Male, Gender::Female] {
                let score: u32 = opts
                    .iter()
                    .map(|os| {
                        os.iter()
                            .filter(|o| compatible(o, g, c))
                            .map(|o| u32::from(o.quality))
                            .max()
                            .unwrap_or(0)
                    })
                    .sum();
                if best.is_none_or(|b| score > b.0) {
                    best = Some((score, g, c));
                }
            }
        }
        let (_, g, c) = best.expect("at least one combination");
        let mut chosen: Vec<Opt> = opts
            .iter()
            .map(|os| {
                os.iter()
                    .filter(|o| compatible(o, g, c))
                    .max_by_key(|o| (o.quality, o.case.is_some()))
                    .cloned()
                    .unwrap_or_else(|| os[0].clone())
            })
            .collect();

        let has = |r: NameRole| chosen.iter().any(|o| o.role == Some(r));
        let (given, patronymic, surname) = (
            has(NameRole::Given),
            has(NameRole::Patronymic),
            has(NameRole::Surname),
        );
        if !(given || patronymic || surname || title || initials) {
            return;
        }
        if name_words.iter().any(|w| w.tok == Tok::Lower) {
            let person_context = lex.context_for("PERSON").is_some_and(|words| {
                let toks = context_words(text, ws[0].start, ws[ws.len() - 1].end);
                words.iter().any(|w| toks.iter().any(|t| t == w))
            });
            if !(title || initials || person_context) {
                return;
            }
        }

        // Роли слов без признаков: рядом с именем, обращением или инициалами это фамилия.
        let n = chosen.len();
        let givens: Vec<usize> = (0..n)
            .filter(|&i| chosen[i].role == Some(NameRole::Given))
            .collect();
        if givens.len() >= 2 {
            let last = n - 1;
            let tail_is_surname = chosen[last].role != Some(NameRole::Given);
            for &i in &givens[1..] {
                if i == last && !tail_is_surname {
                    chosen[i].role = Some(NameRole::Surname);
                } else if i != last {
                    chosen[i].role = Some(NameRole::Patronymic);
                }
            }
        }
        for o in chosen.iter_mut() {
            if o.role.is_none() {
                o.role = Some(NameRole::Surname);
            }
        }

        let mut ev: Vec<(String, f32)> = Vec::new();
        if given {
            ev.push(("name:given".into(), names.weight("given", 2.0)));
        }
        if patronymic {
            ev.push(("name:patronymic".into(), names.weight("patronymic", 2.5)));
        }
        if surname {
            ev.push(("name:surname".into(), names.weight("surname", 1.0)));
        }
        if title {
            ev.push(("name:title".into(), names.weight("title", 2.0)));
        }
        if initials {
            ev.push(("name:initials".into(), names.weight("initials", 1.5)));
        }
        if (n >= 2 && (given || patronymic || surname)) || initials {
            ev.push(("name:pair".into(), names.weight("pair", 1.5)));
        }
        // Коллизия — если совпадает со словом сама форма или любая её начальная форма (`Ростова` → `Ростов`).
        let collisions = name_words
            .iter()
            .zip(&opts)
            .filter(|(w, os)| {
                names.is_collision(&text[w.start..w.end])
                    || os.iter().any(|o| names.is_collision(&o.lemma))
            })
            .count();
        if collisions > 0 {
            let w = if n == 1 {
                names.weight("collision", -2.0)
            } else {
                names.weight("collision_in_group", -0.5)
            };
            ev.push(("name:collision".into(), w));
        }
        let common = name_words
            .iter()
            .zip(&chosen)
            .any(|(w, o)| (o.quality == 0 || n == 1) && lex.is_word(&text[w.start..w.end]));
        if common {
            ev.push(("name:common-word".into(), names.weight("common_word", -1.5)));
        }
        if n == 1 && !title && !initials && sentence_start(text, ws[0].start) {
            ev.push((
                "name:sentence-start".into(),
                names.weight("sentence_start", -1.0),
            ));
        }
        if let Some(words) = lex.context_for("PERSON") {
            let toks = context_words(text, ws[0].start, ws[ws.len() - 1].end);
            if let Some(w) = words.iter().find(|w| toks.iter().any(|t| t == *w)) {
                ev.push((format!("context:{w}"), 1.5));
            }
        }

        for (w, o) in name_words.iter().zip(&chosen) {
            let role = o.role.expect("role assigned");
            let kind = role.kind();
            let surface = &text[w.start..w.end];
            let mut cand = Candidate::new(
                w.start,
                w.end,
                kind,
                Category::Identity,
                ID,
                names.weight("prior", 0.1),
            );
            for (src, l) in &ev {
                cand = cand.with_evidence(src.clone(), *l);
            }
            // Окончание после токена — только если пакет восстановит из леммы ровно это слово.
            let restorable = !o.suffix.is_empty()
                && surface.ends_with(o.suffix.as_str())
                && ctx
                    .packs
                    .iter()
                    .any(|p| p.restore_form(kind, &o.lemma, &o.suffix).as_deref() == Some(surface));
            cand = if restorable {
                cand.canonical(name_key(&o.lemma))
                    .inflected(o.lemma.clone(), o.suffix.len())
            } else {
                cand.canonical(name_key(surface))
            };
            out.push(cand);
        }
    }
}

impl Recognizer for Names {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        if ctx.lexicon.names.is_empty() {
            return;
        }
        let text = input.text;
        let names = &ctx.lexicon.names;
        let ws = words(text);
        let calendar: Vec<_> = self
            .calendar
            .find_iter(text)
            .map(|m| (m.start(), m.end()))
            .collect();
        let mut i = 0;
        while i < ws.len() {
            let w = &ws[i];
            if calendar.iter().any(|&(a, b)| w.start < b && a < w.end) {
                i += 1;
                continue;
            }
            let starts = match w.tok {
                Tok::Name | Tok::Initial => true,
                // Строчная словоформа рассматривается отдельно и только при признаках имени:
                // обычные глаголы не должны начинать группу и поглощать следующие слова.
                Tok::Lower => !self.weak(text, w, ctx),
                Tok::Other => false,
            } && !glued(text, w.start, w.end - usize::from(w.tok == Tok::Initial));
            if !starts {
                i += 1;
                continue;
            }
            // Группа: имена, инициалы и частицы между ними.
            let mut j = i + 1;
            while j < ws.len() && joined(text, &ws[j - 1], &ws[j]) {
                let t = &ws[j];
                let ok = match t.tok {
                    Tok::Name if w.tok != Tok::Lower => !glued(text, t.start, t.end),
                    Tok::Initial if w.tok != Tok::Lower => true,
                    Tok::Lower if w.tok != Tok::Lower => {
                        names.particles.contains(&name_key(&text[t.start..t.end]))
                            && ws
                                .get(j + 1)
                                .is_some_and(|n| n.tok == Tok::Name && joined(text, t, n))
                    }
                    _ => false,
                };
                if !ok {
                    break;
                }
                j += 1;
            }
            let group = &ws[i..j];
            // Обращение прямо перед группой: `Mr. Smith`, `господин Иванов`, `Уважаемый Иван`.
            let title = i > 0 && {
                let t = &ws[i - 1];
                let gap = &text[t.end..w.start];
                let gap = gap.strip_prefix('.').unwrap_or(gap);
                gap.chars().count() <= 2
                    && gap.chars().all(|c| c == ' ' || c == '\u{a0}')
                    && !gap.is_empty()
                    && names.titles.contains(&name_key(&text[t.start..t.end]))
            };
            // Обращение с заглавной само по себе не имя.
            let (mut group, title) = if names
                .titles
                .contains(&name_key(&text[group[0].start..group[0].end]))
                && group.len() > 1
            {
                (&group[1..], true)
            } else {
                (group, title)
            };
            // Края группы без признаков имени: обычные слова и слова контекста (`Передай Петрову`,
            // `Ask John`), а в начале предложения — любое такое слово (`Позвони Ивану`).
            while group.len() > 1
                && group[0].tok == Tok::Name
                && self.weak(text, &group[0], ctx)
                && (self.ordinary(text, &group[0], ctx)
                    || (sentence_start(text, group[0].start) && group[1].tok == Tok::Name))
            {
                group = &group[1..];
            }
            while group.len() > 1
                && group[group.len() - 1].tok == Tok::Name
                && self.weak(text, &group[group.len() - 1], ctx)
                && self.ordinary(text, &group[group.len() - 1], ctx)
            {
                group = &group[..group.len() - 1];
            }
            self.group(text, group, title, ctx, out);
            i = j;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kaponir_core::NameLexicon;

    fn lex() -> Lexicon {
        let mut n = NameLexicon::default();
        for (g, gender) in [
            ("иван", Gender::Male),
            ("john", Gender::Male),
            ("анна", Gender::Female),
            ("mary", Gender::Female),
            ("will", Gender::Male),
            ("thu", Gender::Female),
        ] {
            n.given.insert(g.into(), Some(gender));
        }
        n.surname_suffixes = vec![
            ("ов".into(), Some(Gender::Male)),
            ("ова".into(), Some(Gender::Female)),
        ];
        n.patronymic_suffixes = vec![("ович".into(), Some(Gender::Male))];
        n.titles = ["mr", "господин"].into_iter().map(String::from).collect();
        n.particles = ["van"].into_iter().map(String::from).collect();
        n.collisions = ["will"].into_iter().map(String::from).collect();
        let mut l = Lexicon {
            names: n,
            ..Lexicon::default()
        };
        l.words.insert("report".into());
        l
    }

    fn found(text: &str) -> Vec<(String, String, f32)> {
        let l = lex();
        let mut out = Vec::new();
        Names::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        out.into_iter()
            .map(|c| {
                (
                    c.kind.clone(),
                    text[c.start..c.end].to_string(),
                    c.probability(),
                )
            })
            .collect()
    }

    #[test]
    fn json_context_keeps_the_word_at_the_left_boundary() {
        let text = concat!(
            "{\n  \"instructions\": \"Ответственный: Михаил Голошанов\\n",
            "передайте голошанову письмо о проверке доступа.\\n\\nРаздел 01.\\n\"\n}"
        );
        let start = text.find("голошанову").unwrap();
        let toks = context_words(text, start, start + "голошанову".len());
        assert!(toks.iter().any(|w| w == "передайте"), "{toks:?}");
    }

    #[test]
    fn full_names_and_roles() {
        let f = found("Отчёт подготовил Петров Иван Иванович, проверила Анна Смирнова.");
        let kinds: Vec<(&str, &str)> = f.iter().map(|(k, s, _)| (k.as_str(), s.as_str())).collect();
        assert_eq!(
            kinds,
            vec![
                ("LNAME", "Петров"),
                ("FNAME", "Иван"),
                ("MNAME", "Иванович"),
                ("FNAME", "Анна"),
                ("LNAME", "Смирнова")
            ]
        );
        assert!(f.iter().all(|(_, _, p)| *p > 0.9), "{f:?}");
    }

    #[test]
    fn calendar_dates_are_not_people_but_real_names_remain() {
        for text in [
            "Thu Nov 12 10:40:00 2026",
            "Date: Thu, 12 Nov 2026 10:40:00 +0700",
            "Wed, 11 Nov 2026",
        ] {
            assert!(found(text).is_empty(), "{text}");
        }
        let f = found("cc John Smith");
        assert!(f.iter().any(|(_, v, _)| v == "John"));
        assert!(
            found("Thu Lan Nguyen reviewed it.")
                .iter()
                .any(|(_, value, _)| value == "Thu")
        );
    }

    #[test]
    fn titles_initials_particles() {
        let f = found("Ask Mr. Smith or J. Doe; Ludwig van Beethoven wrote it.");
        let got: Vec<&str> = f.iter().map(|(_, s, _)| s.as_str()).collect();
        assert_eq!(got, vec!["Smith", "Doe"]);
        let f = found("cc John van Dyke");
        let got: Vec<(&str, &str)> = f.iter().map(|(k, s, _)| (k.as_str(), s.as_str())).collect();
        assert_eq!(got, vec![("FNAME", "John"), ("LNAME", "Dyke")]);
    }

    #[test]
    fn weak_and_rejected() {
        // Совпадение с обычным словом в одиночку — не имя.
        assert!(found("Will this work?").iter().all(|(_, _, p)| *p < 0.2));
        // Слова с заглавной без признаков имени.
        assert!(found("The Report Builder Service").is_empty());
        // Идентификаторы, пути, адреса.
        assert!(found("/home/Ivan/x Ivan_Petrov Ivan.Petrov@x.ru IVAN").is_empty());
        assert!(found(r"C:\work\Brown\report.txt").is_empty());
        // Одиночная фамилия по окончанию — спорная.
        let f = found("потом Сидоров ответил");
        assert_eq!(f.len(), 1);
        assert!(f[0].2 > 0.2 && f[0].2 < 0.7, "{f:?}");
    }
}
