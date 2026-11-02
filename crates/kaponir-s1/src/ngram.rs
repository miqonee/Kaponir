//! Локальная модель: логистическая регрессия на хешированных признаках L0 — символьных
//! триграммах, форме, длине, энтропии, доле цифр и заглавных, типе и детекторе кандидата.
//! Исходных строк модель не хранит: только веса корзин.

use kaponir_core::Category;
use kaponir_core::s1::{
    Calibration, CandidateView, Disclosure, Features, S1Error, S1Provider, features,
};
use kaponir_core::score::{logit, sigmoid};
use serde::{Deserialize, Serialize};
use std::path::Path;

use crate::Error;

pub const MODEL_FILE: &str = "ngram.json";
pub const PREV_FILE: &str = "ngram.prev.json";
pub const ID: &str = "ngram";

const FORMAT: u32 = 1;
const DIM: u32 = 1 << 16;
const EPOCHS: usize = 40;
const L2: f32 = 1e-4;

/// Минимум подтверждённых меток каждого класса.
pub const MIN_PER_CLASS: usize = 3;

/// Подтверждённая метка: значение с решением человека (`true` — маскировать).
#[derive(Clone, Debug)]
pub struct Sample {
    pub kind: Option<String>,
    pub category: Option<Category>,
    pub recognizer: Option<String>,
    pub features: Features,
    pub label: bool,
}

impl Sample {
    pub fn new(
        surface: &str,
        is_word: bool,
        kind: Option<&str>,
        category: Option<Category>,
        recognizer: Option<&str>,
        label: bool,
    ) -> Self {
        Self {
            kind: kind.map(String::from),
            category,
            recognizer: recognizer.map(String::from),
            features: features(surface, is_word),
            label,
        }
    }

    fn ids(&self) -> Vec<u32> {
        feature_ids(
            self.kind.as_deref(),
            self.category,
            self.recognizer.as_deref(),
            &self.features,
        )
    }
}

fn bucket(s: &str) -> u32 {
    let h = blake3::hash(s.as_bytes());
    u32::from_le_bytes(h.as_bytes()[..4].try_into().expect("4 bytes")) % DIM
}

/// Номера корзин признаков. Тип и детектор неизвестны для меток из правил — их признаки опускаются.
pub fn feature_ids(
    kind: Option<&str>,
    category: Option<Category>,
    recognizer: Option<&str>,
    f: &Features,
) -> Vec<u32> {
    let mut ids = vec![bucket("bias")];
    if let Some(k) = kind {
        ids.push(bucket(&format!("kind:{k}")));
    }
    if let Some(c) = category {
        ids.push(bucket(&format!("cat:{c:?}")));
    }
    if let Some(r) = recognizer {
        ids.push(bucket(&format!("rec:{r}")));
    }
    let shape: String = f.shape.chars().take(24).collect();
    ids.push(bucket(&format!("shape:{shape}")));
    ids.push(bucket(&format!("len:{}", f.len.min(48) / 4)));
    ids.push(bucket(&format!("ent:{}", (f.entropy * 2.0) as u32)));
    ids.push(bucket(&format!(
        "dig:{}",
        (f.digit_ratio * 4.0).round() as u32
    )));
    ids.push(bucket(&format!(
        "up:{}",
        (f.upper_ratio * 4.0).round() as u32
    )));
    if f.is_word {
        ids.push(bucket("word"));
    }
    ids.extend(
        f.trigrams
            .iter()
            .map(|t| t.wrapping_mul(0x9E37_79B1).rotate_left(7) % DIM),
    );
    ids.sort_unstable();
    ids.dedup();
    ids
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Metrics {
    pub samples: usize,
    pub accuracy: f32,
    /// Доля верных среди предсказанных «маскировать».
    pub precision: f32,
    /// Доля найденных среди тех, что нужно маскировать.
    pub recall: f32,
    pub log_loss: f32,
}

impl Metrics {
    fn of(probs: &[f32], labels: &[bool]) -> Self {
        let n = probs.len();
        let (mut tp, mut fp, mut fn_, mut ok, mut loss) = (0f32, 0f32, 0f32, 0f32, 0f32);
        for (&p, &y) in probs.iter().zip(labels) {
            let pred = p >= 0.5;
            ok += f32::from(u8::from(pred == y));
            tp += f32::from(u8::from(pred && y));
            fp += f32::from(u8::from(pred && !y));
            fn_ += f32::from(u8::from(!pred && y));
            let p = p.clamp(1e-4, 1.0 - 1e-4);
            loss -= if y { p.ln() } else { (1.0 - p).ln() };
        }
        let nf = n.max(1) as f32;
        Self {
            samples: n,
            accuracy: ok / nf,
            precision: if tp + fp > 0.0 { tp / (tp + fp) } else { 1.0 },
            recall: if tp + fn_ > 0.0 { tp / (tp + fn_) } else { 1.0 },
            log_loss: loss / nf,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Model {
    pub format: u32,
    /// Unix-время обучения — версия модели.
    pub trained_at: u64,
    pub positives: usize,
    pub negatives: usize,
    /// Калибровка Платта на предсказаниях вне обучающей части (k-fold).
    pub calibration: Calibration,
    /// Качество на отложенных частях (k-fold), уже с калибровкой.
    pub cv: Metrics,
    weights: Vec<(u32, f32)>,
    #[serde(skip)]
    dense: Vec<f32>,
}

impl Model {
    pub fn version(&self) -> u64 {
        self.trained_at
    }

    fn densify(&mut self) {
        let mut d = vec![0f32; DIM as usize];
        for &(i, w) in &self.weights {
            if let Some(slot) = d.get_mut(i as usize) {
                *slot = w;
            }
        }
        self.dense = d;
    }

    fn raw(&self, ids: &[u32]) -> f32 {
        ids.iter().map(|&i| self.dense[i as usize]).sum()
    }

    /// Вероятность без калибровки (калибрует ядро).
    pub fn predict_raw(&self, ids: &[u32]) -> f32 {
        sigmoid(self.raw(ids))
    }

    /// Качество на метках с калибровкой модели.
    pub fn evaluate(&self, samples: &[Sample]) -> Metrics {
        let probs: Vec<f32> = samples
            .iter()
            .map(|s| self.calibration.apply(self.predict_raw(&s.ids())))
            .collect();
        let labels: Vec<bool> = samples.iter().map(|s| s.label).collect();
        Metrics::of(&probs, &labels)
    }

    pub fn load(path: &Path) -> Result<Self, Error> {
        let p = path.display().to_string();
        let s = std::fs::read_to_string(path).map_err(|source| Error::Io {
            path: p.clone(),
            source,
        })?;
        let mut m: Model = serde_json::from_str(&s).map_err(|source| Error::Json {
            path: p.clone(),
            source,
        })?;
        if m.format != FORMAT {
            return Err(Error::Msg(format!(
                "{p}: model format {} is not supported (expected {FORMAT})",
                m.format
            )));
        }
        if !m.calibration.a.is_finite()
            || m.calibration.a <= 0.0
            || !m.calibration.b.is_finite()
            || m.weights.iter().any(|(i, w)| *i >= DIM || !w.is_finite())
        {
            return Err(Error::Msg(format!(
                "{p}: invalid model weights or calibration"
            )));
        }
        m.densify();
        Ok(m)
    }

    fn save(&self, path: &Path) -> Result<(), Error> {
        let p = path.display().to_string();
        let tmp = path.with_extension("json.tmp");
        let json = serde_json::to_string(self).map_err(|source| Error::Json {
            path: p.clone(),
            source,
        })?;
        std::fs::write(&tmp, json).map_err(|source| Error::Io {
            path: p.clone(),
            source,
        })?;
        std::fs::rename(&tmp, path).map_err(|source| Error::Io { path: p, source })
    }

    /// Делает модель активной; предыдущая сохраняется для отката.
    pub fn install(&self, dir: &Path) -> Result<(), Error> {
        std::fs::create_dir_all(dir).map_err(|source| Error::Io {
            path: dir.display().to_string(),
            source,
        })?;
        let cur = dir.join(MODEL_FILE);
        let next = dir.join("ngram.next.json");
        self.save(&next)?;
        if cur.exists() {
            let backup = dir.join("ngram.prev.json.tmp");
            std::fs::copy(&cur, &backup).map_err(|source| Error::Io {
                path: cur.display().to_string(),
                source,
            })?;
            std::fs::rename(&backup, dir.join(PREV_FILE)).map_err(|source| Error::Io {
                path: backup.display().to_string(),
                source,
            })?;
        }
        std::fs::rename(&next, &cur).map_err(|source| Error::Io {
            path: cur.display().to_string(),
            source,
        })
    }
}

/// Меняет местами активную и предыдущую модели.
pub fn rollback(dir: &Path) -> Result<(), Error> {
    let (cur, prev) = (dir.join(MODEL_FILE), dir.join(PREV_FILE));
    if !prev.exists() {
        return Err(Error::Msg(format!(
            "no previous model at {}",
            prev.display()
        )));
    }
    Model::load(&prev)?;
    let io = |path: &Path| {
        let path = path.display().to_string();
        move |source| Error::Io { path, source }
    };
    let tmp = dir.join("ngram.swap.json");
    if cur.exists() {
        std::fs::rename(&cur, &tmp).map_err(io(&cur))?;
    }
    std::fs::rename(&prev, &cur).map_err(io(&prev))?;
    if tmp.exists() {
        std::fs::rename(&tmp, &prev).map_err(io(&tmp))?;
    }
    Ok(())
}

impl S1Provider for Model {
    fn id(&self) -> &str {
        ID
    }

    fn max_disclosure(&self) -> Disclosure {
        Disclosure::L0
    }

    fn score(&self, batch: &[CandidateView]) -> Result<Vec<f32>, S1Error> {
        Ok(batch
            .iter()
            .map(|v| {
                self.predict_raw(&feature_ids(
                    Some(&v.kind),
                    Some(v.category),
                    Some(&v.recognizer),
                    &v.features,
                ))
            })
            .collect())
    }
}

/// Детерминированный генератор для перемешивания (одинаковые метки ⇒ одинаковая модель).
struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0 >> 33
    }

    fn shuffle<T>(&mut self, v: &mut [T]) {
        for i in (1..v.len()).rev() {
            let j = (self.next() % (i as u64 + 1)) as usize;
            v.swap(i, j);
        }
    }
}

/// SGD с весами классов (меток «маскировать» обычно больше) и L2.
fn fit(data: &[(Vec<u32>, bool)]) -> Vec<f32> {
    let mut w = vec![0f32; DIM as usize];
    let pos = data.iter().filter(|d| d.1).count().max(1) as f32;
    let neg = data.iter().filter(|d| !d.1).count().max(1) as f32;
    let n = data.len() as f32;
    let (wp, wn) = (n / (2.0 * pos), n / (2.0 * neg));
    let mut order: Vec<usize> = (0..data.len()).collect();
    let mut rng = Lcg(0x5EED);
    for epoch in 0..EPOCHS {
        rng.shuffle(&mut order);
        let lr = 0.3 / (1.0 + epoch as f32 * 0.1);
        for &i in &order {
            let (ids, y) = &data[i];
            let p = sigmoid(ids.iter().map(|&j| w[j as usize]).sum());
            let g = (p - f32::from(u8::from(*y))) * if *y { wp } else { wn };
            for &j in ids {
                let wj = &mut w[j as usize];
                *wj -= lr * (g + L2 * *wj);
            }
        }
    }
    w
}

/// Калибровка Платта: `σ(a·logit(p) + b)` по предсказаниям вне обучающей части.
fn platt(probs: &[f32], labels: &[bool]) -> Calibration {
    let xs: Vec<f32> = probs.iter().map(|&p| logit(p)).collect();
    let (mut a, mut b) = (1.0f32, 0.0f32);
    let n = xs.len().max(1) as f32;
    for _ in 0..3000 {
        let (mut ga, mut gb) = (0f32, 0f32);
        for (&x, &y) in xs.iter().zip(labels) {
            let d = sigmoid(a * x + b) - f32::from(u8::from(y));
            ga += d * x;
            gb += d;
        }
        a -= 0.05 * ga / n;
        b -= 0.05 * gb / n;
        a = a.clamp(0.05, 10.0);
        b = b.clamp(-10.0, 10.0);
    }
    Calibration { a, b }
}

/// Обучает модель на подтверждённых метках; качество — по k-fold на тех же метках.
pub fn train(samples: &[Sample], now: u64) -> Result<Model, Error> {
    let positives = samples.iter().filter(|s| s.label).count();
    let negatives = samples.len() - positives;
    if positives < MIN_PER_CLASS || negatives < MIN_PER_CLASS {
        return Err(Error::Msg(format!(
            "need at least {MIN_PER_CLASS} confirmed labels of each class, have {positives} mask / {negatives} keep: \
             decide journal entries with `kaponir review` or add value rules"
        )));
    }
    let data: Vec<(Vec<u32>, bool)> = samples.iter().map(|s| (s.ids(), s.label)).collect();
    let labels: Vec<bool> = data.iter().map(|d| d.1).collect();

    // k-fold: каждая метка предсказывается моделью, которая её не видела.
    let k = 5.min(positives.min(negatives));
    let mut oof = vec![0.5f32; data.len()];
    let mut order: Vec<usize> = (0..data.len()).collect();
    Lcg(0xF01D).shuffle(&mut order);
    for fold in 0..k {
        let (test, train): (Vec<usize>, Vec<usize>) = order
            .iter()
            .partition(|&&i| order.iter().position(|&x| x == i).unwrap_or(0) % k == fold);
        let part: Vec<(Vec<u32>, bool)> = train.iter().map(|&i| data[i].clone()).collect();
        let w = fit(&part);
        for i in test {
            oof[i] = sigmoid(data[i].0.iter().map(|&j| w[j as usize]).sum());
        }
    }
    let calibration = platt(&oof, &labels);
    let calibrated: Vec<f32> = oof.iter().map(|&p| calibration.apply(p)).collect();
    let cv = Metrics::of(&calibrated, &labels);

    let w = fit(&data);
    let weights = w
        .iter()
        .enumerate()
        .filter(|(_, x)| x.abs() > 1e-6)
        .map(|(i, &x)| (i as u32, x))
        .collect();
    let mut m = Model {
        format: FORMAT,
        trained_at: now,
        positives,
        negatives,
        calibration,
        cv,
        weights,
        dense: Vec::new(),
    };
    m.densify();
    Ok(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn samples() -> Vec<Sample> {
        let mut out = Vec::new();
        for v in [
            "zorg",
            "zorgpay",
            "zorg-core",
            "sberzorg",
            "zorgbank",
            "zorg-billing",
            "corezorg",
            "zorgs",
        ] {
            out.push(Sample::new(
                v,
                false,
                Some("PROJ"),
                Some(Category::Infra),
                Some("paths"),
                true,
            ));
        }
        for v in [
            "release",
            "staging",
            "values",
            "charts",
            "manifests",
            "templates",
            "configs",
            "helpers",
        ] {
            out.push(Sample::new(
                v,
                true,
                Some("PROJ"),
                Some(Category::Infra),
                Some("paths"),
                false,
            ));
        }
        out
    }

    #[test]
    fn learns_and_calibrates() {
        let m = train(&samples(), 1).unwrap();
        assert!(m.cv.accuracy >= 0.75, "{:?}", m.cv);
        let probe = |v: &str, w: bool| {
            let s = Sample::new(
                v,
                w,
                Some("PROJ"),
                Some(Category::Infra),
                Some("paths"),
                false,
            );
            m.calibration.apply(m.predict_raw(&s.ids()))
        };
        assert!(
            probe("zorgmail", false) > 0.6,
            "{}",
            probe("zorgmail", false)
        );
        assert!(probe("secrets", true) < 0.4, "{}", probe("secrets", true));
        // Детерминированность: одинаковые метки — одинаковая модель.
        assert_eq!(
            serde_json::to_string(&m).unwrap(),
            serde_json::to_string(&train(&samples(), 1).unwrap()).unwrap()
        );
    }

    #[test]
    fn install_and_rollback() {
        let dir = std::env::temp_dir().join(format!("kaponir-s1-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let a = train(&samples(), 100).unwrap();
        let b = train(&samples(), 200).unwrap();
        a.install(&dir).unwrap();
        b.install(&dir).unwrap();
        assert_eq!(Model::load(&dir.join(MODEL_FILE)).unwrap().version(), 200);
        rollback(&dir).unwrap();
        assert_eq!(Model::load(&dir.join(MODEL_FILE)).unwrap().version(), 100);
        assert_eq!(Model::load(&dir.join(PREV_FILE)).unwrap().version(), 200);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn refuses_too_few_labels() {
        assert!(train(&samples()[..4], 1).is_err());
    }

    #[test]
    fn failed_install_preserves_active_model_and_invalid_models_cannot_be_loaded() {
        let dir =
            std::env::temp_dir().join(format!("kaponir-s1-failure-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let a = train(&samples(), 100).unwrap();
        a.install(&dir).unwrap();
        // Ошибка сохранения резервной версии не должна удалять активную.
        std::fs::create_dir(dir.join(PREV_FILE)).unwrap();
        assert!(train(&samples(), 200).unwrap().install(&dir).is_err());
        assert_eq!(Model::load(&dir.join(MODEL_FILE)).unwrap().version(), 100);
        std::fs::remove_dir(dir.join(PREV_FILE)).unwrap();
        let mut invalid = a;
        invalid.calibration.a = -1.0;
        invalid.save(&dir.join(PREV_FILE)).unwrap();
        assert!(rollback(&dir).is_err());
        assert_eq!(Model::load(&dir.join(MODEL_FILE)).unwrap().version(), 100);
        invalid.calibration.a = 1.0;
        invalid.weights.push((DIM, 1.0));
        invalid.save(&dir.join(PREV_FILE)).unwrap();
        assert!(Model::load(&dir.join(PREV_FILE)).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
}
