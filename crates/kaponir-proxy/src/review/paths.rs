//! Suggest one directory decision for distinct absolute paths in the same rule scope.
use super::path_prefixes::directories;
use super::*;
use kaponir_core::rules::Rule;

// A home identifies an account even when only one distinct file has been seen.
pub(super) fn account_directory(item: &Value) -> Option<&str> {
    if item["kind"] != "PATH" || item["protected"] == true || item["path_suffix"] == true {
        return None;
    }
    let path = item["value"].as_str()?;
    let prefix = directories(path).first().copied()?;
    let windows = prefix.get(3..).is_some_and(|tail| {
        tail.split('\\')
            .next()
            .is_some_and(|root| root.eq_ignore_ascii_case("Users"))
    }) && prefix.get(1..3) == Some(":\\");
    (prefix.starts_with("/home/") || prefix.starts_with("/Users/") || windows).then_some(prefix)
}

pub(crate) fn selected_prefix<'a>(row: &Value, prefix: &'a str) -> Option<&'a str> {
    (row["kind"] == "PATH"
        && needs_decision(row)
        && directories(row["value"].as_str()?).contains(&prefix))
    .then_some(prefix)
}

pub(super) fn scope(item: &Value) -> Scope {
    serde_json::from_value(
        item.get("scope")
            .filter(|s| s.is_string())
            .unwrap_or(&item["recommended_scope"])
            .clone(),
    )
    .unwrap_or_else(|_| kaponir_core::s1::default_scope_for_kind("PATH"))
}

pub(crate) fn pending_id(kind: &str, scope: Scope, value: &str) -> String {
    if kind == "PATH"
        && let Some(prefix) = directories(value).first()
    {
        return format!("\0path:{}:{prefix}", scope.as_str());
    }
    value.to_owned()
}

pub(super) fn group(rows: Vec<Value>) -> Vec<Value> {
    let mut buckets: HashMap<(Scope, String), Vec<usize>> = HashMap::new();
    for (index, item) in rows.iter().enumerate() {
        if item["kind"] == "PATH"
            && item["protected"] != true
            && item["path_suffix"] != true
            && item["unmatched_rule"].is_null()
            && (item["proposal"]["declined"] != false
                || (item["proposal"]["action"] == "mask"
                    && item["proposal"]["kind"] == "PATH"
                    && item["proposal"]["scope"] == json!(scope(item))))
            && needs_decision(item)
            && let Some(value) = item["value"].as_str()
            && let Some(prefix) = directories(value).first()
        {
            buckets
                .entry((scope(item), prefix.to_string()))
                .or_default()
                .push(index);
        }
    }
    let mut rows: Vec<_> = rows.into_iter().map(Some).collect();
    for ((scope, prefix), positions) in buckets {
        let mut paths: BTreeMap<String, (Vec<Value>, u64)> = BTreeMap::new();
        let mut keys = Vec::new();
        let mut sources = BTreeSet::new();
        for &index in &positions {
            let row = rows[index].as_ref().unwrap();
            sources.insert(
                row["source"]
                    .as_str()
                    .or(row["session"].as_str())
                    .or(row["origin"].as_str())
                    .unwrap_or_default(),
            );
            let path = paths
                .entry(row["value"].as_str().unwrap().to_owned())
                .or_default();
            path.0.push(row["key"].clone());
            path.1 += row["count"].as_u64().unwrap_or(1).max(1);
            keys.push(row["key"].clone());
        }
        if paths.len() < 2 {
            continue;
        }
        let mut counts = BTreeMap::new();
        for path in paths.keys() {
            for p in directories(path) {
                *counts.entry(p.to_owned()).or_insert(0usize) += 1;
            }
        }
        let choices: Vec<_> = counts
            .into_iter()
            .filter_map(|(p, n)| (n >= 2).then_some(p))
            .collect();
        let records: Vec<_> = paths
            .into_iter()
            .map(|(value, (keys, count))| json!({"value":value,"keys":keys,"count":count}))
            .collect();
        let source_count = sources.len();
        let Some((&first, others)) = positions.split_first() else {
            continue;
        };
        let item = rows[first].as_mut().unwrap();
        item["value"] = json!(prefix);
        item["scope"] = json!(scope);
        item["path_group"] =
            json!({"prefix":prefix,"choices":choices,"paths":records,"source_count":source_count});
        item["occurrence_keys"] = json!(keys);
        for field in [
            "proposal",
            "recommendation",
            "in_dataset",
            "deferred_at",
            "account_directory",
        ] {
            item.as_object_mut().unwrap().remove(field);
        }
        for &index in others {
            rows[index] = None;
        }
    }
    rows.into_iter().flatten().collect()
}

#[derive(Deserialize)]
pub(super) struct ApplyPrefix {
    keys: Vec<String>,
    prefix: String,
    scope: Scope,
}

pub(super) async fn apply(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(req): Json<ApplyPrefix>,
) -> ResultJson {
    authorized(&st, &headers, true)?;
    let _guard = st.rule_changes.lock().await;
    let sources = st
        .with_vault(|_, v| {
            req.keys
                .iter()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .map(|key| {
                    let (origin, id) = key
                        .split_once(':')
                        .ok_or_else(|| anyhow::anyhow!("entry_not_found"))?;
                    raw_finding(v, origin, id)?.ok_or_else(|| anyhow::anyhow!("entry_not_found"))
                })
                .collect::<anyhow::Result<Vec<_>>>()
        })
        .map_err(|_| bad("entry_not_found"))?;
    if sources.iter().any(|row| scope(row) != req.scope)
        || sources
            .windows(2)
            .all(|pair| pair[0]["value"] == pair[1]["value"])
    {
        return Err(bad("path_group_changed"));
    }
    apply_directory(&st, sources, &req.prefix, req.scope, None)
}

// The caller holds rule_changes and has verified the decision or group that the user saw.
pub(super) fn apply_directory(
    st: &Shared,
    sources: Vec<Value>,
    prefix: &str,
    target_scope: Scope,
    seen_key: Option<&str>,
) -> ResultJson {
    let ctl = st.control.as_ref().ok_or_else(|| bad("read_only"))?;
    for row in &sources {
        let value = row["value"].as_str().unwrap_or_default();
        if row["kind"] != "PATH"
            || row["protected"] == true
            || !directories(value).contains(&prefix)
        {
            return Err(bad("path_group_changed"));
        }
    }
    let rule =
        Rule::for_directory(prefix, target_scope).ok_or_else(|| bad("path_group_changed"))?;
    let mut sets = ctl.rules().map_err(internal)?;
    let current = ActiveRules::new(sets.clone()).map_err(internal)?;
    let set = sets
        .iter_mut()
        .find(|s| s.scope == target_scope)
        .ok_or_else(|| bad("invalid_scope"))?;
    let mut existing = false;
    let mut disabled = None;
    if let Some((index, old)) = set
        .file
        .rules
        .iter()
        .enumerate()
        .find(|(_, r)| r.id == rule.id)
    {
        if old.directory_prefix(target_scope) != Some(prefix) {
            return Err(bad("rule_was_edited"));
        }
        if old.disabled {
            disabled = Some((
                index,
                blake3::hash(&serde_json::to_vec(&set.file).map_err(|e| internal(e.into()))?)
                    .to_hex()
                    .to_string(),
            ));
            set.file.rules[index].disabled = false;
        } else {
            existing = true;
        }
    } else {
        set.file.rules.push(rule.clone());
    }
    let prospective = ActiveRules::new(sets).map_err(internal)?;
    let mut projected = Vec::new();
    for mut row in sources {
        current.project(&mut row);
        if !row["unmatched_rule"].is_null() {
            return Err(unmatched_rule_error(&row));
        }
        if !(needs_decision(&row)
            || (row["state"] == "covered" && row["document_rule"]["id"] == rule.id))
        {
            return Err(bad("path_group_changed"));
        }
        prospective.project(&mut row);
        if row["document_rule"]["id"] != rule.id {
            if let Some(conflict) = prospective.directory_conflict(&row, &rule.id, prefix) {
                return Err(bad("directory_rule_conflict").with_details(json!({
                    "finding":finding_excerpt(&row), "conflict":conflict
                })));
            }
            return Err(bad("directory_rule_misses_occurrence")
                .with_details(json!({"finding":finding_excerpt(&row)})));
        }
        projected.push(row);
    }
    if let Some((i, rev)) = disabled {
        ctl.set_rule_disabled(target_scope, i, &rev, false)
            .map_err(internal)?;
    } else if !existing {
        ctl.add_rule(target_scope, rule).map_err(internal)?;
    }
    st.replace_engine(ctl.build_engine().map_err(internal)?);
    let applied_rule = projected.first().map(|row| &row["document_rule"]);
    let dataset = st
        .with_vault(|_, v| {
            learning::transaction(v, || {
                let mut drafts = 0;
                let mut checked = 0;
                if crate::dataset::capture_enabled(v)? {
                    for row in &projected {
                        let seen = seen_key.is_some() && seen_key == row["key"].as_str();
                        match crate::dataset::capture_finding(v, row, Some(("mask", "PATH")), seen)?
                        {
                            crate::dataset::Capture::Draft => drafts += 1,
                            crate::dataset::Capture::Checked => checked += 1,
                            crate::dataset::Capture::Unchanged => {}
                        }
                    }
                }
                Ok(json!({"drafts":drafts,"checked":checked}))
            })
        })
        .unwrap_or_else(|_| json!({"error":true}));
    Ok(Json(
        json!({"ok":true,"state":"covered","already_covered":existing,"dataset":dataset,"rule":applied_rule}),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_single_path_only_suggests_a_private_account_directory() {
        let row = |value: &str| json!({"kind":"PATH","value":value,"state":"pending"});
        for (value, prefix) in [
            (
                "/home/group.demo/alice.smith/.local/bin/kaponir",
                "/home/group.demo/alice.smith/",
            ),
            ("/Users/алиса/private/log", "/Users/алиса/"),
            (r"C:\Users\user\work\file", r"C:\Users\user\"),
        ] {
            assert_eq!(account_directory(&row(value)), Some(prefix));
        }
        for value in [
            "/home/group.demo/Shared/logs/file",
            "/tmp/x/file",
            "/usr/bin/kaponir",
            r"C:\Users\Public\notes.txt",
            "/projects/app/docs/plan",
            "/home/user/../other/file",
        ] {
            assert_eq!(account_directory(&row(value)), None, "{value}");
        }
        let mut path = row("/home/user/notes/file");
        path["path_suffix"] = json!(true);
        assert_eq!(account_directory(&path), None);
    }

    #[test]
    fn repeats_and_different_scopes_cannot_create_a_directory_group() {
        let base = json!({"key":"a","value":"/home/alice/plan.txt","kind":"PATH","scope":"team","state":"open"});
        let mut other = base.clone();
        other["key"] = json!("b");
        assert_eq!(group(vec![base.clone(), other.clone()]).len(), 2);
        other["value"] = json!("/home/alice/notes.txt");
        other["scope"] = json!("personal");
        assert_eq!(group(vec![base.clone(), other]).len(), 2);
        let scoped = json!({"recommended_scope":"personal"});
        assert_eq!(scope(&scoped), Scope::Personal);
    }

    #[test]
    fn an_edited_or_disabled_rule_does_not_skip_filename_detection() {
        let prefix = "/home/alice/";
        let rule = Rule::for_directory(prefix, Scope::Team).unwrap();
        let mut set = kaponir_core::rules::ScopedRules {
            scope: Scope::Team,
            file: Default::default(),
        };
        set.file.rules.push(rule.clone());
        assert!(set.lexicon().reviewed_directories.contains(prefix));
        set.file.rules[0].disabled = true;
        assert!(set.lexicon().reviewed_directories.is_empty());
        set.file.rules[0] = rule;
        set.file.rules[0].regex = Some("other".into());
        assert!(set.lexicon().reviewed_directories.is_empty());
    }
}
