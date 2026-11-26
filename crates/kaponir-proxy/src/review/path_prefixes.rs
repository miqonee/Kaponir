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
        let domain_home = sep == '/'
            && root == "home"
            && (["group.", "domain.", "corp."]
                .iter()
                .any(|p| user.starts_with(p))
                || [".local", ".corp", ".internal"]
                    .iter()
                    .any(|tail| user.ends_with(tail)));
        let depth = if domain_home { 3 } else { 2 };
        if parts.get(depth - 1).is_none_or(|account| {
            [
                "shared",
                "public",
                "default",
                "default user",
                "common",
                "all users",
                "guest",
                "общие",
            ]
            .iter()
            .any(|name| account.eq_ignore_ascii_case(name))
        }) {
            return vec![];
        }
        depth
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

#[cfg(test)]
mod tests {
    use super::directories;

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
}
