use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

use crate::score::{Strategy, logit, sigmoid};

/// Контур правил и записей vault. Порядок важен: `Global` старше `Team` старше `Personal`.
#[derive(
    Clone, Copy, Debug, Default, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize,
)]
#[serde(rename_all = "lowercase")]
pub enum Scope {
    #[default]
    Personal,
    Team,
    Global,
}

impl Scope {
    pub fn as_str(&self) -> &'static str {
        match self {
            Scope::Personal => "personal",
            Scope::Team => "team",
            Scope::Global => "global",
        }
    }
}

impl fmt::Display for Scope {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Scope {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_ascii_lowercase().as_str() {
            "personal" => Ok(Scope::Personal),
            "team" => Ok(Scope::Team),
            "global" => Ok(Scope::Global),
            other => Err(format!("unknown scope `{other}` (personal|team|global)")),
        }
    }
}

/// Откуда взялся кандидат: встроенный детектор или правило конкретного контура.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum Origin {
    #[default]
    Builtin,
    Rule {
        scope: Scope,
        enforced: bool,
        id: String,
    },
}

impl Origin {
    /// Старшинство при пересечениях: Global enforced > Global > Team > Personal > Builtin.
    pub fn rank(&self) -> u8 {
        match self {
            Origin::Builtin => 0,
            Origin::Rule {
                scope: Scope::Personal,
                ..
            } => 1,
            Origin::Rule {
                scope: Scope::Team, ..
            } => 2,
            Origin::Rule {
                scope: Scope::Global,
                enforced: false,
                ..
            } => 3,
            Origin::Rule {
                scope: Scope::Global,
                enforced: true,
                ..
            } => 4,
        }
    }

    pub fn is_enforced(&self) -> bool {
        matches!(
            self,
            Origin::Rule {
                scope: Scope::Global,
                enforced: true,
                ..
            }
        )
    }
}

/// Категория данных определяет пороги решения.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Category {
    /// Ключи, токены, пароли — самодостаточны для эксплуатации.
    Credential,
    /// IP, MAC.
    Network,
    /// Домены, пути, имена сервисов и проектов.
    Infra,
    /// Люди, логины, документы личности.
    Identity,
    /// Счета, карты, реквизиты.
    Financial,
    /// Email, телефоны.
    Contact,
    Custom,
}

impl Category {
    pub const ALL: [Category; 7] = [
        Category::Credential,
        Category::Network,
        Category::Infra,
        Category::Identity,
        Category::Financial,
        Category::Contact,
        Category::Custom,
    ];
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Polarity {
    #[default]
    Mask,
    /// Allowlist: «никогда не маскировать».
    Keep,
}

/// Форма токена зависит от контекста подстановки.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TokenForm {
    /// `ANON_SVC_1` — проза, код, конфиги, пути.
    #[default]
    Snake,
    /// `anon-svc-1` — DNS-метки, хостнеймы, имена подов.
    Dns,
}

/// Свидетельство в пользу (logit > 0) или против (logit < 0) маскирования.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Evidence {
    pub source: String,
    pub logit: f32,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Format {
    #[default]
    Auto,
    Text,
    Yaml,
    Json,
    Toml,
    Env,
    Ini,
    Csv,
    Tsv,
}

impl FromStr for Format {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s.to_ascii_lowercase().as_str() {
            "auto" => Format::Auto,
            "text" | "txt" | "md" => Format::Text,
            "yaml" | "yml" => Format::Yaml,
            "json" => Format::Json,
            "toml" => Format::Toml,
            "env" | "dotenv" => Format::Env,
            "ini" | "conf" | "cfg" => Format::Ini,
            "csv" => Format::Csv,
            "tsv" => Format::Tsv,
            other => return Err(format!("unknown format `{other}`")),
        })
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Input<'a> {
    pub text: &'a str,
    pub format: Format,
}

impl<'a> Input<'a> {
    pub fn text(text: &'a str) -> Self {
        Self {
            text,
            format: Format::Auto,
        }
    }
}

/// Кандидат на замену, найденный распознавателем. Диапазон — байтовые смещения в исходном тексте.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Candidate {
    pub start: usize,
    pub end: usize,
    pub kind: String,
    pub category: Category,
    pub recognizer: String,
    /// Канонический ключ для vault; `None` — поверхностная форма (или нормализация пакета).
    pub canonical: Option<String>,
    pub origin: Origin,
    pub polarity: Polarity,
    /// Учётные данные и значения с валидной контрольной суммой: не ослабляются allowlist и S1.
    pub hard: bool,
    pub form: TokenForm,
    pub evidence: Vec<Evidence>,
    /// Переопределение стратегии подстановки для этого кандидата.
    pub strategy: Option<Strategy>,
    /// Контур записи в vault; `None` — контур из опций запроса.
    pub scope: Option<Scope>,
    /// Словоформа: токен заменяет слово, а окончание остаётся после него (`Иванову` →
    /// `ANON_LNAME_1у`), в vault пишется начальная форма.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inflection: Option<Inflection>,
}

/// Окончание, которое остаётся в тексте после токена, и начальная форма для vault.
/// Инвариант: пакет восстанавливает из `lemma` и окончания ровно исходное слово.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Inflection {
    pub lemma: String,
    /// Длина окончания в байтах (хвост диапазона кандидата).
    pub ending: usize,
}

impl Candidate {
    /// `prior` — априорная вероятность того, что фрагмент чувствителен.
    pub fn new(
        start: usize,
        end: usize,
        kind: impl Into<String>,
        category: Category,
        recognizer: impl Into<String>,
        prior: f32,
    ) -> Self {
        let recognizer = recognizer.into();
        Self {
            start,
            end,
            kind: kind.into(),
            category,
            evidence: vec![Evidence {
                source: format!("prior:{recognizer}"),
                logit: logit(prior),
            }],
            recognizer,
            canonical: None,
            origin: Origin::Builtin,
            polarity: Polarity::Mask,
            hard: false,
            form: TokenForm::Snake,
            strategy: None,
            scope: None,
            inflection: None,
        }
    }

    pub fn keep(start: usize, end: usize, recognizer: impl Into<String>, origin: Origin) -> Self {
        let mut c = Self::new(start, end, "KEEP", Category::Custom, recognizer, 0.5);
        c.polarity = Polarity::Keep;
        c.origin = origin;
        c
    }

    pub fn hard(mut self) -> Self {
        self.hard = true;
        self
    }

    pub fn dns(mut self) -> Self {
        self.form = TokenForm::Dns;
        self
    }

    pub fn canonical(mut self, key: impl Into<String>) -> Self {
        self.canonical = Some(key.into());
        self
    }

    pub fn origin(mut self, origin: Origin) -> Self {
        if let Origin::Rule { scope, .. } = &origin {
            self.scope = Some(*scope);
        }
        self.origin = origin;
        self
    }

    pub fn with_evidence(mut self, source: impl Into<String>, logit: f32) -> Self {
        self.evidence.push(Evidence {
            source: source.into(),
            logit,
        });
        self
    }

    pub fn strategy(mut self, strategy: Strategy) -> Self {
        self.strategy = Some(strategy);
        self
    }

    /// Окончание `suffix` (хвост диапазона) остаётся после токена, в vault — `lemma`.
    pub fn inflected(mut self, lemma: impl Into<String>, suffix_len: usize) -> Self {
        if suffix_len > 0 && suffix_len < self.len() {
            self.inflection = Some(Inflection {
                lemma: lemma.into(),
                ending: suffix_len,
            });
        }
        self
    }

    pub fn len(&self) -> usize {
        self.end - self.start
    }

    pub fn is_empty(&self) -> bool {
        self.start >= self.end
    }

    pub fn total_logit(&self) -> f32 {
        self.evidence.iter().map(|e| e.logit).sum()
    }

    pub fn probability(&self) -> f32 {
        sigmoid(self.total_logit())
    }

    pub fn surface<'t>(&self, text: &'t str) -> &'t str {
        &text[self.start..self.end]
    }
}
