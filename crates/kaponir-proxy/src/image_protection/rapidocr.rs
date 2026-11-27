use super::{Code, Failure, ocr::OUTPUT_LIMIT};
use std::{
    ffi::{OsStr, OsString},
    path::PathBuf,
    process::Stdio,
    sync::Mutex,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::{Child, Command},
    sync::Semaphore,
};

const SCRIPT: &str = include_str!("../../../../scripts/kaponir_rapidocr.py");
const CHARACTER_BOXES: u32 = 1 << 31;

pub(crate) struct LocalOcr {
    pub(crate) python: Option<PathBuf>,
    pub(crate) runtime: Option<Box<dyn crate::OcrRuntime>>,
    slots: Semaphore,
    idle: Mutex<Vec<(OsString, Child)>>,
}

impl Default for LocalOcr {
    fn default() -> Self {
        Self {
            python: None,
            runtime: None,
            slots: Semaphore::new(4),
            idle: Mutex::default(),
        }
    }
}

impl LocalOcr {
    pub(super) fn python(&self) -> OsString {
        std::env::var_os("KAPONIR_OCR_PYTHON")
            .or_else(|| {
                self.runtime
                    .as_ref()?
                    .managed_python()
                    .map(PathBuf::into_os_string)
            })
            .or_else(|| self.python.clone().map(PathBuf::into_os_string))
            .or_else(|| std::env::var_os("KAPONIR_WORKER_PYTHON"))
            .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into())
    }

    fn spawn(python: &OsStr) -> Result<Child, Failure> {
        let threads = std::env::var("KAPONIR_OCR_THREADS").unwrap_or_else(|_| {
            std::thread::available_parallelism()
                .map_or(1, std::num::NonZeroUsize::get)
                .min(4)
                .to_string()
        });
        Command::new(python)
            .args(["-u", "-c", SCRIPT, &threads])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| Failure::new(Code::Unavailable))
    }

    pub(super) async fn call(&self, png: &[u8], character_boxes: bool) -> Result<Vec<u8>, Failure> {
        let _slot = self
            .slots
            .acquire()
            .await
            .map_err(|_| Failure::new(Code::Unavailable))?;
        let python = self.python();
        let mut child = {
            let mut idle = self
                .idle
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            idle.retain(|(interpreter, _)| interpreter == &python);
            idle.pop().map(|(_, child)| child)
        };
        // The current call owns the child. Cancelling it drops an incomplete
        // exchange so the next request cannot read its OCR result.
        if child
            .as_mut()
            .is_some_and(|child| child.try_wait().ok() != Some(None))
        {
            child = None;
        }
        let mut child = match child {
            Some(child) => child,
            None => Self::spawn(&python)?,
        };
        let data = Self::exchange(&mut child, png, character_boxes).await?;
        self.idle
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((python, child));
        Ok(data)
    }

    async fn exchange(
        child: &mut Child,
        png: &[u8],
        character_boxes: bool,
    ) -> Result<Vec<u8>, Failure> {
        let size = u32::try_from(png.len()).map_err(|_| Failure::new(Code::Limits))?;
        let frame = size | if character_boxes { CHARACTER_BOXES } else { 0 };
        let io_error = |_| Failure::new(Code::Unavailable);
        let input = child.stdin.as_mut().expect("piped OCR input");
        input.write_u32(frame).await.map_err(io_error)?;
        input.write_all(png).await.map_err(io_error)?;
        input.flush().await.map_err(io_error)?;
        let output = child.stdout.as_mut().expect("piped OCR output");
        let status = output.read_u8().await.map_err(io_error)?;
        let size = output.read_u32().await.map_err(io_error)? as usize;
        if size > OUTPUT_LIMIT {
            return Err(Failure::new(Code::Limits));
        }
        match status {
            0 => {}
            2 => return Err(Failure::new(Code::Unreadable)),
            3 => return Err(Failure::new(Code::Limits)),
            _ => return Err(Failure::new(Code::Unavailable)),
        }
        let mut data = vec![0; size];
        output.read_exact(&mut data).await.map_err(io_error)?;
        Ok(data)
    }
}
