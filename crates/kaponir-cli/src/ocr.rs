use crate::Home;
use anyhow::{Context, Result, ensure};
use std::fs::{self, DirBuilder, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::Command;

const OCR_REQUIREMENTS: &str = include_str!("../../../scripts/requirements-ocr.txt");
const WORKER_REQUIREMENTS: &str = include_str!("../../../scripts/requirements-worker.txt");
const SCRIPT: &str = include_str!("../../../scripts/kaponir_rapidocr.py");

pub(crate) fn runtime_dir(home: &Home) -> PathBuf {
    let requirements = format!("{OCR_REQUIREMENTS}\n{WORKER_REQUIREMENTS}");
    let version = blake3::hash(requirements.as_bytes()).to_hex();
    home.dir
        .join("ocr")
        .join(format!("venv-{}", &version[..16]))
}

fn interpreter(dir: &Path) -> PathBuf {
    dir.join(if cfg!(windows) {
        "Scripts/python.exe"
    } else {
        "bin/python"
    })
}

pub fn managed_python(home: &Home) -> Option<PathBuf> {
    let dir = runtime_dir(home);
    let python = interpreter(&dir);
    (dir.join(".ready").is_file() && python.is_file()).then_some(python)
}

impl kaponir_proxy::OcrRuntime for Home {
    fn managed_python(&self) -> Option<PathBuf> {
        managed_python(self)
    }

    fn install(&self) -> Result<()> {
        setup(self, default_python())
    }
}

fn check(python: &Path) -> Result<()> {
    let status = Command::new(python)
        .args(["-I", "-c", SCRIPT, "--check"])
        .status()?;
    ensure!(
        status.success(),
        "RapidOCR could not load its bundled models"
    );
    let status = Command::new(python)
        .args(["-I", "-c", "import pypdfium2; from PIL import Image"])
        .status()?;
    ensure!(status.success(), "PDFium or Pillow could not load");
    Ok(())
}

fn install_cyrillic(python: &Path) -> Result<()> {
    let status = Command::new(python)
        .args(["-I", "-c", SCRIPT, "--install-cyrillic"])
        .status()
        .context("installing the Cyrillic RapidOCR model")?;
    ensure!(
        status.success(),
        "could not install the Cyrillic RapidOCR model; check access to ModelScope"
    );
    Ok(())
}

pub fn default_python() -> &'static Path {
    Path::new(if cfg!(windows) { "python" } else { "python3" })
}

pub fn setup(home: &Home, python: &Path) -> Result<()> {
    let dir = runtime_dir(home);
    let root = dir.parent().expect("OCR runtime parent");
    let mut mkdir = DirBuilder::new();
    mkdir.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        mkdir.mode(0o700);
    }
    mkdir.create(root)?;
    let lock = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .open(root.join("setup.lock"))?;
    lock.lock().context("locking OCR setup")?;
    if let Some(managed) = managed_python(home)
        && check(&managed).is_ok()
    {
        install_cyrillic(&managed)?;
        println!("RapidOCR is ready in {}", managed.display());
        return Ok(());
    }

    // An incomplete environment is never selected by the server. Recreate it
    // in place because virtual-environment entry points use absolute paths.
    let ready = dir.join(".ready");
    if ready.try_exists()? {
        fs::remove_file(&ready)?;
    }
    let status = Command::new(python)
        .args(["-I", "-m", "venv", "--clear"])
        .arg(&dir)
        .status()
        .with_context(|| format!("running {} for OCR setup", python.display()))?;
    ensure!(
        status.success(),
        "could not create OCR environment; install Python with venv support or pass --python"
    );

    fs::write(dir.join("requirements-ocr.txt"), OCR_REQUIREMENTS)?;
    let requirements = dir.join("requirements-worker.txt");
    fs::write(&requirements, WORKER_REQUIREMENTS)?;
    let managed = interpreter(&dir);
    let status = Command::new(&managed)
        .args([
            "-I",
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "--disable-pip-version-check",
            "-r",
        ])
        .arg(&requirements)
        .status()
        .context("installing RapidOCR dependencies")?;
    ensure!(
        status.success(),
        "OCR dependency installation failed; try a supported Python such as 3.12 or 3.13 with --python"
    );
    check(&managed)?;
    fs::write(ready, b"")?;
    install_cyrillic(&managed)?;
    println!("RapidOCR is ready in {}.", managed.display());
    Ok(())
}
