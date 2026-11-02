//! S1-провайдеры вероятностей для спорных кандидатов.
//!
//! - `ngram` — локальная логистическая модель на хешированных символьных триграммах и форме
//!   значения. Видит только признаки (L0), обучается только на подтверждённых метках: решениях
//!   разбора журнала и значениях правил. Новая версия заменяет старую с сохранением предыдущей
//!   (`kaponir s1 rollback`), проверяется в теневом режиме.
//! - `http` — внешний сервис оценки. Раскрытие ограничено настройкой (L0/L1/L2), учётные данные
//!   ядро всё равно отдаёт только как L0; ответ ждём не дольше бюджета задержки, сбой ⇒ политика.

pub mod http;
pub mod ngram;

use kaponir_core::rules::ScopedRules;
use kaponir_core::s1::{Calibration, Disclosure, S1Config, S1Provider, confirmed_profile};
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Msg(String),
    #[error("{path}: {source}")]
    Io {
        path: String,
        source: std::io::Error,
    },
    #[error("{path}: {source}")]
    Json {
        path: String,
        source: serde_json::Error,
    },
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ProviderKind {
    /// Без S1: решения только по детекторам, правилам и политике.
    #[default]
    None,
    Ngram,
    Http,
}

/// Уровень раскрытия в конфиге (`l0`, `l1`, `l2`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Level {
    #[default]
    L0,
    L1,
    L2,
}

impl From<Level> for Disclosure {
    fn from(l: Level) -> Self {
        match l {
            Level::L0 => Disclosure::L0,
            Level::L1 => Disclosure::L1,
            Level::L2 => Disclosure::L2,
        }
    }
}

/// Секция `[s1]` конфига.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Settings {
    pub provider: ProviderKind,
    /// Вес слагаемого S1 в log-odds.
    pub weight: f32,
    /// Ослабить решение (не маскировать) S1 может только при откалиброванной `p ≤ keep_below`.
    pub keep_below: f32,
    /// Оценка пишется в объяснение и журнал, но решение не меняет.
    pub shadow: bool,
    /// Байты UTF-8 контекста до и после кандидата на L2.
    pub context_bytes: usize,
    /// Калибровка Платта поверх выхода провайдера. Для `ngram` по умолчанию — из файла модели.
    pub calibration: Option<Calibration>,
    pub http: http::HttpSettings,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            provider: ProviderKind::None,
            weight: 1.0,
            keep_below: 0.1,
            shadow: false,
            context_bytes: 60,
            calibration: None,
            http: http::HttpSettings::default(),
        }
    }
}

impl Settings {
    pub fn validate(&self) -> Result<(), Error> {
        if !(0.0..=4.0).contains(&self.weight) {
            return Err(Error::Msg(format!(
                "[s1] weight = {}: need 0 ≤ weight ≤ 4",
                self.weight
            )));
        }
        if !(0.0..=0.5).contains(&self.keep_below) {
            return Err(Error::Msg(format!(
                "[s1] keep_below = {}: need 0 ≤ keep_below ≤ 0.5",
                self.keep_below
            )));
        }
        if self.context_bytes > 4096 {
            return Err(Error::Msg(format!(
                "[s1] context_bytes = {}: need 0–4096",
                self.context_bytes
            )));
        }
        if let Some(c) = self.calibration
            && (!c.a.is_finite() || !c.b.is_finite() || c.a <= 0.0)
        {
            return Err(Error::Msg(
                "[s1] calibration: need finite a > 0 and b".into(),
            ));
        }
        Ok(())
    }
}

type ProviderWithCalibration = (Box<dyn S1Provider>, Calibration);

/// Провайдер из настроек. `dir` — каталог моделей (`KAPONIR_HOME/s1`).
pub fn provider(s: &Settings, dir: &Path) -> Result<Option<ProviderWithCalibration>, Error> {
    Ok(match s.provider {
        ProviderKind::None => None,
        ProviderKind::Ngram => {
            let path = dir.join(ngram::MODEL_FILE);
            if !path.exists() {
                return Err(Error::Msg(format!(
                    "[s1] provider = \"ngram\", but {} does not exist: run `kaponir s1 train` or set provider = \"none\"",
                    path.display()
                )));
            }
            let m = ngram::Model::load(&path)?;
            let cal = m.calibration;
            Some((Box::new(m), cal))
        }
        ProviderKind::Http => Some((
            Box::new(http::HttpProvider::new(&s.http)?),
            Calibration::default(),
        )),
    })
}

/// Конфигурация S1 для движка; `None` — S1 выключен.
pub fn build(s: &Settings, dir: &Path, rules: &[ScopedRules]) -> Result<Option<S1Config>, Error> {
    s.validate()?;
    let Some((provider, model_cal)) = provider(s, dir)? else {
        return Ok(None);
    };
    Ok(Some(S1Config {
        provider,
        calibration: s.calibration.unwrap_or(model_cal),
        weight: s.weight,
        keep_below: s.keep_below,
        shadow: s.shadow,
        context_bytes: s.context_bytes,
        profile: std::sync::Arc::new(confirmed_profile(rules)),
    }))
}

/// Оценка из теневого режима в свидетельствах (`s1-shadow:<id>:p=0.87`).
pub fn shadow_probability(source: &str) -> Option<(&str, f32)> {
    let rest = source.strip_prefix("s1-shadow:")?;
    let (id, p) = rest.rsplit_once(":p=")?;
    Some((id, p.parse().ok()?))
}
