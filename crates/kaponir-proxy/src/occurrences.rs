use kaponir_core::{Candidate, Category, PositionEdits};
use serde::{
    Deserialize, Deserializer,
    de::{Error, MapAccess, Visitor},
};
use serde_json::value::RawValue;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap},
    fmt::{self, Write as _},
    ops::Range,
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Default)]
pub(crate) struct OccurrencePolicy {
    windows_audit_records: bool,
    grants: HashMap<String, Vec<Grant>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Grant {
    document_sha256: String,
    record: usize,
    field: String,
    start_byte: usize,
    end_byte: usize,
    status: Status,
    expires_unix: u64,
}

#[derive(Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum Status {
    Public,
    Private,
    Review,
}

#[derive(Deserialize)]
struct Document<'a> {
    #[serde(borrow)]
    records: Vec<Record<'a>>,
}

struct Record<'a>(BTreeMap<String, &'a RawValue>);

impl<'de: 'a, 'a> Deserialize<'de> for Record<'a> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Fields;
        impl<'de> Visitor<'de> for Fields {
            type Value = Record<'de>;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a record with unique field names")
            }

            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let mut fields = BTreeMap::new();
                while let Some((key, value)) = map.next_entry()? {
                    if fields.insert(key, value).is_some() {
                        return Err(M::Error::custom("duplicate record field"));
                    }
                }
                Ok(Record(fields))
            }
        }
        deserializer.deserialize_map(Fields)
    }
}

impl OccurrencePolicy {
    pub(crate) fn new(windows_audit_records: bool, path: Option<&Path>) -> Self {
        let mut policy = Self {
            windows_audit_records,
            grants: HashMap::new(),
        };
        if let Some(path) = path {
            match std::fs::read(path)
                .map_err(anyhow::Error::from)
                .and_then(|file| {
                    let grants: Vec<Grant> = serde_json::from_slice(&file)?;
                    Ok(grants)
                }) {
                Ok(grants) => {
                    for mut grant in grants {
                        grant.document_sha256.make_ascii_lowercase();
                        policy
                            .grants
                            .entry(grant.document_sha256.clone())
                            .or_default()
                            .push(grant);
                    }
                }
                Err(err) => eprintln!(
                    "kaponir: public occurrence file unavailable, using normal masking: {err}"
                ),
            }
        }
        policy
    }

    pub(crate) fn may_have_grants(&self, text: &str) -> bool {
        !self.grants.is_empty() && self.grants.contains_key(&document_hash(text))
    }

    pub(crate) fn edits(&self, text: &str) -> Option<PositionEdits> {
        if !text.trim_start().starts_with('{') {
            return None;
        }
        let grants = if self.grants.is_empty() {
            None
        } else {
            self.grants.get(&document_hash(text))
        };
        if !self.windows_audit_records && grants.is_none() {
            return None;
        }
        let doc: Document<'_> = serde_json::from_str(text).ok()?;
        let windows_masks = windows_masks(text, &doc);
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .ok()
            .map(|d| d.as_secs());
        let public_ranges = grants
            .zip(now)
            .and_then(|(grants, now)| public_ranges(text, &doc, grants, now, &windows_masks))
            .unwrap_or_default();
        let additional_masks = if self.windows_audit_records {
            windows_masks
        } else {
            Vec::new()
        };
        (!additional_masks.is_empty() || !public_ranges.is_empty()).then_some(PositionEdits {
            additional_masks,
            public_ranges,
        })
    }
}

fn document_hash(text: &str) -> String {
    let mut hash = String::with_capacity(64);
    for byte in Sha256::digest(text.as_bytes()) {
        write!(hash, "{byte:02x}").expect("writing to a string cannot fail");
    }
    hash
}

fn string_body<'a>(raw: &'a RawValue, document: &str) -> Option<(usize, &'a str)> {
    let source = raw.get();
    let body = source.strip_prefix('"')?.strip_suffix('"')?;
    if body.contains('\\') {
        // RawValue can borrow syntax without decoding Unicode surrogate pairs.
        serde_json::from_str::<String>(source).ok()?;
    }
    // Borrowed RawValue points into the input deserialized from text.
    let start = (body.as_ptr() as usize).checked_sub(document.as_ptr() as usize)?;
    (document.get(start..start.checked_add(body.len())?)? == body).then_some((start, body))
}

fn public_ranges(
    text: &str,
    doc: &Document<'_>,
    grants: &[Grant],
    now: u64,
    windows_masks: &[Candidate],
) -> Option<Vec<Range<usize>>> {
    let mut located = Vec::new();
    for grant in grants.iter().filter(|g| g.expires_unix > now) {
        let field = doc.records.get(grant.record)?.0.get(&grant.field)?;
        let (start, body) = string_body(field, text)?;
        let range = grant.start_byte..grant.end_byte;
        let a = range.start.checked_sub(start)?;
        let b = range.end.checked_sub(start)?;
        if range.is_empty() || !string_boundary(body, a) || !string_boundary(body, b) {
            return None;
        }
        located.push((range, &grant.status));
    }
    located.sort_unstable_by_key(|(r, _)| r.start);
    if located
        .windows(2)
        .any(|pair| pair[0].0.end > pair[1].0.start)
    {
        return None;
    }
    Some(
        located
            .into_iter()
            .filter_map(|(r, status)| {
                (*status == Status::Public
                    && !windows_masks
                        .iter()
                        .any(|c| r.start < c.end && c.start < r.end))
                .then_some(r)
            })
            .collect(),
    )
}

/// Offsets must bound complete characters in the encoded JSON string.
fn string_boundary(body: &str, offset: usize) -> bool {
    if offset == 0 || offset == body.len() {
        return true;
    }
    if offset > body.len() {
        return false;
    }
    let mut pos = 0;
    while pos < offset {
        pos += json_unit_len(&body[pos..]);
    }
    pos == offset
}

fn json_unit_len(s: &str) -> usize {
    if s.starts_with(r"\u") {
        // A validated high surrogate is followed by a low surrogate escape.
        let high_surrogate = matches!(s.as_bytes()[2], b'd' | b'D')
            && matches!(s.as_bytes()[3], b'8' | b'9' | b'a' | b'A' | b'b' | b'B');
        if high_surrogate && s.len() >= 12 {
            12
        } else {
            6
        }
    } else if s.starts_with('\\') {
        2
    } else {
        s.chars()
            .next()
            .expect("nonempty JSON string suffix")
            .len_utf8()
    }
}

fn windows_masks(text: &str, doc: &Document<'_>) -> Vec<Candidate> {
    let mut masks = Vec::new();
    for record in &doc.records {
        let fields = &record.0;
        let Some(actor) = fields.get("actor") else {
            continue;
        };
        let Some((start, body)) = string_body(actor, text) else {
            continue;
        };
        let Some(origin) = fields
            .get("record_origin")
            .and_then(|v| serde_json::from_str::<String>(v.get()).ok())
        else {
            continue;
        };
        let origin = origin.to_lowercase();
        // Source text can prevent an extra contextual mask. It cannot cancel an
        // existing detector decision. Unknown/audit origins get the extra masks.
        if (origin.starts_with("вымышленная запись") && origin.contains("открыт"))
            || (origin.starts_with("fictional account") && origin.contains("published"))
        {
            continue;
        }
        let mut separators = Vec::new();
        let mut pos = 0;
        while pos < body.len() {
            let end = pos + json_unit_len(&body[pos..]);
            let unit = &body[pos..end];
            if unit == r"\\" || unit.eq_ignore_ascii_case(r"\u005c") {
                separators.push(pos..end);
            }
            pos = end;
        }
        if let [sep] = separators.as_slice()
            && sep.start > 0
            && sep.end < body.len()
        {
            let add = |a, b, kind, category| {
                Candidate::new(a, b, kind, category, "windows-audit-record", 0.99)
            };
            masks.push(add(start, start + sep.start, "DOM", Category::Infra));
            masks.push(add(
                start + sep.end,
                start + body.len(),
                "USER",
                Category::Identity,
            ));
            if let Some((start, body)) = fields.get("account").and_then(|v| string_body(v, text))
                && !body.is_empty()
            {
                masks.push(add(start, start + body.len(), "USER", Category::Identity));
            }
        }
    }
    masks
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grant(text: &str, record: usize, field: &str, range: Range<usize>, status: Status) -> Grant {
        Grant {
            document_sha256: document_hash(text),
            record,
            field: field.into(),
            start_byte: range.start,
            end_byte: range.end,
            status,
            expires_unix: 100,
        }
    }

    #[test]
    fn audit_positions_keep_domain_separator_and_public_record_bytes() {
        let text = r#"{"title":"Смена","records":[
            {"actor":"TEAM\\Мария","account":"Мария","record_origin":"вымышленная запись открытого курса"},
            {"actor":"TEAM\u005CМария","account":"Мария","record_origin":"закрытый аудит"}]}"#;
        let doc: Document<'_> = serde_json::from_str(text).unwrap();
        let masks = windows_masks(text, &doc);
        let ranges: Vec<_> = masks
            .iter()
            .map(|c| (&text[c.start..c.end], c.kind.as_str()))
            .collect();
        assert_eq!(
            ranges,
            [("TEAM", "DOM"), ("Мария", "USER"), ("Мария", "USER")]
        );
        assert_eq!(masks[0].start, text.rfind("TEAM").unwrap());
        assert_eq!(&text[masks[0].end..masks[1].start], r"\u005C");
    }

    #[test]
    fn grant_binds_document_record_field_and_complete_encoded_characters() {
        let text = r#"{"records":[{"value":"☀\uD83D\uDE00"},{"value":"☀\uD83D\uDE00"}]}"#;
        let doc: Document<'_> = serde_json::from_str(text).unwrap();
        let start = text.find('☀').unwrap();
        let end = start + "☀".len() + 12;
        let allowed = |records: &[Grant], now| public_ranges(text, &doc, records, now, &[]);
        let ranges = allowed(&[grant(text, 0, "value", start..end, Status::Public)], 0).unwrap();
        assert_eq!(ranges.len(), 1);
        assert_eq!(ranges[0], start..end);
        assert!(allowed(&[grant(text, 1, "value", start..end, Status::Public)], 0).is_none());
        assert!(allowed(&[grant(text, 0, "other", start..end, Status::Public)], 0).is_none());
        assert!(
            allowed(
                &[grant(text, 0, "value", start + 1..end, Status::Public)],
                0
            )
            .is_none()
        );
        assert!(
            allowed(
                &[grant(text, 0, "value", start..end - 6, Status::Public)],
                0
            )
            .is_none()
        );
        assert!(
            allowed(&[grant(text, 0, "value", start..end, Status::Public)], 100)
                .unwrap()
                .is_empty()
        );
        assert!(
            allowed(
                &[
                    grant(text, 0, "value", start..end, Status::Public),
                    grant(text, 0, "value", start..end, Status::Review),
                ],
                0
            )
            .is_none()
        );

        let policy = OccurrencePolicy {
            grants: HashMap::from([(
                document_hash(text),
                vec![grant(text, 0, "value", start..end, Status::Public)],
            )]),
            ..OccurrencePolicy::default()
        };
        assert!(policy.edits(text).is_none()); // historical fixture grants have expired
        assert!(!policy.may_have_grants(&format!("{text}\n")));
        assert!(
            serde_json::from_str::<Document<'_>>(r#"{"records":[{"value":"a","value":"b"}]}"#)
                .is_err()
        );
    }
}
