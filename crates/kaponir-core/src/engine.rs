//! Конвейер: распознаватели → контекст → log-odds [+S1] → решение → пересечения → токены.

use crate::metrics::{Metrics, S1FailureCode};
use hmac::{Hmac, KeyInit, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::net::Ipv4Addr;
use std::sync::Arc;
use std::sync::atomic::Ordering::Relaxed;
use std::time::Instant;

use crate::journal::{JournalEntry, shape};
use crate::lexicon::{Lexicon, name_key};
use crate::pack::{PACK_API_VERSION, Pack};
use crate::recognizer::{AnalyzeCtx, Recognizer};
use crate::s1::{CandidateView, Disclosure, S1Config, features};
use crate::score::{Policy, Strategy, UncertainAction, Verdict, logit};
use crate::surrogate::{ipv4_surrogate, is_surrogate_range};
use crate::token::{MIN_HASH_LEN, TokenCodec, TokenId, TokenMatch};
use crate::types::{Candidate, Category, Evidence, Input, Origin, Polarity, Scope, TokenForm};
use crate::vault::{AllocRequest, Principal, Vault, VaultError};

#[derive(Debug, thiserror::Error)]
pub enum EngineError {
    #[error(transparent)]
    Vault(#[from] VaultError),
    #[error("pack `{0}`: {1}")]
    Pack(String, String),
    #[error("config: {0}")]
    Config(String),
}

pub struct Engine {
    metrics: Arc<Metrics>,
    recognizers: Vec<Box<dyn Recognizer>>,
    packs: Vec<Arc<dyn Pack>>,
    lexicon: Lexicon,
    policy: Policy,
    codec: TokenCodec,
    token_key: [u8; 32],
    surrogate_key: [u8; 32],
    s1: Option<S1Config>,
}

pub struct EngineBuilder {
    recognizers: Vec<Box<dyn Recognizer>>,
    packs: Vec<Arc<dyn Pack>>,
    lexicon: Lexicon,
    policy: Policy,
    prefix: String,
    token_key: [u8; 32],
    s1: Option<S1Config>,
}

impl EngineBuilder {
    /// `token_key` — секрет для HMAC-токенов и суррогатов. Одинаковый ключ на машинах команды
    /// даёт одинаковые псевдонимы.
    pub fn new(token_key: [u8; 32]) -> Self {
        Self {
            recognizers: Vec::new(),
            packs: Vec::new(),
            lexicon: Lexicon::default(),
            policy: Policy::default(),
            prefix: "ANON".into(),
            token_key,
            s1: None,
        }
    }

    pub fn pack(mut self, pack: Arc<dyn Pack>) -> Self {
        self.packs.push(pack);
        self
    }

    pub fn recognizer(mut self, r: Box<dyn Recognizer>) -> Self {
        self.recognizers.push(r);
        self
    }

    /// Дополнительный словарь (например, из правил: «никогда не маскировать», корпоративные домены).
    pub fn lexicon(mut self, lex: &Lexicon) -> Self {
        self.lexicon.merge(lex);
        self
    }

    pub fn policy(mut self, policy: Policy) -> Self {
        self.policy = policy;
        self
    }

    pub fn token_prefix(mut self, prefix: impl Into<String>) -> Self {
        self.prefix = prefix.into();
        self
    }

    pub fn s1(mut self, s1: S1Config) -> Self {
        self.s1 = Some(s1);
        self
    }

    pub fn build(mut self) -> Result<Engine, EngineError> {
        let mut lexicon = Lexicon::default();
        for p in &self.packs {
            let info = p.info();
            if info.api_version != PACK_API_VERSION {
                return Err(EngineError::Pack(
                    info.id.clone(),
                    format!("api_version {} != {PACK_API_VERSION}", info.api_version),
                ));
            }
            lexicon.merge(p.lexicon());
            self.recognizers.extend(p.recognizers());
        }
        lexicon.merge(&self.lexicon);
        let codec = TokenCodec::new(&self.prefix).map_err(EngineError::Config)?;
        Ok(Engine {
            metrics: Arc::new(Metrics::default()),
            recognizers: self.recognizers,
            packs: self.packs,
            lexicon,
            policy: self.policy,
            codec,
            surrogate_key: blake3::derive_key("anon ipv4 surrogate v1", &self.token_key),
            token_key: self.token_key,
            s1: self.s1,
        })
    }
}

#[derive(Clone, Debug)]
pub struct MaskOptions {
    /// Контур новых записей (правила задают свой).
    pub scope: Scope,
    pub owner: String,
    /// HMAC без записи в vault и без журнала — восстановление невозможно.
    pub irreversible: bool,
    /// Переопределить стратегию для всех кандидатов.
    pub strategy: Option<Strategy>,
    pub journal: bool,
    pub explain: bool,
    /// Значения, уже замаскированные или восстановленные в этой сессии.
    pub known: Vec<Known>,
}

/// Значение, уже замаскированное или восстановленное в сессии. В следующих текстах оно
/// маскируется тем же токеном, даже если детекторы его не находят: иначе оригинал, восстановленный
/// в ответе LLM, на следующем ходу ушёл бы провайдеру открытым текстом из истории.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Known {
    pub text: String,
    pub kind: String,
    pub canonical: String,
    pub scope: Scope,
    pub strategy: Strategy,
    pub form: TokenForm,
}

/// Короче — слишком часто совпадает с обычными словами и частями чисел.
const MIN_KNOWN_LEN: usize = 3;

/// Распознаватель кандидатов из памяти сессии.
const SESSION: &str = "session";

impl Default for MaskOptions {
    fn default() -> Self {
        Self {
            scope: Scope::Personal,
            owner: String::new(),
            irreversible: false,
            strategy: None,
            journal: true,
            explain: false,
            known: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Replacement {
    pub category: Category,
    pub start: usize,
    pub end: usize,
    pub original: String,
    pub token: String,
    pub kind: String,
    pub scope: Scope,
    pub recognizer: String,
    pub probability: f32,
    pub verdict: Verdict,
}

#[derive(Clone, Debug, Serialize)]
pub struct Explained {
    pub start: usize,
    pub end: usize,
    pub surface: String,
    pub kind: String,
    pub recognizer: String,
    pub probability: f32,
    pub verdict: Verdict,
    pub applied: bool,
    pub evidence: Vec<Evidence>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Masked {
    pub text: String,
    pub replacements: Vec<Replacement>,
    pub uncertain: usize,
    /// Строки вида токена во входе, которых нет в vault (зарезервированы как литералы).
    pub literals: usize,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub explain: Vec<Explained>,
    /// Замаскированные значения — для памяти сессии.
    #[serde(skip)]
    pub known: Vec<Known>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Escape {
    #[default]
    None,
    /// Значение подставляется внутрь JSON-строки (аргументы tool_use).
    Json,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Restored {
    pub text: String,
    pub restored: usize,
    /// Похожие на токен строки без записи в vault.
    pub unknown: Vec<String>,
    /// Токены, на восстановление которых у пользователя нет прав.
    pub denied: Vec<String>,
    /// Восстановленные значения — для памяти сессии.
    #[serde(skip)]
    pub known: Vec<Known>,
}

struct Scored {
    c: Candidate,
    verdict: Verdict,
}

impl Scored {
    fn applied_mask(&self, uncertain: UncertainAction) -> bool {
        self.c.polarity == Polarity::Mask
            && (self.verdict == Verdict::Mask
                || (self.verdict == Verdict::Uncertain && uncertain == UncertainAction::Mask))
    }
}

impl Engine {
    pub fn builder(token_key: [u8; 32]) -> EngineBuilder {
        EngineBuilder::new(token_key)
    }

    pub fn codec(&self) -> &TokenCodec {
        &self.codec
    }

    pub fn lexicon(&self) -> &Lexicon {
        &self.lexicon
    }

    pub fn packs(&self) -> &[Arc<dyn Pack>] {
        &self.packs
    }

    pub fn policy(&self) -> &Policy {
        &self.policy
    }

    /// Все кандидаты с оценками, без решения о пересечениях — для отладки и тестов пакетов.
    pub fn analyze(&self, input: &Input<'_>) -> Vec<Candidate> {
        let ctx = AnalyzeCtx {
            lexicon: &self.lexicon,
            packs: &self.packs,
        };
        let mut out = Vec::new();
        for r in &self.recognizers {
            r.analyze(input, &ctx, &mut out);
        }
        let text = input.text;
        out.retain(|c| {
            c.start < c.end
                && c.end <= text.len()
                && text.is_char_boundary(c.start)
                && text.is_char_boundary(c.end)
        });
        for c in out.iter_mut() {
            self.boost_context(text, c);
        }
        out
    }

    pub fn s1_config(&self) -> Option<&S1Config> {
        self.s1.as_ref()
    }

    pub(crate) fn discovery_s1(&self) -> Option<&S1Config> {
        self.s1.as_ref()
    }

    pub fn s1_mode(&self) -> &'static str {
        match &self.s1 {
            None => "off",
            Some(config) if config.shadow => "shadow",
            Some(_) => "active",
        }
    }

    pub fn metrics(&self) -> &Arc<Metrics> {
        &self.metrics
    }

    /// Сохраняет счётчики процесса при перезагрузке правил.
    pub fn with_metrics(mut self, metrics: Arc<Metrics>) -> Self {
        self.metrics = metrics;
        self
    }

    pub fn mask(
        &self,
        input: &Input<'_>,
        opts: &MaskOptions,
        vault: &mut dyn Vault,
    ) -> Result<Masked, EngineError> {
        let started = Instant::now();
        let result = self.mask_impl(input, opts, vault, true);
        self.metrics
            .mask
            .observe(started.elapsed(), result.is_err());
        if let Ok(masked) = &result {
            self.metrics
                .replacements
                .fetch_add(masked.replacements.len() as u64, Relaxed);
        }
        result
    }

    /// Локальный анализ без вызовов внешнего или обученного S1. Для discover и других
    /// предпросмотров, которые не должны раскрывать документы классификатору.
    pub fn mask_without_s1(
        &self,
        input: &Input<'_>,
        opts: &MaskOptions,
        vault: &mut dyn Vault,
    ) -> Result<Masked, EngineError> {
        self.mask_impl(input, opts, vault, false)
    }

    fn mask_impl(
        &self,
        input: &Input<'_>,
        opts: &MaskOptions,
        vault: &mut dyn Vault,
        use_s1: bool,
    ) -> Result<Masked, EngineError> {
        let text = input.text;
        let mut result = Masked::default();

        // 1. Токены, уже присутствующие во входе: свои не трогаем, чужие резервируем как литералы.
        let mut protected: Vec<(usize, usize)> = Vec::new();
        for m in self.codec.find_iter(text) {
            if vault.resolve(&m.kind, &m.id)?.is_none() && !opts.irreversible {
                vault.reserve(&m.kind, &m.id)?;
                result.literals += 1;
            }
            protected.push((m.start, m.end));
        }

        // 2–3. Кандидаты и контекст.
        let mut cands = self.analyze(input);
        cands.extend(known_candidates(text, &opts.known, &self.packs));
        cands.retain(|c| !protected.iter().any(|&(a, b)| c.start < b && a < c.end));
        let mut own_surrogates = Vec::new();
        for (i, c) in cands.iter().enumerate() {
            if let Ok(ip) = c.surface(text).parse::<Ipv4Addr>()
                && is_surrogate_range(ip)
                && vault
                    .resolve("IP", &TokenId::Surrogate(ip.to_string()))?
                    .is_some()
            {
                own_surrogates.push(i);
            }
        }
        for i in own_surrogates.into_iter().rev() {
            cands.remove(i);
        }

        // 4. Решения.
        let mut scored: Vec<Scored> = cands
            .into_iter()
            .map(|c| {
                let verdict = self.verdict(&c);
                Scored { c, verdict }
            })
            .collect();
        if use_s1 && let Some(s1) = &self.s1 {
            self.apply_s1(text, s1, &mut scored);
        }
        // Кандидат внутри «никогда не маскировать» решён правилом: не маскируется и не спорный
        // (иначе решение пользователя снова попадало бы в журнал разбора).
        let keep_index = KeepIndex::new(&scored);
        let kept: Vec<usize> = (0..scored.len())
            .filter(|&i| keep_index.covers(&scored[i].c))
            .collect();
        for i in kept {
            scored[i].verdict = Verdict::Keep;
            scored[i].c.evidence.push(Evidence {
                source: "rule-keep".into(),
                logit: 0.0,
            });
        }

        // 5. Пересечения.
        let accepted = self.resolve_conflicts(&scored);
        let mut applied_flags = vec![false; scored.len()];
        for &i in &accepted {
            applied_flags[i] = true;
        }

        if opts.explain {
            result.explain = scored
                .iter()
                .enumerate()
                .map(|(i, s)| Explained {
                    start: s.c.start,
                    end: s.c.end,
                    surface: s.c.surface(text).to_string(),
                    kind: s.c.kind.clone(),
                    recognizer: s.c.recognizer.clone(),
                    probability: s.c.probability(),
                    verdict: s.verdict,
                    applied: applied_flags[i],
                    evidence: s.c.evidence.clone(),
                })
                .collect();
        }

        // 6. Журнал спорных решений.
        for (i, s) in scored.iter().enumerate() {
            if s.verdict != Verdict::Uncertain || s.c.polarity != Polarity::Mask {
                continue;
            }
            let applied = applied_flags[i];
            let end = accepted.partition_point(|&j| scored[j].c.start < s.c.end);
            let shadowed = !applied && end > 0 && scored[accepted[end - 1]].c.end > s.c.start;
            if shadowed {
                continue;
            }
            result.uncertain += 1;
            if opts.journal && !opts.irreversible {
                let surface = s.c.surface(text);
                vault.record(&JournalEntry {
                    kind: s.c.kind.clone(),
                    category: s.c.category,
                    recognizer: s.c.recognizer.clone(),
                    value: surface.to_string(),
                    shape: shape(surface),
                    context: masked_window(text, s.c.start, s.c.end, &scored, &accepted, 60),
                    probability: s.c.probability(),
                    applied: if applied {
                        Verdict::Mask
                    } else {
                        Verdict::Keep
                    },
                    evidence: s.c.evidence.clone(),
                })?;
            }
        }

        // 7. Токены и сборка результата.
        let mut out = String::with_capacity(text.len());
        let mut pos = 0;
        for (n, &i) in accepted.iter().enumerate() {
            let s = &scored[i];
            let c = &s.c;
            let next_start = accepted
                .get(n + 1)
                .map_or(text.len(), |&j| scored[j].c.start);
            let strategy = self.strategy_for(c, opts);
            // Словоформа: токен заменяет основу, окончание остаётся в тексте после него.
            // Хвост цифр не поглощается: между номером токена и цифрами стоят буквы окончания.
            let (token_end, end, original, canonical) = match &c.inflection {
                Some(inf) => {
                    let canonical = c.canonical.clone().unwrap_or_else(|| name_key(&inf.lemma));
                    (c.end - inf.ending, c.end, inf.lemma.as_str(), canonical)
                }
                None => {
                    let end = absorb_tail(text, c.end, next_start, strategy);
                    let base = c
                        .canonical
                        .clone()
                        .or_else(|| {
                            self.packs
                                .iter()
                                .find_map(|p| p.canonicalize(&c.kind, c.surface(text)))
                        })
                        .unwrap_or_else(|| c.surface(text).to_string());
                    (
                        end,
                        end,
                        &text[c.start..end],
                        format!("{base}{}", &text[c.end..end]),
                    )
                }
            };
            // Регистр нельзя восстановить из «токен + окончание»: у строчной и заглавной
            // словоформы должны быть разные записи. Словоформы одного регистра делят токен.
            let canonical = if crate::pack::is_name_kind(&c.kind)
                && original.chars().next().is_some_and(char::is_lowercase)
                && !canonical.starts_with("lower:")
            {
                format!("lower:{canonical}")
            } else {
                canonical
            };
            let scope = c.scope.unwrap_or(opts.scope);
            let id = self.token_id(&c.kind, &canonical, original, strategy, scope, opts, vault)?;
            let token = self.codec.format(&c.kind, &id, c.form);
            if !opts.irreversible {
                result.known.push(Known {
                    text: original.to_string(),
                    kind: c.kind.clone(),
                    canonical: canonical.clone(),
                    scope,
                    strategy: strategy_of(&id),
                    form: c.form,
                });
            }
            out.push_str(&text[pos..c.start]);
            out.push_str(&token);
            out.push_str(&text[token_end..end]);
            pos = end;
            result.replacements.push(Replacement {
                category: s.c.category,
                start: c.start,
                end: token_end,
                original: original.to_string(),
                token,
                kind: c.kind.clone(),
                scope,
                recognizer: c.recognizer.clone(),
                probability: c.probability(),
                verdict: s.verdict,
            });
        }
        out.push_str(&text[pos..]);
        result.text = out;
        Ok(result)
    }

    #[allow(clippy::too_many_arguments)]
    fn token_id(
        &self,
        kind: &str,
        canonical: &str,
        original: &str,
        strategy: Strategy,
        scope: Scope,
        opts: &MaskOptions,
        vault: &mut dyn Vault,
    ) -> Result<TokenId, EngineError> {
        let surrogate = match strategy {
            Strategy::Surrogate => original
                .parse::<Ipv4Addr>()
                .ok()
                .map(|ip| ipv4_surrogate(&self.surrogate_key, ip).to_string()),
            _ => None,
        };
        let hash_hex = self.hash_hex(kind, canonical);
        if opts.irreversible {
            return Ok(match surrogate {
                Some(s) => TokenId::Surrogate(s),
                None => TokenId::Hash(hash_hex[..MIN_HASH_LEN].to_string()),
            });
        }
        let strategy = if strategy == Strategy::Surrogate && surrogate.is_none() {
            Strategy::Variable
        } else {
            strategy
        };
        Ok(vault.allocate(&AllocRequest {
            scope,
            owner: &opts.owner,
            kind,
            canonical,
            original,
            strategy,
            hash_hex: &hash_hex,
            surrogate: surrogate.as_deref(),
        })?)
    }

    fn hash_hex(&self, kind: &str, canonical: &str) -> String {
        let mut mac =
            <Hmac<Sha256> as KeyInit>::new_from_slice(&self.token_key).expect("any key length");
        mac.update(kind.as_bytes());
        mac.update(&[0]);
        mac.update(canonical.as_bytes());
        mac.finalize()
            .into_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    fn strategy_for(&self, c: &Candidate, opts: &MaskOptions) -> Strategy {
        if opts.irreversible && opts.strategy != Some(Strategy::Surrogate) {
            return Strategy::Hmac;
        }
        opts.strategy
            .or(c.strategy)
            .unwrap_or_else(|| self.policy.strategy(&c.kind))
    }

    fn verdict(&self, c: &Candidate) -> Verdict {
        if c.polarity == Polarity::Keep {
            return Verdict::Keep;
        }
        if c.hard || matches!(c.origin, Origin::Rule { .. }) {
            return Verdict::Mask;
        }
        self.policy.thresholds(c.category).verdict(c.probability())
    }

    fn boost_context(&self, text: &str, c: &mut Candidate) {
        let Some(words) = self.lexicon.context_for(&c.kind) else {
            return;
        };
        let lo = floor_boundary(text, c.start.saturating_sub(48));
        let hi = ceil_boundary(text, (c.end + 24).min(text.len()));
        let window = format!("{} {}", &text[lo..c.start], &text[c.end..hi]).to_lowercase();
        let tokens: Vec<&str> = window
            .split(|ch: char| !ch.is_alphanumeric())
            .filter(|w| !w.is_empty())
            .collect();
        let hit = words.iter().find(|w| {
            if w.chars().all(char::is_alphanumeric) {
                tokens.contains(&w.as_str())
            } else {
                window.contains(w.as_str())
            }
        });
        if let Some(w) = hit {
            c.evidence.push(Evidence {
                source: format!("context:{w}"),
                logit: 1.5,
            });
        }
    }

    fn apply_s1(&self, text: &str, s1: &S1Config, scored: &mut [Scored]) {
        let keep_index = KeepIndex::new(scored);
        let idx: Vec<usize> = scored
            .iter()
            .enumerate()
            .filter(|(_, s)| {
                s.verdict == Verdict::Uncertain
                    && s.c.polarity == Polarity::Mask
                    && !s.c.hard
                    && s.c.origin == Origin::Builtin
                    && !keep_index.covers(&s.c)
            })
            .map(|(i, _)| i)
            .collect();
        if idx.is_empty() {
            return;
        }
        let masks: Vec<usize> = (0..scored.len())
            .filter(|&i| scored[i].c.polarity == Polarity::Mask)
            .collect();
        let views: Vec<CandidateView> = idx
            .iter()
            .map(|&i| {
                let c = &scored[i].c;
                let credential = masks.iter().any(|&j| {
                    let other = &scored[j].c;
                    other.category == Category::Credential
                        && other.start < c.end
                        && c.start < other.end
                });
                let cap = if credential {
                    Disclosure::L0
                } else {
                    Disclosure::L2
                };
                let level = s1.provider.max_disclosure().min(cap);
                let surface = c.surface(text);
                CandidateView {
                    kind: c.kind.clone(),
                    category: c.category,
                    recognizer: c.recognizer.clone(),
                    features: features(surface, self.lexicon.is_word(surface)),
                    surface: (level >= Disclosure::L1).then(|| surface.to_string()),
                    context: (level >= Disclosure::L2).then(|| {
                        masked_window(text, c.start, c.end, scored, &masks, s1.context_bytes)
                    }),
                }
            })
            .collect();
        let started = Instant::now();
        self.metrics
            .s1_candidates
            .fetch_add(views.len() as u64, Relaxed);
        let scores = if views.iter().any(|v| v.context.is_some()) {
            s1.provider
                .score_with_profile(&views, &crate::s1::disclosure_profile(&s1.profile))
        } else {
            s1.provider.score(&views)
        };
        let ps = match scores {
            Ok(ps) => ps,
            Err(e) => {
                self.metrics.s1.observe(started.elapsed(), true);
                self.metrics.s1_failure(match e {
                    crate::s1::S1Error::Timeout => S1FailureCode::Timeout,
                    crate::s1::S1Error::Other(_) => S1FailureCode::ProviderError,
                });
                return; // сбой S1 ⇒ решает политика
            }
        };
        if ps.len() != idx.len()
            || ps
                .iter()
                .any(|p| !p.is_finite() || !(0.0..=1.0).contains(p))
        {
            self.metrics.s1.observe(started.elapsed(), true);
            self.metrics.s1_failure(S1FailureCode::InvalidResponse);
            return;
        }
        self.metrics.s1.observe(started.elapsed(), false);
        for (&i, p) in idx.iter().zip(ps) {
            let pc = s1.calibration.apply(p);
            let s = &mut scored[i];
            if s1.shadow {
                // Теневой режим: оценка видна в объяснении, но на решение не влияет.
                s.c.evidence.push(Evidence {
                    source: format!("s1-shadow:{}:p={pc:.2}", s1.provider.id()),
                    logit: 0.0,
                });
                continue;
            }
            s.c.evidence.push(Evidence {
                source: format!("s1:{}", s1.provider.id()),
                logit: s1.weight * logit(pc),
            });
            let v = self.verdict(&s.c);
            s.verdict = if v == Verdict::Keep && pc > s1.keep_below {
                Verdict::Uncertain
            } else {
                v
            };
        }
    }

    /// Индексы принятых масок, отсортированные по началу.
    fn resolve_conflicts(&self, scored: &[Scored]) -> Vec<usize> {
        let mut masks: Vec<usize> = (0..scored.len())
            .filter(|&i| scored[i].applied_mask(self.policy.uncertain))
            .collect();
        masks.sort_by(|&a, &b| {
            let (x, y) = (&scored[a].c, &scored[b].c);
            y.origin
                .rank()
                .cmp(&x.origin.rank())
                .then(y.hard.cmp(&x.hard))
                .then(y.probability().total_cmp(&x.probability()))
                .then(y.len().cmp(&x.len()))
                .then(x.start.cmp(&y.start))
        });
        // Значение из памяти сессии внутри более длинной маски не дробит её (`m.ivanov@corp.ru`
        // целиком, а не только домен) — но возвращается, если длинная маска проиграла другой.
        let inside_longer = |i: usize| {
            let c = &scored[i].c;
            c.recognizer == SESSION
                && masks.iter().any(|&j| {
                    let o = &scored[j].c;
                    o.recognizer != SESSION
                        && o.start <= c.start
                        && c.end <= o.end
                        && o.len() > c.len()
                })
        };
        let (deferred, first): (Vec<usize>, Vec<usize>) =
            masks.iter().copied().partition(|&i| inside_longer(i));
        // Принятые интервалы не пересекаются: достаточно ближайшего слева от c.end.
        let mut accepted = std::collections::BTreeMap::<usize, usize>::new();
        for i in first.into_iter().chain(deferred) {
            let c = &scored[i].c;
            let overlaps = accepted
                .range(..c.end)
                .next_back()
                .is_some_and(|(_, &j)| scored[j].c.end > c.start);
            if !overlaps {
                accepted.insert(c.start, i);
            }
        }
        accepted.into_values().collect()
    }

    // ---------- Восстановление ----------

    pub fn restore(
        &self,
        text: &str,
        principal: &Principal,
        vault: &dyn Vault,
        escape: Escape,
    ) -> Result<Restored, EngineError> {
        let mut r = Restored::default();
        self.restore_into(text, principal, vault, escape, &mut r)?;
        Ok(r)
    }

    fn restore_into(
        &self,
        text: &str,
        principal: &Principal,
        vault: &dyn Vault,
        escape: Escape,
        r: &mut Restored,
    ) -> Result<(), EngineError> {
        let started = Instant::now();
        let before = (r.restored, r.unknown.len(), r.denied.len());
        let result = self.restore_into_impl(text, principal, vault, escape, r);
        self.metrics
            .restore
            .observe(started.elapsed(), result.is_err());
        self.metrics
            .restored
            .fetch_add((r.restored - before.0) as u64, Relaxed);
        self.metrics
            .unknown_tokens
            .fetch_add((r.unknown.len() - before.1) as u64, Relaxed);
        self.metrics
            .denied_tokens
            .fetch_add((r.denied.len() - before.2) as u64, Relaxed);
        result
    }

    fn restore_into_impl(
        &self,
        text: &str,
        principal: &Principal,
        vault: &dyn Vault,
        escape: Escape,
        r: &mut Restored,
    ) -> Result<(), EngineError> {
        let mut found: Vec<Found> = self.codec.find_iter(text).map(Found::Token).collect();
        for m in ipv4_re().find_iter(text) {
            let before = text[..m.start()].chars().next_back();
            let after = text[m.end()..].chars().next();
            if before.is_some_and(|c| c.is_ascii_digit() || c == '.')
                || after.is_some_and(|c| {
                    c.is_ascii_digit()
                        || (c == '.'
                            && text[m.end() + 1..].starts_with(|d: char| d.is_ascii_digit()))
                })
            {
                continue;
            }
            if let Ok(ip) = m.as_str().parse::<Ipv4Addr>()
                && is_surrogate_range(ip)
            {
                found.push(Found::Surrogate(m.start(), m.end(), ip.to_string()));
            }
        }
        found.sort_by_key(Found::start);

        let mut out = String::with_capacity(text.len());
        let mut pos = 0;
        for f in found {
            if f.start() < pos {
                continue;
            }
            match f {
                Found::Surrogate(a, b, s) => {
                    let Some(res) = vault.resolve("IP", &TokenId::Surrogate(s))? else {
                        continue;
                    };
                    if !principal.can_restore(&res) {
                        r.denied.push(text[a..b].to_string());
                        continue;
                    }
                    out.push_str(&text[pos..a]);
                    push_escaped(&mut out, &res.original, escape);
                    pos = b;
                    r.restored += 1;
                    r.known.push(Known {
                        text: res.original.clone(),
                        kind: "IP".into(),
                        canonical: res.canonical.clone(),
                        scope: res.scope,
                        strategy: Strategy::Surrogate,
                        form: TokenForm::Snake,
                    });
                }
                Found::Token(m) => {
                    let Some((res, tail)) = self.resolve_token(text, &m, vault)? else {
                        if !vault.is_reserved(&m.kind, &m.id)? {
                            r.unknown.push(text[m.start..m.end].to_string());
                        }
                        continue;
                    };
                    if !principal.can_restore(&res) {
                        r.denied.push(text[m.start..m.end].to_string());
                        continue;
                    }
                    out.push_str(&text[pos..m.start]);
                    let mut end = m.end;
                    let mut value = res.original.clone();
                    if !tail.is_empty() {
                        value.push_str(tail);
                    } else if m.end == m.id_end {
                        let suffix: String = text[m.end..]
                            .chars()
                            .take_while(|c| c.is_alphabetic())
                            .take(8)
                            .collect();
                        if !suffix.is_empty()
                            && let Some(form) = self
                                .packs
                                .iter()
                                .find_map(|p| p.restore_form(&m.kind, &res.original, &suffix))
                        {
                            value = form;
                            end += suffix.len();
                        }
                    }
                    push_escaped(&mut out, &value, escape);
                    pos = end;
                    r.restored += 1;
                    let form = if text[m.start..m.end].contains('_') {
                        TokenForm::Snake
                    } else {
                        TokenForm::Dns
                    };
                    r.known.push(Known {
                        text: res.original.clone(),
                        kind: m.kind.clone(),
                        canonical: res.canonical.clone(),
                        scope: res.scope,
                        strategy: strategy_of(&m.id),
                        form,
                    });
                }
            }
        }
        out.push_str(&text[pos..]);
        r.text.push_str(&out);
        Ok(())
    }

    /// Для HMAC-токена пробует самый длинный существующий префикс: LLM мог приклеить к токену
    /// шестнадцатеричные символы. Возвращает запись и «лишний» хвост id.
    fn resolve_token<'t>(
        &self,
        text: &'t str,
        m: &TokenMatch,
        vault: &dyn Vault,
    ) -> Result<Option<(crate::vault::Resolved, &'t str)>, EngineError> {
        match &m.id {
            TokenId::Hash(h) => {
                for len in (MIN_HASH_LEN..=h.len()).rev() {
                    if let Some(res) =
                        vault.resolve(&m.kind, &TokenId::Hash(h[..len].to_string()))?
                    {
                        // id в тексте: `h` + hex; хвост — после `len` символов hex.
                        let tail = &text[m.id_start + 1 + len..m.id_end];
                        return Ok(Some((res, tail)));
                    }
                }
                Ok(None)
            }
            id => Ok(vault.resolve(&m.kind, id)?.map(|r| (r, ""))),
        }
    }

    fn max_hold(&self) -> usize {
        self.codec.max_token_len() + 32
    }
}

enum Found {
    Token(TokenMatch),
    Surrogate(usize, usize, String),
}

impl Found {
    fn start(&self) -> usize {
        match self {
            Found::Token(m) => m.start,
            Found::Surrogate(a, ..) => *a,
        }
    }
}

/// Потоковое восстановление: придерживает только хвост, который может оказаться началом токена
/// или суррогата; остальное отдаётся сразу. Не держит ссылок — живёт внутри асинхронного потока,
/// а движок, vault и пользователь передаются в каждый вызов.
#[derive(Debug, Default)]
pub struct StreamRestorer {
    escape: Escape,
    buf: String,
    stats: Restored,
}

impl StreamRestorer {
    pub fn new(escape: Escape) -> Self {
        Self {
            escape,
            buf: String::new(),
            stats: Restored::default(),
        }
    }

    pub fn push(
        &mut self,
        engine: &Engine,
        principal: &Principal,
        vault: &dyn Vault,
        chunk: &str,
    ) -> Result<String, EngineError> {
        self.buf.push_str(chunk);
        let cut = self.safe_cut(engine);
        let ready: String = self.buf.drain(..cut).collect();
        self.emit(engine, principal, vault, &ready)
    }

    /// Отдаёт придержанный хвост (конец блока или потока).
    pub fn flush(
        &mut self,
        engine: &Engine,
        principal: &Principal,
        vault: &dyn Vault,
    ) -> Result<String, EngineError> {
        let rest = std::mem::take(&mut self.buf);
        self.emit(engine, principal, vault, &rest)
    }

    pub fn pending(&self) -> usize {
        self.buf.len()
    }

    pub fn stats(&self) -> &Restored {
        &self.stats
    }

    pub fn take_stats(&mut self) -> Restored {
        std::mem::take(&mut self.stats)
    }

    fn emit(
        &mut self,
        engine: &Engine,
        principal: &Principal,
        vault: &dyn Vault,
        ready: &str,
    ) -> Result<String, EngineError> {
        if ready.is_empty() {
            return Ok(String::new());
        }
        let mut r = Restored::default();
        engine.restore_into(ready, principal, vault, self.escape, &mut r)?;
        self.stats.restored += r.restored;
        self.stats.unknown.extend(r.unknown);
        self.stats.denied.extend(r.denied);
        self.stats.known.extend(r.known);
        Ok(r.text)
    }

    fn safe_cut(&self, engine: &Engine) -> usize {
        let buf = &self.buf;
        let lo = floor_boundary(buf, buf.len().saturating_sub(engine.max_hold()));
        for (off, _) in buf[lo..].char_indices() {
            let i = lo + off;
            let rest = &buf[i..];
            let prev = buf[..i].chars().next_back();
            let ip_start = !prev.is_some_and(|c| c.is_ascii_digit() || c == '.');
            if engine.codec.could_continue(rest) || (ip_start && ipv4_could_continue(rest)) {
                return i;
            }
        }
        buf.len()
    }
}

fn strategy_of(id: &TokenId) -> Strategy {
    match id {
        TokenId::Num(_) => Strategy::Variable,
        TokenId::Hash(_) => Strategy::Hmac,
        TokenId::Surrogate(_) => Strategy::Surrogate,
    }
}

/// Маска закрыта keep-кандидатом (правилом или словарём), который её покрывает и не уступает
/// ей по контуру. Жёсткие маски (секреты) перекрывает только enforced.
struct KeepIndex {
    by_rank: [Vec<(usize, usize)>; 5],
}

impl KeepIndex {
    fn new(scored: &[Scored]) -> Self {
        let mut by_rank: [Vec<(usize, usize)>; 5] = Default::default();
        for s in scored {
            if s.c.polarity == Polarity::Keep {
                by_rank[s.c.origin.rank() as usize].push((s.c.start, s.c.end));
            }
        }
        for spans in &mut by_rank {
            spans.sort_unstable();
            let mut end = 0;
            for span in spans {
                end = end.max(span.1);
                span.1 = end;
            }
        }
        Self { by_rank }
    }

    fn covers(&self, candidate: &Candidate) -> bool {
        if candidate.polarity != Polarity::Mask {
            return false;
        }
        let min_rank = if candidate.hard {
            4
        } else {
            candidate.origin.rank() as usize
        };
        self.by_rank[min_rank..].iter().any(|spans| {
            let end = spans.partition_point(|&(start, _)| start <= candidate.start);
            end > 0 && spans[end - 1].1 >= candidate.end
        })
    }
}

/// Вхождения значений из памяти сессии — по границам идентификатора, с учётом регистра.
/// Форма токена — по месту: внутри хостнейма или имени пода всегда `anon-x-1`, иначе как
/// при первом маскировании. Словоформы (`Голошанову` для `Голошанов`) берутся у пакетов.
fn known_candidates(text: &str, known: &[Known], packs: &[Arc<dyn Pack>]) -> Vec<Candidate> {
    let is_ident = |c: char| c.is_alphanumeric() || c == '_';
    let mut out = Vec::new();
    for k in known {
        if k.text.chars().count() < MIN_KNOWN_LEN {
            continue;
        }
        let dns_like = k
            .text
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
        let first_ident = k.text.chars().next().is_some_and(is_ident);
        let last_ident = k.text.chars().next_back().is_some_and(is_ident);
        let mut forms: Vec<(String, String)> = vec![(k.text.clone(), String::new())];
        for p in packs {
            for f in p.inflect(&k.kind, &k.text) {
                if !f.1.is_empty() && !forms.contains(&f) {
                    forms.push(f);
                }
            }
        }
        for (form, suffix) in &forms {
            for (s, _) in text.match_indices(form.as_str()) {
                let e = s + form.len();
                if (first_ident && text[..s].chars().next_back().is_some_and(is_ident))
                    || (last_ident && text[e..].chars().next().is_some_and(is_ident))
                {
                    continue;
                }
                let mut c = Candidate::new(s, e, k.kind.clone(), Category::Custom, SESSION, 0.99)
                    .hard()
                    .canonical(k.canonical.clone())
                    .strategy(k.strategy)
                    .with_evidence("session-known", 2.0)
                    .inflected(k.text.clone(), suffix.len());
                let in_dns = matches!(text[..s].chars().next_back(), Some('-' | '.'))
                    || matches!(text[e..].chars().next(), Some('-' | '.'));
                c.form = if dns_like && in_dns {
                    TokenForm::Dns
                } else {
                    k.form
                };
                c.scope = Some(k.scope);
                out.push(c);
            }
        }
    }
    out
}

fn ipv4_re() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"\b(?:\d{1,3}\.){3}\d{1,3}\b").expect("valid regex"))
}

/// Может ли `s` быть началом IPv4-адреса из 240.0.0.0/4 (или полным адресом, который ещё продолжится).
fn ipv4_could_continue(s: &str) -> bool {
    let b = s.as_bytes();
    if !b.first().is_some_and(u8::is_ascii_digit) {
        return false;
    }
    let (mut octets, mut cur, mut first) = (0, 0usize, 0u32);
    for &c in b {
        match c {
            b'0'..=b'9' => {
                cur += 1;
                if cur > 3 {
                    return false;
                }
                if octets == 0 {
                    first = first * 10 + u32::from(c - b'0');
                }
            }
            b'.' => {
                if cur == 0 || (octets == 0 && !(240..=255).contains(&first)) {
                    return false;
                }
                octets += 1;
                if octets > 3 {
                    return false;
                }
                cur = 0;
            }
            _ => return false,
        }
    }
    if octets == 0 {
        return match cur {
            1 => first == 2,
            2 => first == 24 || first == 25,
            _ => (240..=255).contains(&first),
        };
    }
    true
}

/// Поглощает цифры (а для HMAC — и hex), идущие сразу за фрагментом: иначе `ANON_SVC_1` + `2`
/// было бы неотличимо от `ANON_SVC_12`.
fn absorb_tail(text: &str, end: usize, limit: usize, strategy: Strategy) -> usize {
    if strategy == Strategy::Surrogate {
        return end;
    }
    let mut e = end;
    for c in text[end..limit].chars() {
        let ok = match strategy {
            Strategy::Hmac => c.is_ascii_hexdigit(),
            _ => c.is_ascii_digit(),
        };
        if !ok {
            break;
        }
        e += c.len_utf8();
    }
    e
}

/// Окно контекста вокруг `[start, end)`, в котором другие принятые маски заменены на `[KIND]`.
fn masked_window(
    text: &str,
    start: usize,
    end: usize,
    scored: &[Scored],
    masks: &[usize],
    context_bytes: usize,
) -> String {
    let lo = floor_boundary(text, start.saturating_sub(context_bytes));
    let hi = ceil_boundary(text, end.saturating_add(context_bytes).min(text.len()));
    let mut spans: Vec<(usize, usize, &str)> = masks
        .iter()
        .map(|&j| &scored[j].c)
        .filter(|o| o.start < hi && o.end > lo && !(o.start == start && o.end == end))
        .map(|o| (o.start.max(lo), o.end.min(hi), o.kind.as_str()))
        .collect();
    spans.sort_by_key(|s| s.0);
    let mut merged: Vec<(usize, usize, &str)> = Vec::new();
    for (a, b, kind) in spans {
        if let Some(last) = merged.last_mut()
            && a <= last.1
        {
            last.1 = last.1.max(b);
        } else {
            merged.push((a, b, kind));
        }
    }
    let mut out = String::new();
    let mut pos = lo;
    for (a, b, kind) in merged {
        if a < pos {
            continue;
        }
        out.push_str(&text[pos..a]);
        out.push('[');
        out.push_str(kind);
        out.push(']');
        pos = b;
    }
    out.push_str(&text[pos..hi]);
    out.replace(['\n', '\r'], " ")
}

fn push_escaped(out: &mut String, value: &str, escape: Escape) {
    match escape {
        Escape::None => out.push_str(value),
        Escape::Json => {
            for c in value.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                    c => out.push(c),
                }
            }
        }
    }
}

pub fn floor_boundary(s: &str, mut i: usize) -> usize {
    i = i.min(s.len());
    while !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

pub fn ceil_boundary(s: &str, mut i: usize) -> usize {
    i = i.min(s.len());
    while !s.is_char_boundary(i) {
        i += 1;
    }
    i
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vault::MemoryVault;

    /// Находит `needle` только после `/` — как сегмент пути, но не в прозе.
    struct PathWord(&'static str);

    impl Recognizer for PathWord {
        fn id(&self) -> &str {
            "path-word"
        }

        fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
            for (i, _) in input.text.match_indices(&format!("/{}", self.0)) {
                out.push(Candidate::new(
                    i + 1,
                    i + 1 + self.0.len(),
                    "PROJ",
                    Category::Infra,
                    "path-word",
                    0.99,
                ));
            }
        }
    }

    fn engine(word: &'static str) -> Engine {
        Engine::builder([1; 32])
            .recognizer(Box::new(PathWord(word)))
            .build()
            .unwrap()
    }

    fn opts(known: Vec<Known>) -> MaskOptions {
        MaskOptions {
            owner: "me".into(),
            known,
            ..MaskOptions::default()
        }
    }

    #[test]
    fn session_memory_masks_restored_values_in_prose() {
        let e = engine("zorg");
        let mut v = MemoryVault::new();
        let me = Principal::local("me");
        let m = e
            .mask(&Input::text("cd /srv/zorg"), &opts(vec![]), &mut v)
            .unwrap();
        assert_eq!(m.text, "cd /srv/ANON_PROJ_1");
        assert_eq!(m.known.len(), 1);

        let r = e
            .restore("ANON_PROJ_1 is down", &me, &v, Escape::None)
            .unwrap();
        assert_eq!(r.text, "zorg is down");
        assert_eq!(r.known, m.known);

        // Проза детекторами не ловится — без памяти сессии оригинал утёк бы.
        let plain = e
            .mask(
                &Input::text("zorg is down, zorgs ok"),
                &opts(vec![]),
                &mut v,
            )
            .unwrap();
        assert_eq!(plain.text, "zorg is down, zorgs ok");
        let again = e
            .mask(
                &Input::text("zorg is down, zorgs ok"),
                &opts(r.known),
                &mut v,
            )
            .unwrap();
        assert_eq!(again.text, "ANON_PROJ_1 is down, zorgs ok");
    }

    #[test]
    fn stream_holds_only_token_prefix_and_escapes_json() {
        let e = engine(r#"a"b\c"#);
        let mut v = MemoryVault::new();
        let me = Principal::local("me");
        let m = e
            .mask(&Input::text(r#"x /a"b\c"#), &opts(vec![]), &mut v)
            .unwrap();
        assert_eq!(m.text, "x /ANON_PROJ_1");

        let mut s = StreamRestorer::new(Escape::Json);
        let mut out = String::new();
        for chunk in [
            "{\"path\": \"/srv/AN",
            "ON_PR",
            "OJ_",
            "1\", \"n\": 1",
            "0}",
        ] {
            out.push_str(&s.push(&e, &me, &v, chunk).unwrap());
            assert!(s.pending() <= e.max_hold());
        }
        out.push_str(&s.flush(&e, &me, &v).unwrap());
        assert_eq!(out, r#"{"path": "/srv/a\"b\\c", "n": 10}"#);
        assert_eq!(s.stats().restored, 1);
        assert_eq!(
            serde_json_like_unescape(&out),
            r#"{"path": "/srv/a"b\c", "n": 10}"#
        );
    }

    /// Находит email целиком (prior 0.9) — слабее, чем память сессии (hard).
    struct Email;

    impl Recognizer for Email {
        fn id(&self) -> &str {
            "email"
        }

        fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
            for (i, _) in input.text.match_indices("m.ivanov@zorg.ru") {
                out.push(Candidate::new(
                    i,
                    i + 16,
                    "EMAIL",
                    Category::Contact,
                    "email",
                    0.9,
                ));
            }
        }
    }

    #[test]
    fn session_value_does_not_fragment_longer_mask() {
        let e = Engine::builder([1; 32])
            .recognizer(Box::new(PathWord("zorg")))
            .recognizer(Box::new(Email))
            .build()
            .unwrap();
        let mut v = MemoryVault::new();
        let known = e
            .mask(&Input::text("/zorg"), &opts(vec![]), &mut v)
            .unwrap()
            .known;
        let m = e
            .mask(
                &Input::text("mail m.ivanov@zorg.ru, zorg"),
                &opts(known.clone()),
                &mut v,
            )
            .unwrap();
        assert_eq!(m.text, "mail ANON_EMAIL_1, ANON_PROJ_1");

        // Email проиграл более сильному кандидату, который пересекает его, но не покрывает
        // `zorg`, — известное значение всё равно маскируется.
        let e = Engine::builder([1; 32])
            .recognizer(Box::new(Email))
            .recognizer(Box::new(Strong("id m.ivanov")))
            .build()
            .unwrap();
        let m = e
            .mask(&Input::text("id m.ivanov@zorg.ru"), &opts(known), &mut v)
            .unwrap();
        // Внутри домена — DNS-форма токена.
        assert_eq!(m.text, "ANON_KEY_1@anon-proj-1.ru");
    }

    /// Жёсткий кандидат на точное вхождение.
    struct Strong(&'static str);

    impl Recognizer for Strong {
        fn id(&self) -> &str {
            "strong"
        }

        fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
            for (i, _) in input.text.match_indices(self.0) {
                out.push(
                    Candidate::new(
                        i,
                        i + self.0.len(),
                        "KEY",
                        Category::Credential,
                        "strong",
                        0.99,
                    )
                    .hard(),
                );
            }
        }
    }

    fn serde_json_like_unescape(s: &str) -> String {
        s.replace("\\\\", "\u{0}")
            .replace("\\\"", "\"")
            .replace('\u{0}', "\\")
    }

    #[test]
    fn interval_indexes_preserve_legacy_priority_and_keep_semantics() {
        let engine = Engine::builder([1; 32]).build().unwrap();
        let mut seed = 713_u64;
        let mut next = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            (seed >> 32) as usize
        };
        for _ in 0..12 {
            let scored: Vec<Scored> = (0..256)
                .map(|i| {
                    let start = next() % 1024;
                    let mut c = Candidate::new(
                        start,
                        start + 1 + next() % 90,
                        "PROJ",
                        Category::Infra,
                        if i % 4 == 0 { SESSION } else { "fixture" },
                        0.5,
                    );
                    if i % 7 == 0 {
                        c.polarity = Polarity::Keep;
                    }
                    c.hard = i % 3 == 0;
                    c.origin = match i % 5 {
                        0 => Origin::Builtin,
                        rank => Origin::Rule {
                            scope: match rank {
                                1 => Scope::Personal,
                                2 => Scope::Team,
                                _ => Scope::Global,
                            },
                            enforced: rank == 4,
                            id: i.to_string(),
                        },
                    };
                    Scored {
                        verdict: engine.verdict(&c),
                        c,
                    }
                })
                .collect();
            let index = KeepIndex::new(&scored);
            for s in &scored {
                let c = &s.c;
                let legacy = c.polarity == Polarity::Mask
                    && scored.iter().any(|k| {
                        let k = &k.c;
                        k.polarity == Polarity::Keep
                            && k.start <= c.start
                            && c.end <= k.end
                            && (k.origin.is_enforced()
                                || (!c.hard && k.origin.rank() >= c.origin.rank()))
                    });
                assert_eq!(index.covers(c), legacy);
            }
            // Оракул — исходный полный перебор пересечений до оптимизации.
            let mut masks: Vec<_> = (0..scored.len())
                .filter(|&i| scored[i].applied_mask(engine.policy.uncertain))
                .collect();
            masks.sort_by(|&a, &b| {
                let (x, y) = (&scored[a].c, &scored[b].c);
                y.origin
                    .rank()
                    .cmp(&x.origin.rank())
                    .then(y.hard.cmp(&x.hard))
                    .then(y.probability().total_cmp(&x.probability()))
                    .then(y.len().cmp(&x.len()))
                    .then(x.start.cmp(&y.start))
            });
            let (deferred, first): (Vec<_>, Vec<_>) = masks.iter().copied().partition(|&i| {
                let c = &scored[i].c;
                c.recognizer == SESSION
                    && masks.iter().any(|&j| {
                        let o = &scored[j].c;
                        o.recognizer != SESSION
                            && o.start <= c.start
                            && c.end <= o.end
                            && o.len() > c.len()
                    })
            });
            let mut legacy: Vec<usize> = Vec::new();
            for i in first.into_iter().chain(deferred) {
                let c = &scored[i].c;
                if !legacy
                    .iter()
                    .any(|&j| c.start < scored[j].c.end && scored[j].c.start < c.end)
                {
                    legacy.push(i);
                }
            }
            legacy.sort_by_key(|&i| scored[i].c.start);
            assert_eq!(engine.resolve_conflicts(&scored), legacy);
        }
    }
}
