//! Обход JSON: маскирование и восстановление строковых полей и листьев.

use kaponir_core::Escape;
use serde_json::Value;

use crate::state::Cx;

pub(crate) fn ty(v: &Value) -> String {
    v.get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

pub(crate) fn mask_field(obj: &mut Value, field: &str, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    if let Some(Value::String(s)) = obj.get_mut(field) {
        *s = cx.mask(s)?;
    }
    Ok(())
}

pub(crate) fn restore_field(
    obj: &mut Value,
    field: &str,
    cx: &mut Cx<'_>,
    escape: Escape,
) -> anyhow::Result<()> {
    if let Some(Value::String(s)) = obj.get_mut(field) {
        *s = cx.restore(s, escape)?;
    }
    Ok(())
}

/// Маскирует все строковые значения (ключи объектов — нет: это схема инструмента).
pub(crate) fn mask_leaves(v: &mut Value, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    match v {
        Value::String(s) => *s = cx.mask(s)?,
        Value::Array(a) => {
            for x in a {
                mask_leaves(x, cx)?;
            }
        }
        Value::Object(o) => {
            for (_, x) in o.iter_mut() {
                mask_leaves(x, cx)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// Восстанавливает строковые значения уже разобранного JSON — без экранирования.
pub(crate) fn restore_leaves(v: &mut Value, cx: &mut Cx<'_>) -> anyhow::Result<()> {
    match v {
        Value::String(s) => *s = cx.restore(s, Escape::None)?,
        Value::Array(a) => {
            for x in a {
                restore_leaves(x, cx)?;
            }
        }
        Value::Object(o) => {
            for (_, x) in o.iter_mut() {
                restore_leaves(x, cx)?;
            }
        }
        _ => {}
    }
    Ok(())
}
