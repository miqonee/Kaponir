use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use crate::types::Category;

pub fn logit(p: f32) -> f32 {
    let p = p.clamp(1e-4, 1.0 - 1e-4);
    (p / (1.0 - p)).ln()
}

pub fn sigmoid(x: f32) -> f32 {
    1.0 / (1.0 + (-x).exp())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Verdict {
    Mask,
    Keep,
    Uncertain,
}

/// `p ≥ mask` → MASK, `p ≤ keep` → KEEP, иначе UNCERTAIN.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Thresholds {
    pub mask: f32,
    pub keep: f32,
}

impl Thresholds {
    pub fn verdict(&self, p: f32) -> Verdict {
        if p >= self.mask {
            Verdict::Mask
        } else if p <= self.keep {
            Verdict::Keep
        } else {
            Verdict::Uncertain
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UncertainAction {
    #[default]
    Mask,
    Keep,
}

/// Стратегия подстановки.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Strategy {
    /// `ANON_SVC_1` — нумерованная переменная.
    #[default]
    Variable,
    /// `ANON_SVC_h8f2a1c` — детерминированный HMAC, одинаков между машинами.
    Hmac,
    /// Сохраняющий формат суррогат (IPv4 → 240.0.0.0/4).
    Surrogate,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Policy {
    pub thresholds: HashMap<Category, Thresholds>,
    pub uncertain: UncertainAction,
    pub default_strategy: Strategy,
    /// kind → стратегия.
    pub strategies: HashMap<String, Strategy>,
}

impl Default for Policy {
    fn default() -> Self {
        let t = |mask, keep| Thresholds { mask, keep };
        let thresholds = HashMap::from([
            (Category::Credential, t(0.5, 0.02)),
            (Category::Network, t(0.6, 0.2)),
            (Category::Infra, t(0.7, 0.3)),
            (Category::Identity, t(0.7, 0.2)),
            (Category::Financial, t(0.7, 0.2)),
            (Category::Contact, t(0.7, 0.2)),
            (Category::Custom, t(0.5, 0.1)),
        ]);
        Self {
            thresholds,
            uncertain: UncertainAction::Mask,
            default_strategy: Strategy::Variable,
            strategies: HashMap::from([("IP".to_string(), Strategy::Surrogate)]),
        }
    }
}

impl Policy {
    pub fn thresholds(&self, category: Category) -> Thresholds {
        self.thresholds
            .get(&category)
            .copied()
            .unwrap_or(Thresholds {
                mask: 0.5,
                keep: 0.1,
            })
    }

    pub fn strategy(&self, kind: &str) -> Strategy {
        self.strategies
            .get(kind)
            .copied()
            .unwrap_or(self.default_strategy)
    }
}
