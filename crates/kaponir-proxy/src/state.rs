//! Общее состояние сервера: движок, vault и сессии. Всё изменяемое лежит под одним мьютексом,
//! поэтому порядок блокировок не важен. Движок неизменяем; при смене правил он заменяется
//! целиком, а операции, начатые со старым движком, его же и дорабатывают.

use kaponir_core::{
    Engine, Escape, Input, Known, MaskOptions, Masked, Principal, Restored, Scope, StreamRestorer,
    rules::Rule,
};
use kaponir_vault::SqliteVault;
use std::collections::hash_map::Entry;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use crate::traffic::{Traffic, Usage};
use crate::{Control, ProxyConfig, RulePattern, RuleSet};

pub(crate) enum RuleEditError {
    ReadOnly,
    Conflict(anyhow::Error),
    Internal(anyhow::Error),
    Edit(anyhow::Error),
}

fn check_rule_revision(
    ctl: &dyn Control,
    scope: Scope,
    expected: Option<&str>,
) -> Result<(), RuleEditError> {
    if let Some(expected) = expected {
        let current = ctl
            .rules()
            .map_err(RuleEditError::Internal)?
            .iter()
            .find(|set| set.scope == scope)
            .map(RuleSet::revision)
            .transpose()
            .map_err(RuleEditError::Internal)?;
        if current.as_deref() != Some(expected) {
            return Err(RuleEditError::Conflict(anyhow::anyhow!(
                "rule_list_changed"
            )));
        }
    }
    Ok(())
}

struct CachedMask {
    text: String,
    usage: Usage,
}

/// Сколько замаскированных блоков держать в кэше сессии.
const CACHE_LIMIT: usize = 8192;
pub(crate) const MEMORY_AUDIT_SETTING: &str = "audit:ignored-memory:v1";

const FORGOTTEN_MEMORY: &str = "audit:forgotten-memory:v1";

pub(crate) fn memory_id(kind: &str, value: &str) -> String {
    blake3::hash(format!("{}\0{}", kind.to_uppercase(), value.to_lowercase()).as_bytes())
        .to_hex()
        .to_string()
}

fn forgotten_memory(vault: &SqliteVault) -> anyhow::Result<HashSet<String>> {
    Ok(vault
        .private_setting(FORGOTTEN_MEMORY)?
        .map(|bytes| serde_json::from_slice(&bytes))
        .transpose()?
        .unwrap_or_default())
}

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

    fn prune(&mut self, suppressed: &HashSet<String>) {
        if suppressed.is_empty() {
            return;
        }
        let n = self.known.len();
        self.known
            .retain(|k| !suppressed.contains(&memory_id(&k.kind, &k.text)));
        if n != self.known.len() {
            self.seen = self.known.iter().cloned().collect();
            self.cache.clear();
            self.dirty = true;
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
    suppressed: HashSet<String>,
}

/// Concurrency shared by background discovery and explicit calls to an external model.
#[derive(Default)]
pub(crate) struct ApiSlots {
    count: Mutex<usize>,
    changed: tokio::sync::Notify,
}
pub(crate) struct ApiSlot<'a>(&'a ApiSlots);
impl ApiSlots {
    pub(crate) async fn acquire(&self, limit: usize) -> ApiSlot<'_> {
        loop {
            let notified = self.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            {
                let mut count = self
                    .count
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                if *count < limit.max(1) {
                    *count += 1;
                    return ApiSlot(self);
                }
            }
            notified.await;
        }
    }
    pub(crate) fn active(&self) -> usize {
        *self
            .count
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}
impl Drop for ApiSlot<'_> {
    fn drop(&mut self) {
        *self
            .0
            .count
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) -= 1;
        self.0.changed.notify_waiters();
    }
}
pub struct State {
    pub(crate) images: crate::image_protection::Service,
    pub(crate) api_slots: ApiSlots,
    pub(crate) learning: Arc<crate::learning::Learning>,
    pub(crate) learning_changes: tokio::sync::Mutex<()>,
    pub(crate) provider_refresh: tokio::sync::Mutex<()>,
    pub(crate) review_queue: tokio::sync::Mutex<()>,
    pub(crate) rule_changes: tokio::sync::Mutex<()>,
    pub(crate) worker_actions: tokio::sync::Mutex<()>,
    pub(crate) provider_affinity: Mutex<HashMap<[u8; 32], crate::providers::Binding>>,
    pub(crate) traffic: Arc<Traffic>,
    pub(crate) started: Instant,
    pub(crate) requests: kaponir_core::metrics::OperationMetrics,
    pub(crate) stream_errors: std::sync::atomic::AtomicU64,
    engine: RwLock<Arc<Engine>>,
    occurrences: crate::occurrences::OccurrencePolicy,
    pub principal: Principal,
    pub cfg: ProxyConfig,
    pub(crate) http: reqwest::Client,
    /// Управление правилами из Web UI; без него UI работает только на чтение.
    pub(crate) control: Option<Box<dyn Control>>,
    pub(crate) worker_manager: Option<Arc<dyn crate::WorkerManager>>,
    owner: String,
    inner: Arc<Mutex<Inner>>,
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
        let occurrences = crate::occurrences::OccurrencePolicy::new(
            cfg.windows_audit_records,
            cfg.public_occurrences.as_deref(),
        );
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(30))
            .build()?;
        let traffic = Arc::new(Traffic::default());
        let learning = crate::learning::Learning::load(&vault)?;
        let images = crate::image_protection::Service::load(&vault, &cfg.ocr)?;
        let inner = Arc::new(Mutex::new(Inner {
            vault,
            sessions: HashMap::new(),
            last_evict: Instant::now(),
            suppressed: HashSet::new(),
        }));
        let inner_weak = Arc::downgrade(&inner);
        let learning_sink = learning.clone();
        traffic.set_persister(Arc::new(move |event, archive, messages| {
            let Some(inner_arc) = inner_weak.upgrade() else {
                return;
            };
            let Ok(bytes) = serde_json::to_vec(event) else {
                return;
            };
            if let Ok(guard) = inner_arc.lock() {
                let result = (|| -> anyhow::Result<()> {
                    let aggregate = serde_json::to_vec(archive)?;
                    guard.vault.begin()?;
                    guard
                        .vault
                        .save_traffic_event(event.id, event.time_ms / 1000, &bytes, 200)?;
                    guard
                        .vault
                        .set_private_setting("traffic:archive:v1", &aggregate)?;
                    crate::learning::enqueue_event(&guard.vault, &learning_sink, event, messages)?;
                    guard.vault.commit()?;
                    Ok(())
                })();
                if let Err(e) = result {
                    let _ = guard.vault.rollback();
                    eprintln!("kaponir: traffic persistence: {e}");
                }
            }
        }));
        let state = Self {
            images,
            learning,
            learning_changes: tokio::sync::Mutex::new(()),
            provider_refresh: tokio::sync::Mutex::new(()),
            review_queue: tokio::sync::Mutex::new(()),
            api_slots: ApiSlots::default(),
            rule_changes: tokio::sync::Mutex::new(()),
            worker_actions: tokio::sync::Mutex::new(()),
            provider_affinity: Mutex::new(HashMap::new()),
            traffic,
            started: Instant::now(),
            requests: Default::default(),
            stream_errors: Default::default(),
            engine: RwLock::new(Arc::new(engine)),
            principal: Principal::local(owner.clone()),
            cfg,
            occurrences,
            http,
            control: None,
            worker_manager: None,
            owner,
            inner,
        };
        state.refresh_suppressed();
        if let Ok(raw) = state.with_vault(|_, v| Ok(v.load_traffic_events(200)?)) {
            let mut events: Vec<crate::traffic::Event> = raw
                .into_iter()
                .filter_map(|bytes| serde_json::from_slice(&bytes).ok())
                .collect();
            let mut legacy = Vec::new();
            for event in &mut events {
                let scrubbed = event.mark_legacy_images();
                if crate::opencode::repair_event(&state, event).is_err() {
                    eprintln!("kaponir: could not repair title for request {}", event.id);
                }
                if scrubbed {
                    legacy.push((event.id, event.time_ms / 1000, serde_json::to_vec(event)?));
                }
            }
            if !legacy.is_empty() {
                state.with_vault(|_, v| {
                    crate::learning::transaction(v, || {
                        for (id, created, bytes) in legacy {
                            v.save_traffic_event(id, created, &bytes, 0)?;
                        }
                        Ok(())
                    })
                })?;
            }
            state.traffic.restore_events(events);
        }
        if let Some(bytes) =
            state.with_vault(|_, v| Ok(v.private_setting("traffic:archive:v1")?))?
            && let Ok(archive) = serde_json::from_slice(&bytes)
        {
            state.traffic.restore_archive(archive);
        }
        Ok(state)
    }

    pub fn with_control(mut self, control: Box<dyn Control>) -> Self {
        self.control = Some(control);
        if let Err(e) = self.learning.migrate(&self) {
            eprintln!("kaponir: learning settings migration: {e}");
        }
        self.refresh_suppressed();
        self
    }

    pub fn with_worker_manager(mut self, manager: Arc<dyn crate::WorkerManager>) -> Self {
        self.worker_manager = Some(manager);
        self
    }

    pub(crate) fn refresh_suppressed(&self) {
        let sets = self
            .control
            .as_ref()
            .and_then(|c| c.rules().ok())
            .unwrap_or_default();
        let mut suppressed: HashSet<String> = sets
            .into_iter()
            .flat_map(|s| s.file.rules)
            .filter(|r| r.disabled)
            .filter_map(|r| r.value.map(|v| memory_id(&r.kind, &v)))
            .collect();
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Ok(Some(bytes)) = inner.vault.private_setting(MEMORY_AUDIT_SETTING)
            && let Ok(values) = serde_json::from_slice::<Vec<(String, String)>>(&bytes)
        {
            suppressed.extend(values.into_iter().map(|(k, v)| memory_id(&k, &v)));
        }
        if let Ok(forgotten) = forgotten_memory(&inner.vault) {
            suppressed.extend(forgotten);
        }
        inner.suppressed = suppressed;
        for session in inner.sessions.values_mut() {
            session.cache.clear();
        }
    }

    pub(crate) fn forget_session_memory(&self, ids: &HashSet<String>) -> anyhow::Result<()> {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let v = &inner.vault;
        crate::learning::transaction(v, || {
            v.forget_session_values(|k| ids.contains(&memory_id(&k.kind, &k.text)))?;
            let mut ignored: Vec<(String, String)> = v
                .private_setting(MEMORY_AUDIT_SETTING)?
                .map(|bytes| serde_json::from_slice(&bytes))
                .transpose()?
                .unwrap_or_default();
            ignored.retain(|(kind, value)| !ids.contains(&memory_id(kind, value)));
            v.set_private_setting(MEMORY_AUDIT_SETTING, &serde_json::to_vec(&ignored)?)?;
            let mut forgotten = forgotten_memory(v)?;
            forgotten.extend(ids.iter().cloned());
            v.set_private_setting(FORGOTTEN_MEMORY, &serde_json::to_vec(&forgotten)?)?;
            Ok(())
        })?;
        inner.suppressed.extend(ids.iter().cloned());
        for session in inner.sessions.values_mut() {
            session.prune(ids);
            session.cache.clear();
        }
        Ok(())
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

    pub(crate) fn reload_rules(&self, ctl: &dyn Control) -> anyhow::Result<()> {
        self.replace_engine(ctl.build_engine()?);
        Ok(())
    }

    async fn edit_rules<T>(
        &self,
        change: impl FnOnce(&dyn Control) -> Result<T, RuleEditError>,
    ) -> Result<T, RuleEditError> {
        let _rules = self.rule_changes.lock().await;
        let ctl = self.control.as_deref().ok_or(RuleEditError::ReadOnly)?;
        let result = change(ctl)?;
        self.reload_rules(ctl).map_err(RuleEditError::Internal)?;
        Ok(result)
    }

    pub(crate) async fn add_rule(&self, scope: Scope, rule: Rule) -> Result<(), RuleEditError> {
        self.edit_rules(|ctl| ctl.add_rule(scope, rule).map_err(RuleEditError::Edit))
            .await
    }

    pub(crate) async fn remove_rule(
        &self,
        scope: Scope,
        index: usize,
        revision: Option<&str>,
    ) -> Result<Rule, RuleEditError> {
        self.edit_rules(|ctl| {
            // Deletion shifts indices. Check the caller's snapshot before touching the file.
            check_rule_revision(ctl, scope, revision)?;
            ctl.remove_rule(scope, index).map_err(RuleEditError::Edit)
        })
        .await
    }

    pub(crate) async fn set_corp_domains(
        &self,
        scope: Scope,
        domains: &[String],
        revision: Option<&str>,
    ) -> Result<(), RuleEditError> {
        self.edit_rules(|ctl| {
            check_rule_revision(ctl, scope, revision)?;
            ctl.set_corp_domains(scope, domains)
                .map_err(RuleEditError::Edit)
        })
        .await
    }

    pub(crate) async fn set_rules_disabled(
        &self,
        scope: Scope,
        indices: &[usize],
        revision: &str,
        disabled: bool,
    ) -> Result<(), RuleEditError> {
        self.edit_rules(|ctl| {
            ctl.set_rules_disabled(scope, indices, revision, disabled)
                .map_err(RuleEditError::Conflict)
        })
        .await
    }

    pub(crate) async fn update_rule_pattern(
        &self,
        scope: Scope,
        index: usize,
        revision: &str,
        pattern: RulePattern,
    ) -> Result<(), RuleEditError> {
        let _rules = self.rule_changes.lock().await;
        let ctl = self.control.as_deref().ok_or(RuleEditError::ReadOnly)?;
        check_rule_revision(ctl, scope, Some(revision))?;
        let engine = ctl
            .update_rule_pattern(scope, index, revision, pattern)
            .map_err(|err| {
                if err.to_string() == "rule_list_changed" {
                    RuleEditError::Conflict(err)
                } else {
                    RuleEditError::Edit(err)
                }
            })?;
        self.replace_engine(engine);
        Ok(())
    }

    /// Новый движок (правила изменились). Кэши замаскированных блоков сбрасываются: история
    /// разговоров должна маскироваться по новым правилам, даже ценой одного промаха prompt cache.
    pub fn replace_engine(&self, engine: Engine) {
        self.refresh_suppressed();
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
        self.with_capture(session, tx, None, f)
    }

    pub(crate) fn with_reply_usage<T>(
        &self,
        session: &str,
        span: &crate::traffic::Span,
        f: impl FnOnce(&mut Cx<'_>) -> anyhow::Result<T>,
    ) -> anyhow::Result<(T, Usage)> {
        let mut input = span
            .learning_input
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        self.with_capture(session, false, input.as_mut().map(|i| &mut i.answer), f)
    }

    fn with_capture<T>(
        &self,
        session: &str,
        tx: bool,
        answer: Option<&mut crate::learning::question::Answer>,
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
            suppressed,
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
        sess.prune(suppressed);
        sess.last_used = Instant::now();
        let known_before = sess.known.len();
        let dirty_before = sess.dirty;
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
            commands: Default::default(),
            disclosed: false,
            screening: false,
            answer,
            usage: &mut usage,
            engine: &engine,
            occurrences: &self.occurrences,
            principal: &self.principal,
            vault: &mut *vault,
            session: &mut *sess,
            opts,
        })
        .and_then(|v| {
            sess.prune(suppressed);
            if sess.dirty {
                vault.save_session(session, &sess.known)?;
                sess.dirty = false;
            }
            if tx {
                vault.commit()?;
            }
            Ok(v)
        });
        match result {
            Ok(v) => Ok((v, usage)),
            Err(e) => {
                if tx {
                    let _ = vault.rollback();
                    sess.cache.clear();
                    sess.known.truncate(known_before);
                    sess.seen = sess.known.iter().cloned().collect();
                    sess.dirty = dirty_before;
                }
                Err(e)
            }
        }
    }
}

/// Доступ к движку, vault и сессии на время одной операции.
pub struct Cx<'a> {
    pub(crate) commands: crate::chat_commands::Active,
    pub(crate) disclosed: bool,
    pub(crate) screening: bool,
    answer: Option<&'a mut crate::learning::question::Answer>,
    usage: &'a mut Usage,
    engine: &'a Engine,
    occurrences: &'a crate::occurrences::OccurrencePolicy,
    principal: &'a Principal,
    vault: &'a mut SqliteVault,
    session: &'a mut Session,
    pub opts: MaskOptions,
}

impl Cx<'_> {
    /// Check the whole outgoing request before releasing any named value. A
    /// credential may first occur in prose and only later in a tool's password field.
    fn screen(&mut self, text: &str, field: Option<&str>) -> bool {
        if !self.screening {
            return false;
        }
        if self.commands.open_in(text) {
            let mut protected: Vec<_> = self
                .engine
                .analyze(&Input::text(text))
                .iter()
                .filter(|c| c.requires_chat_mask())
                .map(|c| c.start..c.end)
                .collect();
            if let Some(c) = field.and_then(|name| {
                kaponir_recognizers::kv::credential_field(self.engine.lexicon(), name, text)
            }) {
                protected.push(c.start..c.end);
            }
            if let Some(edits) = self.occurrences.edits(text) {
                protected.extend(edits.additional_masks.into_iter().map(|c| c.start..c.end));
            }
            for known in &self.session.known {
                if kaponir_core::kinds::category(&known.kind) == kaponir_core::Category::Credential
                    && !known.text.is_empty()
                {
                    protected.extend(text.match_indices(&known.text).map(|(i, s)| i..i + s.len()));
                }
            }
            self.commands.protect_overlaps(text, &protected);
        }
        true
    }

    pub(crate) fn check_signed_text(&mut self, text: &str) -> anyhow::Result<()> {
        if self.screen(text, None) {
            return Ok(());
        }
        let mentions = self.commands.signed_mentions(text);
        if mentions.is_empty() {
            return Ok(());
        }
        // The signature covers the original bytes. Check only whether a named
        // part would need rewriting, without learning or allocating real tokens.
        let mut edits = self.occurrences.edits(text);
        self.commands.edits(text, &mut edits);
        let opts = MaskOptions {
            irreversible: true,
            journal: false,
            known: self.session.known.clone(),
            ..self.opts.clone()
        };
        let checked = self.engine.mask_without_s1_positioned(
            &Input::text(text),
            &opts,
            self.vault,
            &edits.unwrap_or_default(),
        )?;
        if checked
            .replacements
            .iter()
            .any(|m| mentions.iter().any(|r| r.start < m.end && m.start < r.end))
        {
            return Err(crate::chat_commands::Invalid("История содержит подписанный блок рассуждений, требующий маскирования. Отправьте вопрос без этого блока").into());
        }
        self.disclosed |= self.commands.open_in(text);
        Ok(())
    }

    pub(crate) fn configure_commands(
        &mut self,
        request: &crate::chat_commands::Request,
        chat: &str,
        recipient: &str,
        inferred: bool,
        auxiliary: bool,
    ) -> anyhow::Result<()> {
        self.commands = request.resolve(self.vault, chat, recipient, inferred, auxiliary)?;
        Ok(())
    }

    pub(crate) fn reply_text(&mut self, slot: [u64; 2], wire: &str, restored: &str) {
        if let Some(answer) = &mut self.answer {
            answer.append(slot, wire);
        }
        self.reply().text(slot, wire, restored);
    }

    pub(crate) fn reply(&mut self) -> &mut crate::conversation::Reply {
        self.usage.reply.get_or_insert_with(Default::default)
    }

    /// Маскирует блок текста с кэшем сессии.
    pub fn mask(&mut self, text: &str) -> anyhow::Result<String> {
        if self.screen(text, None) || text.trim().is_empty() {
            return Ok(text.to_string());
        }
        // Permissions can expire within a chat. Never reuse a mask decision
        // for a matched document from an earlier request.
        if self.commands.may_match(text) || self.occurrences.may_have_grants(text) {
            return Ok(self.mask_full(&Input::text(text))?.text);
        }
        let key = *blake3::hash(text.as_bytes()).as_bytes();
        if let Some(m) = self.session.cache.get(&key) {
            self.usage.add_cached(&m.usage);
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
        let edits = self.occurrences.edits(input.text);
        self.mask_with_edits(input, edits)
    }

    pub(crate) fn mask_tool_field(&mut self, key: &str, text: &str) -> anyhow::Result<String> {
        if self.screen(text, Some(key)) {
            return Ok(text.to_owned());
        }
        let Some(credential) =
            kaponir_recognizers::kv::credential_field(self.engine.lexicon(), key, text)
        else {
            return self.mask(text);
        };
        let mut edits = self.occurrences.edits(text).unwrap_or_default();
        edits.additional_masks.push(credential);
        Ok(self.mask_with_edits(&Input::text(text), Some(edits))?.text)
    }

    fn mask_with_edits(
        &mut self,
        input: &Input<'_>,
        mut edits: Option<kaponir_core::engine::PositionEdits>,
    ) -> anyhow::Result<Masked> {
        self.commands.edits(input.text, &mut edits);
        let may_disclose = edits.as_ref().is_some_and(|e| !e.chat_open.is_empty());
        self.opts.known = std::mem::take(&mut self.session.known);
        let r = match edits {
            Some(edits) => {
                self.engine
                    .mask_without_s1_positioned(input, &self.opts, &mut *self.vault, &edits)
            }
            None => self
                .engine
                .mask_without_s1(input, &self.opts, &mut *self.vault),
        };
        self.session.known = std::mem::take(&mut self.opts.known);
        let mut m = r?;
        if may_disclose && !self.disclosed {
            self.disclosed = self.commands.open_in(&m.text);
        }
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
        let mut u = Usage::restored(&r);
        u.raw_response_buf = crate::traffic::cap_str(text, 32_768);
        u.restored_response_buf = crate::traffic::cap_str(&r.text, 32_768);
        self.usage.add(&u);
        self.session.learn(std::mem::take(&mut r.known));
        Ok(r)
    }

    pub fn push(&mut self, r: &mut StreamRestorer, chunk: &str) -> anyhow::Result<String> {
        let out = r.push(self.engine, self.principal, &*self.vault, chunk)?;
        let stats = r.take_stats();
        let mut u = Usage::restored(&stats);
        u.raw_response_buf = crate::traffic::cap_str(chunk, 8192);
        u.restored_response_buf = crate::traffic::cap_str(&out, 8192);
        self.usage.add(&u);
        self.session.learn(stats.known);
        Ok(out)
    }

    pub fn flush(&mut self, r: &mut StreamRestorer) -> anyhow::Result<String> {
        let out = r.flush(self.engine, self.principal, &*self.vault)?;
        let stats = r.take_stats();
        let mut u = Usage::restored(&stats);
        u.restored_response_buf = crate::traffic::cap_str(&out, 8192);
        self.usage.add(&u);
        self.session.learn(stats.known);
        Ok(out)
    }

    pub(crate) fn finish_stream(&mut self) {
        self.usage.record_finish(true);
    }

    /// Извлекает счётчики токенов LLM (`prompt_tokens`/`input_tokens`, `completion_tokens`/`output_tokens`)
    /// из JSON-ответа или SSE-чанка OpenAI / Anthropic.
    pub fn record_token_usage(&mut self, v: &serde_json::Value) {
        if let Some(finished) = crate::opencode::completion_status(v) {
            self.usage.record_finish(finished);
        }
        let usage_obj = v.get("usage").or_else(|| v.pointer("/message/usage"));
        let Some(u) = usage_obj else {
            return;
        };
        let prompt = u
            .get("prompt_tokens")
            .or_else(|| u.get("input_tokens"))
            .and_then(|x| x.as_u64())
            .unwrap_or(0);
        let completion = u
            .get("completion_tokens")
            .or_else(|| u.get("output_tokens"))
            .and_then(|x| x.as_u64())
            .unwrap_or(0);
        let total = u
            .get("total_tokens")
            .and_then(|x| x.as_u64())
            .unwrap_or(prompt + completion);
        self.usage.prompt_tokens = self.usage.prompt_tokens.max(prompt);
        self.usage.completion_tokens = self.usage.completion_tokens.max(completion);
        self.usage.total_tokens = self
            .usage
            .total_tokens
            .max(total)
            .max(self.usage.prompt_tokens + self.usage.completion_tokens);
    }

    pub(crate) fn streams(&mut self) -> &mut HashMap<String, StreamRestorer> {
        &mut self.session.streams
    }
}
