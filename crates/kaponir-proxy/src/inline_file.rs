//! Inline YAML files use the same local detector as a pasted document.
use crate::state::Cx;
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::Value;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub(crate) struct Unreadable(pub &'static str);

pub(crate) struct InlineFile<'a> {
    field: &'static str,
    prefix: &'a str,
    pub text: String,
}

impl<'a> InlineFile<'a> {
    pub fn read(block: &'a Value) -> Result<Option<Self>, Unreadable> {
        let (field, filename, media) = match block["type"].as_str() {
            Some("file") => ("/file/file_data", &block["file"]["filename"], None),
            Some("document") => (
                "/source/data",
                &block["title"],
                block["source"]["media_type"].as_str(),
            ),
            _ => return Ok(None),
        };
        let name = filename.as_str().unwrap_or("").to_ascii_lowercase();
        let data = block.pointer(field).and_then(Value::as_str).unwrap_or("");
        let uri = data.strip_prefix("data:").and_then(|s| s.split_once(','));
        let mime = media
            .or_else(|| uri.map(|(meta, _)| meta.split(';').next().unwrap_or("")))
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        if name.ends_with(".tgz")
            || name.ends_with(".tar.gz")
            || matches!(
                mime.as_str(),
                "application/gzip" | "application/x-gzip" | "application/x-compressed-tar"
            )
        {
            return Err(Unreadable(
                "Compressed chart cannot be inspected locally. Send its YAML files or rendered YAML.",
            ));
        }
        if block["source"]["type"] == "text" {
            return Ok(None); // Native Anthropic text documents already go through mask_field.
        }
        if !name.ends_with(".yaml")
            && !name.ends_with(".yml")
            && !matches!(
                mime.as_str(),
                "application/yaml"
                    | "application/x-yaml"
                    | "text/yaml"
                    | "text/x-yaml"
                    | "text/plain"
            )
        {
            return Ok(None);
        }
        let invalid = Unreadable(
            "Cannot inspect this text attachment locally. Send its contents as UTF-8 text or inline base64.",
        );
        if block["file"].get("file_id").is_some_and(|id| !id.is_null())
            || block["source"].get("url").is_some_and(|url| !url.is_null())
        {
            return Err(invalid);
        }
        let (prefix, bytes) = match uri {
            Some((meta, bytes))
                if meta
                    .split(';')
                    .any(|part| part.eq_ignore_ascii_case("base64")) =>
            {
                (&data[..data.len() - bytes.len()], bytes)
            }
            None if block.pointer(field).is_some_and(Value::is_string) => ("", data),
            _ => return Err(invalid),
        };
        let text = STANDARD
            .decode(bytes)
            .ok()
            .and_then(|v| String::from_utf8(v).ok())
            .ok_or(invalid)?;
        Ok(Some(Self {
            field,
            prefix,
            text,
        }))
    }
}

pub(crate) fn mask(block: &mut Value, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    if let Some(file) = InlineFile::read(block)? {
        let masked = cx.mask(&file.text)?;
        if masked != file.text {
            let field = file.field;
            let data = format!("{}{}", file.prefix, STANDARD.encode(masked));
            *block.pointer_mut(field).expect("inline data field") = Value::String(data);
        }
    }
    Ok(())
}
