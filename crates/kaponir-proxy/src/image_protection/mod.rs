//! Image and PDF protection in the request path. OCR data never enters the text vault or S1.
mod layout;
mod ocr;
mod pdf;
mod rapidocr;
mod raster;
mod web;
pub(crate) mod wire;

use axum::http::StatusCode;
use base64::{Engine as _, engine::general_purpose::STANDARD};
use image::RgbImage;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    ops::Range,
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, Ordering::Relaxed},
    },
    time::{Duration, Instant},
};

use crate::{
    Shared,
    traffic::{EventInspector, Span, now_ms},
};
pub use raster::Rect;
pub(crate) use web::routes;

const SETTINGS: &str = "image-protection:v1";
const MAX_IMAGES: usize = 8;
const MAX_PDF_PAGES: usize = 15;
const PREVIEW_HOURS: u64 = 7 * 24;

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    #[default]
    Local,
    Vision,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Node {
    #[default]
    Proxy,
    Worker,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Profile {
    #[default]
    Standard,
    #[serde(rename = "ru_en")]
    RussianEnglish,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UrlMask {
    #[default]
    Full,
    Partial,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(default)]
pub struct Settings {
    pub enabled: bool,
    pub mode: Mode,
    pub node: Node,
    pub profile: Profile,
    pub languages: String,
    pub connection_id: String,
    pub model: String,
    pub approved_recipient: String,
    pub max_mib: u32,
    pub max_megapixels: u32,
    pub timeout_ms: u64,
    pub parallelism: usize,
    pub queue_limit: usize,
    pub url_mask: UrlMask,
    pub save_originals: bool,
    pub original_hours: u64,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            enabled: false,
            mode: Mode::Local,
            node: Node::Proxy,
            profile: Profile::Standard,
            languages: "rus+eng".into(),
            connection_id: String::new(),
            model: String::new(),
            approved_recipient: String::new(),
            max_mib: 16,
            max_megapixels: 20,
            timeout_ms: 20_000,
            parallelism: 2,
            queue_limit: 8,
            url_mask: UrlMask::Full,
            save_originals: false,
            original_hours: 24,
        }
    }
}
impl Settings {
    fn checked(&self) -> bool {
        self.languages.len() <= 80
            && self
                .languages
                .split('+')
                .all(|x| !x.is_empty() && x.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'))
            && self.connection_id.len() <= 80
            && self.model.len() <= 240
            && !self.model.chars().any(char::is_control)
            && self.approved_recipient.len() <= 2048
            && (1..=24).contains(&self.max_mib)
            && (1..=32).contains(&self.max_megapixels)
            && (250..=60_000).contains(&self.timeout_ms)
            && (1..=4).contains(&self.parallelism)
            && self.queue_limit <= 32
            && (1..=168).contains(&self.original_hours)
    }
    fn max_bytes(&self) -> usize {
        self.max_mib as usize * (1 << 20)
    }
    fn max_pixels(&self) -> u64 {
        u64::from(self.max_megapixels) * 1_000_000
    }
    fn max_pdf_pixels(&self) -> u64 {
        self.max_pixels() * 2
    }
    fn timeout(&self) -> Duration {
        Duration::from_millis(self.timeout_ms)
    }
    fn request_timeout(&self, has_pdf: bool) -> Duration {
        if has_pdf {
            (self.timeout() * 2).min(Duration::from_secs(60))
        } else {
            self.timeout()
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Code {
    InlineRequired,
    Format,
    InvalidImage,
    InvalidPdf,
    PdfUnavailable,
    Animated,
    Limits,
    Unavailable,
    ProfileUnavailable,
    Languages,
    Empty,
    Unreadable,
    Boundaries,
    Timeout,
    QueueFull,
    Cancelled,
    Encoding,
    Rules,
    Storage,
    Consent,
    VisionAddress,
    VisionImages,
    VisionCoordinates,
}
impl Code {
    fn message(self) -> &'static str {
        match self {
            Self::InlineRequired => "Attach a static PNG, JPEG, WebP or PDF as inline base64.",
            Self::Format => "Use a static PNG, JPEG, WebP or PDF.",
            Self::InvalidImage => "The image is damaged. Export it again and retry.",
            Self::InvalidPdf => "The PDF cannot be rendered. Send a valid PDF without a password.",
            Self::PdfUnavailable => {
                "PDF rendering is unavailable. Install the managed OCR runtime or add pypdfium2 and Pillow to the proxy's OCR Python."
            }
            Self::Animated => "Export one frame as a static PNG, JPEG or WebP.",
            Self::Limits => "Reduce the attachment size or send fewer images and PDF pages.",
            Self::Unavailable => "RapidOCR or the configured OCR worker is unavailable.",
            Self::ProfileUnavailable => {
                "The Russian + English OCR profile is unavailable. Install the updated RapidOCR runtime on the selected node."
            }
            Self::Languages => "Install the selected OCR languages on the worker.",
            Self::Empty | Self::Unreadable => {
                "Text could not be read confidently. Try a larger or sharper image."
            }
            Self::Boundaries => {
                "The full boundaries of the sensitive field could not be determined."
            }
            Self::Timeout => "Attachment protection timed out. Try fewer images or PDF pages.",
            Self::QueueFull => {
                "The image protection queue is full. Retry when a slot becomes available."
            }
            Self::Cancelled => "Image protection was cancelled.",
            Self::Encoding => "Could not encode the protected image.",
            Self::Rules => "Could not apply the local masking rules.",
            Self::Storage => "Could not save the protected image in the inspector.",
            Self::Consent => "Approve the exact OCR recipient in image protection settings.",
            Self::VisionAddress => "The selected Vision API address is unavailable.",
            Self::VisionImages => {
                "The selected Vision API did not return an answer for an input image."
            }
            Self::VisionCoordinates => {
                "The selected Vision API did not return usable image coordinates."
            }
        }
    }
    fn status(self) -> StatusCode {
        match self {
            Self::Limits => StatusCode::PAYLOAD_TOO_LARGE,
            Self::Timeout => StatusCode::GATEWAY_TIMEOUT,
            Self::QueueFull => StatusCode::TOO_MANY_REQUESTS,
            Self::Unavailable
            | Self::ProfileUnavailable
            | Self::PdfUnavailable
            | Self::Languages
            | Self::Consent
            | Self::VisionAddress
            | Self::VisionImages
            | Self::VisionCoordinates => StatusCode::SERVICE_UNAVAILABLE,
            Self::Rules | Self::Encoding | Self::Storage => StatusCode::INTERNAL_SERVER_ERROR,
            _ => StatusCode::UNPROCESSABLE_ENTITY,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(crate) struct Failure {
    pub code: Code,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attachment: Option<usize>,
}
impl Failure {
    fn new(code: Code) -> Self {
        Self {
            code,
            field: None,
            attachment: None,
        }
    }
    fn field(key: &str) -> Self {
        Self {
            field: Some(key.to_owned()),
            ..Self::new(Code::Boundaries)
        }
    }
    fn at(mut self, index: usize) -> Self {
        self.attachment = Some(index);
        self
    }

    pub(crate) fn api(&self) -> crate::server::ApiError {
        let message = match &self.field {
            Some(field) => format!(
                "{} ({field}). The request was stopped.",
                self.code.message()
            ),
            None => self.code.message().to_owned(),
        };
        crate::server::error(self.code.status(), message)
            .with_details(serde_json::to_value(self).expect("OCR error metadata"))
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Stage {
    #[default]
    Queued,
    Validating,
    Reading,
    Masking,
    Encoding,
    Ready,
    Sending,
    Sent,
    Disabled,
    Blocked,
    ProviderError,
    Cancelled,
    Legacy,
}

#[derive(Clone, Copy, Default, Debug, Serialize, Deserialize)]
pub(crate) struct Timings {
    pub wait_ms: u64,
    pub decode_ms: u64,
    pub ocr_ms: u64,
    pub rules_ms: u64,
    pub encode_ms: u64,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Reason {
    FieldValue,
    FieldRow,
    Detector,
    Vision,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct Area {
    pub id: usize,
    pub rectangles: Vec<Rect>,
    pub category: kaponir_core::Category,
    pub kind: String,
    pub scope: kaponir_core::Scope,
    pub reason: Reason,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub review_id: Option<i64>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct PdfPage {
    pub number: usize,
    pub total: usize,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct Attachment {
    pub id: String,
    pub message_id: String,
    pub role: String,
    pub latest: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pdf: Option<PdfPage>,
    pub width: u32,
    pub height: u32,
    pub areas: Vec<Area>,
    pub preview_until_ms: Option<u64>,
    pub original_until_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct Audit {
    pub attachments: Vec<Attachment>,
    pub stage: Stage,
    pub current: usize,
    pub timings: Timings,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure: Option<Failure>,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub(crate) struct Progress {
    pub stage: Stage,
    pub current: usize,
    pub total: usize,
}
impl From<&Audit> for Progress {
    fn from(audit: &Audit) -> Self {
        Self {
            stage: audit.stage,
            current: audit.current,
            total: audit.attachments.len(),
        }
    }
}

pub(crate) fn job_rows(st: &Shared) -> Vec<Value> {
    let snapshot = st.traffic.summary_snapshot();
    snapshot
        .active
        .into_iter()
        .chain(snapshot.events.into_iter().take(30))
        .filter_map(|event| {
            let progress = event.image_progress?;
            let state = match progress.stage {
                Stage::Queued => "queued",
                Stage::Validating
                | Stage::Reading
                | Stage::Masking
                | Stage::Encoding
                | Stage::Ready
                | Stage::Sending => "running",
                Stage::Sent => "succeeded",
                Stage::Blocked | Stage::ProviderError => "failed",
                Stage::Cancelled => "cancelled",
                Stage::Disabled | Stage::Legacy => return None,
            };
            Some(serde_json::json!({
                "family":"ocr", "kind":"ocr", "id":event.id.to_string(), "event_id":event.id,
                "session_id":event.session_id, "turn_id":event.turn.map(|t| t.id), "state":state,
                "created":event.started_at_ms, "updated":event.time_ms, "ocr":progress,
            }))
        })
        .collect()
}

#[derive(Default)]
struct Counts {
    active: usize,
    waiting: usize,
}
#[derive(Default)]
struct Queue {
    counts: Mutex<Counts>,
    changed: tokio::sync::Notify,
}
struct Ticket {
    queue: Arc<Queue>,
    active: bool,
}
impl Ticket {
    async fn acquire(queue: Arc<Queue>, settings: &Settings) -> Result<Self, Failure> {
        let mut ticket = {
            let mut counts = queue
                .counts
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if counts.active < settings.parallelism && counts.waiting == 0 {
                counts.active += 1;
                return Ok(Self {
                    queue: queue.clone(),
                    active: true,
                });
            }
            if counts.waiting >= settings.queue_limit {
                return Err(Failure::new(Code::QueueFull));
            }
            counts.waiting += 1;
            Self {
                queue: queue.clone(),
                active: false,
            }
        };
        loop {
            let notified = queue.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            {
                let mut counts = queue
                    .counts
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                if counts.active < settings.parallelism {
                    counts.active += 1;
                    counts.waiting -= 1;
                    ticket.active = true;
                    return Ok(ticket);
                }
            }
            notified.await;
        }
    }

    fn try_borrow(&self, parallelism: usize) -> Option<Self> {
        let mut counts = self
            .queue
            .counts
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if counts.waiting != 0 || counts.active >= parallelism {
            return None;
        }
        counts.active += 1;
        Some(Self {
            queue: self.queue.clone(),
            active: true,
        })
    }
}
impl Drop for Ticket {
    fn drop(&mut self) {
        let mut counts = self
            .queue
            .counts
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if self.active {
            counts.active -= 1;
        } else {
            counts.waiting -= 1;
        }
        self.queue.changed.notify_waiters();
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallFailure {
    PythonEnvironment,
    Dependencies,
    RuntimeCheck,
    CyrillicModel,
}

impl std::fmt::Display for InstallFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::PythonEnvironment => "Could not create the OCR environment. Install Python with venv support or pass --python",
            Self::Dependencies => "Could not install OCR dependencies. Check PyPI access and try Python 3.12 or 3.13 with --python",
            Self::RuntimeCheck => "could not verify the OCR runtime",
            Self::CyrillicModel => "Could not install the Cyrillic RapidOCR model. Check access to ModelScope",
        })
    }
}

impl std::error::Error for InstallFailure {}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
enum InstallState {
    #[default]
    Idle,
    Installing,
    Ready,
    Failed(Option<InstallFailure>),
}

impl InstallState {
    fn name(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Installing => "installing",
            Self::Ready => "ready",
            Self::Failed(_) => "failed",
        }
    }

    fn failure(self) -> Option<InstallFailure> {
        match self {
            Self::Failed(failure) => failure,
            _ => None,
        }
    }
}

pub(crate) struct Service {
    settings: RwLock<Settings>,
    queue: Arc<Queue>,
    pub(crate) local: rapidocr::LocalOcr,
    settings_changes: tokio::sync::Mutex<()>,
    install: Mutex<InstallState>,
}
impl Service {
    pub fn load(vault: &kaponir_vault::SqliteVault, config: &Settings) -> anyhow::Result<Self> {
        let settings = vault
            .private_setting(SETTINGS)?
            .map(|bytes| serde_json::from_slice(&bytes))
            .transpose()?
            .unwrap_or_else(|| config.clone());
        anyhow::ensure!(settings.checked(), "invalid image protection settings");
        vault.clean_traffic_images(now_ms(), true)?;
        Ok(Self {
            settings: RwLock::new(settings),
            queue: Arc::new(Queue::default()),
            local: Default::default(),
            settings_changes: Default::default(),
            install: Default::default(),
        })
    }
    fn settings(&self) -> Settings {
        self.settings
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    fn install_state(&self) -> std::sync::MutexGuard<'_, InstallState> {
        self.install
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}
fn elapsed(start: Instant) -> u64 {
    start.elapsed().as_millis() as u64
}

pub(crate) fn set_stage(span: &Span, stage: Stage) {
    update(span, |job| job.stage = stage);
}
pub(crate) fn upstream_stage(span: &Span, stage: Stage) {
    update(span, |audit| {
        if audit.stage != Stage::Disabled {
            audit.stage = stage;
        }
    });
}
fn update(span: &Span, f: impl FnOnce(&mut Audit)) {
    if let Some(audit) = span
        .inspector
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .as_mut()
        .and_then(|inspector| inspector.images.as_mut())
    {
        f(audit);
    }
}

pub(crate) fn start_cleanup(st: &Shared) {
    let weak = Arc::downgrade(st);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(60)).await;
            let Some(st) = weak.upgrade() else {
                break;
            };
            let _ = tokio::task::spawn_blocking(move || {
                st.with_vault(|_, v| {
                    v.clean_traffic_images(now_ms(), false)?;
                    Ok(())
                })
            })
            .await;
        }
    });
}

struct CancelOnDrop(Arc<AtomicBool>);
impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.store(true, Relaxed);
    }
}
struct Work {
    ticket: Arc<Ticket>,
    cancelled: Arc<AtomicBool>,
}
impl Work {
    async fn cpu<T: Send + 'static>(
        &self,
        f: impl FnOnce(&AtomicBool) -> Result<T, Failure> + Send + 'static,
    ) -> Result<T, Failure> {
        let ticket = self.ticket.clone();
        let cancelled = self.cancelled.clone();
        tokio::task::spawn_blocking(move || {
            let _ticket = ticket;
            if cancelled.load(Relaxed) {
                return Err(Failure::new(Code::Cancelled));
            }
            f(&cancelled)
        })
        .await
        .map_err(|_| Failure::new(Code::Encoding))?
    }
}

enum InputImage {
    Bytes(Vec<u8>),
    Base64 { data: String, offset: usize },
}
impl InputImage {
    fn decode(self, remaining: &mut usize, invalid: Code) -> Result<Vec<u8>, Failure> {
        let bytes = match self {
            Self::Bytes(bytes) => bytes,
            Self::Base64 { data, offset } => {
                let value = &data[offset..];
                if value.len() > remaining.div_ceil(3) * 4 {
                    return Err(Failure::new(Code::Limits));
                }
                STANDARD.decode(value).map_err(|_| Failure::new(invalid))?
            }
        };
        *remaining = remaining
            .checked_sub(bytes.len())
            .ok_or_else(|| Failure::new(Code::Limits))?;
        Ok(bytes)
    }
}
struct Raster {
    pixels: RgbImage,
    original: Option<(Vec<u8>, &'static str)>,
}
enum Decoded {
    Image(Raster),
    Pdf(Vec<u8>),
}
struct ProcessedPage {
    attachment: Attachment,
    review_values: Vec<(usize, String)>,
    png: Vec<u8>,
    original: Option<(Vec<u8>, &'static str)>,
}

#[derive(Default)]
struct PageTimings {
    ocr: Option<Range<Instant>>,
    rules_ms: u64,
    encode_ms: u64,
}

impl PageTimings {
    fn record(self, timings: &mut Timings) {
        if let Some(ocr) = self.ocr {
            timings.ocr_ms += (ocr.end - ocr.start).as_millis() as u64;
        }
        timings.rules_ms += self.rules_ms;
        timings.encode_ms += self.encode_ms;
    }

    fn ocr_overlap_ms(&self, other: &Self) -> u64 {
        match (&self.ocr, &other.ocr) {
            (Some(a), Some(b)) => a
                .end
                .min(b.end)
                .saturating_duration_since(a.start.max(b.start))
                .as_millis() as u64,
            _ => 0,
        }
    }
}

struct InputPage {
    raster: Raster,
    native_text: Option<pdf::PageText>,
    attachment: Attachment,
}

impl InputPage {
    async fn render(
        renderer: &mut pdf::Renderer,
        index: usize,
        attachment: Attachment,
        timings: &mut Timings,
    ) -> Result<Self, Failure> {
        let started = Instant::now();
        let (pixels, native_text) = renderer.read_page(index).await?;
        timings.decode_ms += elapsed(started);
        Ok(Self {
            raster: Raster {
                pixels,
                original: None,
            },
            native_text,
            attachment,
        })
    }
}

struct PageProtection<'a> {
    st: &'a Shared,
    settings: &'a Settings,
    backend: ocr::Backend,
    engine: Arc<kaponir_core::Engine>,
    span: Option<&'a Span>,
    original_size: Mutex<usize>,
}

impl PageProtection<'_> {
    async fn redact(
        &self,
        work: &Work,
        page: InputPage,
        page_threads: Option<usize>,
        report_stages: bool,
    ) -> Result<(ProcessedPage, PageTimings), Failure> {
        let InputPage {
            raster: Raster {
                pixels,
                mut original,
            },
            native_text,
            mut attachment,
        } = page;
        let is_pdf = attachment.pdf.is_some();
        let (w, h) = pixels.dimensions();
        let mut timings = PageTimings::default();
        let encoding_started = Instant::now();
        let (pixels, input, blank) = work
            .cpu(move |_| {
                // A uniform PDF page has no visible content to recognize.
                let blank = is_pdf && pixels.pixels().all(|pixel| pixel == pixels.get_pixel(0, 0));
                let input = raster::png(&pixels)?;
                Ok((pixels, input, blank))
            })
            .await?;
        timings.encode_ms += elapsed(encoding_started);
        if is_pdf && self.settings.save_originals {
            original = Some((input.clone(), "image/png"));
        }
        if let Some((bytes, _)) = &original {
            let mut used = self
                .original_size
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            *used = used
                .checked_add(bytes.len())
                .filter(|&size| size <= raster::MAX_PNG_BYTES)
                .ok_or_else(|| Failure::new(Code::Limits))?;
        }
        let (png, areas, review_values) = if blank {
            (input, Vec::new(), Vec::new())
        } else {
            let ocr_started = Instant::now();
            let mut text = self
                .backend
                .read(self.st, self.settings, input, w, h, page_threads)
                .await?;
            timings.ocr = Some(ocr_started..Instant::now());
            text.native_text = native_text;
            let progress = self.span.filter(|_| report_stages);
            if let Some(span) = progress {
                set_stage(span, Stage::Masking);
            }
            let rules_started = Instant::now();
            let engine = self.engine.clone();
            let link_reviews = self.span.is_some();
            let scope = self.st.cfg.scope;
            let url_mask = self.settings.url_mask;
            let (pixels, areas, review_values) = work
                .cpu(move |cancelled| {
                    let mut pixels = pixels;
                    let layout::Masks {
                        areas,
                        review_values,
                    } = layout::mask(
                        &engine,
                        &pixels,
                        text,
                        scope,
                        cancelled,
                        link_reviews,
                        url_mask,
                    )?;
                    raster::paint(&mut pixels, &areas, cancelled)?;
                    Ok((pixels, areas, review_values))
                })
                .await?;
            timings.rules_ms += elapsed(rules_started);
            if let Some(span) = progress {
                set_stage(span, Stage::Encoding);
            }
            let encoding_started = Instant::now();
            let png = work.cpu(move |_| raster::png(&pixels)).await?;
            timings.encode_ms += elapsed(encoding_started);
            (png, areas, review_values)
        };
        attachment.width = w;
        attachment.height = h;
        attachment.areas = areas;
        Ok((
            ProcessedPage {
                attachment,
                review_values,
                png,
                original,
            },
            timings,
        ))
    }
}

struct Processed {
    pages: Vec<ProcessedPage>,
    pdf: Option<Vec<u8>>,
}
impl Processed {
    fn payload(&self) -> &[u8] {
        self.pdf.as_deref().unwrap_or_else(|| &self.pages[0].png)
    }
}

async fn decode(
    work: &Work,
    images: Vec<InputImage>,
    metadata: Vec<Attachment>,
    settings: &Settings,
) -> Result<Vec<(Decoded, Attachment)>, Failure> {
    let mut remaining_bytes = settings.max_bytes();
    let mut remaining_pixels = settings.max_pixels();
    let save_originals = settings.save_originals;
    work.cpu(move |cancelled| {
        images
            .into_iter()
            .zip(metadata)
            .enumerate()
            .map(|(i, (image, attachment))| {
                let decoded = || {
                    if cancelled.load(Relaxed) {
                        return Err(Failure::new(Code::Cancelled));
                    }
                    if attachment.pdf.is_some() {
                        return image
                            .decode(&mut remaining_bytes, Code::InvalidPdf)
                            .map(Decoded::Pdf);
                    }
                    let bytes = image.decode(&mut remaining_bytes, Code::InvalidImage)?;
                    let source = raster::open(&bytes, &mut remaining_pixels)?;
                    let mime = source.mime;
                    let pixels = source.decode()?;
                    Ok(Decoded::Image(Raster {
                        pixels,
                        original: save_originals.then_some((bytes, mime)),
                    }))
                };
                Ok((decoded().map_err(|e| e.at(i))?, attachment))
            })
            .collect()
    })
    .await
}

async fn process(
    st: &Shared,
    settings: Settings,
    span: Option<&Span>,
    images: Vec<InputImage>,
    metadata: Vec<Attachment>,
    started: Instant,
) -> Result<(Vec<Processed>, Timings), Failure> {
    let ticket = Arc::new(Ticket::acquire(st.images.queue.clone(), &settings).await?);
    let mut timings = Timings {
        wait_ms: elapsed(started),
        ..Timings::default()
    };
    let cancel_on_drop = CancelOnDrop(Arc::new(AtomicBool::new(false)));
    let work = Work {
        ticket,
        cancelled: cancel_on_drop.0.clone(),
    };
    if let Some(span) = span {
        set_stage(span, Stage::Validating);
    }
    let decode_started = Instant::now();
    let images = decode(&work, images, metadata, &settings).await?;
    timings.decode_ms = elapsed(decode_started);
    let backend = ocr::Backend::resolve(st, &settings)?;
    backend.ready(st, &settings).await?;
    let parallel_threads = if matches!(backend, ocr::Backend::RapidOcr) {
        rapidocr::LocalOcr::parallel_page_threads()
    } else {
        None
    };
    let protection = PageProtection {
        st,
        settings: &settings,
        backend,
        engine: st.engine(),
        span,
        original_size: Mutex::default(),
    };
    let mut output = Vec::with_capacity(images.len());
    let mut encoded_size = 0;
    let mut wire_size = 0;
    let mut current = 0;
    let mut remaining_pdf_pages = MAX_PDF_PAGES;
    let mut remaining_pdf_pixels = settings.max_pdf_pixels();
    for (image, attachment) in images {
        let first = current;
        let (mut renderer, mut raster, attachments) = match image {
            Decoded::Image(raster) => (None, Some(raster), vec![attachment]),
            Decoded::Pdf(bytes) => {
                if let Some(span) = span {
                    set_stage(span, Stage::Validating);
                }
                let started = Instant::now();
                let renderer = pdf::Renderer::open(
                    &st.images.local.python(),
                    &bytes,
                    remaining_pdf_pages,
                    settings.max_pixels(),
                    &mut remaining_pdf_pixels,
                )
                .await
                .map_err(|e| e.at(first))?;
                timings.decode_ms += elapsed(started);
                let total = renderer.len();
                remaining_pdf_pages -= total;
                let attachments: Vec<_> = (1..=total)
                    .map(|number| {
                        let mut page = attachment.clone();
                        if number > 1 {
                            page.id.push_str(&format!("-p{number}"));
                        }
                        page.pdf = Some(PdfPage { number, total });
                        page
                    })
                    .collect();
                if let Some(span) = span {
                    update(span, |audit| {
                        audit
                            .attachments
                            .splice(first..first + 1, attachments.iter().cloned());
                    });
                }
                (Some(renderer), None, attachments)
            }
        };
        let mut pages = Vec::with_capacity(attachments.len());
        let mut attachments = attachments.into_iter().enumerate().peekable();
        let mut threads = None;
        while let Some((index, attachment)) = attachments.next() {
            // Reuse a spare queue slot for this pair. The two raw rasters
            // together must fit inside the existing single-page pixel limit.
            let spare = if parallel_threads.is_some()
                && let Some(renderer) = &renderer
                && let Some((next, _)) = attachments.peek()
                && renderer.pixels(index) + renderer.pixels(*next) <= settings.max_pixels()
            {
                work.ticket.try_borrow(settings.parallelism)
            } else {
                None
            };
            let i = current;
            current += if spare.is_some() { 2 } else { 1 };
            if let Some(span) = span {
                update(span, |audit| {
                    audit.current = current;
                    audit.stage = Stage::Reading;
                    audit.timings = timings;
                });
            }
            let page = if let Some(renderer) = &mut renderer {
                InputPage::render(renderer, index, attachment, &mut timings)
                    .await
                    .map_err(|e| e.at(i))?
            } else {
                InputPage {
                    raster: raster.take().expect("one standalone image"),
                    native_text: None,
                    attachment,
                }
            };
            let (first, second) = if let Some(ticket) = spare {
                // The last odd page can reuse the same warm OCR process.
                threads = parallel_threads;
                let second_work = Work {
                    ticket: Arc::new(ticket),
                    cancelled: work.cancelled.clone(),
                };
                let (next, attachment) = attachments.next().expect("paired PDF page");
                let renderer = renderer.as_mut().expect("PDF renderer for paired pages");
                let next_page = InputPage::render(renderer, next, attachment, &mut timings)
                    .await
                    .map_err(|e| e.at(i + 1))?;
                let (first, second) = tokio::try_join!(
                    async {
                        protection
                            .redact(&work, page, threads, false)
                            .await
                            .map_err(|e| e.at(i))
                    },
                    async {
                        protection
                            .redact(&second_work, next_page, threads, false)
                            .await
                            .map_err(|e| e.at(i + 1))
                    },
                )?;
                (first, Some(second))
            } else {
                let first = protection
                    .redact(&work, page, threads, true)
                    .await
                    .map_err(|e| e.at(i))?;
                (first, None)
            };
            let overlap = second
                .as_ref()
                .map_or(0, |(_, timings)| first.1.ocr_overlap_ms(timings));
            for (offset, (page, page_timings)) in std::iter::once(first).chain(second).enumerate() {
                encoded_size += page.png.len();
                if encoded_size > raster::MAX_PNG_BYTES {
                    return Err(Failure::new(Code::Limits).at(i + offset));
                }
                page_timings.record(&mut timings);
                pages.push(page);
            }
            timings.ocr_ms = timings.ocr_ms.saturating_sub(overlap);
        }
        let pdf = if let Some(renderer) = renderer {
            renderer.finish().await.map_err(|e| e.at(first))?;
            if let Some(span) = span {
                set_stage(span, Stage::Encoding);
            }
            let encoding_started = Instant::now();
            let bytes = pdf::assemble(
                &st.images.local.python(),
                pages.iter().map(|page| page.png.as_slice()),
                raster::MAX_PNG_BYTES - wire_size,
            )
            .await
            .map_err(|e| e.at(first))?;
            timings.encode_ms += elapsed(encoding_started);
            Some(bytes)
        } else {
            None
        };
        let processed = Processed { pages, pdf };
        wire_size += processed.payload().len();
        if wire_size > raster::MAX_PNG_BYTES {
            return Err(Failure::new(Code::Limits).at(first));
        }
        output.push(processed);
    }
    Ok((output, timings))
}

pub(crate) async fn protect(
    st: &Shared,
    body: &mut Value,
    span: &Span,
    session: &str,
    original: &crate::conversation::Transcript,
) -> Result<bool, Failure> {
    let blocks = wire::scan(body, session);
    if blocks.is_empty() {
        return Ok(false);
    }
    let settings = st.images.settings();
    let current = now_ms();
    let metadata: Vec<_> = blocks
        .iter()
        .take(MAX_IMAGES)
        .map(|b| b.disposition.clone())
        .collect();
    let timeout =
        settings.request_timeout(metadata.iter().any(|attachment| attachment.pdf.is_some()));
    let audit = Audit {
        attachments: metadata.clone(),
        stage: if settings.enabled {
            Stage::Queued
        } else {
            Stage::Disabled
        },
        ..Audit::default()
    };
    {
        let mut lock = span
            .inspector
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let inspector = lock.get_or_insert_with(EventInspector::default);
        inspector.original_request = crate::traffic::cap_str(&original.text, 32_768);
        inspector.last_user_offset = original.last_user_offset.filter(|&i| i < 32_768);
        inspector.user_span = original.user_span.filter(|s| s[1] <= 32_768);
        inspector.tool_results = crate::conversation::incoming_tools(body)
            .into_iter()
            .map(|mut result| {
                result.result.clear();
                result
            })
            .collect();
        inspector.images = Some(audit);
    }
    if !settings.enabled {
        return Ok(true);
    }
    let started = Instant::now();
    let result = async {
        if blocks.len() > MAX_IMAGES {
            return Err(Failure::new(Code::Limits));
        }
        let images = blocks
            .iter()
            .enumerate()
            .map(|(i, block)| block.take(body).map_err(|e| e.at(i)))
            .collect::<Result<Vec<_>, _>>()?;
        let (processed, timings) =
            process(st, settings.clone(), Some(span), images, metadata, started).await?;
        let tag = session.to_owned();
        let st = st.clone();
        let id = span.id;
        let output = tokio::task::spawn_blocking(move || {
            st.with_vault(|_, v| {
                crate::learning::transaction(v, || {
                    let mut output = processed;
                    for item in output.iter_mut().flat_map(|item| &mut item.pages) {
                        use kaponir_core::Vault as _;
                        for (id, value) in item.review_values.drain(..) {
                            if let Some(area) = item.attachment.areas.get_mut(id - 1) {
                                area.review_id = v.journal_id(&area.kind, &value)?;
                            }
                        }
                        let expires = current + PREVIEW_HOURS * 3_600_000;
                        let key = kaponir_vault::TrafficImage {
                            event: id,
                            session: &tag,
                            attachment: &item.attachment.id,
                            original: false,
                        };
                        v.save_traffic_image(key, expires, "image/png", &item.png)?;
                        item.attachment.preview_until_ms = Some(expires);
                        if let Some((bytes, mime)) = item.original.take() {
                            let expires = current + settings.original_hours * 3_600_000;
                            let key = kaponir_vault::TrafficImage {
                                event: id,
                                session: &tag,
                                attachment: &item.attachment.id,
                                original: true,
                            };
                            v.save_traffic_image(key, expires, mime, &bytes)?;
                            item.attachment.original_until_ms = Some(expires);
                        }
                    }
                    v.clean_traffic_images(now_ms(), false)?;
                    Ok(output)
                })
            })
        })
        .await
        .map_err(|_| Failure::new(Code::Storage))?
        .map_err(|_| Failure::new(Code::Storage))?;
        Ok((output, timings))
    };
    match tokio::time::timeout(timeout, result)
        .await
        .unwrap_or_else(|_| Err(Failure::new(Code::Timeout)))
    {
        Ok((processed, timings)) => {
            for (block, item) in blocks.iter().zip(&processed) {
                block.replace(body, item.payload());
            }
            update(span, |audit| {
                audit.attachments = processed
                    .into_iter()
                    .flat_map(|item| item.pages)
                    .map(|page| page.attachment)
                    .collect();
                audit.stage = Stage::Ready;
                audit.timings = timings;
            });
            Ok(true)
        }
        Err(failure) => {
            update(span, |audit| {
                audit.stage = Stage::Blocked;
                audit.failure = Some(failure.clone());
            });
            Err(failure)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_pdf_pair_leaves_the_next_free_slot_to_a_queued_request() {
        let queue = Arc::new(Queue::default());
        let settings = Settings {
            parallelism: 2,
            ..Settings::default()
        };
        let first = Ticket::acquire(queue.clone(), &settings).await.unwrap();
        let spare = first.try_borrow(settings.parallelism).unwrap();
        assert!(first.try_borrow(settings.parallelism).is_none());

        let waiting = Ticket::acquire(queue, &settings);
        tokio::pin!(waiting);
        assert!(futures_util::poll!(waiting.as_mut()).is_pending());
        drop(spare);
        assert!(first.try_borrow(settings.parallelism).is_none());

        let next = waiting.await.unwrap();
        assert!(first.try_borrow(settings.parallelism).is_none());
        drop(next);
        assert!(first.try_borrow(settings.parallelism).is_some());
    }
}
