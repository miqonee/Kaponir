//! Render with a bounded local PDFium process. Only processed rasters reach the PDF writer.
use super::{Code, Failure, Rect, ocr::OUTPUT_LIMIT, raster};
use image::RgbImage;
use serde::Deserialize;
use std::{ffi::OsStr, process::Stdio};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::{Child, ChildStdin, ChildStdout, Command},
};

const SCRIPT: &str = include_str!("../../../../scripts/kaponir_pdf.py");
pub(super) const HEADER_PREFIX: usize = 1032;

pub(super) fn signature(bytes: &[u8]) -> bool {
    bytes[..bytes.len().min(HEADER_PREFIX)]
        .windows(8)
        .any(|prefix| {
            matches!(
                prefix,
                [b'%', b'P', b'D', b'F', b'-', b'1' | b'2', b'.', b'0'..=b'9']
            )
        })
}

struct Page {
    width: u32,
    height: u32,
}

pub(super) struct PageText {
    pub text: String,
    pub glyphs: Vec<(usize, Rect)>,
}

impl PageText {
    fn parse(bytes: &[u8], width: u32, height: u32) -> Result<Option<Self>, Failure> {
        if bytes.is_empty() {
            return Ok(None);
        }
        #[derive(Deserialize)]
        struct Geometry {
            text: String,
            boxes: Vec<Option<[u32; 4]>>,
        }
        let invalid = || Failure::new(Code::InvalidPdf);
        let Geometry { text, boxes } = serde_json::from_slice(bytes).map_err(|_| invalid())?;
        if boxes.len() != text.chars().count() {
            return Err(invalid());
        }
        let mut glyphs = Vec::new();
        for ((offset, ch), bounds) in text.char_indices().zip(boxes) {
            if let Some([x0, y0, x1, y1]) = bounds {
                let rect = Rect { x0, y0, x1, y1 };
                if ch.is_whitespace() || !rect.valid(width, height) {
                    return Err(invalid());
                }
                glyphs.push((offset, rect));
            } else if !ch.is_whitespace() {
                return Err(invalid());
            }
        }
        Ok(Some(Self { text, glyphs }))
    }
}

fn spawn(python: &OsStr, mode: &str, limits: &[String]) -> Result<Child, Failure> {
    Command::new(python)
        .args(["-I", "-u", "-c", SCRIPT, mode])
        .args(limits)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| Failure::new(Code::PdfUnavailable))
}

async fn status(output: &mut ChildStdout, invalid: Code) -> Result<(), Failure> {
    match output.read_u8().await {
        Ok(0) => Ok(()),
        Ok(1) => Err(Failure::new(Code::PdfUnavailable)),
        Ok(3) => Err(Failure::new(Code::Limits)),
        _ => Err(Failure::new(invalid)),
    }
}

pub(super) struct Renderer {
    child: Child,
    input: ChildStdin,
    output: ChildStdout,
    pages: Vec<Page>,
}

impl Renderer {
    pub(super) async fn open(
        python: &OsStr,
        bytes: &[u8],
        max_pages: usize,
        max_page_pixels: u64,
        remaining_pixels: &mut u64,
    ) -> Result<Self, Failure> {
        let invalid = |_| Failure::new(Code::InvalidPdf);
        if !signature(bytes) {
            return Err(Failure::new(Code::InvalidPdf));
        }
        let mut child = spawn(
            python,
            "render",
            &[
                max_pages.to_string(),
                remaining_pixels.to_string(),
                max_page_pixels.to_string(),
                bytes.len().to_string(),
            ],
        )?;
        let mut input = child.stdin.take().expect("piped PDF input");
        // A missing dependency can close stdin before the attachment is written.
        let written = input.write_all(bytes).await;
        let mut output = child.stdout.take().expect("piped PDF output");
        status(&mut output, Code::InvalidPdf).await?;
        written.map_err(invalid)?;
        let count = output.read_u8().await.map_err(invalid)? as usize;
        if count == 0 || count > max_pages {
            return Err(Failure::new(Code::Limits));
        }
        let mut pages = Vec::with_capacity(count);
        for _ in 0..count {
            let width = output.read_u32().await.map_err(invalid)?;
            let height = output.read_u32().await.map_err(invalid)?;
            let pixels = u64::from(width) * u64::from(height);
            if !(1..=raster::MAX_EDGE).contains(&width)
                || !(1..=raster::MAX_EDGE).contains(&height)
                || pixels > max_page_pixels
            {
                return Err(Failure::new(Code::Limits));
            }
            *remaining_pixels = remaining_pixels
                .checked_sub(pixels)
                .ok_or_else(|| Failure::new(Code::Limits))?;
            pages.push(Page { width, height });
        }
        Ok(Self {
            child,
            input,
            output,
            pages,
        })
    }

    pub(super) fn len(&self) -> usize {
        self.pages.len()
    }

    pub(super) fn pixels(&self, index: usize) -> u64 {
        let Page { width, height } = self.pages[index];
        u64::from(width) * u64::from(height)
    }

    pub(super) async fn read_page(
        &mut self,
        index: usize,
    ) -> Result<(RgbImage, Option<PageText>), Failure> {
        let invalid = |_| Failure::new(Code::InvalidPdf);
        let written = self.input.write_u8(1).await;
        status(&mut self.output, Code::InvalidPdf).await?;
        written.map_err(invalid)?;
        let Page { width, height } = self.pages[index];
        let text_len = self.output.read_u32().await.map_err(invalid)? as usize;
        if text_len > OUTPUT_LIMIT {
            return Err(Failure::new(Code::Limits));
        }
        let mut text = vec![0; text_len];
        self.output.read_exact(&mut text).await.map_err(invalid)?;
        let text = PageText::parse(&text, width, height)?;
        let mut rgb = vec![0; (u64::from(width) * u64::from(height) * 3) as usize];
        self.output.read_exact(&mut rgb).await.map_err(invalid)?;
        Ok((
            RgbImage::from_raw(width, height, rgb).expect("validated PDF raster size"),
            text,
        ))
    }

    pub(super) async fn finish(self) -> Result<(), Failure> {
        let Self {
            mut child,
            input,
            mut output,
            ..
        } = self;
        let invalid = |_| Failure::new(Code::InvalidPdf);
        drop(input);
        if output.read_u8().await.is_ok() || !child.wait().await.map_err(invalid)?.success() {
            return Err(Failure::new(Code::InvalidPdf));
        }
        Ok(())
    }
}

pub(super) async fn assemble<'a>(
    python: &OsStr,
    rasters: impl ExactSizeIterator<Item = &'a [u8]>,
    remaining_bytes: usize,
) -> Result<Vec<u8>, Failure> {
    let encoding = |_| Failure::new(Code::Encoding);
    let mut child = spawn(python, "assemble", &[remaining_bytes.to_string()])?;
    let mut input = child.stdin.take().expect("piped PDF input");
    let written = async {
        input.write_u8(rasters.len() as u8).await?;
        for png in rasters {
            input.write_u32(png.len() as u32).await?;
            input.write_all(png).await?;
        }
        Ok(())
    }
    .await;
    drop(input);
    let mut output = child.stdout.take().expect("piped PDF output");
    status(&mut output, Code::Encoding).await?;
    written.map_err(encoding)?;
    let length = output.read_u32().await.map_err(encoding)? as usize;
    if length > remaining_bytes {
        return Err(Failure::new(Code::Limits));
    }
    let mut pdf = vec![0; length];
    output.read_exact(&mut pdf).await.map_err(encoding)?;
    if !pdf.starts_with(b"%PDF-") || !child.wait().await.map_err(encoding)?.success() {
        return Err(Failure::new(Code::Encoding));
    }
    Ok(pdf)
}
