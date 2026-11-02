//! Правила пользователя и команды: что маскировать всегда, что никогда, составные шаблоны.
//! Здесь только данные; компиляция в распознаватель — в `kaponir-recognizers`.

use serde::{Deserialize, Serialize};

use crate::lexicon::Lexicon;
use crate::score::Strategy;
use crate::types::{Category, Scope, TokenForm};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RuleAction {
    #[default]
    Mask,
    Keep,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Rule {
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub id: String,
    #[serde(default = "default_kind")]
    pub kind: String,
    #[serde(default)]
    pub action: RuleAction,
    /// Точное значение (по границам слова).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value: Option<String>,
    /// Составной шаблон: `{env:prod|stage}-{SVC}-{hash}`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub template: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub regex: Option<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub case_sensitive: bool,
    /// Только для Global: правило нельзя перекрыть правилами других контуров.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub enforced: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub category: Option<Category>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub strategy: Option<Strategy>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub form: Option<TokenForm>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub note: String,
}

fn default_kind() -> String {
    "CUSTOM".into()
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CorpSection {
    /// Корпоративные домены: метка регистрируемого домена маскируется всегда.
    #[serde(default)]
    pub domains: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct RuleFile {
    #[serde(default)]
    pub corp: CorpSection,
    #[serde(default, rename = "rule")]
    pub rules: Vec<Rule>,
}

impl RuleFile {
    pub fn parse(s: &str) -> Result<Self, String> {
        toml::from_str(s).map_err(|e: toml::de::Error| e.to_string())
    }

    /// Вклад файла в общий словарь: однословные «никогда не маскировать» и корпоративные домены.
    pub fn lexicon(&self) -> Lexicon {
        let mut lex = Lexicon::default();
        for d in &self.corp.domains {
            lex.corp_domains
                .insert(d.trim().trim_start_matches('.').to_lowercase());
        }
        for r in &self.rules {
            if r.action == RuleAction::Keep
                && let Some(v) = &r.value
                && !v.is_empty()
                && v.chars().all(char::is_alphanumeric)
            {
                lex.words.insert(v.to_lowercase());
            }
        }
        lex
    }
}

/// Правила одного контура.
#[derive(Clone, Debug)]
pub struct ScopedRules {
    pub scope: Scope,
    pub file: RuleFile,
}
