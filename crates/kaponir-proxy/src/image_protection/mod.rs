//! Image protection in the request path. OCR data never enters the text vault or S1.
mod layout;
mod ocr;
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

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(default)]
pub struct Settings {
    pub enabled: bool,
    pub mode: Mode,
    pub node: Node,
    pub languages: String,
    pub connection_id: String,
    pub model: String,
    pub approved_recipient: String,
    pub max_mib: u32,
    pub max_megapixels: u32,
    pub timeout_ms: u64,
    pub parallelism: usize,
    pub queue_limit: usize,
    pub save_originals: bool,
    pub original_hours: u64,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            enabled: false,
            mode: Mode::Local,
            node: Node::Proxy,
            languages: "rus+eng".into(),
            connection_id: String::new(),
            model: String::new(),
            approved_recipient: String::new(),
            max_mib: 16,
            max_megapixels: 20,
            timeout_ms: 20_000,
            parallelism: 2,
            queue_limit: 8,
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
    fn timeout(&self) -> Duration {
        Duration::from_millis(self.timeout_ms)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Code {
    InlineRequired,
    Format,
    InvalidImage,
    Animated,
    Limits,
    Unavailable,
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
            Self::InlineRequired => "Attach a static PNG, JPEG or WebP as inline base64.",
            Self::Format => "Use a static PNG, JPEG or WebP.",
            Self::InvalidImage => "The image is damaged. Export it again and retry.",
            Self::Animated => "Export one frame as a static PNG, JPEG or WebP.",
            Self::Limits => "Reduce the image size or attach fewer images.",
            Self::Unavailable => "RapidOCR or the configured OCR worker is unavailable.",
            Self::Languages => "Install the selected OCR languages on the worker.",
            Self::Empty | Self::Unreadable => {
                "Text could not be read confidently. Try a larger or sharper image."
            }
            Self::Boundaries => {
                "The full boundaries of the sensitive field could not be determined."
            }
            Self::Timeout => "Image protection timed out. Try fewer or smaller images.",
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
pub(crate) struct Attachment {
    pub id: String,
    pub message_id: String,
    pub role: String,
    pub latest: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_id: Option<String>,
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

pub(crate) struct Service {
    settings: RwLock<Settings>,
    queue: Arc<Queue>,
    local: rapidocr::LocalOcr,
    settings_changes: tokio::sync::Mutex<()>,
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
        })
    }
    fn settings(&self) -> Settings {
        self.settings
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
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
    cancelled: CancelOnDrop,
}
impl Work {
    async fn cpu<T: Send + 'static>(
        &self,
        f: impl FnOnce(&AtomicBool) -> Result<T, Failure> + Send + 'static,
    ) -> Result<T, Failure> {
        let ticket = self.ticket.clone();
        let cancelled = self.cancelled.0.clone();
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
    fn decode(self, remaining: &mut usize) -> Result<Vec<u8>, Failure> {
        let bytes = match self {
            Self::Bytes(bytes) => bytes,
            Self::Base64 { data, offset } => {
                let value = &data[offset..];
                if value.len() > remaining.div_ceil(3) * 4 {
                    return Err(Failure::new(Code::Limits));
                }
                STANDARD
                    .decode(value)
                    .map_err(|_| Failure::new(Code::InvalidImage))?
            }
        };
        *remaining = remaining
            .checked_sub(bytes.len())
            .ok_or_else(|| Failure::new(Code::Limits))?;
        Ok(bytes)
    }
}
struct Normalized {
    pixels: RgbImage,
    original: Option<(Vec<u8>, &'static str)>,
}
struct Processed {
    attachment: Attachment,
    review_values: Vec<(usize, String)>,
    png: Vec<u8>,
    original: Option<(Vec<u8>, &'static str)>,
}

fn normalize(
    images: Vec<InputImage>,
    settings: &Settings,
    cancelled: &AtomicBool,
) -> Result<Vec<Normalized>, Failure> {
    let mut remaining_bytes = settings.max_bytes();
    let images = images
        .into_iter()
        .enumerate()
        .map(|(i, image)| {
            if cancelled.load(Relaxed) {
                return Err(Failure::new(Code::Cancelled));
            }
            image.decode(&mut remaining_bytes).map_err(|e| e.at(i))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut remaining = settings.max_pixels();
    // Validate every header and the aggregate raster budget before decoding a raster.
    let sources = images
        .iter()
        .enumerate()
        .map(|(i, source)| raster::open(source, &mut remaining).map_err(|e| e.at(i)))
        .collect::<Result<Vec<_>, _>>()?;
    let decoded = sources
        .into_iter()
        .enumerate()
        .map(|(i, source)| {
            if cancelled.load(Relaxed) {
                return Err(Failure::new(Code::Cancelled));
            }
            let mime = source.mime;
            Ok((source.decode().map_err(|e| e.at(i))?, mime))
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(decoded
        .into_iter()
        .zip(images)
        .map(|((pixels, mime), source)| Normalized {
            pixels,
            original: settings.save_originals.then_some((source, mime)),
        })
        .collect())
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
    let work = Work {
        ticket,
        cancelled: CancelOnDrop(Arc::new(AtomicBool::new(false))),
    };
    if let Some(span) = span {
        set_stage(span, Stage::Validating);
    }
    let decode_started = Instant::now();
    let decode_settings = settings.clone();
    let images = work
        .cpu(move |cancelled| normalize(images, &decode_settings, cancelled))
        .await?;
    timings.decode_ms = elapsed(decode_started);
    let backend = ocr::Backend::resolve(st, &settings)?;
    backend.ready(st, &settings).await?;
    let engine = st.engine();
    let scope = st.cfg.scope;
    let mut output = Vec::with_capacity(images.len());
    let mut encoded_size = 0;
    for (i, (mut image, mut attachment)) in images.into_iter().zip(metadata).enumerate() {
        if let Some(span) = span {
            update(span, |audit| {
                audit.current = i + 1;
                audit.stage = Stage::Reading;
                audit.timings = timings;
            });
        }
        let (w, h) = image.pixels.dimensions();
        let ocr_started = Instant::now();
        let (pixels, input) = work
            .cpu(move |_| {
                let input = raster::png(&image.pixels)?;
                Ok((image.pixels, input))
            })
            .await
            .map_err(|e| e.at(i))?;
        image.pixels = pixels;
        timings.encode_ms += elapsed(ocr_started);
        let ocr_started = Instant::now();
        let text = backend
            .read(st, &settings, input, w, h)
            .await
            .map_err(|e| e.at(i))?;
        timings.ocr_ms += elapsed(ocr_started);
        if let Some(span) = span {
            set_stage(span, Stage::Masking);
        }
        let rules_started = Instant::now();
        let engine = engine.clone();
        let link_reviews = span.is_some();
        let (pixels, areas, review_values) = work
            .cpu(move |cancelled| {
                let mut pixels = image.pixels;
                let layout::Masks {
                    areas,
                    review_values,
                } = layout::mask(&engine, &pixels, text, scope, cancelled, link_reviews)?;
                for area in &areas {
                    for rect in &area.rectangles {
                        rect.fill(&mut pixels);
                    }
                }
                Ok((pixels, areas, review_values))
            })
            .await
            .map_err(|e| e.at(i))?;
        timings.rules_ms += elapsed(rules_started);
        if let Some(span) = span {
            set_stage(span, Stage::Encoding);
        }
        let encoding_started = Instant::now();
        let png = work
            .cpu(move |_| raster::png(&pixels))
            .await
            .map_err(|e| e.at(i))?;
        timings.encode_ms += elapsed(encoding_started);
        encoded_size += png.len();
        if encoded_size > raster::MAX_PNG_BYTES {
            return Err(Failure::new(Code::Limits).at(i));
        }
        attachment.width = w;
        attachment.height = h;
        attachment.areas = areas;
        output.push(Processed {
            attachment,
            review_values,
            png,
            original: image.original,
        });
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
                    for item in &mut output {
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
    match tokio::time::timeout(settings.timeout(), result)
        .await
        .unwrap_or_else(|_| Err(Failure::new(Code::Timeout)))
    {
        Ok((processed, timings)) => {
            for (block, item) in blocks.iter().zip(&processed) {
                block.replace(body, &item.png);
            }
            update(span, |audit| {
                audit.attachments = processed.into_iter().map(|item| item.attachment).collect();
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
