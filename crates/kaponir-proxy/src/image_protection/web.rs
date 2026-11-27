use super::{
    Attachment, Code, Failure, InputImage, InstallState, Mode, Node, SETTINGS, Settings, elapsed,
    now_ms, ocr, pdf, process, wire,
};
use crate::{
    Shared, providers,
    server::{ApiError, authorized, error},
};
use axum::{
    Router,
    body::Bytes,
    extract::{Json, Path, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::Instant;

type ResultResponse = Result<Response, ApiError>;

pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/ui/ocr-example.png", get(example))
        .route("/api/v1/ui/ocr/settings", get(settings).put(save))
        .route("/api/v1/ui/ocr/install", get(installation).post(install))
        .route("/api/v1/ui/ocr/check", post(check))
        .route("/api/v1/ui/ocr/models", post(models))
        .route("/api/v1/ui/ocr/preview", post(preview))
        .route("/api/v1/ui/ocr/review-links/{id}", get(review_links))
        .route(
            "/api/v1/ui/ocr/images/{session}/{event}/{attachment}/{variant}",
            get(image),
        )
}

fn private(mut response: Response) -> Response {
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("private, no-store"),
    );
    response.headers_mut().insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    response
}

async fn example() -> Response {
    (
        [
            (header::CONTENT_TYPE, "image/png"),
            (header::CACHE_CONTROL, "public, max-age=3600"),
        ],
        ocr::EXAMPLE,
    )
        .into_response()
}

fn can_install(st: &Shared, settings: &Settings, worker_managed: bool) -> bool {
    st.images.local.runtime.is_some()
        && settings.mode == Mode::Local
        && match settings.node {
            Node::Proxy => std::env::var_os("KAPONIR_OCR_PYTHON").is_none(),
            Node::Worker => worker_managed && std::env::var_os("KAPONIR_WORKER_PYTHON").is_none(),
        }
}

async fn installation(State(st): State<Shared>, headers: HeaderMap) -> ResultResponse {
    authorized(&st, &headers, true)?;
    Ok(private(
        Json(json!({"state": *st.images.install_state()})).into_response(),
    ))
}

// Requiring a JSON object also excludes a cross-origin HTML form submission.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InstallRequest {}

async fn install(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(_): Json<InstallRequest>,
) -> ResultResponse {
    authorized(&st, &headers, true)?;
    let worker_managed = ocr::worker_recipient(&st)
        .is_some_and(|recipient| ocr::managed_worker(&st, &recipient).is_some());
    if !can_install(&st, &st.images.settings(), worker_managed) {
        return Err(error(
            StatusCode::FORBIDDEN,
            "local_ocr_install_unavailable",
        ));
    }

    let mut install = st.images.install_state();
    if *install != InstallState::Installing {
        *install = InstallState::Installing;
        let st = st.clone();
        tokio::spawn(async move {
            let worker = st.clone();
            let result = tokio::task::spawn_blocking(move || {
                worker
                    .images
                    .local
                    .runtime
                    .as_ref()
                    .expect("checked OCR runtime")
                    .install()
            })
            .await
            .map_err(anyhow::Error::from)
            .and_then(|result| result);
            *st.images.install_state() = match result {
                Ok(()) => InstallState::Ready,
                Err(error) => {
                    eprintln!("kaponir: RapidOCR setup: {error:#}");
                    InstallState::Failed
                }
            };
        });
    }
    Ok(private(
        (StatusCode::ACCEPTED, Json(json!({"state": *install}))).into_response(),
    ))
}

async fn status(st: &Shared, settings: &Settings) -> Result<Value, ApiError> {
    let registry = providers::registry(st)?;
    let connections: Vec<_> = registry.connections.into_iter().filter(|p| p.enabled)
        .map(|p| json!({"id":p.id, "name":p.name, "kind":p.kind, "recipient":ocr::endpoint(&p), "models":p.models}))
        .collect();
    let worker = ocr::worker_recipient(st);
    let worker_managed = worker
        .as_deref()
        .is_some_and(|url| ocr::managed_worker(st, url).is_some());
    let recipient = match (settings.mode, settings.node) {
        (Mode::Local, Node::Proxy) => None,
        (Mode::Local, Node::Worker) => worker.clone(),
        (Mode::Vision, _) => connections
            .iter()
            .find(|p| p["id"] == settings.connection_id)
            .and_then(|p| p["recipient"].as_str())
            .map(str::to_owned),
    };
    let mut engine = ocr::Backend::engine(settings);
    let (languages, failure) = match ocr::Backend::resolve(st, settings) {
        Ok(backend) => match backend
            .capabilities(st, settings.enabled, settings.profile)
            .await
        {
            Ok(capabilities) => {
                engine = capabilities.engine;
                let missing = capabilities.missing_languages(settings);
                (capabilities.languages, missing.then_some(Code::Languages))
            }
            Err(failure) => (Vec::new(), Some(failure.code)),
        },
        Err(failure) => (Vec::new(), Some(failure.code)),
    };
    Ok(json!({
        "settings":settings, "recipient":recipient, "connections":connections,
        "worker":worker, "worker_managed":worker_managed, "installed_languages":languages, "engine":engine,
        "can_install":can_install(st, settings, worker_managed), "install_state":*st.images.install_state(),
        "engine_state": match failure {
            None => "ready",
            Some(Code::Consent | Code::Languages | Code::ProfileUnavailable) => "needs_setup",
            Some(_) => "unavailable",
        },
        "failure": failure,
        "max_images":super::MAX_IMAGES,
        "max_pdf_pages":super::MAX_PDF_PAGES,
        "max_pdf_megapixels":settings.max_pdf_pixels() / 1_000_000,
        "max_edge":super::raster::MAX_EDGE,
    }))
}

async fn settings(State(st): State<Shared>, headers: HeaderMap) -> ResultResponse {
    authorized(&st, &headers, true)?;
    Ok(private(
        Json(status(&st, &st.images.settings()).await?).into_response(),
    ))
}

async fn save(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(settings): Json<Settings>,
) -> ResultResponse {
    authorized(&st, &headers, true)?;
    if !settings.checked() {
        return Err(error(
            StatusCode::BAD_REQUEST,
            "invalid image protection settings",
        ));
    }
    let guard = st.images.settings_changes.lock().await;
    if settings.enabled {
        ocr::Backend::resolve(&st, &settings).map_err(|e| e.api())?;
    }
    st.with_vault(|_, v| {
        v.set_private_setting(SETTINGS, &serde_json::to_vec(&settings)?)?;
        Ok(())
    })
    .map_err(|_| Failure::new(Code::Storage).api())?;
    *st.images
        .settings
        .write()
        .unwrap_or_else(std::sync::PoisonError::into_inner) = settings.clone();
    drop(guard);
    Ok(private(Json(status(&st, &settings).await?).into_response()))
}

#[derive(Deserialize)]
struct VisionSelection {
    connection_id: String,
    #[serde(default)]
    model: String,
}

async fn check(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(selection): Json<VisionSelection>,
) -> ResultResponse {
    authorized(&st, &headers, true)?;
    if selection.model.trim().is_empty()
        || selection.model.len() > 240
        || selection.model.chars().any(char::is_control)
    {
        return Err(error(StatusCode::BAD_REQUEST, "enter a Vision model ID"));
    }
    let provider = providers::by_id(&st, &selection.connection_id)?;
    if !provider.enabled {
        return Err(Failure::new(Code::Consent).api());
    }
    let settings = st.images.settings();
    let started = Instant::now();
    let result = tokio::time::timeout(settings.timeout(), async {
        let _ticket = super::Ticket::acquire(st.images.queue.clone(), &settings).await?;
        ocr::verify(&st, &provider, &selection.model).await
    })
    .await
    .unwrap_or_else(|_| Err(Failure::new(Code::Timeout)));
    let (reachable, images, coordinates) = match result.as_ref().err().map(|e| e.code) {
        None => (Some(true), Some(true), Some(true)),
        Some(Code::VisionAddress) => (Some(false), None, None),
        Some(Code::VisionImages) => (Some(true), Some(false), None),
        Some(Code::VisionCoordinates | Code::Limits) => (Some(true), Some(true), Some(false)),
        _ => (None, None, None),
    };
    Ok(private(Json(json!({"recipient":ocr::endpoint(&provider),"reachable":reachable,
        "images":images,"coordinates":coordinates,"failure":result.err(),"elapsed_ms":elapsed(started)})).into_response()))
}

async fn models(
    State(st): State<Shared>,
    headers: HeaderMap,
    Json(selection): Json<VisionSelection>,
) -> ResultResponse {
    authorized(&st, &headers, true)?;
    let provider = providers::by_id(&st, &selection.connection_id)?;
    if !provider.enabled {
        return Err(Failure::new(Code::Consent).api());
    }
    let (models, failure) = match providers::fetch_models(&st, &provider).await {
        Ok(models) => (models, None),
        Err(error) => (provider.models, Some(error)),
    };
    Ok(private(
        Json(json!({"models":models,"failure":failure})).into_response(),
    ))
}

async fn preview(State(st): State<Shared>, headers: HeaderMap, bytes: Bytes) -> ResultResponse {
    authorized(&st, &headers, true)?;
    let mut settings = st.images.settings();
    if !settings.enabled {
        settings.mode = Mode::Local;
        settings.node = Node::Proxy;
    } else if (settings.mode == Mode::Vision || settings.node == Node::Worker)
        && headers
            .get("x-kaponir-ocr-recipient")
            .and_then(|value| value.to_str().ok())
            != Some(&settings.approved_recipient)
    {
        return Err(Failure::new(Code::Consent).api());
    }
    let is_pdf = pdf::signature(&bytes)
        || headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(wire::pdf_mime);
    // PDF originals here are rendered page previews returned only to the Lab caller.
    settings.save_originals = is_pdf;
    if bytes.len() > settings.max_bytes() {
        return Err(Failure::new(Code::Limits).api());
    }
    let timeout = settings.request_timeout(is_pdf);
    let started = Instant::now();
    let (mut output, timings) = tokio::time::timeout(
        timeout,
        process(
            &st,
            settings,
            None,
            vec![InputImage::Bytes(bytes.to_vec())],
            vec![Attachment {
                pdf: is_pdf.then(super::PdfPage::default),
                ..Default::default()
            }],
            started,
        ),
    )
    .await
    .unwrap_or_else(|_| Err(Failure::new(Code::Timeout)))
    .map_err(|e| e.api())?;
    let mut item = output.pop().expect("one preview attachment");
    let mut reply = json!({"elapsed_ms":elapsed(started), "timings":timings});
    let png_url = |png: Vec<u8>| format!("data:image/png;base64,{}", STANDARD.encode(png));
    if let Some(pdf) = item.pdf {
        let pages: Vec<_> = item.pages.into_iter().map(|page| {
            let (original, _) = page.original.expect("Lab PDF raster original");
            json!({"image":page.attachment,"data":png_url(page.png),"original":png_url(original)})
        }).collect();
        reply["data"] = json!(format!(
            "data:application/pdf;base64,{}",
            STANDARD.encode(pdf)
        ));
        reply["pages"] = json!(pages);
    } else {
        let page = item.pages.pop().expect("one preview raster");
        reply["data"] = json!(png_url(page.png));
        reply["image"] = json!(page.attachment);
    }
    Ok(private(Json(reply).into_response()))
}

async fn review_links(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> ResultResponse {
    authorized(&st, &headers, true)?;
    let exists = st
        .with_vault(|_, v| Ok(v.journal_states()?.contains_key(&id)))
        .map_err(|_| Failure::new(Code::Storage).api())?;
    let mut links = Vec::new();
    let mut seen = std::collections::HashSet::new();
    if exists {
        'events: for event in st.traffic.snapshot().events {
            let Some(audit) = event.inspector.and_then(|i| i.images) else {
                continue;
            };
            for attachment in audit.attachments {
                if attachment
                    .preview_until_ms
                    .is_none_or(|time| time <= now_ms())
                {
                    continue;
                }
                for area in attachment.areas {
                    if area.review_id == Some(id) && seen.insert((attachment.id.clone(), area.id)) {
                        links.push(json!({
                            "event_id":event.id, "session_id":event.session_id,
                            "turn_id":event.turn.as_ref().map(|t| &t.id),
                            "message_id":attachment.message_id, "attachment_id":attachment.id,
                            "area_id":area.id,
                        }));
                        if links.len() >= 20 {
                            break 'events;
                        }
                    }
                }
            }
        }
    }
    Ok(private(Json(links).into_response()))
}

async fn image(
    State(st): State<Shared>,
    headers: HeaderMap,
    Path((session, event, attachment, variant)): Path<(String, u64, String, String)>,
) -> Response {
    let result: ResultResponse = async {
        authorized(&st, &headers, true)?;
        let missing = || error(StatusCode::NOT_FOUND, "attachment_not_found");
        let (original, area_id) = match variant.as_str() {
            "original" => (true, None),
            "sent" => (false, None),
            _ => {
                let area_id = variant.strip_prefix("area-")
                    .and_then(|id| id.parse::<usize>().ok()).ok_or_else(missing)?;
                (true, Some(area_id))
            }
        };
        let entry = st.traffic.event_by_id(event).ok_or_else(missing)?;
        let tag = entry
            .session_id
            .unwrap_or_else(|| format!("request-{event}"));
        if tag != session {
            return Err(missing());
        }
        let audit = entry
            .inspector
            .and_then(|inspector| inspector.images)
            .ok_or_else(missing)?;
        let data = audit
            .attachments
            .iter()
            .find(|a| a.id == attachment)
            .ok_or_else(missing)?;
        let excerpt = area_id.map(|id| {
            let selected = data.areas.iter().find(|area| area.id == id).ok_or_else(missing)?;
            Ok::<_, ApiError>((data.width, data.height, selected.rectangles.clone()))
        }).transpose()?;
        let expires = if original {
            data.original_until_ms
        } else {
            data.preview_until_ms
        };
        let expires = expires.ok_or_else(|| {
            error(
                StatusCode::NOT_FOUND,
                if original {
                    "original_not_saved"
                } else {
                    "preview_not_saved"
                },
            )
        })?;
        let time = now_ms();
        if expires <= time {
            return Err(error(StatusCode::GONE, "attachment_expired"));
        }
        tokio::task::spawn_blocking(move || {
            let contents = st.with_vault(|_, v| {
                Ok(v.traffic_image(
                    kaponir_vault::TrafficImage {
                        event,
                        session: &session,
                        attachment: &attachment,
                        original,
                    },
                    time,
                )?)
            }).map_err(|_| Failure::new(Code::Storage).api())?
              .ok_or_else(|| error(StatusCode::GONE, "attachment_removed"))?;
            if let Some((width, height, rectangles)) = excerpt {
                let mut remaining_pixels = u64::from(width) * u64::from(height);
                let image = super::raster::open(&contents.1, &mut remaining_pixels)
                    .and_then(super::raster::Source::decode).map_err(|e| e.api())?;
                if image.dimensions() != (width, height) {
                    return Err(Failure::new(Code::Boundaries).api());
                }
                let mut fragments = Vec::with_capacity(rectangles.len());
                let mut remaining_bytes = super::raster::MAX_PNG_BYTES;
                for rect in rectangles {
                    if !rect.valid(width, height) {
                        return Err(Failure::new(Code::Boundaries).api());
                    }
                    let crop = image::imageops::crop_imm(&image, rect.x0, rect.y0, rect.width(), rect.height()).to_image();
                    let png = super::raster::png(&crop).map_err(|e| e.api())?;
                    remaining_bytes = remaining_bytes.checked_sub(png.len())
                        .ok_or_else(|| Failure::new(Code::Limits).api())?;
                    fragments.push(json!({"rect":rect, "data":format!("data:image/png;base64,{}", STANDARD.encode(png))}));
                }
                Ok(Json(json!({"fragments":fragments})).into_response())
            } else {
                Ok(([(header::CONTENT_TYPE, contents.0)], contents.1).into_response())
            }
        })
        .await
        .map_err(|_| Failure::new(Code::Storage).api())?
    }
    .await;
    private(result.unwrap_or_else(IntoResponse::into_response))
}
