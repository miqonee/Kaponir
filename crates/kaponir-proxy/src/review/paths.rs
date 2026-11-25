//! Suggest one directory decision for distinct absolute paths in the same rule scope.
use super::*;
use kaponir_core::rules::Rule;

pub(super) fn directories(path: &str) -> Vec<&str> {
    if path.len() > 4096 || path.chars().any(char::is_control) {
        return vec![];
    }
    let (head, sep) = if path.starts_with('/') && !path.contains('\\') {
        (1, '/')
    } else if path.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
        && path.get(1..3) == Some(":\\")
        && !path.contains('/')
    {
        (3, '\\')
    } else {
        return vec![];
    };
    let parts: Vec<_> = path[head..].split(sep).collect();
    if !kaponir_core::rules::unambiguous_path(path, sep) || parts.contains(&"") {
        return vec![];
    }
    let root = parts[0];
    let home = (sep == '/' && root == "home") || root.eq_ignore_ascii_case("Users");
    let depth = if home {
        let Some(user) = parts.get(1) else {
            return vec![];
        };
        if ["shared", "public", "default", "common", "all users"]
            .iter()
            .any(|s| user.eq_ignore_ascii_case(s))
        {
            return vec![];
        }
        if sep == '/'
            && root == "home"
            && (["group.", "domain.", "corp."]
                .iter()
                .any(|p| user.starts_with(p))
                || [".local", ".corp", ".internal"]
                    .iter()
                    .any(|tail| user.ends_with(tail)))
        {
            3
        } else {
            2
        }
    } else if matches!(root, "workspaces" | "projects" | "repos" | "work") {
        2
    } else if root == "srv"
        && parts
            .get(1)
            .is_some_and(|s| matches!(*s, "projects" | "repos"))
    {
        3
    } else if matches!(root, "opt" | "srv")
        && parts.get(1).is_some_and(|name| {
            !matches!(
                name.to_ascii_lowercase().as_str(),
                "bin" | "lib" | "share" | "www" | "log" | "data" | "tmp" | "apps"
            )
        })
    {
        2
    } else {
        return vec![];
    };
    path[head..]
        .match_indices(sep)
        .skip(depth - 1)
        .map(|(end, _)| &path[..head + end + 1])
        .collect()
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
        for field in ["proposal", "recommendation", "in_dataset", "deferred_at"] {
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
    let ctl = st.control.as_ref().ok_or_else(|| bad("read_only"))?;
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
    let mut distinct = HashSet::new();
    for row in &sources {
        let value = row["value"].as_str().unwrap_or_default();
        if row["kind"] != "PATH"
            || row["protected"] == true
            || scope(row) != req.scope
            || !directories(value).contains(&req.prefix.as_str())
        {
            return Err(bad("path_group_changed"));
        }
        distinct.insert(value);
    }
    if distinct.len() < 2 {
        return Err(bad("path_group_changed"));
    }
    let rule =
        Rule::for_directory(&req.prefix, req.scope).ok_or_else(|| bad("path_group_changed"))?;
    let mut sets = ctl.rules().map_err(internal)?;
    let current = ActiveRules::new(sets.clone()).map_err(internal)?;
    let set = sets
        .iter_mut()
        .find(|s| s.scope == req.scope)
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
        if old.directory_prefix(req.scope) != Some(req.prefix.as_str()) {
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
        if !(needs_decision(&row)
            || (row["state"] == "covered" && row["document_rule"]["id"] == rule.id))
        {
            return Err(bad("path_group_changed"));
        }
        prospective.project(&mut row);
        if row["document_rule"]["id"] != rule.id {
            return Err(bad("rule_misses_occurrence"));
        }
        projected.push(row);
    }
    if let Some((i, rev)) = disabled {
        ctl.set_rule_disabled(req.scope, i, &rev, false)
            .map_err(internal)?;
    } else if !existing {
        ctl.add_rule(req.scope, rule).map_err(internal)?;
    }
    st.replace_engine(ctl.build_engine().map_err(internal)?);
    let dataset = st
        .with_vault(|_, v| {
            learning::transaction(v, || {
                let mut drafts = 0;
                if crate::dataset::capture_enabled(v)? {
                    for row in &projected {
                        if matches!(
                            crate::dataset::capture_finding(v, row, Some(("mask", "PATH")), false)?,
                            crate::dataset::Capture::Draft
                        ) {
                            drafts += 1;
                        }
                    }
                }
                Ok(json!({"drafts":drafts}))
            })
        })
        .unwrap_or_else(|_| json!({"error":true}));
    Ok(Json(
        json!({"ok":true,"state":"covered","already_covered":existing,"dataset":dataset}),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn directories_stop_at_component_and_account_boundaries() {
        assert_eq!(
            directories("/home/group.s7/a.b/dir/x"),
            ["/home/group.s7/a.b/", "/home/group.s7/a.b/dir/"]
        );
        assert_eq!(
            directories(r"C:\Users\Иван\work\x"),
            [r"C:\Users\Иван\", r"C:\Users\Иван\work\"]
        );
        assert_eq!(directories("/Users/alice/work/x")[0], "/Users/alice/");
        for path in [
            "/home/file",
            "/tmp/common/x",
            "/usr/share/x",
            "/Users/Shared/dir/x",
            "../home/name/x",
            "/home/alice/../bob/x",
            "https://example.com/x",
            "/home/group.s7/a.b",
        ] {
            assert!(directories(path).is_empty(), "{path}");
        }
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
