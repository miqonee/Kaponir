use std::sync::Arc;

use crate::lexicon::Lexicon;
use crate::pack::Pack;
use crate::types::{Candidate, Input};

/// Общий контекст анализа: объединённый словарь пакетов и правил, сами пакеты (морфология).
pub struct AnalyzeCtx<'a> {
    pub lexicon: &'a Lexicon,
    pub packs: &'a [Arc<dyn Pack>],
}

impl<'a> AnalyzeCtx<'a> {
    pub fn new(lexicon: &'a Lexicon) -> Self {
        Self {
            lexicon,
            packs: &[],
        }
    }
}

/// Распознаватель находит кандидатов и ничего не решает: решение принимает конвейер.
pub trait Recognizer: Send + Sync {
    fn id(&self) -> &str;
    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>);
}
