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
    /// Disabled rules are retained for audit/undo but never applied or trained on.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub disabled: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub category: Option<Category>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub strategy: Option<Strategy>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub form: Option<TokenForm>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub note: String,
}

/// Traversal and joined expressions do not describe a single reviewed directory tree.
pub fn unambiguous_path(path: &str, separator: char) -> bool {
    let doubled = if separator == '/' { "//" } else { r"\\" };
    !path.contains(doubled)
        && !path
            .split(separator)
            .any(|part| matches!(part, "." | "..") || part.ends_with([',', '=']))
}

impl Rule {
    /// A component-bounded self. Capture just the directory so restoration retains the separator.
    pub fn for_directory(prefix: &str, scope: Scope) -> Option<Self> {
        if prefix.len() < 2 || !prefix.ends_with(['/', '\\']) {
            return None;
        }
        let (directory, sep) = prefix.split_at(prefix.len() - 1);
        let digest = blake3::hash(format!("{}\0{prefix}", scope.as_str()).as_bytes()).to_hex();
        Some(Self {
            id: format!("path-prefix-{}", &digest[..24]),
            kind: "PATH".into(),
            case_sensitive: true,
            regex: Some(format!(
                r"(?:^|[^\p{{L}}\p{{N}}_./\\-])({}){}",
                regex::escape(directory),
                regex::escape(sep)
            )),
            note: prefix.to_owned(),
            ..Rule::default()
        })
    }

    pub fn directory_prefix(&self, scope: Scope) -> Option<&str> {
        if !self.id.starts_with("path-prefix-")
            || self.kind != "PATH"
            || self.action != RuleAction::Mask
            || !self.case_sensitive
            || !self.note.ends_with(['/', '\\'])
        {
            return None;
        }
        let expected = Self::for_directory(&self.note, scope)?;
        (self.id == expected.id && self.regex == expected.regex).then_some(&self.note)
    }

    /// Offsets of the first folder when the approved prefix includes its account folder.
    pub fn directory_domain(&self, scope: Scope) -> Option<[usize; 2]> {
        let home = "/home/";
        let (domain, tail) = self
            .directory_prefix(scope)?
            .strip_prefix(home)?
            .split_once('/')?;
        let (account, _) = tail.split_once('/')?;
        (!domain.is_empty() && !account.is_empty())
            .then_some([home.len(), home.len() + domain.len()])
    }
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
        for r in self.rules.iter().filter(|r| !r.disabled) {
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

impl ScopedRules {
    pub fn lexicon(&self) -> Lexicon {
        let mut lexicon = self.file.lexicon();
        lexicon.reviewed_directories.extend(
            self.file
                .rules
                .iter()
                .filter(|r| !r.disabled)
                .filter_map(|r| r.directory_prefix(self.scope))
                .map(str::to_owned),
        );
        lexicon
    }
}
