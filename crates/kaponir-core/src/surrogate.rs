//! Сохраняющие формат суррогаты IPv4 в зарезервированном диапазоне 240.0.0.0/4.
//!
//! Первые 24 бита проходят prefix-preserving перестановку (схема Crypto-PAn на HMAC):
//! общий префикс оригиналов ⇒ общий префикс суррогатов. Из 24 бит берутся младшие 20,
//! хост-октет сохраняется. Коллизии (≈2⁻²⁰) разрешает vault, откатываясь на обычный токен.

use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;
use std::net::Ipv4Addr;

pub const SURROGATE_NET: u32 = 0xF000_0000;
pub const SURROGATE_MASK: u32 = 0xF000_0000;

pub fn is_surrogate_range(ip: Ipv4Addr) -> bool {
    u32::from(ip) & SURROGATE_MASK == SURROGATE_NET
}

pub fn ipv4_surrogate(key: &[u8; 32], ip: Ipv4Addr) -> Ipv4Addr {
    let v = u32::from(ip);
    let prefix = v >> 8;
    let mut out = 0u32;
    for i in 0..24 {
        let bit = (prefix >> (23 - i)) & 1;
        let head = if i == 0 { 0 } else { prefix >> (24 - i) };
        let mut mac = <Hmac<Sha256> as KeyInit>::new_from_slice(key).expect("any key length");
        mac.update(b"anon-ipv4-pp");
        mac.update(&[i as u8]);
        mac.update(&head.to_be_bytes());
        let flip = (mac.finalize().into_bytes()[0] & 1) as u32;
        out = (out << 1) | (bit ^ flip);
    }
    Ipv4Addr::from(SURROGATE_NET | ((out & 0xF_FFFF) << 8) | (v & 0xFF))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_prefix_and_host() {
        let k = [7u8; 32];
        let a = ipv4_surrogate(&k, "10.50.12.15".parse().unwrap());
        let b = ipv4_surrogate(&k, "10.50.12.16".parse().unwrap());
        let c = ipv4_surrogate(&k, "10.50.13.16".parse().unwrap());
        assert!(is_surrogate_range(a));
        assert_eq!(u32::from(a) >> 8, u32::from(b) >> 8);
        assert_eq!(a.octets()[3], 15);
        assert_eq!(b.octets()[3], 16);
        assert_ne!(u32::from(b) >> 8, u32::from(c) >> 8);
        // Общие 16 бит префикса у оригиналов ⇒ общие старшие биты у суррогатов (без 4 отброшенных).
        assert_eq!((u32::from(b) >> 8) & 0xFFF00, (u32::from(c) >> 8) & 0xFFF00);
    }
}
