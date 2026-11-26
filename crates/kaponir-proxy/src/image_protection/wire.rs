//! Locate binary blocks before touching text, and omit their bodies from diagnostics.
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};

#[derive(Debug)]
pub(super) enum Encoding {
    DataUrl,
    Base64,
    Opaque,
}

pub(super) struct Block {
    pub path: String,
    pub data: String,
    pub encoding: Encoding,
    pub disposition: super::Attachment,
    rewrite: Rewrite,
}

enum Rewrite {
    DataUrl,
    OpenAiFile,
    AnthropicSource,
}

fn image_mime(value: &str) -> bool {
    value
        .get(..6)
        .is_some_and(|part| part.eq_ignore_ascii_case("image/"))
}

fn image_data_url(value: &str) -> bool {
    value
        .get(..5)
        .is_some_and(|part| part.eq_ignore_ascii_case("data:"))
        && image_mime(&value[5..])
}

fn image_name(value: &Value) -> bool {
    value
        .as_str()
        .and_then(|name| name.rsplit_once('.'))
        .is_some_and(|(_, ext)| {
            matches!(
                ext.to_ascii_lowercase().as_str(),
                "png"
                    | "jpg"
                    | "jpeg"
                    | "webp"
                    | "gif"
                    | "bmp"
                    | "tif"
                    | "tiff"
                    | "svg"
                    | "heic"
                    | "avif"
                    | "ico"
                    | "apng"
            )
        })
}

fn image_base64(value: &str) -> bool {
    value
        .get(..16)
        .and_then(|head| STANDARD.decode(head).ok())
        .is_some_and(|head| {
            head.starts_with(b"\x89PNG\r\n\x1a\n")
                || head.starts_with(b"\xff\xd8\xff")
                || (head.starts_with(b"RIFF") && head.get(8..12) == Some(b"WEBP"))
                || head.starts_with(b"GIF87a")
                || head.starts_with(b"GIF89a")
        })
}

/// Returns the data field relative to the block. Inspect known binary wrappers even
/// when their declared MIME or filename disagrees with their actual content.
fn input(block: &Value) -> Option<(&'static str, Encoding, Rewrite)> {
    use Encoding::{Base64, DataUrl, Opaque};
    match block["type"].as_str()? {
        "image_url" | "input_image" => {
            let key = if block["image_url"].is_object() {
                "/image_url/url"
            } else {
                "/image_url"
            };
            Some((key, DataUrl, Rewrite::DataUrl))
        }
        "image" => {
            let encoding = match block["source"]["type"].as_str() {
                Some("base64") => Base64,
                _ => Opaque,
            };
            Some(("/source/data", encoding, Rewrite::AnthropicSource))
        }
        "file" | "input_file" => {
            let nested = block["file"].is_object();
            let file = if nested { &block["file"] } else { block };
            let data = file["file_data"].as_str().unwrap_or_default();
            let field = if nested {
                "/file/file_data"
            } else {
                "/file_data"
            };
            if file["file_id"].is_string() || file["file_url"].is_string() {
                Some((field, Opaque, Rewrite::OpenAiFile))
            } else if image_data_url(data)
                || image_base64(data.split_once(',').map_or(data, |(_, payload)| payload))
                || image_name(&file["filename"])
                || file["media_type"].as_str().is_some_and(image_mime)
            {
                Some((
                    field,
                    if data
                        .get(..5)
                        .is_some_and(|s| s.eq_ignore_ascii_case("data:"))
                    {
                        DataUrl
                    } else {
                        Base64
                    },
                    Rewrite::OpenAiFile,
                ))
            } else {
                None
            }
        }
        "document" => {
            let source = &block["source"];
            let data = source["data"].as_str().unwrap_or_default();
            let encoding = match source["type"].as_str() {
                Some("url" | "file" | "file_id") => Opaque,
                Some("base64")
                    if source["media_type"].as_str().is_some_and(image_mime)
                        || image_base64(data)
                        || image_name(&block["title"]) =>
                {
                    Base64
                }
                _ => return None,
            };
            Some(("/source/data", encoding, Rewrite::AnthropicSource))
        }
        _ => None,
    }
}

pub(crate) fn is_image_block(block: &Value) -> bool {
    input(block).is_some() || block["kaponir_image"] == true
}

pub(super) fn scan(body: &Value, session: &str) -> Vec<Block> {
    let mut out = Vec::new();
    let messages = body["messages"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let latest = messages.iter().rposition(|message| {
        message["role"] == "user"
            && (message["content"]
                .as_str()
                .is_some_and(|s| !s.trim().is_empty())
                || message["content"]
                    .as_array()
                    .is_some_and(|parts| parts.iter().any(crate::conversation::human_block)))
    });
    collect(
        &body["system"],
        "/system",
        "system",
        None,
        false,
        session,
        &mut out,
    );
    for (index, message) in messages.iter().enumerate() {
        let role = message["role"].as_str().unwrap_or("message");
        let tool = matches!(role, "tool" | "function")
            .then(|| crate::conversation::tool_result_id(&message["tool_call_id"], index, 0));
        collect(
            &message["content"],
            &format!("/messages/{index}/content"),
            role,
            tool.as_deref(),
            latest == Some(index),
            session,
            &mut out,
        );
    }
    out
}

fn collect(
    contents: &Value,
    parent: &str,
    role: &str,
    tool_id: Option<&str>,
    latest: bool,
    session: &str,
    out: &mut Vec<Block>,
) {
    for (i, block) in contents.as_array().into_iter().flatten().enumerate() {
        if out.len() > super::MAX_IMAGES {
            break;
        }
        let path = format!("{parent}/{i}");
        if crate::conversation::tool_result(block) {
            let index = parent
                .split('/')
                .nth(2)
                .and_then(|n| n.parse().ok())
                .unwrap_or(0);
            let id = crate::conversation::tool_result_id(&block["tool_use_id"], index, i);
            collect(
                &block["content"],
                &format!("{path}/content"),
                "tool",
                Some(&id),
                false,
                session,
                out,
            );
        } else if let Some((field, encoding, rewrite)) = input(block) {
            let mut message_hash = blake3::Hasher::new_derive_key("Kaponir image message v1");
            message_hash.update(session.as_bytes());
            message_hash.update(parent.as_bytes());
            let message_id = message_hash.finalize().to_hex()[..24].to_string();
            let mut hash = blake3::Hasher::new_derive_key("Kaponir image attachment v1");
            hash.update(message_id.as_bytes());
            hash.update(&i.to_le_bytes());
            let data = format!("{path}{field}");
            hash.update(
                block
                    .pointer(field)
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .as_bytes(),
            );
            out.push(Block {
                path,
                data,
                encoding,
                rewrite,
                disposition: super::Attachment {
                    id: hash.finalize().to_hex()[..24].to_string(),
                    message_id,
                    role: if role == "function" { "tool" } else { role }.to_owned(),
                    tool_id: tool_id.map(str::to_owned),
                    latest,
                    ..Default::default()
                },
            });
        }
    }
}

impl Block {
    pub fn bytes<'a>(&self, body: &'a Value) -> Result<&'a str, super::Failure> {
        let missing = super::Failure::new(super::Code::InlineRequired);
        let data = body
            .pointer(&self.data)
            .and_then(Value::as_str)
            .ok_or_else(|| missing.clone())?;
        match self.encoding {
            Encoding::Opaque => Err(missing),
            Encoding::Base64 => Ok(data),
            Encoding::DataUrl => {
                let (metadata, bytes) = data.split_once(',').ok_or_else(|| missing.clone())?;
                let (scheme, media) = metadata.split_once(':').ok_or_else(|| missing.clone())?;
                if !scheme.eq_ignore_ascii_case("data")
                    || !media
                        .split(';')
                        .any(|tag| tag.eq_ignore_ascii_case("base64"))
                {
                    return Err(missing);
                }
                Ok(bytes)
            }
        }
    }

    pub fn take(&self, body: &mut Value) -> Result<super::InputImage, super::Failure> {
        let payload_len = self.bytes(body)?.len();
        let value = body.pointer_mut(&self.data).expect("validated inline data");
        let Value::String(data) = std::mem::take(value) else {
            unreachable!()
        };
        Ok(super::InputImage::Base64 {
            offset: data.len() - payload_len,
            data,
        })
    }

    pub fn replace(&self, body: &mut Value, png: &[u8]) {
        let encoded = STANDARD.encode(png);
        let data = match self.rewrite {
            Rewrite::DataUrl | Rewrite::OpenAiFile => format!("data:image/png;base64,{encoded}"),
            Rewrite::AnthropicSource => encoded,
        };
        *body
            .pointer_mut(&self.data)
            .expect("validated image data field") = Value::String(data);
        let block = body.pointer_mut(&self.path).expect("validated image block");
        match self.rewrite {
            Rewrite::AnthropicSource => {
                let data = block["source"]["data"].take();
                block["source"] = json!({"type":"base64","media_type":"image/png","data":data});
                // Only cache_control is also defined on an Anthropic image block.
                if block["type"] == "document" {
                    let cache = block["cache_control"].take();
                    let source = block["source"].take();
                    *block = json!({"type":"image", "source":source});
                    if !cache.is_null() {
                        block["cache_control"] = cache;
                    }
                }
            }
            Rewrite::OpenAiFile => {
                // Chat Completions has a native image part. A generic file
                // part carrying an image need not be supported by the provider.
                let file = if block["file"].is_object() {
                    &block["file"]
                } else {
                    &*block
                };
                let url = file["file_data"].clone();
                let mut image = json!({"url":url});
                if let Some(detail) = block["detail"].as_str() {
                    image["detail"] = json!(detail);
                }
                *block = json!({"type":"image_url","image_url":image});
            }
            Rewrite::DataUrl => {}
        }
    }
}

/// Binary image bodies belong exclusively to the authenticated attachment endpoint.
pub(crate) fn omit_images(value: &Value) -> Value {
    if is_image_block(value) {
        return json!({"type": value["type"], "kaponir_image": true});
    }
    match value {
        Value::Array(items) => Value::Array(items.iter().map(omit_images).collect()),
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .map(|(key, value)| (key.clone(), omit_images(value)))
                .collect(),
        ),
        _ => value.clone(),
    }
}

/// Older versions could flatten an image-only request into a JSON snapshot,
/// or retain a truncated provider payload containing the head of its base64.
pub(crate) fn legacy_snapshot<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<String, D::Error> {
    use serde::Deserialize;
    let snapshot = String::deserialize(deserializer)?;
    let trimmed = snapshot.trim_start();
    if !trimmed.starts_with(['{', '[']) || trimmed.starts_with("[user]") {
        return Ok(snapshot);
    }
    if let Ok(value) = serde_json::from_str::<Value>(&snapshot) {
        fn has_images(value: &Value) -> bool {
            is_image_block(value)
                || value
                    .as_array()
                    .is_some_and(|items| items.iter().any(has_images))
                || value
                    .as_object()
                    .is_some_and(|items| items.values().any(has_images))
        }
        if has_images(&value) {
            return Ok(serde_json::to_string_pretty(&omit_images(&value)).unwrap_or_default());
        }
    } else if (trimmed.starts_with('{') || trimmed.starts_with("[{"))
        && (trimmed.contains("data:image/")
            || trimmed.contains("\"image_url\"")
            || (trimmed.contains("\"source\"")
                && (trimmed.contains("\"image\"") || trimmed.contains("image/")))
            || (trimmed.contains("\"file_data\"")
                && ["iVBORw0KG", "/9j/", "UklGR"]
                    .iter()
                    .any(|s| trimmed.contains(s))))
    {
        return Ok(r#"{"kaponir_image":true}"#.into());
    }
    Ok(snapshot)
}

#[cfg(test)]
mod tests {
    use crate::traffic::EventInspector;
    use serde_json::json;

    #[test]
    fn legacy_snapshots_never_return_an_image_only_request_or_truncated_binary() {
        let mut value = serde_json::to_value(EventInspector::default()).unwrap();
        value["original_request"] = json!(
            r#"{"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,FAKE"}}]}]}"#
        );
        value["masked_payload_json"] = json!(
            r#"{"messages":[{"role":"user","content":[{"type":"image","source":{"data":"FAKE… [truncated]"#
        );
        let inspector: EventInspector = serde_json::from_value(value).unwrap();
        for snapshot in [inspector.original_request, inspector.masked_payload_json] {
            assert!(snapshot.contains("kaponir_image"));
            assert!(!snapshot.contains("FAKE"));
        }
    }
}
