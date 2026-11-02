//! Ядро обратимого анонимайзера: конвейер, токены, восстановление, контракты расширений.
//! Без сети, диска и знания конкретных языков.

pub mod discover;
pub mod engine;
pub mod journal;
pub mod lexicon;
pub mod metrics;
pub mod onboarding;
pub mod pack;
pub mod recognizer;
pub mod rules;
pub mod s1;
pub mod score;
pub mod surrogate;
pub mod token;
pub mod types;
pub mod vault;

pub use engine::{
    Engine, EngineBuilder, EngineError, Escape, Explained, Known, MaskOptions, Masked, Replacement,
    Restored, StreamRestorer,
};
pub use journal::JournalEntry;
pub use lexicon::{Lexicon, NameLexicon, PartClass, name_key};
pub use pack::{
    Gender, NAME_KINDS, NameRole, PACK_API_VERSION, Pack, PackInfo, PackKind, WordForm,
    is_name_kind,
};
pub use recognizer::{AnalyzeCtx, Recognizer};
pub use score::{Policy, Strategy, Thresholds, UncertainAction, Verdict};
pub use token::{TokenCodec, TokenId, TokenMatch};
pub use types::{
    Candidate, Category, Evidence, Format, Inflection, Input, Origin, Polarity, Scope, TokenForm,
};
pub use vault::{AllocRequest, MemoryVault, Principal, Resolved, Vault, VaultError};
