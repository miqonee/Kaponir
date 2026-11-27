//! Render with a bounded local PDFium process. Only processed rasters reach the PDF writer.
use super::{Code, Failure, raster};
use image::RgbImage;
use std::{ffi::OsStr, process::Stdio};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::{Child, Command},
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

#[derive(Clone, Copy)]
pub(super) struct Size {
    width: f64,
    height: f64,
}

pub(super) struct Page {
    pub size: Size,
    pub pixels: RgbImage,
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

async fn status(output: &mut tokio::process::ChildStdout, invalid: Code) -> Result<(), Failure> {
    match output.read_u8().await {
        Ok(0) => Ok(()),
        Ok(1) => Err(Failure::new(Code::PdfUnavailable)),
        Ok(3) => Err(Failure::new(Code::Limits)),
        _ => Err(Failure::new(invalid)),
    }
}

pub(super) async fn render(
    python: &OsStr,
    bytes: &[u8],
    max_pages: usize,
    remaining_pixels: &mut u64,
) -> Result<Vec<Page>, Failure> {
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
            bytes.len().to_string(),
        ],
    )?;
    let mut input = child.stdin.take().expect("piped PDF input");
    // A missing dependency can close stdin before the attachment is written.
    let written = input.write_all(bytes).await;
    drop(input);
    let mut output = child.stdout.take().expect("piped PDF output");
    status(&mut output, Code::InvalidPdf).await?;
    written.map_err(invalid)?;
    let count = output.read_u8().await.map_err(invalid)? as usize;
    if count == 0 || count > max_pages {
        return Err(Failure::new(Code::Limits));
    }
    let mut pages = Vec::with_capacity(count);
    for _ in 0..count {
        let size = Size {
            width: output.read_f64().await.map_err(invalid)?,
            height: output.read_f64().await.map_err(invalid)?,
        };
        let w = output.read_u32().await.map_err(invalid)?;
        let h = output.read_u32().await.map_err(invalid)?;
        if !(1..=raster::MAX_EDGE).contains(&w)
            || !(1..=raster::MAX_EDGE).contains(&h)
            || (size.width * 2.0).ceil() != f64::from(w)
            || (size.height * 2.0).ceil() != f64::from(h)
        {
            return Err(Failure::new(Code::Limits));
        }
        let pixels = u64::from(w) * u64::from(h);
        *remaining_pixels = remaining_pixels
            .checked_sub(pixels)
            .ok_or_else(|| Failure::new(Code::Limits))?;
        let mut rgb = vec![0; (pixels * 3) as usize];
        output.read_exact(&mut rgb).await.map_err(invalid)?;
        pages.push(Page {
            size,
            pixels: RgbImage::from_raw(w, h, rgb).expect("validated PDF raster size"),
        });
    }
    if output.read_u8().await.is_ok() || !child.wait().await.map_err(invalid)?.success() {
        return Err(Failure::new(Code::InvalidPdf));
    }
    Ok(pages)
}

pub(super) async fn assemble<'a>(
    python: &OsStr,
    sizes: Vec<Size>,
    rasters: impl Iterator<Item = &'a [u8]>,
    remaining_bytes: usize,
) -> Result<Vec<u8>, Failure> {
    let encoding = |_| Failure::new(Code::Encoding);
    let mut child = spawn(python, "assemble", &[remaining_bytes.to_string()])?;
    let mut input = child.stdin.take().expect("piped PDF input");
    let written = async {
        input.write_u8(sizes.len() as u8).await?;
        for (size, png) in sizes.into_iter().zip(rasters) {
            input.write_f64(size.width).await?;
            input.write_f64(size.height).await?;
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
