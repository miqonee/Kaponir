//! Model JSON must have a single unambiguous meaning, including nested objects.
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};
use std::fmt;

struct Unambiguous(Value);

impl<'de> Deserialize<'de> for Unambiguous {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct JsonVisitor;
        impl<'de> Visitor<'de> for JsonVisitor {
            type Value = Unambiguous;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("unambiguous JSON")
            }
            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(Unambiguous(Value::Null))
            }
            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Self::Value, E> {
                Ok(Unambiguous(Value::Bool(value)))
            }
            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Self::Value, E> {
                Ok(Unambiguous(Value::Number(value.into())))
            }
            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Self::Value, E> {
                Ok(Unambiguous(Value::Number(value.into())))
            }
            fn visit_f64<E: de::Error>(self, value: f64) -> Result<Self::Value, E> {
                Number::from_f64(value)
                    .map(|number| Unambiguous(Value::Number(number)))
                    .ok_or_else(|| de::Error::custom("invalid_json_number"))
            }
            fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
                Ok(Unambiguous(Value::String(value.into())))
            }
            fn visit_string<E: de::Error>(self, value: String) -> Result<Self::Value, E> {
                Ok(Unambiguous(Value::String(value)))
            }
            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut sequence: A,
            ) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(Unambiguous(value)) = sequence.next_element()? {
                    values.push(value);
                }
                Ok(Unambiguous(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut values = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if values.contains_key(&key) {
                        return Err(de::Error::custom("duplicate_json_key"));
                    }
                    let Unambiguous(value) = map.next_value()?;
                    values.insert(key, value);
                }
                Ok(Unambiguous(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(JsonVisitor)
    }
}

pub(crate) fn parse(text: &str) -> serde_json::Result<Value> {
    let mut deserializer = serde_json::Deserializer::from_str(text);
    let Unambiguous(value) = Unambiguous::deserialize(&mut deserializer)?;
    deserializer.end()?;
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested_and_escaped_duplicates_cannot_override_a_decision() {
        for text in [
            r#"{"decisions":[{"action":"mask","action":"keep"}]}"#,
            r#"{"id":"one","\u0069d":"two"}"#,
            r#"{"decisions":[],"decisions":[{}]}"#,
        ] {
            assert!(parse(text).is_err());
        }
    }

    #[test]
    fn complete_valid_json_keeps_types_and_unicode() {
        let text =
            r#"{"values":[null,true,-4,18446744073709551615,0.75,"цена"],"other":{"id":"two"}}"#;
        assert_eq!(
            parse(text).unwrap(),
            serde_json::from_str::<Value>(text).unwrap()
        );
        for text in ["{} {}", "{\"confidence\":NaN}", "{\"confidence\":1e9999}"] {
            assert!(parse(text).is_err());
        }
    }
}
