//! Прозрачный прокси для LLM-агентов и sidecar REST поверх ядра. Маскирует запросы
//! (Anthropic Messages, OpenAI Chat Completions), восстанавливает ответы, в том числе потоковые
//! и аргументы вызовов инструментов.
//!
//! ```text
//! клиент → kaponir serve → провайдер (или 9router)        POST /v1/messages, /v1/chat/completions
//! шлюз   → /api/v1/anonymize, /api/v1/deanonymize[/stream]
//! браузер → /ui/ (Web UI) → /api/v1/ui/*, /api/v1/journal*, /api/v1/rules*
//! ```

pub mod anthropic;
pub mod discovery;
mod metrics;
pub mod openai;
pub mod providers;
mod server;
pub mod sse;
mod state;
mod traffic;
mod walk;
mod web;
mod worker;

use kaponir_core::rules::{Rule, RuleFile};
use kaponir_core::{Engine, Scope};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

pub use server::router;
pub use state::{Cx, State};

pub type Shared = Arc<State>;

/// Правила одного контура, как они лежат в хранилище правил.
#[derive(Clone, Debug, Serialize)]
pub struct RuleSet {
    pub scope: Scope,
    /// Где лежат правила (путь к файлу) — для показа пользователю.
    pub location: String,
    pub file: RuleFile,
}

/// Хранилище правил контуров и сборка движка из них. Реализуется приложением (файлы в `KAPONIR_HOME`),
/// прокси о раскладке файлов не знает. После каждого изменения UI пересобирает движок.
pub trait Control: Send + Sync {
    fn export_settings(&self, _include_secrets: bool) -> anyhow::Result<serde_json::Value> {
        anyhow::bail!("settings transfer is unavailable")
    }
    fn import_settings(&self, _bundle: serde_json::Value) -> anyhow::Result<()> {
        anyhow::bail!("settings transfer is unavailable")
    }
    fn rules(&self) -> anyhow::Result<Vec<RuleSet>>;
    fn s1_settings(&self) -> anyhow::Result<kaponir_s1::Settings>;
    fn s1_local_model_available(&self) -> anyhow::Result<bool>;
    /// Проверяет и сохраняет S1. Ключ нужен только для проверки; Web UI хранит его в vault.
    fn set_s1(&self, settings: &kaponir_s1::Settings, key: Option<&str>) -> anyhow::Result<()>;
    /// Проверяет (правило компилируется) и добавляет правило в конец контура.
    fn add_rule(&self, scope: Scope, rule: Rule) -> anyhow::Result<()>;
    /// Удаляет правило по номеру в контуре (как в `rules()`), возвращает удалённое.
    fn remove_rule(&self, scope: Scope, index: usize) -> anyhow::Result<Rule>;
    /// Заменяет список корпоративных доменов контура.
    fn set_corp_domains(&self, scope: Scope, domains: &[String]) -> anyhow::Result<()>;
    fn build_engine(&self) -> anyhow::Result<Engine>;
    /// Автоматическое переобучение локальной модели S1 (ngram) при появлении новых меток.
    fn auto_train_s1(&self) -> anyhow::Result<bool> {
        Ok(false)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ProxyConfig {
    /// Адрес прослушивания. Не loopback — только с `api_token`.
    pub listen: String,
    /// Optional management API of the separate document worker.
    pub worker_url: Option<String>,
    /// Куда уходят запросы Anthropic Messages (провайдер или следующий прокси, например 9router).
    pub anthropic_upstream: String,
    pub openai_upstream: String,
    /// Необязательная подсказка модели о плейсхолдерах; по умолчанию выключена.
    pub system_hint: bool,
    /// Токен доступа: `Authorization: Bearer` для `/api/v1/*`, заголовок `x-kaponir-token` для прокси.
    pub api_token: Option<String>,
    /// Сессия без запросов дольше этого выгружается из памяти (в vault остаётся до TTL журнала).
    pub session_idle_hours: u64,
    /// Контур новых записей vault.
    pub scope: Scope,
}

impl Default for ProxyConfig {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:8787".into(),
            worker_url: None,
            anthropic_upstream: "https://api.anthropic.com".into(),
            openai_upstream: "https://api.openai.com".into(),
            system_hint: false,
            api_token: None,
            session_idle_hours: 12,
            scope: Scope::Personal,
        }
    }
}

/// Подсказка модели. Без конкретных примеров токенов: модель могла бы повторить пример,
/// и он восстановился бы в чужое значение.
pub fn system_hint(prefix: &str) -> String {
    let p = prefix.to_ascii_uppercase();
    let l = prefix.to_ascii_lowercase();
    format!(
        "Some values in this conversation are redacted and replaced with placeholders shaped like \
         {p}_<KIND>_<N> or {l}-<kind>-<n>, and some IPv4 addresses with addresses from 240.0.0.0/4. \
         Treat each placeholder as the real value: copy it exactly as written (same case and separators) \
         wherever that value is needed, including code, commands, file paths and tool arguments. \
         Do not guess or ask for the original values."
    )
}

pub async fn serve(state: Shared, listen: &str) -> anyhow::Result<()> {
    serve_on(state, tokio::net::TcpListener::bind(listen).await?).await
}

/// Как `serve`, но на уже открытом сокете: вызывающий знает, что порт занят им.
pub async fn serve_on(state: Shared, listener: tokio::net::TcpListener) -> anyhow::Result<()> {
    axum::serve(listener, router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
