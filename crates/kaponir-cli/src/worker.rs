//! Packaged Python worker; it is a separate process and never runs in a proxy request.
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Settings {
    pub listen: String,
    pub input_root: PathBuf,
    pub data_dir: PathBuf,
    pub model_dir: PathBuf,
    pub model: String,
    pub threads: u32,
    pub max_files: u32,
    pub max_file_bytes: u64,
    pub max_pages: u32,
    pub max_edge: u32,
    pub ocr_timeout_seconds: u32,
    pub runtime: Option<PathBuf>,
    pub api_url: Option<String>,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:8788".into(),
            input_root: "inbox".into(),
            data_dir: "worker".into(),
            model_dir: "models".into(),
            model: "q4".into(),
            threads: 2,
            max_files: 1000,
            max_file_bytes: 16777216,
            max_pages: 100,
            max_edge: 1024,
            ocr_timeout_seconds: 300,
            runtime: None,
            api_url: None,
        }
    }
}

pub fn run(
    home: &crate::Home,
    args: &[std::ffi::OsString],
) -> anyhow::Result<std::process::ExitCode> {
    let python = std::env::var_os("KAPONIR_WORKER_PYTHON").unwrap_or_else(|| "python3".into());
    let mut command = std::process::Command::new(python);
    command
        .arg("-c")
        .arg(include_str!("../../../scripts/kaponir_worker.py"))
        .args(args)
        .env("KAPONIR_HOME", &home.dir)
        .env("KAPONIR_EXECUTABLE", std::env::current_exe()?);
    #[cfg(unix)]
    {
        // Python becomes PID 1 in the worker container and receives stop signals directly.
        use std::os::unix::process::CommandExt;
        Err(command.exec().into())
    }
    #[cfg(not(unix))]
    {
        let status = command.status()?;
        Ok(std::process::ExitCode::from(
            status.code().unwrap_or(1) as u8
        ))
    }
}

impl Settings {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            matches!(self.model.as_str(), "q4" | "q5"),
            "worker model must be q4 or q5"
        );
        for (name, value, maximum) in [
            ("threads", u64::from(self.threads), 32),
            ("max_files", u64::from(self.max_files), 10000),
            ("max_file_bytes", self.max_file_bytes, 134217728),
            ("max_pages", u64::from(self.max_pages), 1000),
            ("max_edge", u64::from(self.max_edge), 1536),
            (
                "ocr_timeout_seconds",
                u64::from(self.ocr_timeout_seconds),
                1800,
            ),
        ] {
            anyhow::ensure!(
                (1..=maximum).contains(&value),
                "invalid worker setting: {name}"
            );
        }
        Ok(())
    }
}
