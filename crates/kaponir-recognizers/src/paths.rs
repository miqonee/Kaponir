//! Пути файловой системы (Unix, Windows, относительные). Системные каталоги, тех-термины,
//! расширения и хеши сохраняются; имя пользователя после `/home` / `C:\Users` и неизвестные
//! части сегментов становятся слотами: `/home/ANON_USER_1/ANON_PROJ_1/k8s/prod-values.yaml`.

use kaponir_core::lexicon::split_parts;
use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Lexicon, PartClass, Recognizer};
use regex::Regex;

use crate::{Slot, char_before};

const ID: &str = "paths";

pub struct Paths {
    unix: Regex,
    relative: Regex,
    windows: Regex,
}

impl Default for Paths {
    fn default() -> Self {
        Self::new()
    }
}

impl Paths {
    pub fn new() -> Self {
        let seg = r"[\p{L}\p{N}._@%+=,~\-]+";
        Self {
            unix: Regex::new(&format!(r"(?:~|\.\.?)?(?:/{seg})+/?")).expect("valid regex"),
            relative: Regex::new(&format!(r"{seg}(?:/{seg})+/?")).expect("valid regex"),
            windows: Regex::new(r#"(?i)\b[a-z]:\\(?:[^\\\s:*?"<>|\r\n]+\\?)+"#)
                .expect("valid regex"),
        }
    }
}

fn boundary_ok(text: &str, start: usize) -> bool {
    match char_before(text, start) {
        None => true,
        Some(c) => c.is_whitespace() || "\"'`=([{,:>|;".contains(c),
    }
}

fn segments(
    lex: &Lexicon,
    text: &str,
    start: usize,
    end: usize,
    sep: char,
    out: &mut Vec<Candidate>,
) {
    let proj = Slot {
        kind: "PATH",
        category: Category::Infra,
        prior: 0.6,
        dns: false,
        recognizer: ID,
    };
    let path = &text[start..end];
    let mut pos = start;
    let mut prev: Option<String> = None;
    for (idx, seg) in path.split(sep).enumerate() {
        let (a, b) = (pos, pos + seg.len());
        pos = b + sep.len_utf8();
        if seg.is_empty() || seg == "~" || seg == "." || seg == ".." {
            continue;
        }
        let home = prev.as_deref().is_some_and(|p| p == "home" || p == "users") && idx <= 3;
        if home {
            if lex.classify(seg) == PartClass::Unknown
                || lex.classify(seg) == PartClass::Opaque && seg.len() > 2
            {
                out.push(
                    Candidate::new(a, b, "USER", Category::Identity, ID, 0.95)
                        .with_evidence("home-dir", 1.0),
                );
            }
        } else if let Some((s, e)) = segment_slot(lex, text, a, b) {
            let mut candidate = Candidate::new(s, e, proj.kind, proj.category, ID, proj.prior)
                .with_evidence("unknown-part", 0.0);
            // A server/home directory provides evidence beyond an unknown word.
            let lower = path.to_ascii_lowercase();
            if ["/srv/", "/home/", "/opt/"]
                .iter()
                .any(|root| lower.starts_with(root))
                || (sep == '\\' && lower.starts_with(r"users\"))
            {
                candidate = candidate.with_evidence("private-path-root", 0.2);
            }
            out.push(candidate);
        }
        prev = Some(seg.to_lowercase());
    }
}

/// Сегмент пути с неизвестной частью — это имя проекта или файла целиком (`sber-pay-core`,
/// а не только `sber`): маскируется весь, кроме расширения и хешей/версий по краям.
fn segment_slot(lex: &Lexicon, text: &str, a: usize, b: usize) -> Option<(usize, usize)> {
    let seg = &text[a..b];
    let mut stem_end = b;
    if let Some(dot) = seg.rfind('.').filter(|&d| d > 0) {
        let ext = &seg[dot + 1..];
        if !ext.is_empty() && ext.len() <= 6 && lex.classify(ext) != PartClass::Unknown {
            stem_end = a + dot;
        }
    }
    let parts = split_parts(&text[a..stem_end], a);
    if !parts
        .iter()
        .any(|&(s, e)| lex.classify(&text[s..e]) == PartClass::Unknown)
    {
        return None;
    }
    let core = |&&(s, e): &&(usize, usize)| lex.classify(&text[s..e]) != PartClass::Opaque;
    let first = parts.iter().find(core)?;
    let last = parts.iter().rev().find(core)?;
    Some((first.0, last.1))
}

/// Первый сегмент относительного пути похож на хост — путь разберёт распознаватель доменов.
fn looks_like_host(lex: &Lexicon, seg: &str) -> bool {
    seg.contains('.')
        && seg.rsplit('.').next().is_some_and(|tld| {
            let t = tld.to_lowercase();
            lex.tlds.contains(&t) && !lex.ambiguous_tlds.contains(&t)
        })
}

const KNOWN_DIRS: &[&str] = &[
    "src",
    "lib",
    "bin",
    "pkg",
    "cmd",
    "app",
    "etc",
    "var",
    "opt",
    "srv",
    "usr",
    "home",
    "users",
    "k8s",
    "deploy",
    "config",
    "conf",
    "crates",
    "services",
    "internal",
    "test",
    "tests",
    "docs",
    "scripts",
    "assets",
    "log",
    "logs",
    "api",
    "core",
    "web",
    "ui",
    "db",
    "migrations",
    "helm",
    "charts",
    "terraform",
    "ansible",
    "docker",
];

const KNOWN_EXTS: &[&str] = &[
    "rs",
    "py",
    "js",
    "ts",
    "tsx",
    "jsx",
    "go",
    "java",
    "kt",
    "c",
    "cpp",
    "h",
    "hpp",
    "cs",
    "rb",
    "php",
    "swift",
    "scala",
    "sh",
    "bash",
    "zsh",
    "ps1",
    "yaml",
    "yml",
    "json",
    "toml",
    "ini",
    "cfg",
    "conf",
    "env",
    "xml",
    "html",
    "css",
    "sql",
    "md",
    "txt",
    "csv",
    "tsv",
    "pem",
    "key",
    "crt",
    "cer",
    "lock",
    "dockerfile",
    " proto",
    "graphql",
    "tf",
    "hcl",
];

fn has_file_extension(lex: &Lexicon, seg: &str) -> bool {
    let Some(dot) = seg.rfind('.').filter(|&d| d > 0) else {
        return false;
    };
    let ext = seg[dot + 1..].to_ascii_lowercase();
    !ext.is_empty()
        && ext.len() <= 10
        && ext.chars().all(|c| c.is_ascii_alphanumeric())
        && (KNOWN_EXTS.contains(&ext.as_str()) || lex.classify(&ext) != PartClass::Unknown)
}

/// Отличает реальный относительный путь (`zorgproj/src/main.rs`, `my-svc/k8s`)
/// от обычной речи со слэшем (`сервер/клиент`, `демон/клиент`, `client/server`).
fn looks_like_relative_path(lex: &Lexicon, raw: &str) -> bool {
    // В относительных путях без `./`, `../`, `~/` кириллица и не-ASCII буквы — это естественная речь («или»).
    if !raw.is_ascii() {
        return false;
    }
    let segs: Vec<&str> = raw.split('/').filter(|s| !s.is_empty()).collect();
    if segs.len() < 2 {
        return false;
    }
    let has_ext = segs
        .last()
        .is_some_and(|last| has_file_extension(lex, last));
    let has_known_dir = segs.iter().any(|s| {
        let low = s.to_ascii_lowercase();
        KNOWN_DIRS.contains(&low.as_str()) || lex.is_word(&low)
    });
    if segs.len() >= 3 {
        return has_ext || has_known_dir || segs.iter().any(|s| s.contains(['-', '_', '.']));
    }
    // Для ровно 2 сегментов (`a/b`) требуем файловое расширение (`dir/file.rs`)
    // или сочетание известного каталога с техническим именем (`my-proj/k8s`, `src/zorgproj`).
    if has_ext {
        return true;
    }
    let has_tech_punct = segs.iter().any(|s| s.contains(['-', '_']));
    has_known_dir && has_tech_punct
}

impl Recognizer for Paths {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        let lex = ctx.lexicon;
        let mut covered: Vec<(usize, usize)> = Vec::new();
        for m in self.unix.find_iter(text) {
            if !boundary_ok(text, m.start()) || text[m.end()..].starts_with("//") {
                continue;
            }
            // `https://` уже разобран распознавателем URL.
            if text[..m.start()].ends_with(':') && m.as_str().starts_with("//") {
                continue;
            }
            let raw = m.as_str();
            let seg_count = raw
                .split('/')
                .filter(|s| !s.is_empty() && *s != "~" && *s != "." && *s != "..")
                .count();
            // Одиночный `/слово` без префикса `./` или второго сегмента (особенно кириллицей) —
            // это слэш с пробелом в тексте («сервер /клиент»), а не путь от корня ФС.
            let has_dot_or_tilde =
                raw.starts_with("./") || raw.starts_with("../") || raw.starts_with("~/");
            if !has_dot_or_tilde && (seg_count < 2 || !raw.is_ascii()) {
                continue;
            }
            segments(lex, text, m.start(), m.end(), '/', out);
            covered.push((m.start(), m.end()));
        }
        for m in self.relative.find_iter(text) {
            if covered.iter().any(|&(s, e)| m.start() < e && s < m.end())
                || !boundary_ok(text, m.start())
            {
                continue;
            }
            let first = m.as_str().split('/').next().unwrap_or_default();
            if looks_like_host(lex, first)
                || text[m.end()..].starts_with("://")
                || text[m.start()..].contains("://") && text[m.start()..m.end()].ends_with(':')
                || !looks_like_relative_path(lex, m.as_str())
            {
                continue;
            }
            segments(lex, text, m.start(), m.end(), '/', out);
        }
        for m in self.windows.find_iter(text) {
            let s = m.start() + 3;
            if s < m.end() {
                segments(lex, text, s, m.end(), '\\', out);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex() -> Lexicon {
        let mut l = Lexicon::default();
        for w in [
            "home",
            "k8s",
            "prod",
            "values",
            "yaml",
            "etc",
            "nginx",
            "conf",
            "src",
            "main",
            "users",
            "documents",
            "core",
            "pay",
            "var",
            "log",
            "srv",
            "api",
        ] {
            l.words.insert(w.into());
        }
        l.tlds.insert("ru".into());
        l
    }

    fn found(text: &str) -> Vec<(String, String)> {
        let l = lex();
        let mut out = Vec::new();
        Paths::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        out.into_iter()
            .map(|c| (c.kind.clone(), text[c.start..c.end].to_string()))
            .collect()
    }

    #[test]
    fn home_and_project() {
        let f = found("cat /home/mgoloshanov/sber-pay-core/k8s/prod-values.yaml");
        assert_eq!(
            f,
            vec![
                ("USER".to_string(), "mgoloshanov".to_string()),
                ("PATH".to_string(), "sber-pay-core".to_string())
            ]
        );
    }

    #[test]
    fn file_name_keeps_extension_and_hash() {
        let f = found("/srv/zorg-api-7d9f8b.yaml and /etc/nginx/zorg.conf");
        assert_eq!(
            f,
            vec![
                ("PATH".to_string(), "zorg-api".to_string()),
                ("PATH".to_string(), "zorg".to_string())
            ]
        );
    }

    #[test]
    fn windows_and_relative() {
        let f = found(r"C:\Users\ivanov\Documents\zorgproj\main.rs and zorgproj/src/main.rs");
        assert!(f.contains(&("USER".into(), "ivanov".into())), "{f:?}");
        assert_eq!(
            f.iter()
                .filter(|(k, v)| k == "PATH" && v == "zorgproj")
                .count(),
            2,
            "{f:?}"
        );
    }

    #[test]
    fn system_paths_untouched() {
        assert!(found("/etc/nginx/nginx.conf and /var/log").is_empty());
    }

    #[test]
    fn russian_and_prose_slashes_are_not_projects() {
        let f = found(
            "Архитектура сервер/клиент и демон/клиент, а также ввод/вывод или daemon/client.",
        );
        assert!(
            f.is_empty(),
            "Prose slashes must not be detected as PROJ: {f:?}"
        );
    }
}
