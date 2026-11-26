/// Values which provide no evidence of a private business entity. Exact user rules
/// may still intentionally protect these values; this only filters suggestions.
pub fn weak_business_value(kind: &str, value: &str) -> bool {
    let v = value.trim().to_lowercase();
    if v.is_empty() || v.chars().all(|c| !c.is_alphanumeric()) {
        return true;
    }
    if !matches!(kind, "PROJ" | "ORG" | "CLIENT" | "CUSTOM") {
        return false;
    }
    matches!(
        v.as_str(),
        "или"
            | "и"
            | "как"
            | "это"
            | "для"
            | "the"
            | "and"
            | "or"
            | "project"
            | "character"
            | "questions"
            | "name"
            | "value"
            | "example"
            | "null"
            | "none"
    ) || [
        "gemini-",
        "gpt-",
        "claude-",
        "qwen3-",
        "qwen3.5-",
        "llama-",
        "deepseek-",
    ]
    .iter()
    .any(|p| v.starts_with(p))
}
