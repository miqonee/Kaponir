//! `kaponir` — CLI обратимого анонимайзера: маскирование и восстановление в пайпах, разбор спорных
//! решений, правила контуров и проверка пакетов.

use anyhow::{Context, Result, anyhow, bail};
use clap::{Args, Parser, Subcommand};
use kaponir_cli::{Config, Home, build_engine, format_from_path, load_packs};
use kaponir_core::rules::{Rule, RuleAction, RuleFile};
use kaponir_core::{Category, Escape, Format, Input, MaskOptions, Principal, Scope, Strategy};
use kaponir_vault::{JournalRow, SqliteVault};
use serde::de::DeserializeOwned;
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

#[derive(Parser)]
#[command(
    name = "kaponir",
    version,
    about = "Reversible anonymizer: mask secrets and infrastructure names, restore them later"
)]
struct Cli {
    /// Каталог данных (по умолчанию KAPONIR_HOME или ~/.config/kaponir).
    #[arg(long, global = true, env = "KAPONIR_HOME")]
    home: Option<PathBuf>,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Отдельный worker: serve, run, submit, status, download, pause/resume/cancel.
    #[command(disable_help_flag = true)]
    Worker {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<std::ffi::OsString>,
    },
    /// Перенос настроек, правил и подключений: тот же JSON, что в Web UI.
    Config {
        #[command(subcommand)]
        action: ConfigCmd,
    },
    /// Создать каталог данных, ключ vault, конфиг и файлы правил.
    Init {
        /// Корпоративный домен (можно несколько): записывается в личные правила.
        #[arg(long = "corp-domain")]
        corp_domains: Vec<String>,
        /// Optional setup questions; Enter or EOF may skip the wizard.
        #[arg(long, conflicts_with = "learn")]
        wizard: bool,
        /// Wizard language.
        #[arg(long = "language", value_parser = ["ru", "en"], default_value = "en")]
        setup_language: String,
        /// Локально проанализировать документы; с --select сохранить выбранные предложения.
        #[arg(long, num_args = 1..)]
        learn: Vec<PathBuf>,
        /// ID предложения из предпросмотра (можно несколько).
        #[arg(long, requires = "learn")]
        select: Vec<String>,
    },
    /// Локально предложить точные MASK-правила по документам.
    Discover(DiscoverArgs),
    /// Замаскировать текст из файла или stdin.
    Mask(MaskArgs),
    /// Восстановить оригиналы в тексте (ответ LLM, тикет, исправленный конфиг).
    Restore(RestoreArgs),
    /// Разбор спорных решений: каждое решение становится правилом.
    Review {
        #[command(subcommand)]
        action: Option<ReviewCmd>,
        #[command(flatten)]
        list: ReviewList,
    },
    /// Правила контуров.
    Rule {
        #[command(subcommand)]
        action: RuleCmd,
    },
    /// Языковые, региональные и предметные пакеты.
    Pack {
        #[command(subcommand)]
        action: PackCmd,
    },
    /// Журнал спорных решений.
    Journal {
        #[command(subcommand)]
        action: JournalCmd,
    },
    /// Состояние vault, пакетов и правил.
    Stats,
    /// Обучение, состояние и откат локальной модели S1.
    S1 {
        #[command(subcommand)]
        action: kaponir_cli::s1::Command,
    },
    /// Управление локальными CPU-моделями (Qwen3 GGUF, S1 ngram): список, скачивание, статус.
    Models {
        #[command(subcommand)]
        action: Option<ModelsCmd>,
    },
    /// Журнал прокси-событий и детальный Request Inspector (До -> В LLM -> Ответ).
    Events {
        #[command(subcommand)]
        action: Option<EventsCmd>,
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    /// Прозрачный прокси для агентов (Anthropic Messages, OpenAI Chat Completions), sidecar REST
    /// и Web UI на /ui/.
    Serve(ServeArgs),
    /// Web UI: `kaponir serve` + открыть /ui/ в браузере.
    Ui {
        #[command(flatten)]
        serve: ServeArgs,
        /// Не открывать браузер.
        #[arg(long)]
        no_open: bool,
    },
}

#[derive(Subcommand)]
enum ModelsCmd {
    /// Показать доступные и установленные модели (Qwen3-0.6B Q4/Q8, S1 ngram).
    List,
    /// Скачать текстовую CPU-модель (qwen3-0.6b | qwen3-0.6b-q8 | q4 | q5).
    Download {
        #[arg(default_value = "qwen3-0.6b")]
        model: String,
    },
    /// Проверить статус весов, рантайма llama-server и классификатора S1.
    Status,
}

#[derive(Subcommand)]
enum EventsCmd {
    /// Список последних событий прокси из зашифрованного vault.
    List {
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    /// Детальный просмотр запроса (Request Inspector): исходный текст, обезличенный запрос в LLM, ответ и замены.
    Show {
        id: u64,
        #[arg(long)]
        json: bool,
    },
}

#[derive(Subcommand)]
enum ConfigCmd {
    Export {
        #[arg(short, long)]
        output: PathBuf,
        /// Включить API-ключи в незашифрованный файл.
        #[arg(long)]
        include_secrets: bool,
    },
    Import {
        file: PathBuf,
    },
}

#[derive(Args)]
struct ServeArgs {
    /// Адрес прослушивания (по умолчанию из [serve] конфига, 127.0.0.1:8787).
    #[arg(long)]
    listen: Option<String>,
    /// Куда пересылать запросы Anthropic: провайдер или следующий прокси (9router).
    #[arg(long)]
    anthropic_upstream: Option<String>,
    #[arg(long)]
    openai_upstream: Option<String>,
    /// Не добавлять подсказку о плейсхолдерах в system prompt.
    #[arg(long)]
    no_system_hint: bool,
}

#[derive(Args, Default)]
struct DiscoverArgs {
    /// JSON-массив Document для worker; имена источников сохраняются при повторном импорте.
    #[arg(long, conflicts_with = "saved")]
    documents_json: Option<PathBuf>,
    /// Документы для локального анализа.
    #[arg(required_unless_present_any = ["saved", "documents_json"], conflicts_with_all = ["saved", "documents_json"])]
    files: Vec<PathBuf>,
    /// Явно оценить найденные значения настроенным S1 (по его уровню раскрытия).
    #[arg(long, conflicts_with = "saved")]
    s1: bool,
    /// Пополнить зашифрованную подборку; повторные документы не увеличивают счётчики.
    #[arg(long, conflicts_with = "saved")]
    remember: bool,
    /// Открыть накопленную подборку без чтения документов.
    #[arg(long)]
    saved: bool,
    /// Подтвердить, что эти значения не чувствительные: только метки обучения, без KEEP-правил.
    #[arg(long)]
    reject: Vec<String>,
    /// Применить только эти ID предложений (можно несколько).
    #[arg(long)]
    select: Vec<String>,
    /// Контур сохраняемых правил.
    #[arg(long, default_value = "personal")]
    scope: Scope,
    /// Отчёт целиком в JSON.
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct MaskArgs {
    /// Входной файл (по умолчанию stdin).
    file: Option<PathBuf>,
    #[arg(short, long)]
    output: Option<PathBuf>,
    /// auto | text | yaml | json | toml | env | ini (по умолчанию — по расширению файла).
    #[arg(long)]
    format: Option<Format>,
    /// Контур новых записей: personal | team | global.
    #[arg(long, default_value = "personal")]
    scope: Scope,
    /// Стратегия для всех кандидатов: variable | hmac | surrogate.
    #[arg(long, value_parser = parse_enum::<Strategy>)]
    strategy: Option<Strategy>,
    /// HMAC без записи в vault: восстановить будет нельзя.
    #[arg(long)]
    irreversible: bool,
    /// Не писать спорные решения в журнал.
    #[arg(long)]
    no_journal: bool,
    /// Ничего не сохранять в vault (просмотр результата).
    #[arg(long)]
    dry_run: bool,
    /// Таблица всех кандидатов с вероятностями и причинами — в stderr.
    #[arg(long)]
    explain: bool,
    /// Результат целиком в JSON (текст, замены, объяснения).
    #[arg(long)]
    json: bool,
    /// Краткая сводка в stderr.
    #[arg(short, long)]
    report: bool,
}

#[derive(Args)]
struct RestoreArgs {
    file: Option<PathBuf>,
    #[arg(short, long)]
    output: Option<PathBuf>,
    /// Экранировать значения для вставки внутрь JSON-строки.
    #[arg(long)]
    json_escape: bool,
    /// Код выхода 2, если остались неизвестные токены или токены без прав.
    #[arg(long)]
    strict: bool,
    #[arg(short, long)]
    report: bool,
}

#[derive(Args)]
struct ReviewList {
    /// open | masked | kept | dismissed | all
    #[arg(long, default_value = "open")]
    status: String,
    #[arg(long, default_value_t = 50)]
    limit: usize,
    /// Сгруппировать по типу и форме значения (`x-x-dxdx`).
    #[arg(long)]
    group: bool,
    /// Автоматически классифицировать открытые спорные записи.
    #[arg(long)]
    auto: bool,
    /// Автоматически сохранить правила с высокой уверенностью (>= 85%).
    #[arg(long)]
    auto_save: bool,
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct Selection {
    /// Номера записей журнала.
    ids: Vec<i64>,
    /// Все открытые записи с этой формой (см. `kaponir review --group`).
    #[arg(long)]
    shape: Option<String>,
}

#[derive(Subcommand)]
enum ReviewCmd {
    /// Автоматический разбор спорных записей классификатором.
    Auto {
        /// Автоматически сохранить правила с уверенностью >= threshold.
        #[arg(long)]
        save: bool,
        #[arg(long, default_value_t = 0.85)]
        threshold: f32,
    },
    /// Всегда маскировать эти значения.
    Mask {
        #[command(flatten)]
        sel: Selection,
        /// Тип токена (по умолчанию — найденный детектором).
        #[arg(long)]
        kind: Option<String>,
        #[arg(long, default_value = "personal")]
        scope: Scope,
    },
    /// Никогда не маскировать эти значения.
    Keep {
        #[command(flatten)]
        sel: Selection,
        #[arg(long, default_value = "personal")]
        scope: Scope,
    },
    /// Маска по шаблону, например `{env:prod|stage}-{SVC}-{hash}`; закрывает подходящие записи.
    Template {
        template: String,
        #[arg(long, default_value = "personal")]
        scope: Scope,
    },
    /// Закрыть без правила (решение по политике устраивает).
    Dismiss {
        #[command(flatten)]
        sel: Selection,
    },
}

#[derive(Subcommand)]
enum RuleCmd {
    /// Добавить правило.
    Add {
        #[arg(long, default_value = "personal")]
        scope: Scope,
        #[arg(long, default_value = "CUSTOM")]
        kind: String,
        #[arg(long, group = "pattern")]
        value: Option<String>,
        #[arg(long, group = "pattern")]
        template: Option<String>,
        #[arg(long, group = "pattern")]
        regex: Option<String>,
        /// Никогда не маскировать (allowlist).
        #[arg(long)]
        keep: bool,
        #[arg(long)]
        case_sensitive: bool,
        /// Только для global: нельзя перекрыть.
        #[arg(long)]
        enforced: bool,
        #[arg(long, value_parser = parse_enum::<Strategy>)]
        strategy: Option<Strategy>,
        #[arg(long, value_parser = parse_enum::<Category>)]
        category: Option<Category>,
        #[arg(long, default_value = "")]
        note: String,
    },
    /// Добавить корпоративные домены.
    Domain {
        domains: Vec<String>,
        #[arg(long, default_value = "personal")]
        scope: Scope,
    },
    /// Показать правила всех контуров.
    List {
        #[arg(long)]
        scope: Option<Scope>,
    },
    /// Проверить, что файлы правил разбираются и компилируются.
    Check,
}

#[derive(Subcommand)]
enum PackCmd {
    /// Загруженные пакеты.
    List,
    /// Прогнать размеченный корпус пакета (`tests/corpus.jsonl`) и показать полноту и точность.
    Test {
        /// Каталог пакета.
        dir: PathBuf,
        /// Отдельный файл корпуса вместо `tests/corpus.jsonl`.
        #[arg(long)]
        corpus: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
enum JournalCmd {
    /// Удалить записи старше N дней (по умолчанию — `journal_ttl_days` из конфига).
    Purge {
        #[arg(long)]
        days: Option<u32>,
    },
}

fn parse_enum<T: DeserializeOwned>(s: &str) -> Result<T, String> {
    serde_json::from_value(serde_json::Value::String(s.to_ascii_lowercase()))
        .map_err(|_| format!("unknown value `{s}`"))
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("kaponir: {e:#}");
            ExitCode::from(1)
        }
    }
}

fn run(cli: Cli) -> Result<ExitCode> {
    let home = Home::new(match cli.home {
        Some(h) => h,
        None => kaponir_cli::home_dir()?,
    });
    match cli.cmd {
        Cmd::Worker { args } => kaponir_cli::worker::run(&home, &args),
        Cmd::Config { action } => {
            match action {
                ConfigCmd::Export {
                    output,
                    include_secrets,
                } => {
                    let bundle = home.export_settings(include_secrets)?;
                    kaponir_cli::write_secret(&output, &serde_json::to_string_pretty(&bundle)?)?;
                }
                ConfigCmd::Import { file } => {
                    let bundle = serde_json::from_slice(&std::fs::read(file)?)
                        .map_err(|_| anyhow!("invalid settings JSON"))?;
                    home.import_settings(bundle)?;
                    eprintln!(
                        "Settings imported. Restart any running kaponir server to apply all settings."
                    );
                }
            }
            Ok(ExitCode::SUCCESS)
        }
        Cmd::Init {
            corp_domains,
            wizard,
            setup_language,
            learn,
            select,
        } => {
            let code = init(&home, &corp_domains)?;
            if wizard {
                setup_wizard(&home, &setup_language)?;
            }
            if !learn.is_empty() {
                discover(
                    &home,
                    &DiscoverArgs {
                        files: learn,
                        select,
                        ..Default::default()
                    },
                )?;
            }
            Ok(code)
        }
        Cmd::Discover(a) => discover(&home, &a),
        Cmd::Mask(a) => mask(&home, a),
        Cmd::Restore(a) => restore(&home, a),
        Cmd::Review { action, list } => review(&home, action, list),
        Cmd::Rule { action } => rule(&home, action),
        Cmd::Pack { action } => pack(&home, action),
        Cmd::Journal {
            action: JournalCmd::Purge { days },
        } => {
            let cfg = home.load_config()?;
            let vault = open_vault(&home)?;
            let age = i64::from(days.unwrap_or(cfg.journal_ttl_days)) * 86_400;
            let n = vault.purge_journal(age)?;
            let s = vault.purge_sessions(age)?;
            eprintln!("purged {n} journal entries, {s} session(s)");
            Ok(ExitCode::SUCCESS)
        }
        Cmd::Stats => stats(&home),
        Cmd::S1 { action } => {
            kaponir_cli::s1::run(&home, action)?;
            Ok(ExitCode::SUCCESS)
        }
        Cmd::Models { action } => models_cmd(&home, action.unwrap_or(ModelsCmd::List)),
        Cmd::Events {
            action,
            limit,
            json,
        } => events_cmd(&home, action, limit, json),
        Cmd::Serve(a) => serve(&home, a, false),
        Cmd::Ui { serve: a, no_open } => serve(&home, a, !no_open),
    }
}

// ---------- init ----------

fn init(home: &Home, corp_domains: &[String]) -> Result<ExitCode> {
    std::fs::create_dir_all(home.dir.join("rules"))
        .with_context(|| format!("{}", home.dir.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&home.dir, std::fs::Permissions::from_mode(0o700))?;
    }
    let mut created = Vec::new();
    if !home.key_path().exists() {
        let key = kaponir_vault::generate_key()?;
        kaponir_cli::write_secret(
            &home.key_path(),
            &format!("{}\n", kaponir_vault::key_to_hex(&key)),
        )?;
        created.push(home.key_path());
    }
    if !home.config_path().exists() {
        std::fs::write(home.config_path(), kaponir_cli::CONFIG_TEMPLATE)?;
        created.push(home.config_path());
    }
    for scope in [Scope::Personal, Scope::Team, Scope::Global] {
        let p = home.rules_path(scope);
        if !p.exists() {
            std::fs::write(&p, kaponir_cli::rules_template(scope))?;
            created.push(p);
        }
    }
    if !corp_domains.is_empty() {
        add_domains(home, Scope::Personal, corp_domains)?;
    }
    // Проверяем, что всё открывается.
    let vault = open_vault(home)?;
    engine(home)?;
    drop(vault);
    for p in &created {
        eprintln!("created {}", p.display());
    }
    if created.is_empty() {
        eprintln!("already initialized: {}", home.dir.display());
    }
    eprintln!(
        "vault key: {} — back it up; without it masked data cannot be restored.\n\
         For identical HMAC tokens across a team, share a token key via KAPONIR_TOKEN_KEY or {}.",
        home.key_path().display(),
        home.token_key_path().display()
    );
    Ok(ExitCode::SUCCESS)
}

// ---------- discover ----------

fn discover(home: &Home, args: &DiscoverArgs) -> Result<ExitCode> {
    use kaponir_core::discover::{self, Options};
    let mut documents = if let Some(path) = &args.documents_json {
        let metadata = std::fs::metadata(path)?;
        if metadata.len() > 16 * 1024 * 1024 {
            bail!("documents JSON exceeds 16 MiB");
        }
        serde_json::from_slice(&std::fs::read(path)?)?
    } else {
        Vec::new()
    };
    for path in &args.files {
        documents.push(discover::Document {
            name: path.display().to_string(),
            text: std::fs::read_to_string(path).with_context(|| format!("{}", path.display()))?,
            format: format_from_path(path),
            model_candidates: vec![],
        });
    }
    discover::validate_documents(&documents).map_err(anyhow::Error::msg)?;
    let mut discovery_config = home.load_config()?;
    if !args.s1 {
        discovery_config.s1.provider = kaponir_s1::ProviderKind::None;
    }
    let discovery_engine = home.build_engine(&discovery_config)?;
    let vault = open_vault(home)?;
    let mut report = if args.saved {
        kaponir_proxy::discovery::load(&vault)?.report()
    } else {
        discover::analyze_with_options(&discovery_engine, &documents, &Options { use_s1: args.s1 })
    };
    let writing = args.remember || !args.select.is_empty() || !args.reject.is_empty();
    if writing {
        vault.begin()?;
    }
    let mut profile = kaponir_proxy::discovery::load(&vault)?;
    if args.saved {
        report = profile.report();
    }
    if args.remember {
        profile.merge(&report).map_err(anyhow::Error::msg)?;
        let mut combined = profile.report();
        combined.s1_status = report.s1_status;
        combined.s1_scored = report.s1_scored;
        combined.truncated = report.truncated;
        report = combined;
    }
    // Validate both groups before any write; rejected suggestions never become KEEP rules.
    if args.select.iter().any(|id| args.reject.contains(id)) {
        bail!("same id cannot be selected and rejected");
    }
    let rules = discover::selected_rules(&report, &args.select).map_err(anyhow::Error::msg)?;
    profile
        .decide(&report, &args.select, true)
        .map_err(anyhow::Error::msg)?;
    profile
        .decide(&report, &args.reject, false)
        .map_err(anyhow::Error::msg)?;
    if args.json {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        println!(
            "Анализ: {} документ(ов), {} предложений; S1: {} ({} оценок).",
            report.documents,
            report.suggestions.len(),
            report.s1_status,
            report.s1_scored
        );
        for s in &report.suggestions {
            println!(
                "{}  {:<10} ×{} в {} док.  {}",
                s.id, s.kind, s.count, s.documents, s.value
            );
            for e in &s.examples {
                println!("    {}: {}  S1={:?}", e.document, e.context, e.s1_score);
            }
        }
        if report.truncated {
            eprintln!("Достигнут лимит анализа; часть предложений не показана.");
        }
        if !report.suggestions.is_empty() {
            println!(
                "Применить: kaponir discover --saved --select <ID> (для подборки) или повторить документы с --select <ID>. Метка не чувствительного значения: --reject <ID>."
            );
        }
    }
    for rule in rules {
        home.add_rule(args.scope, rule)?;
    }
    if args.remember || !args.select.is_empty() || !args.reject.is_empty() {
        kaponir_proxy::discovery::save(&vault, &profile)?;
        vault.commit()?;
    }
    if !args.select.is_empty() {
        engine(home)?;
    }
    if !args.select.is_empty() || !args.reject.is_empty() {
        eprintln!(
            "Сохранено MASK-правил: {}; отрицательных меток: {}",
            args.select.len(),
            args.reject.len()
        );
    }
    Ok(ExitCode::SUCCESS)
}

// ---------- mask / restore ----------

fn read_input(file: Option<&Path>) -> Result<String> {
    match file {
        Some(p) if p != Path::new("-") => {
            std::fs::read_to_string(p).with_context(|| format!("{}", p.display()))
        }
        _ => {
            let mut s = String::new();
            std::io::stdin().read_to_string(&mut s).context("stdin")?;
            Ok(s)
        }
    }
}

fn write_output(file: Option<&Path>, s: &str) -> Result<()> {
    match file {
        Some(p) if p != Path::new("-") => {
            std::fs::write(p, s).with_context(|| format!("{}", p.display()))
        }
        _ => {
            let mut out = std::io::stdout().lock();
            out.write_all(s.as_bytes())?;
            out.flush()?;
            Ok(())
        }
    }
}

fn open_vault(home: &Home) -> Result<SqliteVault> {
    let key = home.master_key()?;
    SqliteVault::open(&home.vault_path(), &key)
        .with_context(|| format!("{}", home.vault_path().display()))
}

fn engine(home: &Home) -> Result<(Config, kaponir_core::Engine)> {
    let cfg = home.load_config()?;
    let engine = home.build_engine(&cfg)?;
    Ok((cfg, engine))
}

fn mask(home: &Home, a: MaskArgs) -> Result<ExitCode> {
    let (cfg, engine) = engine(home)?;
    let text = read_input(a.file.as_deref())?;
    let format = a
        .format
        .unwrap_or_else(|| a.file.as_deref().map_or(Format::Auto, format_from_path));
    let opts = MaskOptions {
        scope: a.scope,
        owner: cfg.owner.clone(),
        irreversible: a.irreversible,
        strategy: a.strategy,
        journal: !a.no_journal,
        explain: a.explain || a.json,
        known: Vec::new(),
    };
    let mut vault = open_vault(home)?;
    vault.begin()?;
    let result = engine.mask(
        &Input {
            text: &text,
            format,
        },
        &opts,
        &mut vault,
    );
    let masked = match result {
        Ok(m) if !a.dry_run => {
            vault.commit()?;
            m
        }
        Ok(m) => {
            vault.rollback()?;
            m
        }
        Err(e) => {
            vault.rollback()?;
            return Err(e.into());
        }
    };
    if !a.dry_run {
        vault.purge_journal(i64::from(cfg.journal_ttl_days) * 86_400)?;
    }
    if a.explain && !a.json {
        print_explain(&masked.explain);
    }
    if a.json {
        write_output(
            a.output.as_deref(),
            &format!("{}\n", serde_json::to_string_pretty(&masked)?),
        )?;
    } else {
        write_output(a.output.as_deref(), &masked.text)?;
    }
    if a.report || a.explain {
        eprintln!(
            "masked {} value(s), {} uncertain{}{}",
            masked.replacements.len(),
            masked.uncertain,
            if masked.literals > 0 {
                format!(", {} token-like literal(s) reserved", masked.literals)
            } else {
                String::new()
            },
            if masked.uncertain > 0 && opts.journal && !a.dry_run && !a.irreversible {
                " — run `kaponir review`"
            } else {
                ""
            }
        );
    }
    Ok(ExitCode::SUCCESS)
}

fn print_explain(rows: &[kaponir_core::Explained]) {
    let mut err = std::io::stderr().lock();
    let _ = writeln!(
        err,
        "{:<5} {:<9} {:<6} {:<12} {:<16} {:<30} evidence",
        "apply", "verdict", "p", "kind", "recognizer", "value"
    );
    for e in rows {
        let ev: Vec<String> = e
            .evidence
            .iter()
            .map(|x| format!("{}{:+.1}", x.source, x.logit))
            .collect();
        let _ = writeln!(
            err,
            "{:<5} {:<9} {:<6.3} {:<12} {:<16} {:<30} {}",
            if e.applied { "yes" } else { "-" },
            format!("{:?}", e.verdict).to_lowercase(),
            e.probability,
            e.kind,
            e.recognizer,
            ellipsize(&e.surface, 30),
            ev.join(" ")
        );
    }
}

fn ellipsize(s: &str, max: usize) -> String {
    let one_line = s.replace(['\n', '\r'], "⏎");
    if one_line.chars().count() <= max {
        one_line
    } else {
        format!("{}…", one_line.chars().take(max - 1).collect::<String>())
    }
}

fn restore(home: &Home, a: RestoreArgs) -> Result<ExitCode> {
    let (cfg, engine) = engine(home)?;
    let text = read_input(a.file.as_deref())?;
    let vault = open_vault(home)?;
    let escape = if a.json_escape {
        Escape::Json
    } else {
        Escape::None
    };
    let r = engine.restore(&text, &Principal::local(cfg.owner), &vault, escape)?;
    write_output(a.output.as_deref(), &r.text)?;
    let problems = !r.unknown.is_empty() || !r.denied.is_empty();
    if a.report || problems {
        eprintln!("restored {} value(s)", r.restored);
        if !r.unknown.is_empty() {
            eprintln!(
                "unknown token(s) (not in vault): {}",
                dedup(&r.unknown).join(", ")
            );
        }
        if !r.denied.is_empty() {
            eprintln!("no access to restore: {}", dedup(&r.denied).join(", "));
        }
    }
    Ok(if a.strict && problems {
        ExitCode::from(2)
    } else {
        ExitCode::SUCCESS
    })
}

fn dedup(v: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for s in v {
        if !out.contains(s) {
            out.push(s.clone());
        }
    }
    out
}

// ---------- review ----------

fn review(home: &Home, action: Option<ReviewCmd>, list: ReviewList) -> Result<ExitCode> {
    let vault = open_vault(home)?;
    if list.auto || list.auto_save {
        return review_auto(home, &vault, list.auto_save, 0.85, list.json);
    }
    let Some(action) = action else {
        return review_list(&vault, &list);
    };
    match action {
        ReviewCmd::Auto { save, threshold } => {
            return review_auto(home, &vault, save, threshold, list.json);
        }
        ReviewCmd::Mask { sel, kind, scope } => {
            let rows = select(&vault, &sel)?;
            for r in &rows {
                let p = home.append_rule(scope, r.mask_rule(kind.as_deref()))?;
                vault.set_journal_status(r.id, "masked")?;
                eprintln!("#{}: always mask `{}` → {}", r.id, r.value, p.display());
            }
            let _ = kaponir_cli::s1::auto_train(home);
        }
        ReviewCmd::Keep { sel, scope } => {
            let rows = select(&vault, &sel)?;
            for r in &rows {
                let p = home.append_rule(scope, r.keep_rule())?;
                vault.set_journal_status(r.id, "kept")?;
                eprintln!("#{}: never mask `{}` → {}", r.id, r.value, p.display());
            }
            let _ = kaponir_cli::s1::auto_train(home);
        }
        ReviewCmd::Template { template, scope } => {
            let (re, _) = kaponir_recognizers::rules::compile_template(&template)
                .map_err(|e| anyhow!("template: {e}"))?;
            let re = regex::RegexBuilder::new(&format!("^(?:{re})$"))
                .case_insensitive(true)
                .build()?;
            let rule = Rule {
                template: Some(template.clone()),
                note: "kaponir review template".into(),
                ..Rule::default()
            };
            let p = home.append_rule(scope, rule)?;
            eprintln!("template `{template}` → {}", p.display());
            // Закрываем записи, чьё значение или контекст целиком покрываются шаблоном.
            let mut closed = 0;
            for r in vault.journal(Some("open"), usize::MAX >> 1)? {
                if r.covered_by(|w| re.is_match(w)) {
                    vault.set_journal_status(r.id, "masked")?;
                    closed += 1;
                }
            }
            eprintln!(
                "closed {closed} journal entr{}",
                if closed == 1 { "y" } else { "ies" }
            );
        }
        ReviewCmd::Dismiss { sel } => {
            for r in select(&vault, &sel)? {
                vault.set_journal_status(r.id, "dismissed")?;
                eprintln!("#{}: dismissed", r.id);
            }
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn review_auto(
    home: &Home,
    vault: &SqliteVault,
    auto_save: bool,
    threshold: f32,
    as_json: bool,
) -> Result<ExitCode> {
    use kaponir_core::s1::{CandidateView, Disclosure, classify_from_score, features};
    let rows = vault.journal(Some("open"), 500)?;
    if rows.is_empty() {
        if as_json {
            println!("{{\"items\":[],\"auto_saved\":0,\"total\":0}}");
        } else {
            eprintln!("nothing to review");
        }
        return Ok(ExitCode::SUCCESS);
    }
    let (_, eng) = engine(home)?;
    let lex = eng.lexicon();
    let views: Vec<CandidateView> = rows
        .iter()
        .map(|r| {
            let cat =
                serde_json::from_value(serde_json::json!(r.category)).unwrap_or(Category::Custom);
            let disc = eng
                .s1_config()
                .map_or(Disclosure::L0, |s| s.provider.max_disclosure());
            let disc = if cat == Category::Credential {
                Disclosure::L0
            } else {
                disc
            };
            CandidateView {
                features: features(&r.value, lex.is_word(&r.value)),
                kind: r.kind.clone(),
                category: cat,
                recognizer: r.recognizer.clone(),
                surface: (disc >= Disclosure::L1).then(|| r.value.clone()),
                context: (disc >= Disclosure::L2).then(|| r.context.clone()),
            }
        })
        .collect();
    let fallback = || {
        rows.iter()
            .zip(&views)
            .map(|(r, v)| classify_from_score(v, r.probability as f32, "heuristic"))
            .collect::<Vec<_>>()
    };
    let classifications = eng
        .s1_config()
        .and_then(|s| {
            s.provider
                .classify(&views, &kaponir_core::s1::disclosure_profile(&s.profile))
                .ok()
        })
        .filter(|c| c.len() == rows.len())
        .unwrap_or_else(fallback);

    let mut auto_saved = 0usize;
    let mut items = Vec::with_capacity(rows.len());
    let mut seen = std::collections::HashSet::new();
    let threshold = threshold.clamp(0.5, 0.99);
    for (r, cls) in rows.iter().zip(classifications) {
        let mut saved_this = false;
        if auto_save
            && cls.source != "heuristic"
            && cls.confidence.is_finite()
            && cls.confidence >= threshold
        {
            if seen.insert((cls.scope, r.value.clone())) {
                let rule = if cls.action == kaponir_core::rules::RuleAction::Mask {
                    r.mask_rule(Some(&cls.kind))
                } else {
                    r.keep_rule()
                };
                if home.add_rule(cls.scope, rule).is_ok() {
                    saved_this = true;
                }
            } else {
                saved_this = true;
            }
            if saved_this {
                let status = if cls.action == kaponir_core::rules::RuleAction::Mask {
                    "masked"
                } else {
                    "kept"
                };
                vault.set_journal_status(r.id, status)?;
                auto_saved += 1;
            }
        }
        if !as_json {
            println!(
                "#{:<5} {:<6} -> {:<10} scope={:<8} conf={:.0}%{}  {}",
                r.id,
                if cls.action == kaponir_core::rules::RuleAction::Mask {
                    "MASK"
                } else {
                    "KEEP"
                },
                cls.kind,
                cls.scope.as_str(),
                cls.confidence * 100.0,
                if saved_this { " [SAVED]" } else { "" },
                ellipsize(&r.value, 50)
            );
        }
        items.push(serde_json::json!({
            "id": r.id,
            "value": r.value,
            "action": if cls.action == kaponir_core::rules::RuleAction::Mask { "mask" } else { "keep" },
            "kind": cls.kind,
            "scope": cls.scope,
            "confidence": cls.confidence,
            "auto_saved": saved_this,
        }));
    }
    if auto_saved > 0 {
        let _ = kaponir_cli::s1::auto_train(home);
    }
    if as_json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "items": items,
                "auto_saved": auto_saved,
                "total": rows.len(),
            }))?
        );
    } else {
        eprintln!(
            "\nКлассифицировано записей: {}, автоматически сохранено правил (>={:.0}%): {}",
            rows.len(),
            threshold * 100.0,
            auto_saved
        );
    }
    Ok(ExitCode::SUCCESS)
}

fn select(vault: &SqliteVault, sel: &Selection) -> Result<Vec<JournalRow>> {
    if sel.ids.is_empty() && sel.shape.is_none() {
        bail!("specify journal ids or --shape");
    }
    let all = vault.journal(None, usize::MAX >> 1)?;
    let mut out = Vec::new();
    for id in &sel.ids {
        let r = all
            .iter()
            .find(|r| r.id == *id)
            .ok_or_else(|| anyhow!("no journal entry #{id}"))?;
        out.push(r.clone());
    }
    if let Some(shape) = &sel.shape {
        let before = out.len();
        out.extend(
            all.iter()
                .filter(|r| r.status == "open" && &r.shape == shape && !sel.ids.contains(&r.id))
                .cloned(),
        );
        if out.len() == before && sel.ids.is_empty() {
            bail!("no open journal entries with shape `{shape}`");
        }
    }
    Ok(out)
}

fn review_list(vault: &SqliteVault, list: &ReviewList) -> Result<ExitCode> {
    let status = (list.status != "all").then_some(list.status.as_str());
    let rows = vault.journal(status, list.limit)?;
    if list.json {
        println!("{}", serde_json::to_string_pretty(&rows)?);
        return Ok(ExitCode::SUCCESS);
    }
    if rows.is_empty() {
        eprintln!("nothing to review");
        return Ok(ExitCode::SUCCESS);
    }
    if list.group {
        let mut groups: BTreeMap<(String, String), Vec<&JournalRow>> = BTreeMap::new();
        for r in &rows {
            groups
                .entry((r.kind.clone(), r.shape.clone()))
                .or_default()
                .push(r);
        }
        let mut groups: Vec<_> = groups.into_iter().collect();
        groups.sort_by_key(|(_, v)| std::cmp::Reverse(v.iter().map(|r| r.count).sum::<i64>()));
        for ((kind, shape), v) in groups {
            let total: i64 = v.iter().map(|r| r.count).sum();
            let sample: Vec<&str> = v.iter().take(6).map(|r| r.value.as_str()).collect();
            println!(
                "{kind:<10} shape {shape:<16} {} value(s), seen {total}×: {}",
                v.len(),
                sample.join(", ")
            );
            println!(
                "           kaponir review mask --shape '{shape}' [--kind {kind}]   |   kaponir review keep --shape '{shape}'"
            );
        }
        return Ok(ExitCode::SUCCESS);
    }
    for r in &rows {
        let verdict_note = if r.verdict == "uncertain" {
            format!(
                "Спорно (p={:.2}, политика uncertain={})",
                r.probability, r.applied
            )
        } else {
            format!("{} (p={:.2})", r.applied, r.probability)
        };
        println!(
            "#{:<5} ×{:<4} {:<10} {:<40} [scope: {}] {:<14} {}",
            r.id,
            r.count,
            r.kind,
            verdict_note,
            r.recommended_scope.as_str(),
            r.recognizer,
            ellipsize(&r.value, 60)
        );
        println!("        {}", ellipsize(&r.context, 110));
    }
    println!(
        "\nkaponir review mask <id>… [--kind K] [--scope team] | keep <id>… | auto [--save] | template '<tpl>' | dismiss <id>…  (--group to cluster)"
    );
    Ok(ExitCode::SUCCESS)
}

// ---------- rules ----------

fn add_domains(home: &Home, scope: Scope, domains: &[String]) -> Result<()> {
    let p = home.rules_path(scope);
    let existing = std::fs::read_to_string(&p).unwrap_or_default();
    let current = RuleFile::parse(&existing)
        .map_err(|e| anyhow!("{}: {e}", p.display()))?
        .corp
        .domains;
    home.set_corp_domains(scope, &[current, domains.to_vec()].concat())?;
    eprintln!("corp domains → {}", p.display());
    Ok(())
}

fn rule(home: &Home, action: RuleCmd) -> Result<ExitCode> {
    match action {
        RuleCmd::Add {
            scope,
            kind,
            value,
            template,
            regex,
            keep,
            case_sensitive,
            enforced,
            strategy,
            category,
            note,
        } => {
            if value.is_none() && template.is_none() && regex.is_none() {
                bail!("one of --value, --template, --regex is required");
            }
            if enforced && scope != Scope::Global {
                bail!("--enforced applies only to --scope global");
            }
            let rule = Rule {
                id: String::new(),
                kind: if keep {
                    "KEEP".into()
                } else {
                    kind.to_ascii_uppercase()
                },
                action: if keep {
                    RuleAction::Keep
                } else {
                    RuleAction::Mask
                },
                value,
                template,
                regex,
                case_sensitive,
                enforced,
                disabled: false,
                category,
                strategy,
                form: None,
                note,
            };
            // Компилируем заранее: ошибка в шаблоне не должна сломать файл правил.
            let p = home.add_rule(scope, rule)?;
            eprintln!("rule added → {}", p.display());
        }
        RuleCmd::Domain { domains, scope } => {
            if domains.is_empty() {
                bail!("specify at least one domain");
            }
            add_domains(home, scope, &domains)?;
        }
        RuleCmd::List { scope } => {
            for set in home.load_rules()? {
                if scope.is_some_and(|s| s != set.scope) {
                    continue;
                }
                println!("[{}] {}", set.scope, home.rules_path(set.scope).display());
                if !set.file.corp.domains.is_empty() {
                    println!("  corp domains: {}", set.file.corp.domains.join(", "));
                }
                for (n, r) in set.file.rules.iter().enumerate() {
                    let pattern = r
                        .value
                        .as_ref()
                        .map(|v| format!("value `{v}`"))
                        .or_else(|| r.template.as_ref().map(|t| format!("template `{t}`")))
                        .or_else(|| r.regex.as_ref().map(|x| format!("regex `{x}`")))
                        .unwrap_or_default();
                    let id = if r.id.is_empty() {
                        format!("#{}", n + 1)
                    } else {
                        r.id.clone()
                    };
                    println!(
                        "  {id:<14} {:<4} {:<10} {pattern}{}",
                        format!("{:?}", r.action).to_lowercase(),
                        r.kind,
                        if r.enforced { "  [enforced]" } else { "" }
                    );
                }
            }
        }
        RuleCmd::Check => {
            let rules = home.load_rules()?;
            kaponir_recognizers::RulesRecognizer::new(&rules)?;
            let n: usize = rules.iter().map(|s| s.file.rules.len()).sum();
            eprintln!("ok: {n} rule(s) in {} file(s)", rules.len());
        }
    }
    Ok(ExitCode::SUCCESS)
}

// ---------- packs ----------

fn pack(home: &Home, action: PackCmd) -> Result<ExitCode> {
    let cfg = home.load_config()?;
    match action {
        PackCmd::List => {
            for p in load_packs(&cfg)? {
                let i = p.info();
                println!(
                    "{:<14} {:<7} v{:<7} api {} {:<16} {}",
                    i.id,
                    format!("{:?}", i.kind).to_lowercase(),
                    i.version,
                    i.api_version,
                    i.license,
                    i.description
                );
            }
        }
        PackCmd::Test { dir, corpus } => {
            let tested = kaponir_cli::pack_from_dir(&dir)?;
            let id = tested.info().id.clone();
            // Пакет проверяется вместе с остальными включёнными; одноимённый встроенный заменяется.
            let mut cfg = cfg;
            cfg.packs.retain(|p| *p != id);
            let mut packs = load_packs(&cfg)?;
            packs.push(tested);
            // Корпус проверяет детекторы пакета независимо от обученной модели пользователя.
            let engine = build_engine(&cfg, packs, &[], [7; 32], None)?;
            let path = corpus.unwrap_or_else(|| dir.join("tests").join("corpus.jsonl"));
            let text =
                std::fs::read_to_string(&path).with_context(|| format!("{}", path.display()))?;
            let cases = kaponir_packs::corpus::parse_jsonl(&text)
                .map_err(|e| anyhow!("{}: {e}", path.display()))?;
            let r = kaponir_packs::corpus::run(&engine, &cases);
            for (line, what, out) in &r.failures {
                println!("{}:{line}: {what}", path.display());
                if !out.is_empty() {
                    println!("    → {}", ellipsize(out, 160));
                }
            }
            println!(
                "{id}: {} case(s), recall {:.1}% ({}/{}), kept {:.1}% ({}/{})",
                r.cases,
                r.recall() * 100.0,
                r.mask_ok,
                r.mask_total,
                r.keep_rate() * 100.0,
                r.keep_ok,
                r.keep_total
            );
            if !r.passed() {
                return Ok(ExitCode::from(2));
            }
        }
    }
    Ok(ExitCode::SUCCESS)
}

// ---------- serve ----------

fn serve(home: &Home, a: ServeArgs, open: bool) -> Result<ExitCode> {
    use std::net::ToSocketAddrs;
    let (mut cfg, engine) = engine(home)?;
    let s = &mut cfg.serve;
    if let Some(l) = a.listen {
        s.listen = l;
    }
    if let Some(u) = a.anthropic_upstream {
        s.anthropic_upstream = u;
    }
    if let Some(u) = a.openai_upstream {
        s.openai_upstream = u;
    }
    if a.no_system_hint {
        s.system_hint = false;
    }
    if let Ok(t) = std::env::var("KAPONIR_API_TOKEN")
        && !t.is_empty()
    {
        s.api_token = Some(t);
    }
    let addrs: Vec<_> = s
        .listen
        .to_socket_addrs()
        .with_context(|| format!("listen `{}`", s.listen))?
        .collect();
    if s.api_token.is_none() && !addrs.iter().all(|a| a.ip().is_loopback()) {
        bail!(
            "refusing to listen on {} without a token: the API can restore originals. \
             Set [serve] api_token or KAPONIR_API_TOKEN, or listen on 127.0.0.1",
            s.listen
        );
    }
    let vault = open_vault(home)?;
    let ttl = i64::from(cfg.journal_ttl_days) * 86_400;
    vault.purge_journal(ttl)?;
    vault.purge_sessions(ttl)?;
    let listen = cfg.serve.listen.clone();
    eprintln!("kaponir serve: http://{listen}");
    eprintln!("  anthropic (config) → {}", cfg.serve.anthropic_upstream);
    eprintln!("  openai (config)    → {}", cfg.serve.openai_upstream);
    eprintln!("  Saved providers in the vault override these config defaults.");
    eprintln!("  Claude Code:  ANTHROPIC_BASE_URL=http://{listen} claude");
    eprintln!("  OpenAI SDK:   OPENAI_BASE_URL=http://{listen}/v1");
    let ui = format!("http://{}/ui/", ui_host(&listen));
    eprintln!("  Web UI:       {ui}");
    let control = Box::new(kaponir_cli::HomeControl {
        home: Home::new(home.dir.clone()),
    });
    let external_worker = std::env::var("KAPONIR_WORKER_URL")
        .ok()
        .or_else(|| cfg.serve.worker_url.clone());
    let mut state = kaponir_proxy::State::new(engine, vault, cfg.owner.clone(), cfg.serve.clone())?
        .with_control(control);
    if external_worker.is_none() {
        match kaponir_cli::worker::ManagedWorker::new(
            home,
            format!("http://{}", ui_host(&listen)),
            cfg.serve.api_token.clone(),
        ) {
            Ok(manager) => {
                let manager = std::sync::Arc::new(manager);
                if manager.activation_enabled() {
                    use kaponir_proxy::WorkerManager;
                    if let Err(e) = manager.start() {
                        eprintln!("kaponir: worker autostart: {e}");
                    }
                }
                state = state.with_worker_manager(manager);
            }
            Err(e) => eprintln!("kaponir: local worker: {e}"),
        }
    }
    let state = std::sync::Arc::new(state);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    rt.block_on(async move {
        let listener = tokio::net::TcpListener::bind(&listen)
            .await
            .with_context(|| format!("listen `{listen}`"))?;
        if open {
            open_browser(&ui);
        }
        kaponir_proxy::serve_on(state, listener).await
    })?;
    Ok(ExitCode::SUCCESS)
}

/// Адрес для браузера: `0.0.0.0`/`::` → loopback.
fn ui_host(listen: &str) -> String {
    match listen.rsplit_once(':') {
        Some(("0.0.0.0", port)) => format!("127.0.0.1:{port}"),
        Some(("[::]", port)) => format!("[::1]:{port}"),
        _ => listen.to_string(),
    }
}

fn open_browser(url: &str) {
    let cmd = if cfg!(target_os = "macos") {
        "open"
    } else if cfg!(windows) {
        "explorer"
    } else {
        "xdg-open"
    };
    if std::process::Command::new(cmd)
        .arg(url)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .is_err()
    {
        eprintln!("open {url} in a browser");
    }
}

// ---------- stats ----------

fn stats(home: &Home) -> Result<ExitCode> {
    let (cfg, engine) = engine(home)?;
    let vault = open_vault(home)?;
    let s = vault.stats()?;
    println!("home:      {}", home.dir.display());
    println!("owner:     {}", cfg.owner);
    println!(
        "vault:     {} entr(ies), {} reserved literal(s)",
        s.entries, s.reserved
    );
    println!(
        "journal:   {} open / {} total (ttl {} days)",
        s.journal_open, s.journal_total, cfg.journal_ttl_days
    );
    let ids: Vec<&str> = engine
        .packs()
        .iter()
        .map(|p| p.info().id.as_str())
        .collect();
    println!("packs:     {}", ids.join(", "));
    for set in home.load_rules()? {
        println!(
            "rules:     {:<8} {} rule(s), {} corp domain(s)",
            set.scope.as_str(),
            set.file.rules.len(),
            set.file.corp.domains.len()
        );
    }
    Ok(ExitCode::SUCCESS)
}

fn setup_wizard(home: &Home, language: &str) -> Result<()> {
    let ru = language == "ru";
    let msg = |r: &'static str, e: &'static str| if ru { r } else { e };
    fn ask(prompt: &str) -> Result<Option<String>> {
        print!("{prompt} ");
        std::io::stdout().flush()?;
        let mut answer = String::new();
        if std::io::stdin().read_line(&mut answer)? == 0 {
            return Ok(None);
        }
        Ok(Some(answer.trim().to_string()))
    }
    println!(
        "{}",
        msg(
            "Необязательная настройка. Базовые детекторы уже включены.",
            "Optional setup. Built-in detectors are already enabled."
        )
    );
    let Some(start) = ask(msg("Начать? [y/N]", "Start setup? [y/N]"))? else {
        return Ok(());
    };
    if !["y", "yes", "да", "д"].contains(&start.to_lowercase().as_str()) {
        return Ok(());
    }
    let Some(mode) = ask(msg(
        "Использование: 1 — API-прокси, 2 — чат в браузере [1]",
        "Usage: 1 — API proxy, 2 — browser chatbot [1]",
    ))?
    else {
        return Ok(());
    };
    let Some(domains) = ask(msg(
        "Внутренние домены через запятую (можно пропустить):",
        "Internal domains, comma-separated (optional):",
    ))?
    else {
        return Ok(());
    };
    let Some(projects) = ask(msg(
        "Конфиденциальные проекты через точку с запятой (можно пропустить):",
        "Confidential projects, separated by semicolons (optional):",
    ))?
    else {
        return Ok(());
    };
    let answers = kaponir_core::onboarding::Answers {
        domains: domains
            .split([',', ';', ' '])
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect(),
        projects: projects
            .split(';')
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect(),
    };
    let plan = kaponir_core::onboarding::plan(&answers).map_err(|e| {
        anyhow!(
            "{}: {e}",
            msg("Некорректные ответы", "Invalid setup answers")
        )
    })?;
    println!(
        "{}",
        msg(
            "Будут добавлены личные MASK-правила. Существующие настройки сохраняются.",
            "Personal MASK rules will be added. Existing settings are preserved."
        )
    );
    println!("{}: {}", msg("Домены", "Domains"), plan.domains.join(", "));
    for rule in &plan.rules {
        println!("PROJ: {}", rule.value.as_deref().unwrap_or_default());
    }
    let Some(confirm) = ask(msg("Применить? [y/N]", "Apply? [y/N]"))? else {
        return Ok(());
    };
    if !["y", "yes", "да", "д"].contains(&confirm.to_lowercase().as_str()) {
        return Ok(());
    }
    let sets = home.load_rules()?;
    let existing = sets.iter().find(|s| s.scope == Scope::Personal);
    for rule in plan.rules {
        if !existing.is_some_and(|s| {
            s.file.rules.iter().any(|r| {
                r.action == RuleAction::Mask
                    && r.kind == rule.kind
                    && r.value
                        .as_ref()
                        .zip(rule.value.as_ref())
                        .is_some_and(|(a, b)| a.to_lowercase() == b.to_lowercase())
            })
        }) {
            home.add_rule(Scope::Personal, rule)?;
        }
    }
    if !plan.domains.is_empty() {
        add_domains(home, Scope::Personal, &plan.domains)?;
    }
    println!(
        "{}",
        if mode == "2" {
            msg(
                "Готово. Откройте kaponir ui → Лаборатория.",
                "Ready. Open kaponir ui → Lab.",
            )
        } else {
            msg(
                "Готово. Откройте kaponir ui → Подключение.",
                "Ready. Open kaponir ui → Connection.",
            )
        }
    );
    Ok(())
}

// ---------- models ----------

fn models_cmd(home: &Home, action: ModelsCmd) -> Result<ExitCode> {
    let cfg = home.load_config()?;
    let model_dir = if cfg.worker.model_dir.is_absolute() {
        cfg.worker.model_dir.clone()
    } else {
        home.dir.join(&cfg.worker.model_dir)
    };
    let s1_ngram_path = home.s1_dir().join(kaponir_s1::ngram::MODEL_FILE);
    let runtime_path = cfg.worker.runtime.clone().unwrap_or_else(|| {
        model_dir
            .join("runtime")
            .join("llama-b12038")
            .join(if cfg!(windows) {
                "llama-server.exe"
            } else {
                "llama-server"
            })
    });
    let catalog = [
        (
            "qwen3-0.6b",
            "q4",
            "Qwen3-0.6B-Q4_K_M.gguf",
            396_705_472u64,
            "Локальная CPU микро-LLM (быстрая, ~397 МБ, рекомендуется)",
        ),
        (
            "qwen3-0.6b-q8",
            "q5",
            "Qwen3-0.6B-Q8_0.gguf",
            639_446_688u64,
            "Текстовая Qwen3-0.6B Q8_0 (~639 МБ)",
        ),
    ];
    match action {
        ModelsCmd::List => {
            println!("Локальные модели Kaponir ({}):", model_dir.display());
            for (id, alias, file, size, desc) in catalog {
                let p = model_dir.join(file);
                let installed = p.is_file() && p.metadata().is_ok_and(|m| m.len() == size);
                println!(
                    "  {:<14} (алиас {:<2}) {:<12} {:>6.1} МБ  {}",
                    id,
                    alias,
                    if installed {
                        "УСТАНОВЛЕНА"
                    } else {
                        "не скачана"
                    },
                    size as f64 / (1024.0 * 1024.0),
                    desc
                );
            }
            println!(
                "  {:<14}            {:<12}            Локальный классификатор правил ({})",
                "s1:ngram",
                if s1_ngram_path.is_file() {
                    "ОБУЧЕНА"
                } else {
                    "не обучена"
                },
                s1_ngram_path.display()
            );
            println!(
                "  {:<14}            {:<12}            {}",
                "llama-server",
                if runtime_path.is_file() {
                    "ГОТОВ"
                } else {
                    "не установлен"
                },
                runtime_path.display()
            );
            println!(
                "\nКоманды: kaponir models download qwen3-0.6b | kaponir models status | kaponir s1 train"
            );
            Ok(ExitCode::SUCCESS)
        }
        ModelsCmd::Status => {
            let mut models = serde_json::Map::new();
            for (id, alias, file, size, desc) in catalog {
                let p = model_dir.join(file);
                let installed = p.is_file() && p.metadata().is_ok_and(|m| m.len() == size);
                models.insert(
                    id.to_string(),
                    serde_json::json!({
                        "alias": alias,
                        "file": file,
                        "path": p,
                        "bytes": size,
                        "downloaded": installed,
                        "description": desc,
                    }),
                );
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "model_dir": model_dir,
                    "runtime_ready": runtime_path.is_file(),
                    "runtime_path": runtime_path,
                    "s1_ngram_ready": s1_ngram_path.is_file(),
                    "s1_provider": cfg.s1.provider,
                    "models": models,
                }))?
            );
            Ok(ExitCode::SUCCESS)
        }
        ModelsCmd::Download { model } => kaponir_cli::worker::run(
            home,
            &[
                std::ffi::OsString::from("download"),
                std::ffi::OsString::from(model),
            ],
        ),
    }
}

// ---------- events & request inspector ----------

fn events_cmd(
    home: &Home,
    action: Option<EventsCmd>,
    default_limit: usize,
    default_json: bool,
) -> Result<ExitCode> {
    let vault = open_vault(home)?;
    match action.unwrap_or(EventsCmd::List {
        limit: default_limit,
        json: default_json,
    }) {
        EventsCmd::List { limit, json } => {
            let raw_list = vault.load_traffic_events(limit)?;
            let mut events: Vec<serde_json::Value> = raw_list
                .into_iter()
                .filter_map(|s| serde_json::from_slice(&s).ok())
                .collect();
            events.reverse();
            if json {
                println!("{}", serde_json::to_string_pretty(&events)?);
                return Ok(ExitCode::SUCCESS);
            }
            if events.is_empty() {
                eprintln!("Журнал событий пуст.");
                return Ok(ExitCode::SUCCESS);
            }
            println!(
                "{:<6} {:<9} {:<6} {:<16} {:<12} {:<10} {:<12} {:<24}",
                "ID", "API", "HTTP", "Модель", "Скрыто/Кэш", "Восст.", "Оверхед", "Диагностика"
            );
            for ev in &events {
                let id = ev["id"].as_u64().unwrap_or(0);
                let api = ev["api"].as_str().unwrap_or("-");
                let status = ev["status"].as_u64().unwrap_or(0);
                let model = ev["model"].as_str().unwrap_or("-");
                let repl = ev["replacements"].as_u64().unwrap_or(0);
                let cached = ev["cached_replacements"].as_u64().unwrap_or(0);
                let rest = ev["restored"].as_u64().unwrap_or(0);
                let mask_us = ev["timings"]["mask_us"].as_u64().unwrap_or(0);
                let cls_us = ev["timings"]["classifier_us"].as_u64().unwrap_or(0);
                let rest_us = ev["timings"]["restore_us"].as_u64().unwrap_or(0);
                let overhead_ms = (mask_us + cls_us + rest_us) as f64 / 1000.0;
                let diag = if let Some(err) = ev.get("error").filter(|v| !v.is_null()) {
                    format!(
                        "ОШИБКА: {} ({})",
                        err["kind"].as_str().unwrap_or("upstream"),
                        err["message"].as_str().unwrap_or("")
                    )
                } else if ev["unknown"].as_u64().unwrap_or(0) > 0 {
                    format!("Неизв. токены: {}", ev["unknown"].as_u64().unwrap_or(0))
                } else if ev["denied"].as_u64().unwrap_or(0) > 0 {
                    format!("Отказ доступа: {}", ev["denied"].as_u64().unwrap_or(0))
                } else if ev["uncertain"].as_u64().unwrap_or(0) > 0 {
                    format!("Спорных: {}", ev["uncertain"].as_u64().unwrap_or(0))
                } else {
                    "ОК".to_string()
                };
                println!(
                    "#{:<5} {:<9} {:<6} {:<16} {:<12} {:<10} {:>7.2} мс   {}",
                    id,
                    api,
                    status,
                    ellipsize(model, 16),
                    format!("{repl}+{cached}"),
                    rest,
                    overhead_ms,
                    ellipsize(&diag, 45)
                );
            }
            println!("\nДетальный просмотр запроса (Request Inspector): kaponir events show <ID>");
            Ok(ExitCode::SUCCESS)
        }
        EventsCmd::Show { id, json } => {
            let raw_list = vault.load_traffic_events(200)?;
            let ev: serde_json::Value = raw_list
                .into_iter()
                .filter_map(|s| serde_json::from_slice::<serde_json::Value>(&s).ok())
                .find(|v| v["id"].as_u64() == Some(id))
                .ok_or_else(|| anyhow!("событие #{id} не найдено в vault"))?;
            if json {
                println!("{}", serde_json::to_string_pretty(&ev)?);
                return Ok(ExitCode::SUCCESS);
            }
            println!(
                "=== Событие #{} ({}) ===",
                id,
                ev["api"].as_str().unwrap_or("-")
            );
            println!(
                "Статус HTTP: {} | Модель: {} | Поток (SSE): {}",
                ev["status"].as_u64().unwrap_or(0),
                ev["model"].as_str().unwrap_or("-"),
                ev["streaming"].as_bool().unwrap_or(false)
            );
            let t = &ev["timings"];
            println!(
                "Тайминги: Маскирование {:.2} мс | Классификатор {:.2} мс | Внешняя LLM {:.2} мс | Демаскировка {:.2} мс",
                t["mask_us"].as_u64().unwrap_or(0) as f64 / 1000.0,
                t["classifier_us"].as_u64().unwrap_or(0) as f64 / 1000.0,
                t["upstream_us"].as_u64().unwrap_or(0) as f64 / 1000.0,
                t["restore_us"].as_u64().unwrap_or(0) as f64 / 1000.0,
            );
            println!(
                "Подстановки: {} новых + {} из кэша сессии | Восстановлено: {} | Токены LLM: {} (in={} / out={})",
                ev["replacements"].as_u64().unwrap_or(0),
                ev["cached_replacements"].as_u64().unwrap_or(0),
                ev["restored"].as_u64().unwrap_or(0),
                ev["total_tokens"].as_u64().unwrap_or(0),
                ev["prompt_tokens"].as_u64().unwrap_or(0),
                ev["completion_tokens"].as_u64().unwrap_or(0),
            );
            if let Some(err) = ev.get("error").filter(|v| !v.is_null()) {
                println!(
                    "\n[!] ОШИБКА ПРОВАЙДЕРА: {} (HTTP {:?}) URL: {}\n    {}",
                    err["kind"].as_str().unwrap_or("error"),
                    err["http_status"],
                    err["upstream_url"].as_str().unwrap_or("-"),
                    err["message"].as_str().unwrap_or("-")
                );
            }
            if let Some(items) = ev["replacement_items"].as_array().filter(|a| !a.is_empty()) {
                println!("\n--- Замаскированные сущности ({}) ---", items.len());
                for item in items {
                    println!(
                        "  {}  <=  `{}`  (тип: {}, контур: {}, вердикт: {}, p={:.2})",
                        item["token"].as_str().unwrap_or(""),
                        item["original"].as_str().unwrap_or(""),
                        item["kind"].as_str().unwrap_or(""),
                        item["scope"].as_str().unwrap_or(""),
                        item["verdict"].as_str().unwrap_or(""),
                        item["probability"].as_f64().unwrap_or(0.0),
                    );
                }
            }
            if let Some(insp) = ev.get("inspector").filter(|v| !v.is_null()) {
                println!(
                    "\n--- 1. Исходный запрос пользователя ---\n{}",
                    insp["original_request"].as_str().unwrap_or("")
                );
                println!(
                    "\n--- 2. Реальный обезличенный запрос в LLM (Upstream) ---\n{}",
                    insp["masked_request"].as_str().unwrap_or("")
                );
                if let Some(raw_resp) = insp["raw_response"].as_str().filter(|s| !s.is_empty()) {
                    println!("\n--- 3. Сырой ответ LLM (до демаскировки) ---\n{raw_resp}");
                }
                if let Some(rest_resp) =
                    insp["restored_response"].as_str().filter(|s| !s.is_empty())
                {
                    println!("\n--- 4. Восстановленный ответ пользователю ---\n{rest_resp}");
                }
            }
            Ok(ExitCode::SUCCESS)
        }
    }
}
