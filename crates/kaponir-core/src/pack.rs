//! Контракт пакетов: языковых, региональных, предметных. Ядро не зависит ни от одного пакета.

use serde::{Deserialize, Serialize};

use crate::lexicon::Lexicon;
use crate::recognizer::Recognizer;

/// Версия контракта. Пакет с другой мажорной версией не загружается.
pub const PACK_API_VERSION: u32 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PackKind {
    Lang,
    Region,
    Domain,
    Common,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PackInfo {
    pub id: String,
    pub kind: PackKind,
    pub version: String,
    #[serde(default = "default_api")]
    pub api_version: u32,
    #[serde(default)]
    pub scripts: Vec<String>,
    #[serde(default)]
    pub license: String,
    #[serde(default)]
    pub description: String,
}

fn default_api() -> u32 {
    PACK_API_VERSION
}

/// Роль слова в имени человека.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum NameRole {
    Given,
    Patronymic,
    Surname,
}

impl NameRole {
    /// KIND токена для части имени.
    pub fn kind(&self) -> &'static str {
        match self {
            NameRole::Given => "FNAME",
            NameRole::Patronymic => "MNAME",
            NameRole::Surname => "LNAME",
        }
    }

    pub fn from_kind(kind: &str) -> Option<Self> {
        match kind {
            "FNAME" => Some(NameRole::Given),
            "MNAME" => Some(NameRole::Patronymic),
            "LNAME" => Some(NameRole::Surname),
            _ => None,
        }
    }
}

/// KIND, которые означают имя человека или его часть.
pub const NAME_KINDS: &[&str] = &["FNAME", "MNAME", "LNAME", "PERSON"];

pub fn is_name_kind(kind: &str) -> bool {
    NAME_KINDS.contains(&kind)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Gender {
    Male,
    Female,
}

/// Разбор словоформы: `surface == restore_form(kind, lemma, suffix)`.
/// `suffix` — окончание, которое остаётся в тексте после токена (`ANON_LNAME_1у`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WordForm {
    /// Начальная форма в регистре исходного слова.
    pub lemma: String,
    pub suffix: String,
    pub role: Option<NameRole>,
    pub gender: Option<Gender>,
    /// Грамматический падеж в нумерации пакета: слова одного имени должны согласоваться.
    /// `None` — согласуется с любым.
    pub case: Option<u8>,
}

pub trait Pack: Send + Sync {
    fn info(&self) -> &PackInfo;

    fn lexicon(&self) -> &Lexicon;

    /// Канонический ключ для vault (лемма, нормальный порядок ФИО и т.п.).
    fn canonicalize(&self, _kind: &str, _surface: &str) -> Option<String> {
        None
    }

    /// Восстановление с буквенным хвостом, приклеенным LLM к токену (`ANON_PERSON_1у`).
    /// Возвращает замену для «токен + суффикс»; `None` — суффикс сохраняется как есть.
    fn restore_form(&self, _kind: &str, _original: &str, _suffix: &str) -> Option<String> {
        None
    }

    fn recognizers(&self) -> Vec<Box<dyn Recognizer>> {
        Vec::new()
    }

    /// Возможные разборы слова как части имени (лемма, окончание, роль, род, падеж).
    /// Именительный падеж тоже входит в список. Пустой список — пакет слово не знает.
    fn name_forms(&self, _word: &str) -> Vec<WordForm> {
        Vec::new()
    }

    /// Словоформы значения типа `kind` для поиска в тексте: `(форма, окончание после токена)`.
    /// Нужны правилам и памяти сессии: «Голошанов» в правиле находит «Голошанову».
    fn inflect(&self, _kind: &str, _lemma: &str) -> Vec<(String, String)> {
        Vec::new()
    }

    /// Варианты написания другой письменностью (`Голошанов` → `Goloshanov`). Только предложения:
    /// связь значений подтверждает человек.
    fn transliterate(&self, _text: &str) -> Vec<String> {
        Vec::new()
    }
}
