//! Общее состояние сервера: движок, vault и сессии. Всё изменяемое лежит под одним мьютексом,
//! поэтому порядок блокировок не важен. Движок неизменяем; при смене правил он заменяется
//! целиком, а операции, начатые со старым движком, его же и дорабатывают.

use kaponir_core::{
    Engine, Escape, Input, Known, MaskOptions, Masked, Principal, Restored, StreamRestorer,
};
use kaponir_vault::SqliteVault;
use std::collections::hash_map::Entry;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use crate::traffic::{Traffic, Usage};
use crate::{Control, ProxyConfig};

struct CachedMask {
    text: String,
    usage: Usage,
}

/// Сколько замаскированных блоков держать в кэше сессии.
const CACHE_LIMIT: usize = 8192;

pub(crate) struct Session {
    known: Vec<Known>,
    seen: HashSet<Known>,
    /// BLAKE3 исходного блока → замаскированный. История чата не маскируется заново,
    /// и префикс запроса не меняется между ходами (prompt caching провайдера).
    cache: HashMap<[u8; 32], CachedMask>,
    /// Потоки sidecar-восстановления по id.
    streams: HashMap<String, StreamRestorer>,
    last_used: Instant,
    dirty: bool,
}

impl Session {
    fn new(known: Vec<Known>) -> Self {
        let seen = known.iter().cloned().collect();
        Self {
            known,
            seen,
            cache: HashMap::new(),
            streams: HashMap::new(),
            last_used: Instant::now(),
            dirty: false,
        }
    }

    fn learn(&mut self, known: Vec<Known>) {
        for k in known {
            if self.seen.insert(k.clone()) {
                self.known.push(k);
                self.dirty = true;
            }
        }
    }
}

struct Inner {
    vault: SqliteVault,
    sessions: HashMap<String, Session>,
    last_evict: Instant,
}

pub struct State {
    pub(crate) provider_refresh: tokio::sync::Mutex<()>,
    pub(crate) provider_affinity: Mutex<HashMap<[u8; 32], crate::providers::Binding>>,
    pub(crate) traffic: Arc<Traffic>,
    pub(crate) started: Instant,
    pub(crate) requests: kaponir_core::metrics::OperationMetrics,
    pub(crate) stream_errors: std::sync::atomic::AtomicU64,
    engine: RwLock<Arc<Engine>>,
    pub principal: Principal,
    pub cfg: ProxyConfig,
    pub(crate) http: reqwest::Client,
    /// Управление правилами из Web UI; без него UI работает только на чтение.
    pub(crate) control: Option<Box<dyn Control>>,
    owner: String,
    inner: Mutex<Inner>,
}

impl State {
    pub fn new(
        engine: Engine,
        vault: SqliteVault,
        owner: impl Into<String>,
        cfg: ProxyConfig,
    ) -> anyhow::Result<Self> {
        engine
            .metrics()
            .set_s1_failure_sink(Arc::new(crate::metrics::StderrDiagnostics));
        let owner = owner.into();
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(30))
            .build()?;
        Ok(Self {
            provider_refresh: tokio::sync::Mutex::new(()),
            provider_affinity: Mutex::new(HashMap::new()),
            traffic: Arc::new(Traffic::default()),
            started: Instant::now(),
            requests: Default::default(),
            stream_errors: Default::default(),
            engine: RwLock::new(Arc::new(engine)),
            principal: Principal::local(owner.clone()),
            cfg,
            http,
            control: None,
            owner,
            inner: Mutex::new(Inner {
                vault,
                sessions: HashMap::new(),
                last_evict: Instant::now(),
            }),
        })
    }

    pub fn with_control(mut self, control: Box<dyn Control>) -> Self {
        self.control = Some(control);
        self
    }

    pub fn engine(&self) -> Arc<Engine> {
        self.engine
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    pub fn owner(&self) -> &str {
        &self.owner
    }

    /// Новый движок (правила изменились). Кэши замаскированных блоков сбрасываются: история
    /// разговоров должна маскироваться по новым правилам, даже ценой одного промаха prompt cache.
    pub fn replace_engine(&self, engine: Engine) {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let engine = engine.with_metrics(self.engine().metrics().clone());
        *self
            .engine
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Arc::new(engine);
        for s in inner.sessions.values_mut() {
            s.cache.clear();
        }
    }

    /// Прямой доступ к движку и vault без сессии (Web UI: анализ, журнал).
    pub fn with_vault<T>(
        &self,
        f: impl FnOnce(&Engine, &mut SqliteVault) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        let mut guard = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        f(&self.engine(), &mut guard.vault)
    }

    /// Выполняет `f` над сессией под блокировкой. `tx` — в транзакции vault: при ошибке
    /// выделенные токены откатываются, а кэш сессии сбрасывается.
    pub fn with<T>(
        &self,
        session: &str,
        tx: bool,
        f: impl FnOnce(&mut Cx<'_>) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        self.with_usage(session, tx, f).map(|(value, _)| value)
    }

    pub(crate) fn with_usage<T>(
        &self,
        session: &str,
        tx: bool,
        f: impl FnOnce(&mut Cx<'_>) -> anyhow::Result<T>,
    ) -> anyhow::Result<(T, Usage)> {
        let mut usage = Usage::default();
        let mut guard = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Inner {
            vault,
            sessions,
            last_evict,
        } = &mut *guard;
        if last_evict.elapsed() > Duration::from_secs(60) {
            let idle = Duration::from_secs(self.cfg.session_idle_hours * 3600);
            sessions.retain(|_, s| s.last_used.elapsed() < idle);
            *last_evict = Instant::now();
        }
        let sess = match sessions.entry(session.to_string()) {
            Entry::Occupied(e) => e.into_mut(),
            Entry::Vacant(e) => e.insert(Session::new(vault.load_session(session)?)),
        };
        sess.last_used = Instant::now();
        if tx {
            vault.begin()?;
        }
        let opts = MaskOptions {
            scope: self.cfg.scope,
            owner: self.owner.clone(),
            ..MaskOptions::default()
        };
        let engine = self.engine();
        let result = f(&mut Cx {
            usage: &mut usage,
            engine: &engine,
            principal: &self.principal,
            vault: &mut *vault,
            session: &mut *sess,
            opts,
        })
        .and_then(|v| {
            if sess.dirty {
                vault.save_session(session, &sess.known)?;
                sess.dirty = false;
            }
            Ok(v)
        });
        match result {
            Ok(v) => {
                if tx {
                    vault.commit()?;
                }
                Ok((v, usage))
            }
            Err(e) => {
                if tx {
                    let _ = vault.rollback();
                    sess.cache.clear();
                }
                Err(e)
            }
        }
    }
}

/// Доступ к движку, vault и сессии на время одной операции.
pub struct Cx<'a> {
    usage: &'a mut Usage,
    engine: &'a Engine,
    principal: &'a Principal,
    vault: &'a mut SqliteVault,
    session: &'a mut Session,
    pub opts: MaskOptions,
}

impl Cx<'_> {
    /// Маскирует блок текста с кэшем сессии.
    pub fn mask(&mut self, text: &str) -> anyhow::Result<String> {
        if text.trim().is_empty() {
            return Ok(text.to_string());
        }
        let key = *blake3::hash(text.as_bytes()).as_bytes();
        if let Some(m) = self.session.cache.get(&key) {
            self.usage.add(&m.usage);
            return Ok(m.text.clone());
        }
        let m = self.mask_full(&Input::text(text))?;
        if self.session.cache.len() >= CACHE_LIMIT {
            self.session.cache.clear();
        }
        self.session.cache.insert(
            key,
            CachedMask {
                text: m.text.clone(),
                usage: Usage::masked(&m),
            },
        );
        Ok(m.text)
    }

    /// Маскирование с полным отчётом, без кэша. Значения из памяти сессии маскируются
    /// теми же токенами.
    pub fn mask_full(&mut self, input: &Input<'_>) -> anyhow::Result<Masked> {
        self.opts.known = std::mem::take(&mut self.session.known);
        let r = self.engine.mask(input, &self.opts, &mut *self.vault);
        self.session.known = std::mem::take(&mut self.opts.known);
        let mut m = r?;
        self.usage.add(&Usage::masked(&m));
        self.session.learn(std::mem::take(&mut m.known));
        Ok(m)
    }

    pub fn restore(&mut self, text: &str, escape: Escape) -> anyhow::Result<String> {
        Ok(self.restore_full(text, escape)?.text)
    }

    pub fn restore_full(&mut self, text: &str, escape: Escape) -> anyhow::Result<Restored> {
        let mut r = self
            .engine
            .restore(text, self.principal, &*self.vault, escape)?;
        self.usage.add(&Usage::restored(&r));
        self.session.learn(std::mem::take(&mut r.known));
        Ok(r)
    }

    pub fn push(&mut self, r: &mut StreamRestorer, chunk: &str) -> anyhow::Result<String> {
        let out = r.push(self.engine, self.principal, &*self.vault, chunk)?;
        let stats = r.take_stats();
        self.usage.add(&Usage::restored(&stats));
        self.session.learn(stats.known);
        Ok(out)
    }

    pub fn flush(&mut self, r: &mut StreamRestorer) -> anyhow::Result<String> {
        let out = r.flush(self.engine, self.principal, &*self.vault)?;
        let stats = r.take_stats();
        self.usage.add(&Usage::restored(&stats));
        self.session.learn(stats.known);
        Ok(out)
    }

    pub(crate) fn streams(&mut self) -> &mut HashMap<String, StreamRestorer> {
        &mut self.session.streams
    }
}
