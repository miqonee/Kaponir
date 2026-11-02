//! Транслитерация кириллицы в латиницу по распространённым схемам: практическая (BGN/PCGN без
//! диакритики), загранпаспорт (ICAO Doc 9303, с 2013 г.) и бытовая (`х` → `h`, `-ий` → `-y`).
//! Результат — варианты для предложений «Голошанов ↔ Goloshanov», а не автоматическая связь.

#[derive(Clone, Copy)]
enum Scheme {
    Practical,
    Icao,
    Casual,
}

fn letter(c: char, s: Scheme, word_start: bool) -> &'static str {
    use Scheme::*;
    match c {
        'а' => "a",
        'б' => "b",
        'в' => "v",
        'г' => "g",
        'д' => "d",
        'е' => match s {
            // В начале слова и после гласной: Елена → Yelena, Достоевский → Dostoyevskiy.
            Practical if word_start => "ye",
            _ => "e",
        },
        'ё' => match s {
            Icao => "e",
            _ => "yo",
        },
        'ж' => "zh",
        'з' => "z",
        'и' => "i",
        'й' => match s {
            Icao => "i",
            _ => "y",
        },
        'к' => "k",
        'л' => "l",
        'м' => "m",
        'н' => "n",
        'о' => "o",
        'п' => "p",
        'р' => "r",
        'с' => "s",
        'т' => "t",
        'у' => "u",
        'ф' => "f",
        'х' => match s {
            Casual => "h",
            _ => "kh",
        },
        'ц' => "ts",
        'ч' => "ch",
        'ш' => "sh",
        'щ' => "shch",
        'ъ' => match s {
            Icao => "ie",
            _ => "",
        },
        'ы' => "y",
        'ь' => "",
        'э' => "e",
        'ю' => match s {
            Icao => "iu",
            _ => "yu",
        },
        'я' => match s {
            Icao => "ia",
            _ => "ya",
        },
        _ => "",
    }
}

fn convert(text: &str, s: Scheme) -> Option<String> {
    let mut out = String::new();
    let mut word_start = true;
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        let lower = c.to_lowercase().next().unwrap_or(c);
        let prev_soft = i > 0 && {
            let p = chars[i - 1].to_lowercase().next().unwrap_or(chars[i - 1]);
            "аеёиоуыэюяъь".contains(p)
        };
        // Окончания прилагательных: Достоевский → Dostoevsky (бытовая), Dostoevskii (ICAO).
        if matches!(s, Scheme::Casual) && (lower == 'и' || lower == 'ы') {
            let next = chars
                .get(i + 1)
                .map(|n| n.to_lowercase().next().unwrap_or(*n));
            let after = chars.get(i + 2);
            if next == Some('й') && after.is_none_or(|a| !a.is_alphabetic()) {
                out.push('y');
                i += 2;
                word_start = false;
                continue;
            }
        }
        if ('а'..='я').contains(&lower) || lower == 'ё' {
            let t = letter(lower, s, word_start || prev_soft);
            if c.is_uppercase() {
                let mut cs = t.chars();
                if let Some(f) = cs.next() {
                    out.extend(f.to_uppercase());
                    // `Щ` в слове капсом — `SHCH`, в слове с заглавной — `Shch`.
                    let all_caps = chars.get(i + 1).is_some_and(|n| n.is_uppercase());
                    for r in cs {
                        if all_caps {
                            out.extend(r.to_uppercase());
                        } else {
                            out.push(r);
                        }
                    }
                }
            } else {
                out.push_str(t);
            }
            word_start = false;
        } else if c.is_alphabetic() {
            return None;
        } else {
            out.push(c);
            word_start = true;
        }
        i += 1;
    }
    Some(out)
}

/// Варианты написания латиницей без повторов; пусто, если в тексте нет кириллицы.
pub fn variants(text: &str) -> Vec<String> {
    if !text
        .chars()
        .any(|c| matches!(c, 'а'..='я' | 'А'..='Я' | 'ё' | 'Ё'))
    {
        return Vec::new();
    }
    let mut out: Vec<String> = Vec::new();
    for s in [Scheme::Practical, Scheme::Icao, Scheme::Casual] {
        if let Some(v) = convert(text, s)
            && !out.contains(&v)
        {
            out.push(v);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    #[test]
    fn schemes() {
        let v = super::variants("Голошанов");
        assert_eq!(v, ["Goloshanov"]);
        let v = super::variants("Юлия Достоевская");
        assert!(v.contains(&"Yuliya Dostoyevskaya".to_string()), "{v:?}");
        assert!(v.contains(&"Iuliia Dostoevskaia".to_string()), "{v:?}");
        let v = super::variants("Харитон Достоевский");
        assert!(v.contains(&"Khariton Dostoyevskiy".to_string()), "{v:?}");
        assert!(v.contains(&"Hariton Dostoevsky".to_string()), "{v:?}");
        assert!(super::variants("Smith").is_empty());
    }
}
