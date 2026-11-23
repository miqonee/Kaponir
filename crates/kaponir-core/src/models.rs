//! Stable local model identifiers. Custom artifacts are resolved and registered by the worker.
pub fn valid_local_id(model: &str) -> bool {
    matches!(
        model,
        "q4" | "q5"
            | "kaponir-privacy-v4-epoch-2"
            | "qwen3-0.6b"
            | "qwen3-0.6b-q8"
            | "qwen3.5-0.8b"
            | "qwen3.5-2b"
            | "minicpm5-2b"
            | "nanbeige42-3b"
            | "granite42-3b"
            | "gemma4-e4b-qat"
            | "lfm2.5-1.2b-instruct"
            | "lfm2.5-2.6b"
            | "qwen3.5-4b"
            | "spark-x2.6-4b"
            | "lfm2.5-8b-a1b"
            | "gemma4-e2b-it-q4km"
            | "gemma4-e4b-it-q4km"
            | "huihui-qwen3.5-2b-abliterated"
    ) || model.strip_prefix("hf-").is_some_and(|id| {
        id.len() == 24
            && id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn local_ids_are_not_urls_or_paths() {
        for model in [
            "qwen3.5-2b",
            "minicpm5-2b",
            "nanbeige42-3b",
            "granite42-3b",
            "gemma4-e4b-qat",
            "huihui-qwen3.5-2b-abliterated",
            "hf-0123456789abcdef01234567",
        ] {
            assert!(valid_local_id(model));
        }
        for model in [
            "hf-",
            "../../weights.gguf",
            "https://huggingface.co/repo",
            "hf-0123456789ABCDEF01234567",
        ] {
            assert!(!valid_local_id(model));
        }
    }
}
