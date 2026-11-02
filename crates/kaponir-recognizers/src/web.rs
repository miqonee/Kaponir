//! Домены, URL и email. Технические поддомены и публичные домены сохраняются, корпоративное ядро
//! и неизвестные метки становятся слотами: `harbor.infra.mycorp.ru` → `harbor.anon-sub-1.anon-dom-1.ru`.

use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Lexicon, Recognizer};
use regex::Regex;
use std::net::IpAddr;

use crate::{
    Slot, char_after, char_before, is_ident, is_placeholder, key_kind_class, kind_for_key,
    push_slots,
};

const ID: &str = "web";

pub struct Web {
    url: Regex,
    host: Regex,
    email: Regex,
}

impl Default for Web {
    fn default() -> Self {
        Self::new()
    }
}

impl Web {
    pub fn new() -> Self {
        let label = r"[\p{L}\p{N}](?:[\p{L}\p{N}\-]{0,61}[\p{L}\p{N}])?";
        Self {
            url: Regex::new(r#"(?i)\b([a-z][a-z0-9+.\-]{1,15})://([^\s"'<>`]+)"#)
                .expect("valid regex"),
            host: Regex::new(&format!(r"{label}(?:\.{label})+")).expect("valid regex"),
            email: Regex::new(&format!(r"[\p{{L}}\p{{N}}._%+\-]+@({label}(?:\.{label})+)"))
                .expect("valid regex"),
        }
    }
}

fn overlaps(spans: &[(usize, usize)], a: usize, b: usize) -> bool {
    spans.iter().any(|&(s, e)| a < e && s < b)
}

/// Разбор хоста. Возвращает `false`, если строка не похожа на хост (нет известного TLD).
fn host(
    lex: &Lexicon,
    text: &str,
    hs: usize,
    he: usize,
    in_url: bool,
    out: &mut Vec<Candidate>,
) -> bool {
    let raw = &text[hs..he];
    if raw.parse::<IpAddr>().is_ok() {
        return true;
    }
    let lower = raw.to_lowercase();
    let mut labels: Vec<(usize, usize)> = Vec::new();
    let mut s = hs;
    for part in raw.split('.') {
        labels.push((s, s + part.len()));
        s += part.len() + 1;
    }
    let n = labels.len();
    let names: Vec<&str> = lower.split('.').collect();
    let join = |k: usize| names[n - k..].join(".");
    let sub = |kind, prior| Slot {
        kind,
        category: Category::Infra,
        prior,
        dns: true,
        recognizer: ID,
    };

    if let Some(k) = (1..=n)
        .rev()
        .find(|&k| lex.internal_suffixes.contains(&join(k)))
    {
        for &(a, b) in &labels[..n - k] {
            push_slots(lex, text, a, b, &sub("HOST", 0.75), out);
        }
        return true;
    }
    let suffix_k = (1..n)
        .rev()
        .find(|&k| lex.suffixes.contains(&join(k)))
        .or_else(|| lex.tlds.contains(names[n - 1]).then_some(1));
    let Some(k) = suffix_k else {
        if in_url {
            for &(a, b) in &labels {
                push_slots(lex, text, a, b, &sub("HOST", 0.7), out);
            }
            return true;
        }
        return false;
    };
    if n <= k {
        return false;
    }
    if !in_url && n == 2 && k == 1 && lex.ambiguous_tlds.contains(names[1]) {
        return false;
    }
    let reg = n - k - 1;
    let registrable = join(k + 1);
    let corp = lex
        .corp_domains
        .iter()
        .any(|d| lower == *d || lower.ends_with(&format!(".{d}")));
    let public =
        !corp && (lex.public_domains.contains(&registrable) || lex.public_domains.contains(&lower));
    let (dom_prior, sub_prior) = if corp {
        (0.99, 0.9)
    } else if public {
        (0.0, 0.6)
    } else {
        (0.65, 0.65)
    };
    if !public {
        let (a, b) = labels[reg];
        out.push(
            Candidate::new(a, b, "DOM", Category::Infra, ID, dom_prior)
                .dns()
                .canonical(text[a..b].to_lowercase())
                .with_evidence(
                    if corp {
                        "corp-domain"
                    } else {
                        "unknown-domain"
                    },
                    0.0,
                ),
        );
    }
    for &(a, b) in &labels[..reg] {
        push_slots(lex, text, a, b, &sub("SUB", sub_prior), out);
    }
    true
}

/// Сегменты пути URL / образа контейнера / scp-пути.
fn url_path(lex: &Lexicon, text: &str, start: usize, end: usize, out: &mut Vec<Candidate>) {
    let slot = Slot {
        kind: "PATH",
        category: Category::Infra,
        prior: 0.6,
        dns: false,
        recognizer: ID,
    };
    let path = &text[start..end];
    let (p, query) = match path.find(['?', '#']) {
        Some(i) => (&path[..i], Some(i)),
        None => (path, None),
    };
    let mut pos = start;
    for seg in p.split('/') {
        if !seg.is_empty() {
            push_slots(lex, text, pos, pos + seg.len(), &slot, out);
        }
        pos += seg.len() + 1;
    }
    let Some(qi) = query else {
        return;
    };
    let qstart = start + qi + 1;
    let q = &text[qstart..end];
    let q = q.split('#').next().unwrap_or_default();
    let mut pos = qstart;
    for pair in q.split('&') {
        if let Some((k, v)) = pair.split_once('=')
            && let Some(kind) = kind_for_key(lex, k)
            && !is_placeholder(v)
        {
            let (category, hard, prior) = key_kind_class(kind);
            let vs = pos + k.len() + 1;
            let mut c = Candidate::new(vs, vs + v.len(), kind, category, ID, prior)
                .with_evidence(format!("query-key:{k}"), 1.0);
            if hard {
                c = c.hard();
            }
            out.push(c);
        }
        pos += pair.len() + 1;
    }
}

fn trim_trailing(s: &str) -> usize {
    let mut end = s.len();
    while let Some(c) = s[..end].chars().next_back() {
        if ".,;:!?)]}'\"*".contains(c) {
            end -= c.len_utf8();
        } else {
            break;
        }
    }
    end
}

/// Длина «хвоста пути» после хоста: до пробела, кавычки или скобки.
fn path_tail(text: &str, from: usize) -> usize {
    let rest = &text[from..];
    let len = rest
        .find(|c: char| c.is_whitespace() || "\"'<>`()[]{}|,;".contains(c))
        .unwrap_or(rest.len());
    trim_trailing(&rest[..len])
}

impl Recognizer for Web {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        let lex = ctx.lexicon;
        let mut covered: Vec<(usize, usize)> = Vec::new();

        for cap in self.email.captures_iter(text) {
            let m = cap.get(0).expect("match");
            let dom = cap.get(1).expect("domain");
            if char_before(text, m.start()).is_some_and(|c| is_ident(c) || c == '/' || c == ':') {
                continue;
            }
            let local = &text[m.start()..dom.start() - 1];
            if local.eq_ignore_ascii_case("git") || local.starts_with('.') {
                continue;
            }
            let domain = dom.as_str().to_lowercase();
            if !lex
                .tlds
                .contains(domain.rsplit('.').next().unwrap_or_default())
            {
                continue;
            }
            let corp = lex
                .corp_domains
                .iter()
                .any(|d| domain == *d || domain.ends_with(&format!(".{d}")));
            let prior = if corp { 0.97 } else { 0.9 };
            out.push(
                Candidate::new(m.start(), m.end(), "EMAIL", Category::Contact, ID, prior)
                    .canonical(m.as_str().to_lowercase()),
            );
            covered.push((m.start(), m.end()));
        }

        for cap in self.url.captures_iter(text) {
            let m = cap.get(0).expect("match");
            let rest = cap.get(2).expect("rest");
            if overlaps(&covered, m.start(), m.end()) {
                continue;
            }
            let end = rest.start() + trim_trailing(rest.as_str());
            let body = &text[rest.start()..end];
            let auth_len = body.find(['/', '?', '#']).unwrap_or(body.len());
            let authority = &body[..auth_len];
            let mut hs = rest.start();
            if let Some(at) = authority.rfind('@') {
                let info = &authority[..at];
                let (user, pass) = match info.split_once(':') {
                    Some((u, p)) => (u, Some(p)),
                    None => (info, None),
                };
                if !user.is_empty()
                    && lex.classify(user) == kaponir_core::PartClass::Unknown
                    && !is_placeholder(user)
                {
                    out.push(Candidate::new(
                        hs,
                        hs + user.len(),
                        "USER",
                        Category::Identity,
                        ID,
                        0.9,
                    ));
                }
                if let Some(p) = pass
                    && !p.is_empty()
                    && !is_placeholder(p)
                {
                    let ps = hs + user.len() + 1;
                    out.push(
                        Candidate::new(ps, ps + p.len(), "SECRET", Category::Credential, ID, 0.98)
                            .hard()
                            .with_evidence("url-password", 1.0),
                    );
                }
                hs += at + 1;
            }
            let hostport = &text[hs..rest.start() + auth_len];
            let host_end = if hostport.starts_with('[') {
                hostport.find(']').map_or(hostport.len(), |i| i + 1)
            } else {
                hostport
                    .rfind(':')
                    .filter(|&i| hostport[i + 1..].chars().all(|c| c.is_ascii_digit()))
                    .unwrap_or(hostport.len())
            };
            if !hostport.starts_with('[') && host_end > 0 {
                host(lex, text, hs, hs + host_end, true, out);
            }
            if auth_len < body.len() {
                url_path(lex, text, rest.start() + auth_len, end, out);
            }
            covered.push((m.start(), end));
        }

        for m in self.host.find_iter(text) {
            let (hs, he) = (m.start(), m.end());
            if overlaps(&covered, hs, he) {
                continue;
            }
            let before = char_before(text, hs);
            if before.is_some_and(|c| is_ident(c) || c == '/' || c == '.' || c == '-' || c == '\\')
            {
                continue;
            }
            let after = char_after(text, he);
            if after == Some('(') || after.is_some_and(|c| is_ident(c) || c == '-') {
                continue;
            }
            if !host(lex, text, hs, he, false, out) {
                continue;
            }
            let mut end = he;
            if let Some(port) = text[he..].strip_prefix(':')
                && port.starts_with(|c: char| c.is_ascii_digit())
            {
                end += 1 + port.chars().take_while(char::is_ascii_digit).count();
            }
            let next = char_after(text, end);
            if next == Some('/')
                || (next == Some(':')
                    && end == he
                    && char_after(text, end + 1).is_some_and(|c| c.is_alphanumeric() || c == '~'))
            {
                let tail = path_tail(text, end + 1);
                url_path(lex, text, end + 1, end + 1 + tail, out);
                end += 1 + tail;
            }
            covered.push((hs, end));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex() -> Lexicon {
        let mut l = Lexicon::default();
        for w in [
            "harbor",
            "manifests",
            "api",
            "team",
            "billing",
            "secret",
            "infra",
            "docs",
        ] {
            l.words.insert(w.into());
        }
        for t in ["ru", "com", "io", "rs", "local"] {
            l.tlds.insert(t.into());
        }
        l.ambiguous_tlds.insert("rs".into());
        l.public_domains.insert("github.com".into());
        l.internal_suffixes.insert("cluster.local".into());
        l.keys.insert("token".into(), "SECRET".into());
        l
    }

    fn found(text: &str) -> Vec<(String, String)> {
        let l = lex();
        let mut out = Vec::new();
        Web::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        out.into_iter()
            .map(|c| (c.kind.clone(), text[c.start..c.end].to_string()))
            .collect()
    }

    #[test]
    fn url_with_corp_core() {
        let f = found(
            "see https://harbor.infra.mycompany.ru/v2/secret-billing/manifests?token=abc123xyz.",
        );
        assert!(f.contains(&("DOM".into(), "mycompany".into())), "{f:?}");
        assert!(
            !f.iter()
                .any(|(_, v)| v == "harbor" || v == "infra" || v == "manifests"),
            "{f:?}"
        );
        assert!(f.contains(&("SECRET".into(), "abc123xyz".into())), "{f:?}");
    }

    #[test]
    fn credentials_in_url() {
        let f = found("postgres://svc_billing:p4ss@db.mycorp.ru:5432/main");
        assert!(f.contains(&("SECRET".into(), "p4ss".into())), "{f:?}");
        assert!(f.contains(&("USER".into(), "svc_billing".into())), "{f:?}");
    }

    #[test]
    fn bare_hosts_images_scp() {
        let f = found(
            "image: harbor.mycorp.ru/team/zorgsvc:1.2 and git@github.com:acmecorp/repo.git, main.rs, x.billing.svc.cluster.local",
        );
        assert!(f.contains(&("DOM".into(), "mycorp".into())), "{f:?}");
        assert!(f.contains(&("PATH".into(), "zorgsvc".into())), "{f:?}");
        assert!(f.contains(&("PATH".into(), "acmecorp".into())), "{f:?}");
        assert!(
            !f.iter().any(|(_, v)| v == "main" || v == "github"),
            "{f:?}"
        );
    }

    #[test]
    fn emails() {
        let f = found("write to ivan.petrov@mycorp.ru today");
        assert_eq!(
            f,
            vec![("EMAIL".to_string(), "ivan.petrov@mycorp.ru".to_string())]
        );
    }
}
