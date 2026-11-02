//! Пакеты-данные: `pack.toml` + текстовые списки. Загружаются из каталога или встроены в бинарник.
//!
//! ```text
//! packs/lang-xx/
//! ├── pack.toml               # [pack] метаданные, [lexicon] inflections, [context], [keys]
//! ├── words.txt               # безопасные слова (через пробел или с новой строки, `#` — комментарий)
//! ├── tlds.txt, ambiguous_tlds.txt, suffixes.txt, internal_suffixes.txt, public_domains.txt
//! ├── names/given.txt         # имена; секции `[m]`, `[f]`, `[u]` задают род
//! ├── names/patterns.toml     # окончания фамилий и отчеств, обращения, частицы, веса признаков
//! ├── collisions.txt          # имена и фамилии, совпадающие с обычными словами
//! └── tests/corpus.jsonl      # {"text": "...", "mask": ["..."], "keep": ["..."]}
//! ```

use kaponir_core::{Gender, Lexicon, NameLexicon, Pack, PackInfo, name_key};
use serde::Deserialize;
use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::Arc;

pub mod corpus;

#[derive(Debug, thiserror::Error)]
pub enum PackError {
    #[error("{path}: {msg}")]
    Load { path: String, msg: String },
}

#[derive(Deserialize)]
struct Manifest {
    pack: PackInfo,
    #[serde(default)]
    lexicon: LexiconSection,
    #[serde(default)]
    context: HashMap<String, Vec<String>>,
    #[serde(default)]
    keys: HashMap<String, Vec<String>>,
}

#[derive(Default, Deserialize)]
struct LexiconSection {
    #[serde(default)]
    inflections: Vec<String>,
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct NamePatterns {
    #[serde(default)]
    titles: Vec<String>,
    #[serde(default)]
    particles: Vec<String>,
    #[serde(default)]
    surname: Gendered,
    #[serde(default)]
    patronymic: Gendered,
    #[serde(default)]
    weights: HashMap<String, f32>,
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Gendered {
    #[serde(default)]
    male: Vec<String>,
    #[serde(default)]
    female: Vec<String>,
    #[serde(default)]
    any: Vec<String>,
}

impl Gendered {
    fn list(self) -> Vec<(String, Option<Gender>)> {
        let tag = |v: Vec<String>, g| v.into_iter().map(move |s| (name_key(s.trim()), g));
        tag(self.male, Some(Gender::Male))
            .chain(tag(self.female, Some(Gender::Female)))
            .chain(tag(self.any, None))
            .collect()
    }
}

/// `names/given.txt`: имена через пробел или с новой строки; `[m]`, `[f]`, `[u]` — род следующих.
fn parse_given(s: &str) -> HashMap<String, Option<Gender>> {
    let mut out = HashMap::new();
    let mut gender = None;
    for line in s.lines() {
        let line = line.split('#').next().unwrap_or_default().trim();
        match line {
            "[m]" => gender = Some(Gender::Male),
            "[f]" => gender = Some(Gender::Female),
            "[u]" => gender = None,
            _ => {
                for w in line.split_whitespace() {
                    let k = name_key(w);
                    // Имя, которое есть в обоих списках (Саша, Женя), — без рода.
                    match out.get(&k) {
                        Some(g) if *g != gender => {
                            out.insert(k, None);
                        }
                        Some(_) => {}
                        None => {
                            out.insert(k, gender);
                        }
                    }
                }
            }
        }
    }
    out
}

pub struct DataPack {
    info: PackInfo,
    lexicon: Lexicon,
}

const LIST_FILES: &[&str] = &[
    "words.txt",
    "tlds.txt",
    "ambiguous_tlds.txt",
    "suffixes.txt",
    "internal_suffixes.txt",
    "public_domains.txt",
];

fn parse_list(s: &str) -> HashSet<String> {
    s.lines()
        .map(|l| l.split('#').next().unwrap_or_default())
        .flat_map(str::split_whitespace)
        .map(str::to_lowercase)
        .collect()
}

impl DataPack {
    /// `file(name)` возвращает содержимое файла пакета или `None`, если его нет.
    pub fn from_parts(
        origin: &str,
        manifest: &str,
        file: impl Fn(&str) -> Option<String>,
    ) -> Result<Self, PackError> {
        let err = |msg: String| PackError::Load {
            path: origin.to_string(),
            msg,
        };
        let m: Manifest = toml::from_str(manifest).map_err(|e| err(e.to_string()))?;
        let mut lex = Lexicon {
            inflections: m.lexicon.inflections,
            ..Lexicon::default()
        };
        for name in LIST_FILES {
            let Some(content) = file(name) else { continue };
            let set = parse_list(&content);
            let target = match *name {
                "words.txt" => &mut lex.words,
                "tlds.txt" => &mut lex.tlds,
                "ambiguous_tlds.txt" => &mut lex.ambiguous_tlds,
                "suffixes.txt" => &mut lex.suffixes,
                "internal_suffixes.txt" => &mut lex.internal_suffixes,
                _ => &mut lex.public_domains,
            };
            target.extend(set);
        }
        for (kind, words) in m.context {
            lex.context
                .entry(kind.to_ascii_uppercase())
                .or_default()
                .extend(words.into_iter().map(|w| w.to_lowercase()));
        }
        for (kind, words) in m.keys {
            for w in words {
                let norm: String = w
                    .to_lowercase()
                    .chars()
                    .filter(|c| c.is_alphanumeric())
                    .collect();
                lex.keys.insert(norm, kind.to_ascii_uppercase());
            }
        }
        lex.names = Self::names(&file).map_err(err)?;
        Ok(Self {
            info: m.pack,
            lexicon: lex,
        })
    }

    fn names(file: &impl Fn(&str) -> Option<String>) -> Result<NameLexicon, String> {
        let mut n = NameLexicon::default();
        if let Some(s) = file("names/given.txt") {
            n.given = parse_given(&s);
        }
        if let Some(s) = file("collisions.txt") {
            n.collisions = parse_list(&s).into_iter().map(|w| name_key(&w)).collect();
        }
        if let Some(s) = file("names/patterns.toml") {
            let p: NamePatterns =
                toml::from_str(&s).map_err(|e| format!("names/patterns.toml: {e}"))?;
            n.titles = p
                .titles
                .iter()
                .map(|t| name_key(t.trim_end_matches('.')))
                .collect();
            n.particles = p.particles.iter().map(|t| name_key(t)).collect();
            n.surname_suffixes = p.surname.list();
            n.patronymic_suffixes = p.patronymic.list();
            n.weights = p.weights;
        }
        Ok(n)
    }

    /// Метаданные и словарь — для пакетов-кода, которые надстраиваются над данными.
    pub fn into_parts(self) -> (PackInfo, Lexicon) {
        (self.info, self.lexicon)
    }

    pub fn from_dir(dir: &Path) -> Result<Self, PackError> {
        let manifest_path = dir.join("pack.toml");
        let manifest = std::fs::read_to_string(&manifest_path).map_err(|e| PackError::Load {
            path: manifest_path.display().to_string(),
            msg: e.to_string(),
        })?;
        Self::from_parts(&dir.display().to_string(), &manifest, |name| {
            std::fs::read_to_string(dir.join(name)).ok()
        })
    }
}

impl Pack for DataPack {
    fn info(&self) -> &PackInfo {
        &self.info
    }

    fn lexicon(&self) -> &Lexicon {
        &self.lexicon
    }
}

macro_rules! embedded {
    ($id:literal, [$($file:literal),*]) => {
        (
            $id,
            include_str!(concat!("../../../packs/", $id, "/pack.toml")),
            &[$(($file, include_str!(concat!("../../../packs/", $id, "/", $file)))),*] as &[(&str, &str)],
        )
    };
}

type Embedded = (
    &'static str,
    &'static str,
    &'static [(&'static str, &'static str)],
);

const EMBEDDED: &[Embedded] = &[
    embedded!(
        "lang-en",
        [
            "words.txt",
            "names/given.txt",
            "names/patterns.toml",
            "collisions.txt"
        ]
    ),
    embedded!(
        "lang-ru",
        [
            "words.txt",
            "names/given.txt",
            "names/patterns.toml",
            "collisions.txt"
        ]
    ),
    embedded!("domain-tech", ["words.txt"]),
    embedded!(
        "domain-web",
        [
            "tlds.txt",
            "ambiguous_tlds.txt",
            "suffixes.txt",
            "internal_suffixes.txt",
            "public_domains.txt"
        ]
    ),
];

/// Идентификаторы встроенных пакетов-данных.
pub fn builtin_ids() -> Vec<&'static str> {
    EMBEDDED.iter().map(|e| e.0).collect()
}

pub fn builtin(id: &str) -> Option<Arc<dyn Pack>> {
    Some(Arc::new(builtin_data(id)?))
}

/// Встроенный пакет-данные как значение — для пакетов-кода поверх него (`lang-ru`).
pub fn builtin_data(id: &str) -> Option<DataPack> {
    let (_, manifest, files) = EMBEDDED.iter().find(|e| e.0 == id)?;
    let pack = DataPack::from_parts(&format!("builtin:{id}"), manifest, |name| {
        files
            .iter()
            .find(|(n, _)| *n == name)
            .map(|(_, c)| (*c).to_string())
    })
    .expect("embedded packs are valid");
    Some(pack)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_packs_load() {
        for id in builtin_ids() {
            let p = builtin(id).unwrap();
            assert_eq!(p.info().id, id);
        }
        let en = builtin("lang-en").unwrap();
        assert!(en.lexicon().is_word("payments"));
        assert_eq!(en.lexicon().key_kind("secretaccesskey"), Some("SECRET"));
        let web = builtin("domain-web").unwrap();
        assert!(web.lexicon().tlds.contains("ru"));
        assert!(web.lexicon().public_domains.contains("github.com"));
    }
}
