//! Разбор и сборка Server-Sent Events. Границы чанков сети не совпадают с границами событий,
//! поэтому байты копятся до пустой строки.

use crate::state::Cx;

#[derive(Clone, Debug)]
pub struct Event {
    pub event: Option<String>,
    pub data: String,
    /// Событие как пришло, вместе с завершающей пустой строкой.
    pub raw: String,
}

#[derive(Debug, Default)]
pub struct SseParser {
    buf: Vec<u8>,
}

impl SseParser {
    pub fn push(&mut self, bytes: &[u8]) -> Vec<Event> {
        self.buf.extend_from_slice(bytes);
        let mut out = Vec::new();
        while let Some((end, sep)) = boundary(&self.buf) {
            let block: Vec<u8> = self.buf.drain(..end + sep).collect();
            // Граница — ASCII `\n`, так что UTF-8 внутри блока цел.
            out.push(parse(&String::from_utf8_lossy(&block[..end])));
        }
        out
    }

    /// Остаток без завершающей пустой строки (поток оборвался).
    pub fn finish(&mut self) -> Option<Event> {
        let block = std::mem::take(&mut self.buf);
        let s = String::from_utf8_lossy(&block);
        let s = s.trim_end();
        (!s.is_empty()).then(|| parse(s))
    }
}

fn boundary(b: &[u8]) -> Option<(usize, usize)> {
    let lf = b.windows(2).position(|w| w == b"\n\n").map(|p| (p, 2));
    let crlf = b.windows(4).position(|w| w == b"\r\n\r\n").map(|p| (p, 4));
    match (lf, crlf) {
        (Some(a), Some(c)) => Some(if a.0 <= c.0 { a } else { c }),
        (a, c) => a.or(c),
    }
}

fn parse(block: &str) -> Event {
    let mut event = None;
    let mut data: Vec<&str> = Vec::new();
    for line in block.lines() {
        if let Some(v) = field(line, "event") {
            event = Some(v.to_string());
        } else if let Some(v) = field(line, "data") {
            data.push(v);
        }
    }
    Event {
        event,
        data: data.join("\n"),
        raw: format!("{block}\n\n"),
    }
}

fn field<'a>(line: &'a str, name: &str) -> Option<&'a str> {
    let rest = line.strip_prefix(name)?.strip_prefix(':')?;
    Some(rest.strip_prefix(' ').unwrap_or(rest))
}

pub fn encode(event: Option<&str>, data: &str) -> String {
    let mut s = String::new();
    if let Some(e) = event {
        s.push_str("event: ");
        s.push_str(e);
        s.push('\n');
    }
    for line in data.split('\n') {
        s.push_str("data: ");
        s.push_str(line);
        s.push('\n');
    }
    s.push('\n');
    s
}

/// Переписывает поток событий одного протокола: восстанавливает токены и дописывает
/// придержанные хвосты перед концом блока.
pub trait Rewriter: Send {
    fn event(&mut self, ev: Event, cx: &mut Cx<'_>) -> anyhow::Result<Vec<String>>;
    /// Поток закончился: отдать всё придержанное.
    fn end(&mut self, cx: &mut Cx<'_>) -> anyhow::Result<Vec<String>>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_events_across_chunks() {
        let mut p = SseParser::default();
        let raw = "event: a\ndata: {\"x\":\"привет\"}\n\nevent: b\r\ndata: 1\r\ndata: 2\r\n\r\n: comment\n\ndata: tail";
        let mut events = Vec::new();
        for chunk in raw.as_bytes().chunks(3) {
            events.extend(p.push(chunk));
        }
        events.extend(p.finish());
        let got: Vec<(Option<&str>, &str)> = events
            .iter()
            .map(|e| (e.event.as_deref(), e.data.as_str()))
            .collect();
        assert_eq!(
            got,
            vec![
                (Some("a"), "{\"x\":\"привет\"}"),
                (Some("b"), "1\n2"),
                (None, ""),
                (None, "tail")
            ]
        );
        assert_eq!(encode(Some("b"), "1\n2"), "event: b\ndata: 1\ndata: 2\n\n");
    }
}
