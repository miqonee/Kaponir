//! Сборка движка из конфига: каталог `KAPONIR_HOME`, ключи, пакеты, правила контуров.

use anyhow::{Context, Result, anyhow, bail};
use kaponir_core::rules::{Rule, RuleFile, ScopedRules};
use kaponir_core::{
    Category, Engine, Format, Pack, Policy, Scope, Strategy, Thresholds, UncertainAction,
};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub const DEFAULT_PACKS: &[&str] = &[
    "lang-en",
    "lang-ru",
    "domain-tech",
    "domain-web",
    "region-ru",
];

pub mod s1;
pub mod settings;
pub mod worker;

const S1_TOKEN_SETTING: &str = "s1:http-token";

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    /// Владелец записей Personal.
    pub owner: String,
    pub token_prefix: String,
    /// Встроенные пакеты по id.
    pub packs: Vec<String>,
    /// Каталоги пакетов-данных.
    pub pack_dirs: Vec<PathBuf>,
    /// TTL журнала спорных решений (он содержит секреты).
    pub journal_ttl_days: u32,
    pub policy: PolicyConfig,
    pub s1: kaponir_s1::Settings,
    /// `kaponir serve`: прокси и sidecar REST.
    pub serve: kaponir_proxy::ProxyConfig,
    pub worker: worker::Settings,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            owner: std::env::var("USER").unwrap_or_else(|_| "local".into()),
            token_prefix: "ANON".into(),
            packs: DEFAULT_PACKS.iter().map(|s| (*s).to_string()).collect(),
            pack_dirs: Vec::new(),
            journal_ttl_days: 30,
            policy: PolicyConfig::default(),
            s1: kaponir_s1::Settings::default(),
            serve: kaponir_proxy::ProxyConfig::default(),
            worker: worker::Settings::default(),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct PolicyConfig {
    pub uncertain: Option<UncertainAction>,
    pub default_strategy: Option<Strategy>,
    /// KIND → стратегия.
    pub strategies: HashMap<String, Strategy>,
    /// Категория (`credential`, `infra`…) → пороги.
    pub thresholds: HashMap<String, Thresholds>,
}

impl PolicyConfig {
    pub fn to_policy(&self) -> Result<Policy> {
        let mut p = Policy::default();
        if let Some(u) = self.uncertain {
            p.uncertain = u;
        }
        if let Some(s) = self.default_strategy {
            p.default_strategy = s;
        }
        for (k, s) in &self.strategies {
            p.strategies.insert(k.to_ascii_uppercase(), *s);
        }
        for (c, t) in &self.thresholds {
            let cat: Category = serde_json::from_value(serde_json::Value::String(c.to_lowercase()))
                .map_err(|_| anyhow!("unknown category `{c}` in [policy.thresholds]"))?;
            if !(0.0..=1.0).contains(&t.keep) || !(0.0..=1.0).contains(&t.mask) || t.keep > t.mask {
                bail!("thresholds for `{c}`: need 0 ≤ keep ≤ mask ≤ 1");
            }
            p.thresholds.insert(cat, *t);
        }
        Ok(p)
    }
}

/// Каталог данных: `KAPONIR_HOME`, иначе `$XDG_CONFIG_HOME/kaponir`, иначе `~/.config/kaponir`.
pub fn home_dir() -> Result<PathBuf> {
    if let Some(h) = std::env::var_os("KAPONIR_HOME") {
        return Ok(PathBuf::from(h));
    }
    if let Some(x) = std::env::var_os("XDG_CONFIG_HOME") {
        return Ok(PathBuf::from(x).join("kaponir"));
    }
    let home =
        std::env::var_os("HOME").ok_or_else(|| anyhow!("neither KAPONIR_HOME nor HOME is set"))?;
    Ok(PathBuf::from(home).join(".config").join("kaponir"))
}

pub struct Home {
    pub dir: PathBuf,
}

impl Home {
    pub fn new(dir: PathBuf) -> Self {
        Self { dir }
    }

    pub fn config_path(&self) -> PathBuf {
        self.dir.join("config.toml")
    }

    pub fn key_path(&self) -> PathBuf {
        self.dir.join("vault.key")
    }

    pub fn token_key_path(&self) -> PathBuf {
        self.dir.join("token.key")
    }

    pub fn vault_path(&self) -> PathBuf {
        self.dir.join("vault.db")
    }

    pub fn s1_dir(&self) -> PathBuf {
        self.dir.join("s1")
    }

    /// Единая сборка для CLI, прокси и перезагрузки правил из UI.
    pub fn build_engine(&self, cfg: &Config) -> Result<Engine> {
        let master = self.master_key()?;
        let mut s1 = cfg.s1.clone();
        if let Ok(vault) = kaponir_vault::SqliteVault::open(&self.vault_path(), &master)
            && let Some(bytes) = vault.private_setting(S1_TOKEN_SETTING)?
        {
            s1.http.token = Some(String::from_utf8(bytes).context("S1 token is not UTF-8")?);
        }
        let rules = self.load_rules()?;
        build_engine(
            cfg,
            load_packs(cfg)?,
            &rules,
            self.token_key(&master)?,
            kaponir_s1::build(&s1, &self.s1_dir(), &rules)?,
        )
    }

    pub fn rules_path(&self, scope: Scope) -> PathBuf {
        self.dir.join("rules").join(format!("{scope}.toml"))
    }

    pub fn load_config(&self) -> Result<Config> {
        let p = self.config_path();
        match std::fs::read_to_string(&p) {
            Ok(s) => toml::from_str(&s).with_context(|| format!("{}", p.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Config::default()),
            Err(e) => Err(e).with_context(|| format!("{}", p.display())),
        }
    }

    /// Мастер-ключ vault: `KAPONIR_VAULT_KEY` (hex) или файл `vault.key`.
    pub fn master_key(&self) -> Result<[u8; 32]> {
        if let Ok(k) = std::env::var("KAPONIR_VAULT_KEY") {
            return kaponir_vault::key_from_hex(&k).context("KAPONIR_VAULT_KEY");
        }
        let p = self.key_path();
        let s = std::fs::read_to_string(&p).with_context(|| {
            format!(
                "vault key not found at {} — run `kaponir init`",
                p.display()
            )
        })?;
        kaponir_vault::key_from_hex(&s).with_context(|| format!("{}", p.display()))
    }

    /// Ключ HMAC-токенов и суррогатов: `KAPONIR_TOKEN_KEY`, файл `token.key` (общий для команды)
    /// или производный от мастер-ключа.
    pub fn token_key(&self, master: &[u8; 32]) -> Result<[u8; 32]> {
        if let Ok(k) = std::env::var("KAPONIR_TOKEN_KEY") {
            return kaponir_vault::key_from_hex(&k).context("KAPONIR_TOKEN_KEY");
        }
        match std::fs::read_to_string(self.token_key_path()) {
            Ok(s) => kaponir_vault::key_from_hex(&s).context("token.key"),
            Err(_) => Ok(blake3::derive_key("anon token key v1", master)),
        }
    }

    pub fn load_rules(&self) -> Result<Vec<ScopedRules>> {
        let mut sets = Vec::new();
        for scope in [Scope::Global, Scope::Team, Scope::Personal] {
            let p = self.rules_path(scope);
            match std::fs::read_to_string(&p) {
                Ok(s) => {
                    let file = RuleFile::parse(&s).map_err(|e| anyhow!("{}: {e}", p.display()))?;
                    sets.push(ScopedRules { scope, file });
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e).with_context(|| format!("{}", p.display())),
            }
        }
        Ok(sets)
    }

    /// Проверяет, что правило компилируется, и дописывает его в контур.
    pub fn add_rule(&self, scope: Scope, rule: Rule) -> Result<PathBuf> {
        kaponir_recognizers::RulesRecognizer::new(&[ScopedRules {
            scope,
            file: RuleFile {
                rules: vec![rule.clone()],
                ..RuleFile::default()
            },
        }])?;
        self.append_rule(scope, rule)
    }

    /// Удаляет `index`-е правило контура (нумерация как в `RuleFile::rules`).
    pub fn remove_rule(&self, scope: Scope, index: usize) -> Result<Rule> {
        let p = self.rules_path(scope);
        let existing = std::fs::read_to_string(&p).with_context(|| format!("{}", p.display()))?;
        let (updated, removed) =
            remove_rule_block(&existing, index).map_err(|e| anyhow!("{}: {e}", p.display()))?;
        std::fs::write(&p, updated)?;
        Ok(removed)
    }

    pub fn set_corp_domains(&self, scope: Scope, domains: &[String]) -> Result<PathBuf> {
        let p = self.rules_path(scope);
        let existing = std::fs::read_to_string(&p).unwrap_or_default();
        let updated = set_corp_domains(&existing, domains)?;
        if let Some(d) = p.parent() {
            std::fs::create_dir_all(d)?;
        }
        std::fs::write(&p, updated)?;
        Ok(p)
    }

    /// Дописывает правило в конец файла контура, не трогая комментарии и остальное содержимое.
    pub fn append_rule(&self, scope: Scope, rule: Rule) -> Result<PathBuf> {
        #[derive(Serialize)]
        struct One {
            rule: Vec<Rule>,
        }
        let p = self.rules_path(scope);
        if let Some(d) = p.parent() {
            std::fs::create_dir_all(d)?;
        }
        let text = toml::to_string(&One { rule: vec![rule] })?;
        // Проверяем, что файл останется валидным.
        let existing = std::fs::read_to_string(&p).unwrap_or_default();
        let combined = format!("{existing}\n{text}");
        RuleFile::parse(&combined).map_err(|e| anyhow!("rule would break {}: {e}", p.display()))?;
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&p)?;
        write!(f, "\n{text}")?;
        Ok(p)
    }
}

/// Формат по имени файла; неизвестное — `Auto` (распознаватели всё равно ищут ключи во всех форматах).
pub fn format_from_path(path: &Path) -> Format {
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if name == ".env" || name.starts_with(".env.") || name.ends_with(".env") {
        return Format::Env;
    }
    match path
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("yaml" | "yml") => Format::Yaml,
        Some("json") => Format::Json,
        Some("toml") => Format::Toml,
        Some("ini" | "conf" | "cfg") => Format::Ini,
        Some("txt" | "log" | "md") => Format::Text,
        _ => Format::Auto,
    }
}

pub const CONFIG_TEMPLATE: &str = r#"# Конфигурация kaponir. Все поля необязательны.

# Владелец записей Personal (по умолчанию $USER).
# owner = "me"

# Префикс токенов: ANON_SVC_1 / anon-svc-1.
token_prefix = "ANON"

# Встроенные пакеты: lang-en, lang-ru, domain-tech, domain-web, region-ru.
packs = ["lang-en", "lang-ru", "domain-tech", "domain-web", "region-ru"]

# Дополнительные пакеты-данные (каталоги с pack.toml).
pack_dirs = []

# Журнал спорных решений содержит исходные значения — храним ограниченное время.
journal_ttl_days = 30

[policy]
# Что делать со спорными (uncertain) кандидатами: mask | keep.
uncertain = "mask"
# Стратегия подстановки по умолчанию: variable | hmac | surrogate.
# default_strategy = "variable"

# Стратегии по типам.
[policy.strategies]
# SVC = "hmac"
# IP = "surrogate"

# Пороги по категориям: credential, network, infra, identity, financial, contact, custom.
[policy.thresholds]
# infra = { mask = 0.8, keep = 0.3 }

[s1]
# none | ngram | http. Обучение само по себе не включает модель.
provider = "none"
weight = 1.0
keep_below = 0.1
# Байты UTF-8 контекста до и после кандидата на L2 (0–4096).
context_bytes = 60
# Сначала проверьте оценки без изменения решений.
shadow = true

[s1.http]
# url = "http://127.0.0.1:9001/score"
# protocol = "probabilities"  # probabilities | systemone
# model = "oc/jev-1.13-free"  # обязательно для systemone
timeout_ms = 200
disclosure = "l0"
# token_env = "KAPONIR_S1_HTTP_TOKEN"

# `kaponir serve`: прокси для агентов и sidecar REST.
[serve]
listen = "127.0.0.1:8787"
# Провайдер или следующий прокси в цепочке (например, 9router).
anthropic_upstream = "https://api.anthropic.com"
openai_upstream = "https://api.openai.com"
# Необязательная инструкция о плейсхолдерах. Включение изменяет system prompt.
system_hint = false
# Обязателен, если listen не loopback. Можно задать через KAPONIR_API_TOKEN.
# api_token = "..."
"#;

pub fn rules_template(scope: Scope) -> String {
    let who = match scope {
        Scope::Personal => "Личные правила: видны и восстанавливаются только владельцем.",
        Scope::Team => {
            "Правила команды: общие для участников. Решения из `kaponir review --scope team` попадают сюда."
        }
        Scope::Global => {
            "Корпоративные правила. `enforced = true` нельзя перекрыть правилами других контуров."
        }
    };
    format!(
        r#"# {who}
#
# [corp]
# domains = ["mycompany.ru", "corp.local"]   # регистрируемый домен компании маскируется всегда
#
# [[rule]]
# kind = "CLIENT"
# value = "Рога и Копыта"                    # точное значение по границам слова
#
# [[rule]]
# template = "{{env:prod|stage|dev}}-{{SVC}}-{{hash}}"   # {{KIND}} — чувствительный слот
#
# [[rule]]
# kind = "TICKET"
# regex = 'SEC-\d{{4,}}'
#
# [[rule]]
# action = "keep"                            # никогда не маскировать
# value = "ingress-nginx"
"#
    )
}

/// Добавляет корпоративные домены в `[corp]` файла правил.
pub fn add_corp_domains(existing: &str, domains: &[String]) -> Result<String> {
    let file = RuleFile::parse(existing).map_err(|e| anyhow!("{e}"))?;
    let mut all = file.corp.domains;
    all.extend(domains.iter().cloned());
    set_corp_domains(existing, &all)
}

/// Заменяет список корпоративных доменов в `[corp]`. Если секции нет — дописывает её в начало;
/// если есть — заменяет однострочный массив `domains`, остальное не трогает.
pub fn set_corp_domains(existing: &str, domains: &[String]) -> Result<String> {
    RuleFile::parse(existing).map_err(|e| anyhow!("{e}"))?;
    let mut list: Vec<String> = Vec::new();
    for d in domains {
        let d = d
            .trim()
            .trim_start_matches('.')
            .trim_end_matches('.')
            .to_lowercase();
        if d.is_empty()
            || !d.contains('.')
            || d.chars().any(|c| c.is_whitespace() || "/:@\"'".contains(c))
        {
            bail!("`{d}` is not a domain (expected e.g. mycompany.ru)");
        }
        if !list.contains(&d) {
            list.push(d);
        }
    }
    let line = format!(
        "domains = {}",
        toml::Value::Array(list.iter().cloned().map(toml::Value::String).collect())
    );
    let lines: Vec<&str> = existing.lines().collect();
    let out = match lines.iter().position(|l| l.trim() == "[corp]") {
        None => format!("[corp]\n{line}\n\n{existing}"),
        Some(at) => {
            let section_end = lines[at + 1..]
                .iter()
                .position(|l| l.trim_start().starts_with('['))
                .map_or(lines.len(), |p| at + 1 + p);
            let mut out: Vec<String> = lines.iter().map(|l| (*l).to_string()).collect();
            match (at + 1..section_end).find(|&i| lines[i].trim_start().starts_with("domains")) {
                Some(i) if lines[i].contains(']') => out[i] = line,
                Some(_) => bail!("multi-line `domains` array in [corp] — edit the file manually"),
                None => out.insert(at + 1, line),
            }
            let mut s = out.join("\n");
            if existing.ends_with('\n') {
                s.push('\n');
            }
            s
        }
    };
    RuleFile::parse(&out).map_err(|e| anyhow!("internal: produced invalid rules file: {e}"))?;
    Ok(out)
}

/// Вырезает `index`-й блок `[[rule]]` из текста файла правил. Комментарии и остальные правила
/// не трогаются; результат проверяется разбором.
pub fn remove_rule_block(existing: &str, index: usize) -> Result<(String, Rule)> {
    let before = RuleFile::parse(existing).map_err(|e| anyhow!("{e}"))?;
    let removed = before
        .rules
        .get(index)
        .cloned()
        .ok_or_else(|| anyhow!("no rule #{index}"))?;
    let lines: Vec<&str> = existing.split_inclusive('\n').collect();
    let headers: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter(|(_, l)| l.trim() == "[[rule]]")
        .map(|(i, _)| i)
        .collect();
    if headers.len() != before.rules.len() {
        bail!("rules are not all written as `[[rule]]` blocks — edit the file manually");
    }
    let start = headers[index];
    // Блок — до следующего заголовка таблицы; хвостовые комментарии и пустые строки остаются.
    let mut end = lines[start + 1..]
        .iter()
        .position(|l| l.trim_start().starts_with('['))
        .map_or(lines.len(), |p| start + 1 + p);
    while end > start + 1 && matches!(lines[end - 1].trim().chars().next(), None | Some('#')) {
        end -= 1;
    }
    let out: String = lines[..start]
        .iter()
        .chain(&lines[end..])
        .copied()
        .collect();
    let after =
        RuleFile::parse(&out).map_err(|e| anyhow!("internal: produced invalid rules file: {e}"))?;
    let mut expected = before.rules;
    expected.remove(index);
    let same = |a: &Rule, b: &Rule| toml::to_string(a).ok() == toml::to_string(b).ok();
    if after.rules.len() != expected.len()
        || !after.rules.iter().zip(&expected).all(|(a, b)| same(a, b))
    {
        bail!("could not remove the rule cleanly — edit the file manually");
    }
    Ok((out, removed))
}

/// Обновляет только поля S1, сохраняя комментарии и остальные секции конфигурации.
fn save_s1_settings(existing: &str, settings: &kaponir_s1::Settings) -> Result<String> {
    let mut doc = existing
        .parse::<toml_edit::DocumentMut>()
        .context("config.toml is not valid TOML")?;
    if !doc.contains_key("s1") {
        doc["s1"] = toml_edit::table();
    }
    let provider = match settings.provider {
        kaponir_s1::ProviderKind::None => "none",
        kaponir_s1::ProviderKind::Ngram => "ngram",
        kaponir_s1::ProviderKind::Http => "http",
    };
    doc["s1"]["provider"] = toml_edit::value(provider);
    doc["s1"]["weight"] = toml_edit::value(f64::from(settings.weight));
    doc["s1"]["keep_below"] = toml_edit::value(f64::from(settings.keep_below));
    doc["s1"]["shadow"] = toml_edit::value(settings.shadow);
    doc["s1"]["context_bytes"] = toml_edit::value(settings.context_bytes as i64);
    if let Some(calibration) = settings.calibration {
        doc["s1"]["calibration"]["a"] = toml_edit::value(f64::from(calibration.a));
        doc["s1"]["calibration"]["b"] = toml_edit::value(f64::from(calibration.b));
    } else if let Some(table) = doc["s1"].as_table_like_mut() {
        table.remove("calibration");
    }
    if !doc["s1"]
        .as_table_like()
        .is_some_and(|t| t.contains_key("http"))
    {
        doc["s1"]["http"] = toml_edit::table();
    }
    doc["s1"]["http"]["url"] = toml_edit::value(settings.http.url.as_str());
    doc["s1"]["http"]["protocol"] = toml_edit::value(match settings.http.protocol {
        kaponir_s1::http::HttpProtocol::Probabilities => "probabilities",
        kaponir_s1::http::HttpProtocol::Systemone => "systemone",
    });
    doc["s1"]["http"]["timeout_ms"] = toml_edit::value(settings.http.timeout_ms as i64);
    doc["s1"]["http"]["disclosure"] = toml_edit::value(match settings.http.disclosure {
        kaponir_s1::Level::L0 => "l0",
        kaponir_s1::Level::L1 => "l1",
        kaponir_s1::Level::L2 => "l2",
    });
    for (name, value) in [
        ("model", settings.http.model.as_deref()),
        ("token_env", settings.http.token_env.as_deref()),
    ] {
        if let Some(value) = value.filter(|v| !v.trim().is_empty()) {
            doc["s1"]["http"][name] = toml_edit::value(value);
        } else if let Some(table) = doc["s1"]["http"].as_table_like_mut() {
            table.remove(name);
        }
    }
    // Токен Web UI намеренно не записывается: он живёт только в encrypted vault.
    Ok(doc.to_string())
}

/// Правила в файлах `KAPONIR_HOME/rules` для Web UI; движок пересобирается из конфига и пакетов.
pub struct HomeControl {
    pub home: Home,
}

impl kaponir_proxy::Control for HomeControl {
    fn export_settings(&self, include_secrets: bool) -> Result<serde_json::Value> {
        self.home.export_settings(include_secrets)
    }

    fn import_settings(&self, bundle: serde_json::Value) -> Result<()> {
        self.home.import_settings(bundle)
    }
    fn rules(&self) -> Result<Vec<kaponir_proxy::RuleSet>> {
        let mut out = Vec::new();
        for scope in [Scope::Global, Scope::Team, Scope::Personal] {
            let p = self.home.rules_path(scope);
            let file = match std::fs::read_to_string(&p) {
                Ok(s) => RuleFile::parse(&s).map_err(|e| anyhow!("{}: {e}", p.display()))?,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => RuleFile::default(),
                Err(e) => return Err(e).with_context(|| format!("{}", p.display())),
            };
            out.push(kaponir_proxy::RuleSet {
                scope,
                location: p.display().to_string(),
                file,
            });
        }
        Ok(out)
    }

    fn s1_settings(&self) -> Result<kaponir_s1::Settings> {
        Ok(self.home.load_config()?.s1)
    }

    fn s1_local_model_available(&self) -> Result<bool> {
        Ok(self
            .home
            .s1_dir()
            .join(kaponir_s1::ngram::MODEL_FILE)
            .is_file())
    }

    fn set_s1(&self, settings: &kaponir_s1::Settings, key: Option<&str>) -> Result<()> {
        let mut checked = settings.clone();
        checked.http.token = key.map(str::to_owned);
        kaponir_s1::build(&checked, &self.home.s1_dir(), &self.home.load_rules()?)?;
        let path = self.home.config_path();
        let existing = std::fs::read_to_string(&path).unwrap_or_default();
        let updated = save_s1_settings(&existing, settings)?;
        let _: Config =
            toml::from_str(&updated).context("invalid configuration after S1 update")?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let tmp = path.with_extension(format!("toml.{}.tmp", std::process::id()));
        {
            let mut file =
                std::fs::File::create(&tmp).with_context(|| format!("{}", tmp.display()))?;
            file.write_all(updated.as_bytes())?;
            file.sync_all()?;
        }
        std::fs::rename(&tmp, &path).with_context(|| format!("{}", path.display()))?;
        Ok(())
    }

    fn add_rule(&self, scope: Scope, rule: Rule) -> Result<()> {
        self.home.add_rule(scope, rule).map(|_| ())
    }

    fn remove_rule(&self, scope: Scope, index: usize) -> Result<Rule> {
        self.home.remove_rule(scope, index)
    }

    fn set_corp_domains(&self, scope: Scope, domains: &[String]) -> Result<()> {
        self.home.set_corp_domains(scope, domains).map(|_| ())
    }

    fn build_engine(&self) -> Result<Engine> {
        let cfg = self.home.load_config()?;
        self.home.build_engine(&cfg)
    }
}

/// Записывает секрет с правами 0600.
pub fn write_secret(path: &Path, content: &str) -> Result<()> {
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts
        .open(path)
        .with_context(|| format!("{}", path.display()))?;
    f.write_all(content.as_bytes())?;
    Ok(())
}

/// Встроенный пакет по id. Пакеты-код (`lang-ru` с морфологией, `region-ru`) имеют приоритет
/// над одноимёнными пакетами-данными.
pub fn builtin_pack(id: &str) -> Result<Arc<dyn Pack>> {
    match id {
        "region-ru" => Ok(kaponir_region_ru::RegionRu::arc()),
        "lang-ru" => Ok(kaponir_lang_ru::LangRu::arc()),
        _ => kaponir_packs::builtin(id).ok_or_else(|| {
            anyhow!(
                "unknown builtin pack `{id}` (available: {}, region-ru)",
                kaponir_packs::builtin_ids().join(", ")
            )
        }),
    }
}

/// Пакет-данные из каталога; для `lang-ru` поверх данных подключается морфология.
pub fn pack_from_dir(dir: &Path) -> Result<Arc<dyn Pack>> {
    let data = kaponir_packs::DataPack::from_dir(dir)?;
    Ok(match data.info().id.as_str() {
        "lang-ru" => Arc::new(kaponir_lang_ru::LangRu::from_data(data)),
        _ => Arc::new(data),
    })
}

pub fn load_packs(cfg: &Config) -> Result<Vec<Arc<dyn Pack>>> {
    let mut packs: Vec<Arc<dyn Pack>> = Vec::new();
    for id in &cfg.packs {
        packs.push(builtin_pack(id)?);
    }
    for dir in &cfg.pack_dirs {
        packs.push(pack_from_dir(dir)?);
    }
    Ok(packs)
}

/// Движок из конфига, набора пакетов и правил. Без доступа к файловой системе.
pub fn build_engine(
    cfg: &Config,
    packs: Vec<Arc<dyn Pack>>,
    rules: &[ScopedRules],
    token_key: [u8; 32],
    s1: Option<kaponir_core::s1::S1Config>,
) -> Result<Engine> {
    let mut b = Engine::builder(token_key)
        .token_prefix(cfg.token_prefix.clone())
        .policy(cfg.policy.to_policy()?);
    if let Some(s1) = s1 {
        b = b.s1(s1);
    }
    for p in packs {
        b = b.pack(p);
    }
    for r in kaponir_recognizers::builtin() {
        b = b.recognizer(r);
    }
    if !rules.is_empty() {
        for set in rules {
            b = b.lexicon(&set.file.lexicon());
        }
        b = b.recognizer(Box::new(kaponir_recognizers::RulesRecognizer::new(rules)?));
    }
    Ok(b.build()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FILE: &str = "# head\n[corp]\ndomains = [\"a.ru\"]\n\n[[rule]]\nkind = \"A\"\nvalue = \"one\"\n\n# about two\n[[rule]]\nkind = \"B\"\nvalue = \"two\"\n# tail\n";

    #[test]
    fn removes_rule_blocks_keeping_comments() {
        let (out, r) = remove_rule_block(FILE, 0).unwrap();
        assert_eq!(r.value.as_deref(), Some("one"));
        assert!(out.contains("# head") && out.contains("# about two") && out.contains("# tail"));
        assert_eq!(RuleFile::parse(&out).unwrap().rules.len(), 1);
        let (out, r) = remove_rule_block(FILE, 1).unwrap();
        assert_eq!(r.value.as_deref(), Some("two"));
        assert!(
            out.ends_with("value = \"one\"\n\n# about two\n# tail\n"),
            "{out}"
        );
        assert!(remove_rule_block(FILE, 2).is_err());
        assert!(remove_rule_block("rule = [{ kind = \"A\", value = \"x\" }]\n", 0).is_err());
    }

    #[test]
    fn saves_s1_without_exposing_vault_token_or_other_config() {
        let mut settings = kaponir_s1::Settings {
            provider: kaponir_s1::ProviderKind::Http,
            ..Default::default()
        };
        settings.http.url = "https://s1.example/score".into();
        settings.http.model = Some("model-a".into());
        settings.http.token = Some("never-write-this-secret".into());
        let source = "# keep me\nowner = \"alice\"\n\n[s1]\n# mode comment\nprovider = \"none\"\n";
        let output = save_s1_settings(source, &settings).unwrap();
        assert!(output.contains("# keep me") && output.contains("# mode comment"));
        assert!(output.contains("owner = \"alice\""));
        assert!(!output.contains("never-write-this-secret"));
        let config: Config = toml::from_str(&output).unwrap();
        assert_eq!(config.owner, "alice");
        assert_eq!(config.s1.provider, kaponir_s1::ProviderKind::Http);
        assert_eq!(config.s1.http.url, "https://s1.example/score");
        assert!(config.s1.http.token.is_none());
    }

    #[test]
    fn sets_corp_domains() {
        let out = set_corp_domains(FILE, &["b.ru".into(), ".B.ru".into()]).unwrap();
        assert_eq!(RuleFile::parse(&out).unwrap().corp.domains, ["b.ru"]);
        assert!(out.contains("# head"));
        assert_eq!(
            RuleFile::parse(&set_corp_domains(FILE, &[]).unwrap())
                .unwrap()
                .corp
                .domains
                .len(),
            0
        );
        assert!(set_corp_domains(FILE, &["http://x.ru".into()]).is_err());
        let added = add_corp_domains(FILE, &["c.ru".into()]).unwrap();
        assert_eq!(
            RuleFile::parse(&added).unwrap().corp.domains,
            ["a.ru", "c.ru"]
        );
    }
}
