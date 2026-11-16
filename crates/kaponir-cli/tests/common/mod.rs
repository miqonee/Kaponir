use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

pub fn temp_dir(prefix: &str) -> PathBuf {
    loop {
        let sequence = NEXT_TEMP.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "kaponir-{prefix}-{}-{sequence}",
            std::process::id()
        ));
        // Clock precision must not let parallel tests share a vault. Atomic
        // creation also avoids reusing directories left by an earlier process.
        match std::fs::create_dir(&path) {
            Ok(()) => return path,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => panic!("cannot create isolated test directory: {error}"),
        }
    }
}
