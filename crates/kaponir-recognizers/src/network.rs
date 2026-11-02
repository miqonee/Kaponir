//! IPv4/IPv6, MAC, международные телефоны.

use kaponir_core::{AnalyzeCtx, Candidate, Category, Input, Recognizer};
use regex::Regex;
use std::net::{Ipv4Addr, Ipv6Addr};

use crate::{char_after, char_before};

const ID: &str = "network";

/// Публичные резолверы — не чувствительны.
const PUBLIC_IPS: &[&str] = &[
    "8.8.8.8",
    "8.8.4.4",
    "1.1.1.1",
    "1.0.0.1",
    "9.9.9.9",
    "77.88.8.8",
    "77.88.8.1",
];

pub struct Network {
    v4: Regex,
    v6: Regex,
    mac: Regex,
    phone: Regex,
}

impl Default for Network {
    fn default() -> Self {
        Self::new()
    }
}

impl Network {
    pub fn new() -> Self {
        Self {
            v4: Regex::new(r"(?:\d{1,3}\.){3}\d{1,3}").expect("valid regex"),
            v6: Regex::new(r"(?i)(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?:%[0-9a-z]+)?")
                .expect("valid regex"),
            mac: Regex::new(r"(?i)\b[0-9a-f]{2}(?:([:\-])[0-9a-f]{2})(?:[:\-][0-9a-f]{2}){4}\b")
                .expect("valid regex"),
            phone: Regex::new(r"\+\d[\d \-()]{8,18}\d").expect("valid regex"),
        }
    }
}

fn is_netmask(ip: Ipv4Addr) -> bool {
    let v = u32::from(ip);
    v != 0 && v.leading_ones() + v.trailing_zeros() == 32 && ip.octets()[0] == 255
}

impl Recognizer for Network {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, _ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        for m in self.v4.find_iter(text) {
            let before = char_before(text, m.start());
            let after = char_after(text, m.end());
            if before.is_some_and(|c| c.is_alphanumeric() || c == '.')
                || after.is_some_and(|c| c.is_alphanumeric())
                || (after == Some('.')
                    && text[m.end() + 1..].starts_with(|c: char| c.is_ascii_digit()))
            {
                continue;
            }
            let Ok(ip) = m.as_str().parse::<Ipv4Addr>() else {
                continue;
            };
            if ip.is_loopback()
                || ip.is_unspecified()
                || ip.is_broadcast()
                || is_netmask(ip)
                || PUBLIC_IPS.contains(&m.as_str())
            {
                continue;
            }
            let prior = if ip.is_private() || ip.is_link_local() || ip.octets()[0] == 100 {
                0.92
            } else {
                0.85
            };
            out.push(Candidate::new(
                m.start(),
                m.end(),
                "IP",
                Category::Network,
                ID,
                prior,
            ));
        }
        for m in self.v6.find_iter(text) {
            let before = char_before(text, m.start());
            let after = char_after(text, m.end());
            if before.is_some_and(|c| c.is_ascii_hexdigit() || c == ':' || c == '.')
                || after.is_some_and(|c| c.is_ascii_hexdigit() || c == ':' || c == '.')
            {
                continue;
            }
            let addr = m.as_str().split('%').next().unwrap_or_default();
            let Ok(ip) = addr.parse::<Ipv6Addr>() else {
                continue;
            };
            if ip.is_loopback() || ip.is_unspecified() || addr.matches(':').count() < 2 {
                continue;
            }
            // `12:30:45` и подобное не парсится как IPv6, но `::1`-подобные короткие формы — да.
            if !addr.contains("::") && addr.split(':').count() < 8 {
                continue;
            }
            out.push(Candidate::new(
                m.start(),
                m.start() + addr.len(),
                "IP",
                Category::Network,
                ID,
                0.85,
            ));
        }
        for m in self.mac.find_iter(text) {
            out.push(Candidate::new(
                m.start(),
                m.end(),
                "MAC",
                Category::Network,
                ID,
                0.8,
            ));
        }
        for m in self.phone.find_iter(text) {
            if char_before(text, m.start()).is_some_and(|c| c.is_alphanumeric()) {
                continue;
            }
            let digits = m.as_str().chars().filter(char::is_ascii_digit).count();
            if (10..=15).contains(&digits) {
                out.push(Candidate::new(
                    m.start(),
                    m.end(),
                    "PHONE",
                    Category::Contact,
                    ID,
                    0.85,
                ));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn found(text: &str) -> Vec<(String, String)> {
        let l = kaponir_core::Lexicon::default();
        let mut out = Vec::new();
        Network::new().analyze(&Input::text(text), &AnalyzeCtx::new(&l), &mut out);
        out.into_iter()
            .map(|c| (c.kind.clone(), text[c.start..c.end].to_string()))
            .collect()
    }

    #[test]
    fn ips() {
        let f = found(
            "host 10.50.12.15/24 dns 8.8.8.8 lo 127.0.0.1 mask 255.255.255.0 v1.2.3.4 at 12:30:45 ip fe80::1%eth0 2001:db8:0:0:0:0:0:1",
        );
        let ips: Vec<&str> = f.iter().map(|(_, v)| v.as_str()).collect();
        assert_eq!(ips, vec!["10.50.12.15", "fe80::1", "2001:db8:0:0:0:0:0:1"]);
    }

    #[test]
    fn mac_and_phone() {
        let f = found("mac 00:1A:2b:3c:4D:5e call +7 (912) 345-67-89");
        assert_eq!(f[0].0, "MAC");
        assert_eq!(
            f[1],
            ("PHONE".to_string(), "+7 (912) 345-67-89".to_string())
        );
    }
}
