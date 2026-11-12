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
    let python = std::env::var_os("KAPONIR_WORKER_PYTHON").unwrap_or_else(|| {
        if cfg!(windows) {
            "python".into()
        } else {
            "python3".into()
        }
    });
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
            kaponir_core::models::valid_local_id(&self.model),
            "unsupported local worker model; select a built-in model or a registered Hugging Face artifact"
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

/// Owns the local worker child; the worker itself persists the activation state.
pub struct ManagedWorker {
    home: PathBuf,
    endpoint: String,
    executable: PathBuf,
    api_url: String,
    api_token: Option<String>,
    child: std::sync::Mutex<Option<std::process::Child>>,
}
impl ManagedWorker {
    pub fn new(
        home: &crate::Home,
        api_url: String,
        api_token: Option<String>,
    ) -> anyhow::Result<Self> {
        let cfg = home.load_config()?;
        let listen = std::env::var("KAPONIR_WORKER_LISTEN").unwrap_or(cfg.worker.listen);
        let addr: std::net::SocketAddr = listen
            .parse()
            .map_err(|_| anyhow::anyhow!("local worker requires a loopback IP address"))?;
        anyhow::ensure!(
            addr.ip().is_loopback(),
            "local worker must listen on loopback"
        );
        Ok(Self {
            executable: std::env::current_exe()?,
            home: home.dir.clone(),
            endpoint: format!("http://{addr}"),
            api_url,
            api_token,
            child: std::sync::Mutex::new(None),
        })
    }
    pub fn activation_enabled(&self) -> bool {
        let home = crate::Home::new(self.home.clone());
        let Ok(cfg) = home.load_config() else {
            return false;
        };
        let path = if cfg.worker.data_dir.is_absolute() {
            cfg.worker.data_dir
        } else {
            self.home.join(cfg.worker.data_dir)
        };
        std::fs::read(path.join("activation.json"))
            .ok()
            .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
            .is_some_and(|v| v["enabled"] == true)
    }
}
impl kaponir_proxy::WorkerManager for ManagedWorker {
    fn endpoint(&self) -> String {
        self.endpoint.clone()
    }
    fn start(&self) -> anyhow::Result<()> {
        let mut guard = self
            .child
            .lock()
            .map_err(|_| anyhow::anyhow!("worker lock failed"))?;
        if let Some(child) = guard.as_mut()
            && child.try_wait()?.is_none()
        {
            return Ok(());
        }
        let addr = self.endpoint.trim_start_matches("http://");
        let probe = std::net::TcpListener::bind(addr).map_err(|_| anyhow::anyhow!("worker port is already in use; configure the existing worker URL or choose a free port"))?;
        drop(probe);
        let home = crate::Home::new(self.home.clone());
        let cfg = home.load_config()?;
        let input_root = if cfg.worker.input_root.is_absolute() {
            cfg.worker.input_root.clone()
        } else {
            self.home.join(&cfg.worker.input_root)
        };
        std::fs::create_dir_all(input_root)?;
        let log_dir = if cfg.worker.data_dir.is_absolute() {
            cfg.worker.data_dir
        } else {
            self.home.join(cfg.worker.data_dir)
        };
        std::fs::create_dir_all(&log_dir)?;
        let mut options = std::fs::OpenOptions::new();
        options.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let log = options.open(log_dir.join("process.log"))?;
        let mut cmd = std::process::Command::new(&self.executable);
        cmd.args(["--home"])
            .arg(&self.home)
            .args(["worker", "serve", "--idle"])
            .env("KAPONIR_WORKER_API_URL", &self.api_url)
            .stdin(std::process::Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log);
        if let Some(token) = &self.api_token {
            cmd.env("KAPONIR_API_TOKEN", token);
        }
        *guard = Some(cmd.spawn()?);
        Ok(())
    }
    fn stop(&self) -> anyhow::Result<()> {
        let mut guard = self
            .child
            .lock()
            .map_err(|_| anyhow::anyhow!("worker lock failed"))?;
        if let Some(mut child) = guard.take() {
            if child.try_wait()?.is_some() {
                return Ok(());
            }
            #[cfg(unix)]
            {
                let _ = std::process::Command::new("kill")
                    .args(["-TERM", &child.id().to_string()])
                    .status();
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
                while child.try_wait()?.is_none() && std::time::Instant::now() < deadline {
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
                if child.try_wait()?.is_none() {
                    child.kill()?;
                }
                child.wait()?;
            }
            #[cfg(not(unix))]
            {
                child.kill()?;
                child.wait()?;
            }
        }
        Ok(())
    }
    fn running(&self) -> bool {
        self.child.lock().ok().is_some_and(|mut guard| {
            guard
                .as_mut()
                .is_some_and(|c| c.try_wait().ok().is_some_and(|v| v.is_none()))
        })
    }
}
impl Drop for ManagedWorker {
    fn drop(&mut self) {
        if let Ok(guard) = self.child.get_mut()
            && let Some(child) = guard.as_mut()
            && child.try_wait().ok().is_some_and(|status| status.is_none())
        {
            // The worker handles SIGTERM and checkpoints its active job.
            #[cfg(unix)]
            {
                let _ = std::process::Command::new("kill")
                    .args(["-TERM", &child.id().to_string()])
                    .status();
            }
            #[cfg(not(unix))]
            {
                let _ = child.kill();
            }
            let _ = child.wait();
        }
    }
}
