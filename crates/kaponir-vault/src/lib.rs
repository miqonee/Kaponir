//! Локальный vault на SQLite. Оригиналы и журнал шифруются ChaCha20-Poly1305, поиск идёт
//! по слепому индексу (keyed BLAKE3), поэтому в открытом виде в файле нет ни одного значения.

use chacha20poly1305::aead::{Aead, Payload};
use chacha20poly1305::{ChaCha20Poly1305, KeyInit, Nonce};
use kaponir_core::journal::JournalEntry;
use kaponir_core::vault::{AllocRequest, Resolved, Vault, VaultError, pick_id};
use kaponir_core::{Known, Scope, TokenId};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

const SCHEMA: &str = r#"
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL,
    ident TEXT NOT NULL,
    blind BLOB NOT NULL UNIQUE,
    nonce BLOB NOT NULL,
    ct BLOB NOT NULL,
    created INTEGER NOT NULL,
    UNIQUE (kind, ident)
);
CREATE TABLE IF NOT EXISTS counters (kind TEXT PRIMARY KEY, next INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reserved (kind TEXT NOT NULL, ident TEXT NOT NULL, PRIMARY KEY (kind, ident));
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    blind BLOB NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    recognizer TEXT NOT NULL,
    shape TEXT NOT NULL,
    probability REAL NOT NULL,
    applied TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    nonce BLOB NOT NULL,
    ct BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    blind BLOB PRIMARY KEY,
    updated INTEGER NOT NULL,
    nonce BLOB NOT NULL,
    ct BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_events (
    id INTEGER PRIMARY KEY,
    created INTEGER NOT NULL,
    nonce BLOB NOT NULL,
    ct BLOB NOT NULL
);
"#;

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

fn db(e: rusqlite::Error) -> VaultError {
    VaultError::Storage(e.to_string())
}

fn ident(id: &TokenId) -> String {
    match id {
        TokenId::Num(n) => format!("n:{n}"),
        TokenId::Hash(h) => format!("h:{h}"),
        TokenId::Surrogate(s) => format!("s:{s}"),
    }
}

#[derive(Serialize, Deserialize)]
struct EntryPayload {
    original: String,
    canonical: String,
}

#[derive(Serialize, Deserialize)]
struct JournalPayload {
    value: String,
    context: String,
    evidence: Vec<kaponir_core::Evidence>,
}

#[derive(Clone, Debug, Serialize)]
pub struct JournalRow {
    pub id: i64,
    pub kind: String,
    pub category: String,
    pub recognizer: String,
    pub shape: String,
    pub probability: f64,
    /// Исходный вердикт детектора (`uncertain` — спорно).
    pub verdict: String,
    /// Действие, применённое по глобальной политике (`mask` или `keep`).
    pub applied: String,
    /// Рекомендуемый контур сохранения правила (`global`, `team`, `personal`).
    pub recommended_scope: String,
    pub count: i64,
    pub first_seen: i64,
    pub last_seen: i64,
    pub status: String,
    pub value: String,
    pub context: String,
    pub evidence: Vec<kaponir_core::Evidence>,
}

/// Решения по журналу: каждое становится правилом (CLI `kaponir review` и Web UI).
impl JournalRow {
    /// «Всегда маскировать» с типом `kind` (по умолчанию — найденный детектором).
    pub fn mask_rule(&self, kind: Option<&str>) -> kaponir_core::rules::Rule {
        kaponir_core::rules::Rule {
            id: format!("review-{}", self.id),
            kind: kind.unwrap_or(&self.kind).to_ascii_uppercase(),
            value: Some(self.value.clone()),
            category: serde_json::from_value(serde_json::Value::String(self.category.clone())).ok(),
            note: format!("kaponir review: {} ×{}", self.recognizer, self.count),
            ..kaponir_core::rules::Rule::default()
        }
    }

    /// «Никогда не маскировать».
    pub fn keep_rule(&self) -> kaponir_core::rules::Rule {
        kaponir_core::rules::Rule {
            id: format!("review-{}", self.id),
            kind: "KEEP".into(),
            action: kaponir_core::rules::RuleAction::Keep,
            value: Some(self.value.clone()),
            note: format!("kaponir review: {} ×{}", self.recognizer, self.count),
            ..kaponir_core::rules::Rule::default()
        }
    }

    /// Покрывает ли шаблон (`is_match` — полное совпадение) значение или слово контекста,
    /// в которое оно входит.
    pub fn covered_by(&self, is_match: impl Fn(&str) -> bool) -> bool {
        is_match(&self.value)
            || self
                .context
                .split(|c: char| c.is_whitespace() || "/\"'=:,".contains(c))
                .any(|w| is_match(w) && w.contains(self.value.as_str()))
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Stats {
    pub entries: i64,
    pub reserved: i64,
    pub journal_open: i64,
    pub journal_total: i64,
}

pub struct SqliteVault {
    conn: Connection,
    cipher: ChaCha20Poly1305,
    blind_key: [u8; 32],
}

impl SqliteVault {
    pub fn open(path: &Path, master: &[u8; 32]) -> Result<Self, VaultError> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| VaultError::Storage(e.to_string()))?;
        }
        Self::init(Connection::open(path).map_err(db)?, master)
    }

    pub fn open_in_memory(master: &[u8; 32]) -> Result<Self, VaultError> {
        Self::init(Connection::open_in_memory().map_err(db)?, master)
    }

    fn init(conn: Connection, master: &[u8; 32]) -> Result<Self, VaultError> {
        conn.execute_batch(SCHEMA).map_err(db)?;
        let enc = blake3::derive_key("anon vault enc v1", master);
        let v = Self {
            conn,
            cipher: ChaCha20Poly1305::new_from_slice(&enc)
                .map_err(|e| VaultError::Crypto(e.to_string()))?,
            blind_key: blake3::derive_key("anon vault blind v1", master),
        };
        v.check_key()?;
        Ok(v)
    }

    /// Защита от открытия vault чужим ключом: иначе записи молча раздвоятся.
    fn check_key(&self) -> Result<(), VaultError> {
        let check = self.blind(&["key-check"]);
        let stored: Option<Vec<u8>> = self
            .conn
            .query_row("SELECT value FROM meta WHERE key = 'key_check'", [], |r| {
                r.get(0)
            })
            .optional()
            .map_err(db)?;
        match stored {
            Some(s) if s != check => Err(VaultError::Crypto("wrong vault key".into())),
            Some(_) => Ok(()),
            None => {
                self.conn
                    .execute(
                        "INSERT INTO meta (key, value) VALUES ('key_check', ?1)",
                        params![check],
                    )
                    .map_err(db)?;
                Ok(())
            }
        }
    }

    fn blind(&self, parts: &[&str]) -> Vec<u8> {
        let mut h = blake3::Hasher::new_keyed(&self.blind_key);
        for p in parts {
            h.update(&(p.len() as u64).to_le_bytes());
            h.update(p.as_bytes());
        }
        h.finalize().as_bytes().to_vec()
    }

    fn seal(&self, plaintext: &[u8], aad: &[u8]) -> Result<(Vec<u8>, Vec<u8>), VaultError> {
        let mut nonce = [0u8; 12];
        getrandom::fill(&mut nonce).map_err(|e| VaultError::Crypto(e.to_string()))?;
        let ct = self
            .cipher
            .encrypt(
                &Nonce::from(nonce),
                Payload {
                    msg: plaintext,
                    aad,
                },
            )
            .map_err(|e| VaultError::Crypto(e.to_string()))?;
        Ok((nonce.to_vec(), ct))
    }

    fn unseal(&self, nonce: &[u8], ct: &[u8], aad: &[u8]) -> Result<Vec<u8>, VaultError> {
        let nonce: [u8; 12] = nonce
            .try_into()
            .map_err(|_| VaultError::Crypto("bad nonce".into()))?;
        self.cipher
            .decrypt(&Nonce::from(nonce), Payload { msg: ct, aad })
            .map_err(|_| {
                VaultError::Crypto("decryption failed (wrong key or corrupted vault)".into())
            })
    }

    /// Encrypted application settings, separated from token mappings by AAD.
    pub fn set_private_setting(&self, name: &str, value: &[u8]) -> Result<(), VaultError> {
        let key = format!("private:{name}");
        let (nonce, ct) = self.seal(value, key.as_bytes())?;
        let blob = [nonce, ct].concat();
        self.conn
            .execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?1, ?2)",
                params![key, blob],
            )
            .map_err(db)?;
        Ok(())
    }

    pub fn delete_private_setting(&self, name: &str) -> Result<(), VaultError> {
        self.conn
            .execute(
                "DELETE FROM meta WHERE key = ?1",
                [format!("private:{name}")],
            )
            .map_err(db)?;
        Ok(())
    }

    pub fn private_setting(&self, name: &str) -> Result<Option<Vec<u8>>, VaultError> {
        let key = format!("private:{name}");
        let blob: Option<Vec<u8>> = self
            .conn
            .query_row("SELECT value FROM meta WHERE key = ?1", [&key], |r| {
                r.get(0)
            })
            .optional()
            .map_err(db)?;
        blob.map(|b| {
            if b.len() < 12 {
                return Err(VaultError::Crypto("invalid private setting".into()));
            }
            self.unseal(&b[..12], &b[12..], key.as_bytes())
        })
        .transpose()
    }

    pub fn begin(&self) -> Result<(), VaultError> {
        self.conn.execute_batch("BEGIN IMMEDIATE").map_err(db)
    }

    pub fn commit(&self) -> Result<(), VaultError> {
        self.conn.execute_batch("COMMIT").map_err(db)
    }

    pub fn rollback(&self) -> Result<(), VaultError> {
        self.conn.execute_batch("ROLLBACK").map_err(db)
    }

    fn taken(&self, kind: &str, id: &TokenId) -> Result<bool, VaultError> {
        let i = ident(id);
        let n: i64 = self
            .conn
            .query_row(
                "SELECT (SELECT COUNT(*) FROM entries WHERE kind = ?1 AND ident = ?2)
                      + (SELECT COUNT(*) FROM reserved WHERE kind = ?1 AND ident = ?2)",
                params![kind, i],
                |r| r.get(0),
            )
            .map_err(db)?;
        Ok(n > 0)
    }

    pub fn stats(&self) -> Result<Stats, VaultError> {
        let q = |sql: &str| -> Result<i64, VaultError> {
            self.conn.query_row(sql, [], |r| r.get(0)).map_err(db)
        };
        Ok(Stats {
            entries: q("SELECT COUNT(*) FROM entries")?,
            reserved: q("SELECT COUNT(*) FROM reserved")?,
            journal_open: q("SELECT COUNT(*) FROM journal WHERE status = 'open'")?,
            journal_total: q("SELECT COUNT(*) FROM journal")?,
        })
    }

    /// Журнал спорных решений, самые частые сначала.
    pub fn journal(
        &self,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<JournalRow>, VaultError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, kind, category, recognizer, shape, probability, applied, count, first_seen, last_seen,
                        status, nonce, ct
                 FROM journal WHERE (?1 IS NULL OR status = ?1)
                 ORDER BY count DESC, last_seen DESC LIMIT ?2",
            )
            .map_err(db)?;
        let rows = stmt
            .query_map(params![status, limit as i64], |r| {
                let kind: String = r.get(1)?;
                let recommended_scope = kaponir_core::s1::default_scope_for_kind(&kind)
                    .as_str()
                    .to_string();
                Ok((
                    JournalRow {
                        id: r.get(0)?,
                        kind,
                        category: r.get(2)?,
                        recognizer: r.get(3)?,
                        shape: r.get(4)?,
                        probability: r.get(5)?,
                        verdict: "uncertain".into(),
                        applied: r.get(6)?,
                        recommended_scope,
                        count: r.get(7)?,
                        first_seen: r.get(8)?,
                        last_seen: r.get(9)?,
                        status: r.get(10)?,
                        value: String::new(),
                        context: String::new(),
                        evidence: Vec::new(),
                    },
                    r.get::<_, Vec<u8>>(11)?,
                    r.get::<_, Vec<u8>>(12)?,
                ))
            })
            .map_err(db)?;
        let mut out = Vec::new();
        for row in rows {
            let (mut j, nonce, ct) = row.map_err(db)?;
            let plain = self.unseal(&nonce, &ct, format!("journal:{}", j.kind).as_bytes())?;
            let p: JournalPayload =
                serde_json::from_slice(&plain).map_err(|e| VaultError::Other(e.to_string()))?;
            j.value = p.value;
            j.context = p.context;
            j.evidence = p.evidence;
            out.push(j);
        }
        Ok(out)
    }

    pub fn set_journal_status(&self, id: i64, status: &str) -> Result<bool, VaultError> {
        let n = self
            .conn
            .execute(
                "UPDATE journal SET status = ?2 WHERE id = ?1",
                params![id, status],
            )
            .map_err(db)?;
        Ok(n > 0)
    }

    /// Память сессии прокси: значения, уже замаскированные или восстановленные в разговоре.
    /// Переживает перезапуск, иначе восстановленный оригинал ушёл бы из истории открытым.
    pub fn load_session(&self, id: &str) -> Result<Vec<Known>, VaultError> {
        let blind = self.blind(&["session", id]);
        let row: Option<(Vec<u8>, Vec<u8>)> = self
            .conn
            .query_row(
                "SELECT nonce, ct FROM sessions WHERE blind = ?1",
                params![blind],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(db)?;
        let Some((nonce, ct)) = row else {
            return Ok(Vec::new());
        };
        let plain = self.unseal(&nonce, &ct, &blind)?;
        serde_json::from_slice(&plain).map_err(|e| VaultError::Other(e.to_string()))
    }

    pub fn save_session(&self, id: &str, known: &[Known]) -> Result<(), VaultError> {
        let blind = self.blind(&["session", id]);
        let payload = serde_json::to_vec(known).map_err(|e| VaultError::Other(e.to_string()))?;
        let (nonce, ct) = self.seal(&payload, &blind)?;
        self.conn
            .execute(
                "INSERT INTO sessions (blind, updated, nonce, ct) VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(blind) DO UPDATE SET updated = ?2, nonce = ?3, ct = ?4",
                params![blind, now(), nonce, ct],
            )
            .map_err(db)?;
        Ok(())
    }

    /// Удаляет память сессий, не обновлявшихся `max_age_secs`.
    pub fn purge_sessions(&self, max_age_secs: i64) -> Result<usize, VaultError> {
        self.conn
            .execute(
                "DELETE FROM sessions WHERE updated < ?1",
                params![now() - max_age_secs],
            )
            .map_err(db)
    }

    /// Удаляет записи журнала старше `max_age_secs` (TTL: журнал содержит секреты).
    pub fn purge_journal(&self, max_age_secs: i64) -> Result<usize, VaultError> {
        self.conn
            .execute(
                "DELETE FROM journal WHERE last_seen < ?1",
                params![now() - max_age_secs],
            )
            .map_err(db)
    }

    /// Сохраняет событие прокси (включая данные Request Inspector) в зашифрованную таблицу `traffic_events`
    /// с автоматической ротацией последних `max_keep` записей.
    pub fn save_traffic_event(
        &self,
        id: u64,
        created: u64,
        payload: &[u8],
        max_keep: usize,
    ) -> Result<(), VaultError> {
        let aad = format!("traffic:{id}");
        let (nonce, ct) = self.seal(payload, aad.as_bytes())?;
        self.conn
            .execute(
                "INSERT OR REPLACE INTO traffic_events (id, created, nonce, ct) VALUES (?1, ?2, ?3, ?4)",
                params![id as i64, created as i64, nonce, ct],
            )
            .map_err(db)?;
        if max_keep > 0 {
            self.conn
                .execute(
                    "DELETE FROM traffic_events WHERE id NOT IN (
                        SELECT id FROM traffic_events ORDER BY id DESC LIMIT ?1
                    )",
                    params![max_keep as i64],
                )
                .map_err(db)?;
        }
        Ok(())
    }

    /// Загружает и расшифровывает последние `limit` событий трафика в хронологическом порядке (от старых к новым).
    pub fn load_traffic_events(&self, limit: usize) -> Result<Vec<Vec<u8>>, VaultError> {
        let mut stmt = self
            .conn
            .prepare("SELECT id, nonce, ct FROM traffic_events ORDER BY id DESC LIMIT ?1")
            .map_err(db)?;
        let rows = stmt
            .query_map(params![limit as i64], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, Vec<u8>>(1)?,
                    r.get::<_, Vec<u8>>(2)?,
                ))
            })
            .map_err(db)?;
        let mut out = Vec::new();
        for row in rows {
            let (id, nonce, ct) = row.map_err(db)?;
            let aad = format!("traffic:{id}");
            if let Ok(plain) = self.unseal(&nonce, &ct, aad.as_bytes()) {
                out.push(plain);
            }
        }
        out.reverse();
        Ok(out)
    }
}

impl Vault for SqliteVault {
    fn allocate(&mut self, req: &AllocRequest<'_>) -> Result<TokenId, VaultError> {
        let strategy = format!("{:?}", req.strategy);
        let blind = self.blind(&[
            "entry",
            req.scope.as_str(),
            req.kind,
            req.canonical,
            &strategy,
        ]);
        let existing: Option<String> = self
            .conn
            .query_row(
                "SELECT ident FROM entries WHERE blind = ?1",
                params![blind],
                |r| r.get(0),
            )
            .optional()
            .map_err(db)?;
        if let Some(i) = existing {
            return parse_ident(&i);
        }
        let mut failure = None;
        let taken = |id: &TokenId| self.taken(req.kind, id).unwrap_or(true);
        let next = || -> u64 {
            let r = self.conn.query_row(
                "INSERT INTO counters (kind, next) VALUES (?1, 1)
                 ON CONFLICT(kind) DO UPDATE SET next = next + 1 RETURNING next",
                params![req.kind],
                |r| r.get::<_, i64>(0),
            );
            match r {
                Ok(n) => n as u64,
                Err(e) => {
                    failure.get_or_insert(e);
                    u64::MAX
                }
            }
        };
        let id = pick_id(req, taken, next);
        if let Some(e) = failure {
            return Err(db(e));
        }
        let payload = serde_json::to_vec(&EntryPayload {
            original: req.original.into(),
            canonical: req.canonical.into(),
        })
        .map_err(|e| VaultError::Other(e.to_string()))?;
        let i = ident(&id);
        let (nonce, ct) = self.seal(&payload, format!("{}:{i}", req.kind).as_bytes())?;
        self.conn
            .execute(
                "INSERT INTO entries (scope, owner, kind, ident, blind, nonce, ct, created) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![req.scope.as_str(), req.owner, req.kind, i, blind, nonce, ct, now()],
            )
            .map_err(db)?;
        Ok(id)
    }

    fn reserve(&mut self, kind: &str, id: &TokenId) -> Result<(), VaultError> {
        self.conn
            .execute(
                "INSERT OR IGNORE INTO reserved (kind, ident) VALUES (?1, ?2)",
                params![kind, ident(id)],
            )
            .map_err(db)?;
        Ok(())
    }

    fn is_reserved(&self, kind: &str, id: &TokenId) -> Result<bool, VaultError> {
        let n: i64 = self
            .conn
            .query_row(
                "SELECT COUNT(*) FROM reserved WHERE kind = ?1 AND ident = ?2",
                params![kind, ident(id)],
                |r| r.get(0),
            )
            .map_err(db)?;
        Ok(n > 0)
    }

    fn resolve(&self, kind: &str, id: &TokenId) -> Result<Option<Resolved>, VaultError> {
        let i = ident(id);
        let row: Option<(String, String, Vec<u8>, Vec<u8>)> = self
            .conn
            .query_row(
                "SELECT scope, owner, nonce, ct FROM entries WHERE kind = ?1 AND ident = ?2",
                params![kind, i],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()
            .map_err(db)?;
        let Some((scope, owner, nonce, ct)) = row else {
            return Ok(None);
        };
        let plain = self.unseal(&nonce, &ct, format!("{kind}:{i}").as_bytes())?;
        let p: EntryPayload =
            serde_json::from_slice(&plain).map_err(|e| VaultError::Other(e.to_string()))?;
        Ok(Some(Resolved {
            original: p.original,
            canonical: p.canonical,
            scope: scope.parse::<Scope>().map_err(VaultError::Other)?,
            owner,
        }))
    }

    fn record(&mut self, e: &JournalEntry) -> Result<(), VaultError> {
        let blind = self.blind(&["journal", &e.kind, &e.value]);
        let payload = serde_json::to_vec(&JournalPayload {
            value: e.value.clone(),
            context: e.context.clone(),
            evidence: e.evidence.clone(),
        })
        .map_err(|err| VaultError::Other(err.to_string()))?;
        let (nonce, ct) = self.seal(&payload, format!("journal:{}", e.kind).as_bytes())?;
        let category = serde_json::to_value(e.category)
            .ok()
            .and_then(|v| v.as_str().map(String::from))
            .unwrap_or_default();
        let applied = serde_json::to_value(e.applied)
            .ok()
            .and_then(|v| v.as_str().map(String::from))
            .unwrap_or_default();
        let t = now();
        self.conn
            .execute(
                "INSERT INTO journal (blind, kind, category, recognizer, shape, probability, applied, first_seen, last_seen, nonce, ct)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8, ?9, ?10)
                 ON CONFLICT(blind) DO UPDATE SET count = count + 1, last_seen = ?8, probability = ?6,
                    applied = ?7, nonce = ?9, ct = ?10",
                params![blind, e.kind, category, e.recognizer, e.shape, f64::from(e.probability), applied, t, nonce, ct],
            )
            .map_err(db)?;
        Ok(())
    }
}

fn parse_ident(s: &str) -> Result<TokenId, VaultError> {
    let bad = || VaultError::Other(format!("bad ident `{s}`"));
    let (t, v) = s.split_once(':').ok_or_else(bad)?;
    Ok(match t {
        "n" => TokenId::Num(v.parse().map_err(|_| bad())?),
        "h" => TokenId::Hash(v.into()),
        "s" => TokenId::Surrogate(v.into()),
        _ => return Err(bad()),
    })
}

/// Новый случайный мастер-ключ.
pub fn generate_key() -> Result<[u8; 32], VaultError> {
    let mut k = [0u8; 32];
    getrandom::fill(&mut k).map_err(|e| VaultError::Crypto(e.to_string()))?;
    Ok(k)
}

pub fn key_to_hex(k: &[u8; 32]) -> String {
    k.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn key_from_hex(s: &str) -> Result<[u8; 32], VaultError> {
    let s = s.trim();
    if s.len() != 64 || !s.is_ascii() {
        return Err(VaultError::Crypto("key must be 64 hex chars".into()));
    }
    let mut k = [0u8; 32];
    for (i, b) in k.iter_mut().enumerate() {
        *b = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16)
            .map_err(|_| VaultError::Crypto("key must be hex".into()))?;
    }
    Ok(k)
}

#[cfg(test)]
mod tests {
    use super::*;
    use kaponir_core::Strategy;

    fn req<'a>(canonical: &'a str, strategy: Strategy, hash: &'a str) -> AllocRequest<'a> {
        AllocRequest {
            scope: Scope::Personal,
            owner: "me",
            kind: "SVC",
            canonical,
            original: canonical,
            strategy,
            hash_hex: hash,
            surrogate: None,
        }
    }

    #[test]
    fn private_settings_are_encrypted_persistent_and_bound_to_name() {
        let path = std::env::temp_dir().join(format!(
            "kaponir-setting-{}.sqlite",
            key_to_hex(&generate_key().unwrap())
        ));
        let key = [17; 32];
        let secret = b"provider-key-never-plaintext";
        {
            let v = SqliteVault::open(&path, &key).unwrap();
            v.set_private_setting("provider:openai", secret).unwrap();
            let raw: Vec<u8> = v
                .conn
                .query_row(
                    "SELECT value FROM meta WHERE key='private:provider:openai'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert!(!raw.windows(secret.len()).any(|w| w == secret));
            v.conn
                .execute(
                    "INSERT INTO meta (key,value) VALUES ('private:provider:anthropic',?1)",
                    [raw],
                )
                .unwrap();
            assert!(v.private_setting("provider:anthropic").is_err());
        }
        let v = SqliteVault::open(&path, &key).unwrap();
        assert_eq!(
            v.private_setting("provider:openai").unwrap().as_deref(),
            Some(secret.as_slice())
        );
        assert!(v.private_setting("missing").unwrap().is_none());
        v.set_private_setting("provider:openai", b"replacement")
            .unwrap();
        assert_eq!(
            v.private_setting("provider:openai").unwrap().unwrap(),
            b"replacement"
        );
        v.delete_private_setting("provider:openai").unwrap();
        assert!(v.private_setting("provider:openai").unwrap().is_none());
        drop(v);
        let bytes = std::fs::read(&path).unwrap();
        assert!(!bytes.windows(secret.len()).any(|w| w == secret));
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn allocate_resolve_reserve() {
        let mut v = SqliteVault::open_in_memory(&[1; 32]).unwrap();
        v.reserve("SVC", &TokenId::Num(1)).unwrap();
        let a = v.allocate(&req("billing", Strategy::Variable, "")).unwrap();
        assert_eq!(a, TokenId::Num(2));
        assert_eq!(
            v.allocate(&req("billing", Strategy::Variable, "")).unwrap(),
            a
        );
        assert_eq!(
            v.allocate(&req("auth", Strategy::Variable, "")).unwrap(),
            TokenId::Num(3)
        );
        let h1 = v.allocate(&req("x", Strategy::Hmac, "8f2a1c99aa")).unwrap();
        let h2 = v.allocate(&req("y", Strategy::Hmac, "8f2a1c77bb")).unwrap();
        assert_eq!(h1, TokenId::Hash("8f2a1c".into()));
        assert_eq!(h2, TokenId::Hash("8f2a1c7".into()));
        assert_eq!(v.resolve("SVC", &a).unwrap().unwrap().original, "billing");
        assert!(v.resolve("SVC", &TokenId::Num(1)).unwrap().is_none());
        assert!(v.is_reserved("SVC", &TokenId::Num(1)).unwrap());
    }

    #[test]
    fn wrong_key_rejected() {
        let dir = std::env::temp_dir().join(format!("kaponir-vault-test-{}", std::process::id()));
        let path = dir.join("v.db");
        let _ = std::fs::remove_dir_all(&dir);
        {
            let mut v = SqliteVault::open(&path, &[1; 32]).unwrap();
            v.allocate(&req("billing", Strategy::Variable, "")).unwrap();
        }
        assert!(SqliteVault::open(&path, &[2; 32]).is_err());
        let v = SqliteVault::open(&path, &[1; 32]).unwrap();
        assert_eq!(
            v.resolve("SVC", &TokenId::Num(1))
                .unwrap()
                .unwrap()
                .original,
            "billing"
        );
        let raw = std::fs::read(&path).unwrap();
        assert!(
            !raw.windows(7).any(|w| w == b"billing"),
            "plaintext leaked into vault file"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn journal_roundtrip() {
        let mut v = SqliteVault::open_in_memory(&[3; 32]).unwrap();
        let e = JournalEntry {
            kind: "PROJ".into(),
            category: kaponir_core::Category::Infra,
            recognizer: "paths".into(),
            value: "zorg".into(),
            shape: "x".into(),
            context: "/srv/zorg/app".into(),
            probability: 0.6,
            applied: kaponir_core::Verdict::Mask,
            evidence: vec![],
        };
        v.record(&e).unwrap();
        v.record(&e).unwrap();
        let rows = v.journal(Some("open"), 10).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].count, 2);
        assert_eq!(rows[0].value, "zorg");
        assert!(v.set_journal_status(rows[0].id, "resolved").unwrap());
        assert!(v.journal(Some("open"), 10).unwrap().is_empty());
    }

    #[test]
    fn session_roundtrip() {
        let v = SqliteVault::open_in_memory(&[4; 32]).unwrap();
        assert!(v.load_session("s1").unwrap().is_empty());
        let k = Known {
            text: "zorg".into(),
            kind: "PROJ".into(),
            canonical: "zorg".into(),
            scope: Scope::Personal,
            strategy: Strategy::Variable,
            form: kaponir_core::TokenForm::Snake,
        };
        v.save_session("s1", std::slice::from_ref(&k)).unwrap();
        v.save_session("s1", &[k.clone(), k.clone()]).unwrap();
        assert_eq!(v.load_session("s1").unwrap(), vec![k.clone(), k]);
        assert!(v.load_session("s2").unwrap().is_empty());
        assert_eq!(v.purge_sessions(-10).unwrap(), 1);
    }
}
