//! Склонение русских имён, отчеств и фамилий. Класс склонения определяется по окончанию
//! начальной формы, роли (имя, отчество, фамилия) и роду; формы строятся как основа + окончание.
//!
//! Окончание падежа — это и есть хвост, который остаётся в тексте после токена:
//! `Ивановой` = `Иванов` + `ой` → `ANON_LNAME_1ой`.

use kaponir_core::{Gender, NameRole};

pub const NOM: u8 = 0;
pub const GEN: u8 = 1;
pub const DAT: u8 = 2;
pub const ACC: u8 = 3;
pub const INS: u8 = 4;
pub const PRE: u8 = 5;

/// Косвенные падежи в порядке предпочтения при неоднозначном окончании.
pub const OBLIQUE: [u8; 5] = [GEN, DAT, ACC, INS, PRE];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    /// Иван, Шмидт, Иванович.
    MascHard,
    /// Фамилии на -ов/-ев/-ин/-ын: Иванов, Пушкин (творительный -ым).
    MascOv,
    /// Андрей, Николай.
    MascJ,
    /// Василий, Дмитрий (предложный -и).
    MascIj,
    /// Игорь.
    MascSoft,
    /// Анна, Никита, Ивановна.
    DeclA,
    /// Таня, Илья, Наталья.
    DeclYa,
    /// Мария, Виктория.
    DeclIya,
    /// Иванова, Пушкина.
    FemOva,
    /// Достоевский, Белый.
    MascSky,
    /// Толстой, Трубецкой.
    MascOj,
    /// Достоевская.
    FemSkaya,
    /// Любовь.
    FemSoft,
    /// Шевченко, Черных, Анна Шмидт.
    Indecl,
}

const VOWELS: &str = "аеёиоуыэюя";

fn ends(l: &str, any: &[&str]) -> bool {
    any.iter().any(|s| l.ends_with(s))
}

fn last_char(s: &str) -> Option<char> {
    s.chars()
        .next_back()
        .map(|c| c.to_lowercase().next().unwrap_or(c))
}

/// Род, который следует из окончания фамилии или отчества.
pub fn gender_by_ending(lemma: &str, role: NameRole) -> Option<Gender> {
    let l = lemma.to_lowercase();
    match role {
        NameRole::Surname if ends(&l, &["ова", "ева", "ёва", "ина", "ына", "ская", "цкая"]) => {
            Some(Gender::Female)
        }
        NameRole::Surname
            if ends(
                &l,
                &["ов", "ев", "ёв", "ин", "ын", "ский", "цкий", "ской", "цкой"],
            ) =>
        {
            Some(Gender::Male)
        }
        NameRole::Patronymic if l.ends_with("ич") => Some(Gender::Male),
        NameRole::Patronymic if l.ends_with("на") => Some(Gender::Female),
        _ => None,
    }
}

pub fn class(lemma: &str, role: NameRole, gender: Option<Gender>) -> Class {
    let l = lemma.to_lowercase();
    let Some(last) = last_char(&l) else {
        return Class::Indecl;
    };
    if l.contains('-') || l.chars().count() < 2 {
        return Class::Indecl;
    }
    let consonant = last.is_alphabetic() && !VOWELS.contains(last) && last != 'ь' && last != 'й';
    match role {
        NameRole::Patronymic => {
            if l.ends_with("ич") {
                Class::MascHard
            } else if l.ends_with("на") {
                Class::DeclA
            } else {
                Class::Indecl
            }
        }
        NameRole::Given => {
            if l.ends_with("ия") {
                Class::DeclIya
            } else if last == 'а' {
                Class::DeclA
            } else if last == 'я' {
                Class::DeclYa
            } else if l.ends_with("ий") {
                Class::MascIj
            } else if last == 'й' {
                Class::MascJ
            } else if last == 'ь' {
                if gender == Some(Gender::Female) {
                    Class::FemSoft
                } else {
                    Class::MascSoft
                }
            } else if consonant && gender != Some(Gender::Female) {
                Class::MascHard
            } else {
                Class::Indecl
            }
        }
        NameRole::Surname => {
            if ends(&l, &["ых", "их"]) || (VOWELS.contains(last) && last != 'а' && last != 'я')
            {
                return Class::Indecl;
            }
            let female = gender == Some(Gender::Female)
                || gender_by_ending(&l, role) == Some(Gender::Female);
            if female {
                if ends(&l, &["ская", "цкая", "ая"]) {
                    Class::FemSkaya
                } else if ends(&l, &["ова", "ева", "ёва", "ина", "ына"]) {
                    Class::FemOva
                } else if l.ends_with("ия") {
                    Class::DeclIya
                } else if last == 'а' {
                    Class::DeclA
                } else if last == 'я' {
                    Class::DeclYa
                } else {
                    Class::Indecl
                }
            } else if ends(&l, &["ский", "цкий", "ий", "ый"]) {
                Class::MascSky
            } else if ends(&l, &["ской", "цкой", "ой"]) {
                Class::MascOj
            } else if ends(&l, &["ов", "ев", "ёв", "ин", "ын"]) {
                Class::MascOv
            } else if l.ends_with("ия") {
                Class::DeclIya
            } else if last == 'а' {
                Class::DeclA
            } else if last == 'я' {
                Class::DeclYa
            } else if last == 'ь' {
                Class::MascSoft
            } else if last == 'й' {
                Class::MascJ
            } else if consonant {
                Class::MascHard
            } else {
                Class::Indecl
            }
        }
    }
}

/// Беглые гласные и ё в основе: Павел → Павла, Пётр → Петра, Лев → Льва.
const FLEETING: &[(&str, &str)] = &[
    ("павел", "павл"),
    ("пётр", "петр"),
    ("петр", "петр"),
    ("лев", "льв"),
];

fn with_case_of(model: &str, lower: &str) -> String {
    let mut out = String::new();
    for (i, c) in lower.chars().enumerate() {
        let upper = model.chars().nth(i).is_some_and(char::is_uppercase);
        if upper {
            out.extend(c.to_uppercase());
        } else {
            out.push(c);
        }
    }
    out
}

/// Основа косвенных падежей для имени (с беглой гласной).
fn oblique_stem(lemma: &str, strip: usize, role: NameRole) -> String {
    let n = lemma.chars().count();
    let stem: String = lemma.chars().take(n - strip).collect();
    if role == NameRole::Given
        && let Some((_, to)) = FLEETING
            .iter()
            .find(|(from, _)| *from == lemma.to_lowercase())
    {
        return with_case_of(lemma, to);
    }
    stem
}

/// Возможные начальные формы для основы косвенного падежа (обратное к `oblique_stem`).
pub fn lemma_stems(stem: &str) -> Vec<String> {
    let mut out = vec![stem.to_string()];
    let l = stem.to_lowercase();
    for (from, to) in FLEETING {
        if l == *to {
            out.push(with_case_of(stem, from));
        }
    }
    out
}

/// Косвенные падежи: `(падеж, форма, окончание)`. Пусто для несклоняемых.
pub fn forms(lemma: &str, class: Class, role: NameRole) -> Vec<(u8, String, String)> {
    let (strip, endings): (usize, [&str; 5]) = {
        let n = lemma.chars().count();
        let stem_last = |strip: usize| {
            lemma
                .chars()
                .nth(n.saturating_sub(strip + 1))
                .map(|c| c.to_lowercase().next().unwrap_or(c))
        };
        let in_set = |strip: usize, set: &str| stem_last(strip).is_some_and(|c| set.contains(c));
        match class {
            Class::MascHard => (
                0,
                [
                    "а",
                    "у",
                    "а",
                    if in_set(0, "жшщчц") {
                        "ем"
                    } else {
                        "ом"
                    },
                    "е",
                ],
            ),
            Class::MascOv => (0, ["а", "у", "а", "ым", "е"]),
            Class::MascJ | Class::MascSoft => (1, ["я", "ю", "я", "ем", "е"]),
            Class::MascIj => (1, ["я", "ю", "я", "ем", "и"]),
            Class::DeclA => (
                1,
                [
                    if in_set(1, "гкхжшщч") {
                        "и"
                    } else {
                        "ы"
                    },
                    "е",
                    "у",
                    if in_set(1, "жшщчц") {
                        "ей"
                    } else {
                        "ой"
                    },
                    "е",
                ],
            ),
            Class::DeclYa => (1, ["и", "е", "ю", "ей", "е"]),
            Class::DeclIya => (1, ["и", "и", "ю", "ей", "и"]),
            Class::FemOva => (1, ["ой", "ой", "у", "ой", "ой"]),
            Class::MascSky | Class::MascOj => (
                2,
                [
                    "ого",
                    "ому",
                    "ого",
                    if in_set(2, "гкхжшщч") {
                        "им"
                    } else {
                        "ым"
                    },
                    "ом",
                ],
            ),
            Class::FemSkaya => (2, ["ой", "ой", "ую", "ой", "ой"]),
            Class::FemSoft => (1, ["и", "и", "ь", "ью", "и"]),
            Class::Indecl => return Vec::new(),
        }
    };
    if lemma.chars().count() <= strip + 1 {
        return Vec::new();
    }
    let stem = oblique_stem(lemma, strip, role);
    OBLIQUE
        .iter()
        .zip(endings)
        .map(|(&case, e)| (case, format!("{stem}{e}"), e.to_string()))
        .collect()
}

/// Все окончания косвенных падежей (для разбора словоформ).
pub const ENDINGS: &[&str] = &[
    "а", "я", "у", "ю", "е", "и", "ы", "ь", "ом", "ем", "ым", "им", "ой", "ей", "ью", "ого", "ому",
    "ую",
];

/// Начальные формы, из которых окончание отрезано при склонении.
pub const LEMMA_TAILS: &[&str] = &["", "а", "я", "й", "ь", "ий", "ый", "ой", "ая"];

/// Падеж по окончанию, которое модель приклеила к токену, если оно не совпало с парадигмой
/// (`ANON_FNAME_1ом` для женского имени): берём падеж, который окончание обычно означает.
pub fn guess_case(suffix: &str) -> Option<u8> {
    Some(match suffix {
        "а" | "я" | "ы" | "и" | "ого" | "его" => GEN,
        "у" | "ю" | "ому" | "ему" => DAT,
        "ую" | "юю" => ACC,
        "ом" | "ем" | "ём" | "ым" | "им" | "ой" | "ей" | "ёй" | "ою" | "ею" | "ью" => {
            INS
        }
        "е" => PRE,
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decl(lemma: &str, role: NameRole, gender: Option<Gender>) -> Vec<String> {
        forms(lemma, class(lemma, role, gender), role)
            .into_iter()
            .map(|f| f.1)
            .collect()
    }

    #[test]
    fn paradigms() {
        use NameRole::*;
        let m = Some(Gender::Male);
        let f = Some(Gender::Female);
        assert_eq!(
            decl("Иван", Given, m),
            ["Ивана", "Ивану", "Ивана", "Иваном", "Иване"]
        );
        assert_eq!(
            decl("Павел", Given, m),
            ["Павла", "Павлу", "Павла", "Павлом", "Павле"]
        );
        assert_eq!(
            decl("Пётр", Given, m),
            ["Петра", "Петру", "Петра", "Петром", "Петре"]
        );
        assert_eq!(
            decl("Андрей", Given, m),
            ["Андрея", "Андрею", "Андрея", "Андреем", "Андрее"]
        );
        assert_eq!(
            decl("Василий", Given, m),
            ["Василия", "Василию", "Василия", "Василием", "Василии"]
        );
        assert_eq!(
            decl("Игорь", Given, m),
            ["Игоря", "Игорю", "Игоря", "Игорем", "Игоре"]
        );
        assert_eq!(
            decl("Анна", Given, f),
            ["Анны", "Анне", "Анну", "Анной", "Анне"]
        );
        assert_eq!(
            decl("Ольга", Given, f),
            ["Ольги", "Ольге", "Ольгу", "Ольгой", "Ольге"]
        );
        assert_eq!(
            decl("Саша", Given, None),
            ["Саши", "Саше", "Сашу", "Сашей", "Саше"]
        );
        assert_eq!(
            decl("Мария", Given, f),
            ["Марии", "Марии", "Марию", "Марией", "Марии"]
        );
        assert_eq!(
            decl("Наталья", Given, f),
            ["Натальи", "Наталье", "Наталью", "Натальей", "Наталье"]
        );
        assert_eq!(
            decl("Любовь", Given, f),
            ["Любови", "Любови", "Любовь", "Любовью", "Любови"]
        );
        assert_eq!(
            decl("Иванович", Patronymic, m),
            [
                "Ивановича",
                "Ивановичу",
                "Ивановича",
                "Ивановичем",
                "Ивановиче"
            ]
        );
        assert_eq!(
            decl("Ивановна", Patronymic, f),
            ["Ивановны", "Ивановне", "Ивановну", "Ивановной", "Ивановне"]
        );
        assert_eq!(
            decl("Иванов", Surname, None),
            ["Иванова", "Иванову", "Иванова", "Ивановым", "Иванове"]
        );
        assert_eq!(
            decl("Иванова", Surname, None),
            ["Ивановой", "Ивановой", "Иванову", "Ивановой", "Ивановой"]
        );
        assert_eq!(
            decl("Достоевский", Surname, None),
            [
                "Достоевского",
                "Достоевскому",
                "Достоевского",
                "Достоевским",
                "Достоевском"
            ]
        );
        assert_eq!(
            decl("Толстой", Surname, None),
            ["Толстого", "Толстому", "Толстого", "Толстым", "Толстом"]
        );
        assert_eq!(
            decl("Достоевская", Surname, None),
            [
                "Достоевской",
                "Достоевской",
                "Достоевскую",
                "Достоевской",
                "Достоевской"
            ]
        );
        assert_eq!(
            decl("Шмидт", Surname, m),
            ["Шмидта", "Шмидту", "Шмидта", "Шмидтом", "Шмидте"]
        );
        assert!(decl("Шмидт", Surname, f).is_empty());
        assert!(decl("Шевченко", Surname, m).is_empty());
        assert!(decl("Черных", Surname, m).is_empty());
    }
}
