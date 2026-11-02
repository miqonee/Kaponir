//! Пакет-код `lang-ru`: данные пакета `packs/lang-ru` (слова, имена, окончания фамилий) плюс
//! морфология имён — разбор словоформ, склонение при восстановлении (`ANON_LNAME_1у` → `Иванову`)
//! и транслитерация для предложений `Голошанов ↔ Goloshanov`.

pub mod morph;
pub mod translit;

use kaponir_core::{Gender, Lexicon, NameRole, Pack, PackInfo, WordForm, is_name_kind, name_key};
use kaponir_packs::DataPack;
use std::sync::Arc;

use morph::{Class, NOM, OBLIQUE};

pub struct LangRu {
    info: PackInfo,
    lexicon: Lexicon,
}

impl Default for LangRu {
    fn default() -> Self {
        Self::new()
    }
}

fn cyrillic_word(w: &str) -> bool {
    !w.is_empty()
        && w.chars()
            .all(|c| matches!(c, 'а'..='я' | 'А'..='Я' | 'ё' | 'Ё'))
}

impl LangRu {
    /// Встроенные данные `packs/lang-ru`.
    pub fn new() -> Self {
        Self::from_data(kaponir_packs::builtin_data("lang-ru").expect("lang-ru is embedded"))
    }

    /// Морфология поверх данных из каталога (проверка изменённого пакета: `kaponir pack test`).
    pub fn from_data(data: DataPack) -> Self {
        let (info, lexicon) = data.into_parts();
        Self { info, lexicon }
    }

    pub fn arc() -> Arc<dyn Pack> {
        Arc::new(Self::new())
    }

    /// Роли, в которых слово может быть начальной формой: по списку имён и окончаниям.
    fn roles(&self, lemma: &str) -> Vec<(NameRole, Option<Gender>)> {
        let n = &self.lexicon.names;
        let mut out = Vec::new();
        if let Some(g) = n.given(lemma) {
            out.push((NameRole::Given, g));
        }
        if let Some(g) = n.patronymic(lemma) {
            out.push((
                NameRole::Patronymic,
                g.or_else(|| morph::gender_by_ending(lemma, NameRole::Patronymic)),
            ));
        }
        if let Some(g) = n.surname(lemma) {
            match g.or_else(|| morph::gender_by_ending(lemma, NameRole::Surname)) {
                Some(g) => out.push((NameRole::Surname, Some(g))),
                // Окончание без рода (-енко, -ук): мужская фамилия склоняется, женская — нет.
                None => {
                    out.push((NameRole::Surname, Some(Gender::Male)));
                    out.push((NameRole::Surname, Some(Gender::Female)));
                }
            }
        }
        out
    }

    /// Роль слова без подсказки (значение правила `PERSON`): имя, отчество, иначе фамилия.
    fn infer_role(&self, word: &str) -> (NameRole, Option<Gender>) {
        self.roles(word).into_iter().next().unwrap_or_else(|| {
            (
                NameRole::Surname,
                morph::gender_by_ending(word, NameRole::Surname),
            )
        })
    }

    fn role_for(&self, kind: &str, word: &str) -> (NameRole, Option<Gender>) {
        match NameRole::from_kind(kind) {
            Some(role) => {
                let gender = self
                    .roles(word)
                    .into_iter()
                    .find(|(r, _)| *r == role)
                    .and_then(|(_, g)| g)
                    .or_else(|| morph::gender_by_ending(word, role));
                (role, gender)
            }
            None => self.infer_role(word),
        }
    }

    fn analyze(&self, word: &str) -> Vec<WordForm> {
        if !cyrillic_word(word) {
            return Vec::new();
        }
        let mut out: Vec<WordForm> = Vec::new();
        let mut push = |f: WordForm| {
            if !out.contains(&f) {
                out.push(f);
            }
        };
        for (role, gender) in self.roles(word) {
            push(WordForm {
                lemma: word.to_string(),
                suffix: String::new(),
                role: Some(role),
                gender,
                case: Some(NOM),
            });
        }
        let n = word.chars().count();
        for e in morph::ENDINGS {
            let el = e.chars().count();
            if !word.ends_with(e) || n < el + 2 {
                continue;
            }
            let stem = &word[..word.len() - e.len()];
            for base in morph::lemma_stems(stem) {
                for tail in morph::LEMMA_TAILS {
                    let lemma = format!("{base}{tail}");
                    if lemma == word {
                        continue;
                    }
                    for (role, gender) in self.roles(&lemma) {
                        let class = morph::class(&lemma, role, gender);
                        for (case, form, ending) in morph::forms(&lemma, class, role) {
                            if form == word && ending == *e {
                                push(WordForm {
                                    lemma: lemma.clone(),
                                    suffix: ending,
                                    role: Some(role),
                                    gender: gender.or_else(|| class_gender(class)),
                                    case: Some(case),
                                });
                            }
                        }
                    }
                }
            }
        }
        out
    }

    /// Склоняет одно слово так, чтобы окончание совпало с `suffix`; если такого окончания
    /// в парадигме нет — по падежу, который окончание обычно означает.
    fn decline_word(
        &self,
        word: &str,
        role: NameRole,
        gender: Option<Gender>,
        suffix: &str,
    ) -> Option<String> {
        let class = morph::class(word, role, gender);
        let forms = morph::forms(word, class, role);
        if let Some((_, f, _)) = forms.iter().find(|(_, _, e)| e == suffix) {
            return Some(f.clone());
        }
        let case = morph::guess_case(suffix)?;
        if class == Class::Indecl {
            return Some(word.to_string());
        }
        forms
            .into_iter()
            .find(|(c, _, _)| *c == case)
            .map(|(_, f, _)| f)
    }

    fn word_in_case(
        &self,
        word: &str,
        role: NameRole,
        gender: Option<Gender>,
        case: u8,
    ) -> (String, String) {
        let class = morph::class(word, role, gender);
        morph::forms(word, class, role)
            .into_iter()
            .find(|(c, _, _)| *c == case)
            .map(|(_, f, e)| (f, e))
            .unwrap_or_else(|| (word.to_string(), String::new()))
    }

    /// Роли и общий род слов многословного значения (`Иван Петров`).
    fn person(&self, words: &[&str]) -> (Vec<NameRole>, Option<Gender>) {
        let roles: Vec<(NameRole, Option<Gender>)> =
            words.iter().map(|w| self.infer_role(w)).collect();
        let gender = roles.iter().find_map(|(_, g)| *g);
        (roles.into_iter().map(|(r, _)| r).collect(), gender)
    }

    fn decline_person(&self, words: &[&str], case: u8) -> (String, String) {
        let (roles, gender) = self.person(words);
        let mut parts = Vec::new();
        let mut last_ending = String::new();
        for (w, r) in words.iter().zip(roles) {
            let (f, e) = self.word_in_case(w, r, gender, case);
            parts.push(f);
            last_ending = e;
        }
        (parts.join(" "), last_ending)
    }
}

fn class_gender(c: Class) -> Option<Gender> {
    match c {
        Class::FemOva | Class::FemSkaya | Class::FemSoft => Some(Gender::Female),
        Class::MascOv | Class::MascSky | Class::MascOj => Some(Gender::Male),
        _ => None,
    }
}

impl Pack for LangRu {
    fn info(&self) -> &PackInfo {
        &self.info
    }

    fn lexicon(&self) -> &Lexicon {
        &self.lexicon
    }

    fn canonicalize(&self, kind: &str, surface: &str) -> Option<String> {
        if !is_name_kind(kind) {
            return None;
        }
        let words: Vec<&str> = surface.split(' ').collect();
        if words.len() != 1 || !cyrillic_word(surface) {
            return words
                .iter()
                .all(|w| cyrillic_word(w))
                .then(|| name_key(surface));
        }
        let forms = self.analyze(surface);
        let lemma = forms
            .iter()
            .find(|f| f.suffix.is_empty())
            .or_else(|| forms.first())
            .map_or(surface, |f| f.lemma.as_str());
        Some(name_key(lemma))
    }

    fn restore_form(&self, kind: &str, original: &str, suffix: &str) -> Option<String> {
        if !is_name_kind(kind) {
            return None;
        }
        let words: Vec<&str> = original.split(' ').collect();
        if !words.iter().all(|w| cyrillic_word(w))
            || !suffix.chars().all(|c| matches!(c, 'а'..='я' | 'ё'))
        {
            return None;
        }
        if let [word] = words.as_slice() {
            let (role, gender) = self.role_for(kind, word);
            return self.decline_word(word, role, gender, suffix);
        }
        // Несколько слов (правило PERSON): падеж — по окончанию последнего слова.
        let last = words[words.len() - 1];
        let (roles, gender) = self.person(&words);
        let last_role = roles[roles.len() - 1];
        let class = morph::class(last, last_role, gender);
        let forms = morph::forms(last, class, last_role);
        let case = forms
            .iter()
            .find(|(_, _, e)| e == suffix)
            .map(|(c, _, _)| *c)
            .or_else(|| morph::guess_case(suffix))?;
        Some(self.decline_person(&words, case).0)
    }

    fn name_forms(&self, word: &str) -> Vec<WordForm> {
        self.analyze(word)
    }

    fn inflect(&self, kind: &str, lemma: &str) -> Vec<(String, String)> {
        if !is_name_kind(kind) {
            return Vec::new();
        }
        let words: Vec<&str> = lemma.split(' ').collect();
        if !words.iter().all(|w| cyrillic_word(w)) {
            return Vec::new();
        }
        let mut out: Vec<(String, String)> = Vec::new();
        for case in OBLIQUE {
            let (form, ending) = match words.as_slice() {
                [word] => {
                    let (role, gender) = self.role_for(kind, word);
                    self.word_in_case(word, role, gender, case)
                }
                _ => self.decline_person(&words, case),
            };
            if form == lemma || out.iter().any(|(f, _)| *f == form) {
                continue;
            }
            // Окончание после токена — только если восстановится ровно эта форма; иначе форма
            // маскируется целиком (без окончания), но не пропускается.
            let ending = if !ending.is_empty()
                && self.restore_form(kind, lemma, &ending).as_deref() == Some(form.as_str())
            {
                ending
            } else {
                String::new()
            };
            out.push((form, ending));
        }
        out
    }

    fn transliterate(&self, text: &str) -> Vec<String> {
        translit::variants(text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lemma_of(p: &LangRu, w: &str) -> Vec<(String, String, Option<NameRole>)> {
        p.name_forms(w)
            .into_iter()
            .map(|f| (f.lemma, f.suffix, f.role))
            .collect()
    }

    #[test]
    fn analyzes_word_forms() {
        let p = LangRu::new();
        assert!(lemma_of(&p, "Ивану").contains(&(
            "Иван".into(),
            "у".into(),
            Some(NameRole::Given)
        )));
        assert!(lemma_of(&p, "Петра").contains(&(
            "Пётр".into(),
            "а".into(),
            Some(NameRole::Given)
        )));
        assert!(lemma_of(&p, "Марией").contains(&(
            "Мария".into(),
            "ей".into(),
            Some(NameRole::Given)
        )));
        assert!(lemma_of(&p, "Ивановой").contains(&(
            "Иванова".into(),
            "ой".into(),
            Some(NameRole::Surname)
        )));
        assert!(lemma_of(&p, "Голошанову").contains(&(
            "Голошанов".into(),
            "у".into(),
            Some(NameRole::Surname)
        )));
        assert!(lemma_of(&p, "Сергеевичем").contains(&(
            "Сергеевич".into(),
            "ем".into(),
            Some(NameRole::Patronymic)
        )));
        // Иванова: женская фамилия в именительном или мужская в родительном.
        let f = lemma_of(&p, "Иванова");
        assert!(f.contains(&("Иванова".into(), "".into(), Some(NameRole::Surname))));
        assert!(f.contains(&("Иванов".into(), "а".into(), Some(NameRole::Surname))));
        assert!(lemma_of(&p, "Smith").is_empty());
    }

    #[test]
    fn restores_forms_from_suffix() {
        let p = LangRu::new();
        assert_eq!(
            p.restore_form("LNAME", "Голошанов", "у").as_deref(),
            Some("Голошанову")
        );
        assert_eq!(
            p.restore_form("LNAME", "Голошанов", "ым").as_deref(),
            Some("Голошановым")
        );
        assert_eq!(
            p.restore_form("FNAME", "Анна", "ой").as_deref(),
            Some("Анной")
        );
        assert_eq!(
            p.restore_form("FNAME", "Пётр", "у").as_deref(),
            Some("Петру")
        );
        // Модель приклеила «мужское» окончание к женскому имени — падеж всё равно понятен.
        assert_eq!(
            p.restore_form("FNAME", "Анна", "ом").as_deref(),
            Some("Анной")
        );
        assert_eq!(
            p.restore_form("LNAME", "Шевченко", "у").as_deref(),
            Some("Шевченко")
        );
        assert_eq!(
            p.restore_form("PERSON", "Иван Петров", "у").as_deref(),
            Some("Ивану Петрову")
        );
        assert_eq!(p.restore_form("SVC", "Голошанов", "у"), None);
        assert_eq!(p.restore_form("LNAME", "Smith", "s"), None);
    }

    #[test]
    fn inflects_rule_values() {
        let p = LangRu::new();
        let forms = p.inflect("LNAME", "Голошанов");
        assert!(forms.contains(&("Голошанову".into(), "у".into())));
        assert!(forms.contains(&("Голошановым".into(), "ым".into())));
        let person = p.inflect("PERSON", "Анна Петрова");
        // Дательный и родительный дают одно окончание фамилии — однозначно восстановить
        // можно только один из них, другой маскируется целиком.
        assert!(person.contains(&("Анны Петровой".into(), "ой".into())));
        assert!(person.contains(&("Анне Петровой".into(), "".into())));
        assert_eq!(
            p.canonicalize("LNAME", "Голошанову").as_deref(),
            Some("голошанов")
        );
    }
}
