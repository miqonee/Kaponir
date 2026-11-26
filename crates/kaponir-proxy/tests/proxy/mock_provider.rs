use super::Seen;
use axum::body::{Body, Bytes};
use axum::extract::{Json, State as Axs};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};
use std::convert::Infallible;

pub(super) fn chunks(s: &str, n: usize) -> Vec<String> {
    let chars: Vec<char> = s.chars().collect();
    chars.chunks(n).map(|c| c.iter().collect()).collect()
}

pub(super) fn last_user_text(body: &Value) -> String {
    body["messages"].as_array().unwrap().last().unwrap()["content"]
        .as_str()
        .unwrap()
        .to_string()
}

/// SSE, нарезанный на куски по 7 байт: события и даже UTF-8 рвутся на границах.
pub(super) fn sse_response(sse: String) -> Response {
    let pieces: Vec<Result<Bytes, Infallible>> = sse
        .into_bytes()
        .chunks(7)
        .map(|c| Ok(Bytes::copy_from_slice(c)))
        .collect();
    Response::builder()
        .header("content-type", "text/event-stream")
        .body(Body::from_stream(futures_util::stream::iter(pieces)))
        .unwrap()
}

pub(super) async fn mock_anthropic(Axs(seen): Axs<Seen>, Json(body): Json<Value>) -> Response {
    seen.lock().unwrap().push(body.clone());
    let text = last_user_text(&body);
    if !body["stream"].as_bool().unwrap_or(false) {
        return Json(json!({
            "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}, {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": text}}]
        }))
        .into_response();
    }
    let mut sse = String::new();
    let mut ev = |name: &str, v: Value| sse.push_str(&format!("event: {name}\ndata: {v}\n\n"));
    ev(
        "message_start",
        json!({"type": "message_start", "message": {"id": "m", "content": []}}),
    );
    ev(
        "content_block_start",
        json!({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
    );
    ev(
        "content_block_delta",
        json!({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "ANON_PROJ_1"}}),
    );
    ev(
        "content_block_stop",
        json!({"type": "content_block_stop", "index": 0}),
    );
    ev(
        "content_block_start",
        json!({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
    );
    for c in chunks(&text, 3) {
        ev(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": c}}),
        );
    }
    ev(
        "content_block_stop",
        json!({"type": "content_block_stop", "index": 1}),
    );
    ev(
        "content_block_start",
        json!({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t1", "name": "bash", "input": {}}}),
    );
    for c in chunks(&json!({"cmd": text}).to_string(), 5) {
        ev(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": c}}),
        );
    }
    ev(
        "content_block_stop",
        json!({"type": "content_block_stop", "index": 2}),
    );
    ev("message_stop", json!({"type": "message_stop"}));
    sse_response(sse)
}

pub(super) async fn mock_openai(Axs(seen): Axs<Seen>, Json(body): Json<Value>) -> Response {
    seen.lock().unwrap().push(body.clone());
    let text = last_user_text(&body);
    let args = json!({"cmd": text}).to_string();
    if !body["stream"].as_bool().unwrap_or(false) {
        return Json(json!({
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": text,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": args}}]
            }}]
        }))
        .into_response();
    }
    let mut sse = String::new();
    let mut chunk = |choice: Value| {
        sse.push_str(&format!(
            "data: {}\n\n",
            json!({"id": "x", "object": "chat.completion.chunk", "choices": [choice]})
        ))
    };
    for c in chunks(&text, 5) {
        chunk(json!({"index": 0, "delta": {"reasoning_content": c}, "finish_reason": null}));
    }
    for c in chunks(&text, 4) {
        chunk(json!({"index": 0, "delta": {"content": c}, "finish_reason": null}));
    }
    chunk(
        json!({"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "bash", "arguments": ""}}]}, "finish_reason": null}),
    );
    for c in chunks(&args, 6) {
        chunk(
            json!({"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": c}}]}, "finish_reason": null}),
        );
    }
    chunk(json!({"index": 0, "delta": {}, "finish_reason": "tool_calls"}));
    sse.push_str("data: [DONE]\n\n");
    sse_response(sse)
}
