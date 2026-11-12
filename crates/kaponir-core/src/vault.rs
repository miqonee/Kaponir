//! Контракт хранилища соответствий «оригинал ↔ токен» и простая реализация в памяти.

use std::collections::{HashMap, HashSet};

use crate::journal::JournalEntry;
use crate::score::Strategy;
use crate::token::{MIN_HASH_LEN, TokenId};
use crate::types::Scope;

#[derive(Debug, thiserror::Error)]
pub enum VaultError {
    #[error("storage: {0}")]
    Storage(String),
    #[error("crypto: {0}")]
    Crypto(String),
    #[error("{0}")]
    Other(String),
}

pub struct AllocRequest<'a> {
    pub scope: Scope,
    pub owner: &'a str,
    pub kind: &'a str,
    pub canonical: &'a str,
    pub original: &'a str,
    pub strategy: Strategy,
    /// Полный HMAC-SHA256 в hex — vault берёт кратчайший уникальный префикс (≥ 6).
    pub hash_hex: &'a str,
    /// Предложенный суррогат; при коллизии vault выдаёт нумерованный токен.
    pub surrogate: Option<&'a str>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub original: String,
    /// Канонический ключ записи: повторное маскирование того же значения даёт тот же токен.
    pub canonical: String,
    pub scope: Scope,
    pub owner: String,
}

pub trait Vault {
    /// Существующий токен для `(scope, kind, canonical, strategy)` или новый.
    fn allocate(&mut self, req: &AllocRequest<'_>) -> Result<TokenId, VaultError>;
    /// Строка вида токена во входе, которой нет в vault: её id никогда не выдаётся.
    fn reserve(&mut self, kind: &str, id: &TokenId) -> Result<(), VaultError>;
    fn is_reserved(&self, kind: &str, id: &TokenId) -> Result<bool, VaultError>;
    fn resolve(&self, kind: &str, id: &TokenId) -> Result<Option<Resolved>, VaultError>;
    /// Журнал спорных решений; повтор того же `(kind, value)` увеличивает счётчик.
    fn record(&mut self, entry: &JournalEntry) -> Result<(), VaultError>;
    /// Optional stable reference for linking a request to its review record.
    fn journal_id(&self, _kind: &str, _value: &str) -> Result<Option<i64>, VaultError> {
        Ok(None)
    }
}

/// Кто восстанавливает. В локальном режиме пользователь — владелец всего, кроме чужих Personal.
#[derive(Clone, Debug)]
pub struct Principal {
    pub user: String,
    pub team_access: bool,
    pub global_access: bool,
}

impl Principal {
    pub fn local(user: impl Into<String>) -> Self {
        Self {
            user: user.into(),
            team_access: true,
            global_access: true,
        }
    }

    pub fn can_restore(&self, r: &Resolved) -> bool {
        match r.scope {
            Scope::Personal => r.owner == self.user,
            Scope::Team => self.team_access,
            Scope::Global => self.global_access,
        }
    }
}

type EntryKey = (Scope, String, String, Strategy);

#[derive(Default)]
pub struct MemoryVault {
    by_key: HashMap<EntryKey, (String, TokenId)>,
    by_id: HashMap<(String, TokenId), Resolved>,
    counters: HashMap<String, u64>,
    reserved: HashSet<(String, TokenId)>,
    pub journal: Vec<JournalEntry>,
}

impl MemoryVault {
    pub fn new() -> Self {
        Self::default()
    }
}

impl Vault for MemoryVault {
    fn allocate(&mut self, req: &AllocRequest<'_>) -> Result<TokenId, VaultError> {
        let key = (
            req.scope,
            req.kind.to_string(),
            req.canonical.to_string(),
            req.strategy,
        );
        if let Some((_, id)) = self.by_key.get(&key) {
            return Ok(id.clone());
        }
        let (by_id, reserved, counters) = (&self.by_id, &self.reserved, &mut self.counters);
        let taken = |id: &TokenId| {
            let k = (req.kind.to_string(), id.clone());
            by_id.contains_key(&k) || reserved.contains(&k)
        };
        let id = pick_id(req, taken, || {
            let n = counters.entry(req.kind.to_string()).or_insert(0);
            *n += 1;
            *n
        });
        self.by_key
            .insert(key, (req.original.to_string(), id.clone()));
        self.by_id.insert(
            (req.kind.to_string(), id.clone()),
            Resolved {
                original: req.original.to_string(),
                canonical: req.canonical.to_string(),
                scope: req.scope,
                owner: req.owner.to_string(),
            },
        );
        Ok(id)
    }

    fn reserve(&mut self, kind: &str, id: &TokenId) -> Result<(), VaultError> {
        self.reserved.insert((kind.to_string(), id.clone()));
        Ok(())
    }

    fn is_reserved(&self, kind: &str, id: &TokenId) -> Result<bool, VaultError> {
        Ok(self.reserved.contains(&(kind.to_string(), id.clone())))
    }

    fn resolve(&self, kind: &str, id: &TokenId) -> Result<Option<Resolved>, VaultError> {
        Ok(self.by_id.get(&(kind.to_string(), id.clone())).cloned())
    }

    fn record(&mut self, entry: &JournalEntry) -> Result<(), VaultError> {
        self.journal.push(entry.clone());
        Ok(())
    }
}

/// Общая логика выбора id для реализаций vault.
/// `taken` — занят ли id (записью или резервом), `next_num` — следующий номер счётчика.
pub fn pick_id(
    req: &AllocRequest<'_>,
    taken: impl Fn(&TokenId) -> bool,
    mut next_num: impl FnMut() -> u64,
) -> TokenId {
    match req.strategy {
        Strategy::Surrogate => {
            if let Some(s) = req.surrogate {
                let id = TokenId::Surrogate(s.to_string());
                if !taken(&id) {
                    return id;
                }
            }
        }
        Strategy::Hmac => {
            for len in MIN_HASH_LEN..=req.hash_hex.len().min(crate::token::MAX_HASH_LEN) {
                let id = TokenId::Hash(req.hash_hex[..len].to_string());
                if !taken(&id) {
                    return id;
                }
            }
        }
        Strategy::Variable => {}
    }
    loop {
        let id = TokenId::Num(next_num());
        if !taken(&id) {
            return id;
        }
    }
}
