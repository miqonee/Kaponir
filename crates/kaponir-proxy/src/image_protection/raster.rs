use super::{Area, Code, Failure};
use image::codecs::{
    jpeg::JpegDecoder,
    png::{CompressionType, FilterType, PngDecoder, PngEncoder},
    webp::WebPDecoder,
};
use image::{DynamicImage, ImageDecoder, ImageEncoder, ImageFormat, Limits, Rgb, RgbImage};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io::Cursor;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};

pub(super) const MAX_EDGE: u32 = 8192;
pub(super) const MAX_PNG_BYTES: usize = 32 << 20;

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct Rect {
    pub x0: u32,
    pub y0: u32,
    pub x1: u32,
    pub y1: u32,
}

impl Rect {
    pub fn width(self) -> u32 {
        self.x1 - self.x0
    }
    pub fn height(self) -> u32 {
        self.y1 - self.y0
    }

    pub fn valid(self, width: u32, height: u32) -> bool {
        self.x0 < self.x1 && self.y0 < self.y1 && self.x1 <= width && self.y1 <= height
    }

    pub fn union(self, other: Self) -> Self {
        Self {
            x0: self.x0.min(other.x0),
            y0: self.y0.min(other.y0),
            x1: self.x1.max(other.x1),
            y1: self.y1.max(other.y1),
        }
    }

    pub fn contains(self, other: Self) -> bool {
        self.x0 <= other.x0 && self.y0 <= other.y0 && self.x1 >= other.x1 && self.y1 >= other.y1
    }

    pub fn pad(self, n: u32, width: u32, height: u32) -> Self {
        Self {
            x0: self.x0.saturating_sub(n),
            y0: self.y0.saturating_sub(n),
            x1: (self.x1 + n).min(width),
            y1: (self.y1 + n).min(height),
        }
    }

    fn fill(self, image: &mut RgbImage, color: Rgb<u8>) {
        let stride = image.width() as usize * 3;
        for y in self.y0 as usize..self.y1 as usize {
            let row = y * stride;
            for pixel in image.as_mut()[row + self.x0 as usize * 3..row + self.x1 as usize * 3]
                .as_chunks_mut::<3>()
                .0
            {
                *pixel = color.0;
            }
        }
    }
}

/// Sample a bounded band around the region. A single solid color is written
/// later so no original shape or alpha survives inside the mask.
pub(super) fn background(image: &RgbImage, rect: Rect) -> Option<Rgb<u8>> {
    let mut buckets = BTreeMap::<[u8; 3], (u32, [u32; 3])>::new();
    let mut samples = 0;
    let mut sample = |x, y| {
        let color = image.get_pixel(x, y).0;
        let (count, sum) = buckets.entry(color.map(|part| part >> 3)).or_default();
        *count += 1;
        for (channel, value) in sum.iter_mut().zip(color) {
            *channel += u32::from(value);
        }
        samples += 1;
    };
    for inset in [1, (rect.height() / 4).clamp(2, 10)] {
        for x in (rect.x0..rect.x1).step_by(rect.width().div_ceil(48).max(1) as usize) {
            if let Some(y) = rect.y0.checked_sub(inset) {
                sample(x, y);
            }
            let y = rect.y1 + inset - 1;
            if y < image.height() {
                sample(x, y);
            }
        }
        for y in (rect.y0..rect.y1).step_by(rect.height().div_ceil(48).max(1) as usize) {
            if let Some(x) = rect.x0.checked_sub(inset) {
                sample(x, y);
            }
            let x = rect.x1 + inset - 1;
            if x < image.width() {
                sample(x, y);
            }
        }
    }
    let (count, sum) = buckets.into_values().max_by_key(|(count, _)| *count)?;
    (samples >= 8 && count * 2 >= samples)
        .then(|| Rgb(sum.map(|channel| ((channel + count / 2) / count) as u8)))
}

pub(super) fn paint(
    image: &mut RgbImage,
    areas: &[Area],
    cancelled: &AtomicBool,
) -> Result<(), Failure> {
    let mut paint = Vec::new();
    for &rect in areas.iter().flat_map(|area| &area.rectangles) {
        if cancelled.load(Relaxed) {
            return Err(Failure::new(Code::Cancelled));
        }
        paint.push((rect, background(image, rect).unwrap_or(Rgb([0; 3]))));
    }
    for (rect, color) in paint {
        if cancelled.load(Relaxed) {
            return Err(Failure::new(Code::Cancelled));
        }
        rect.fill(image, color);
    }
    Ok(())
}

fn image_error(err: image::ImageError) -> Failure {
    Failure::new(if matches!(err, image::ImageError::Limits(_)) {
        Code::Limits
    } else {
        Code::InvalidImage
    })
}

/// JPEG multi-picture files identify the additional pictures in an APP2 MPF segment.
fn multi_picture_jpeg(data: &[u8]) -> bool {
    let mut segments = data.get(2..).unwrap_or_default();
    while let Some(rest) = segments.strip_prefix(&[0xff]) {
        let Some(index) = rest.iter().position(|&b| b != 0xff) else {
            break;
        };
        let marker = rest[index];
        if marker == 0xda {
            // Entropy-coded 0xff bytes are escaped, so an EOI followed by
            // another SOI indicates a concatenated JPEG sequence.
            return rest[index + 1..]
                .windows(2)
                .position(|pair| pair == [0xff, 0xd9])
                .is_some_and(|end| {
                    rest[index + 1 + end + 2..]
                        .windows(3)
                        .any(|window| window == [0xff, 0xd8, 0xff])
                });
        }
        if marker == 0xd9 {
            break;
        }
        let Some(size) = rest.get(index + 1..index + 3) else {
            break;
        };
        let length = usize::from(u16::from_be_bytes([size[0], size[1]]));
        if length < 2 {
            break;
        }
        let Some(segment) = rest.get(index + 1..index + 1 + length) else {
            break;
        };
        if marker == 0xe2 && segment[2..].starts_with(b"MPF\0") {
            return true;
        }
        segments = &rest[index + 1 + length..];
    }
    false
}

pub(super) struct Source<'a> {
    decoder: Box<dyn ImageDecoder + 'a>,
    pub mime: &'static str,
}

pub(super) fn open<'a>(bytes: &'a [u8], pixels: &mut u64) -> Result<Source<'a>, Failure> {
    let reader = Cursor::new(bytes);
    let mut limits = Limits::default();
    limits.max_image_width = Some(MAX_EDGE);
    limits.max_image_height = Some(MAX_EDGE);
    limits.max_alloc = Some((pixels.saturating_mul(16)).min(384 << 20));
    let mut source = match image::guess_format(bytes) {
        Ok(ImageFormat::Png) => {
            let decoder = PngDecoder::with_limits(reader, limits.clone()).map_err(image_error)?;
            if decoder.is_apng().map_err(image_error)? {
                return Err(Failure::new(Code::Animated));
            }
            Source {
                decoder: Box::new(decoder),
                mime: "image/png",
            }
        }
        Ok(ImageFormat::Jpeg) => {
            if multi_picture_jpeg(bytes) {
                return Err(Failure::new(Code::Animated));
            }
            Source {
                decoder: Box::new(JpegDecoder::new(reader).map_err(image_error)?),
                mime: "image/jpeg",
            }
        }
        Ok(ImageFormat::WebP) => {
            let decoder = WebPDecoder::new(reader).map_err(image_error)?;
            if decoder.has_animation() {
                return Err(Failure::new(Code::Animated));
            }
            Source {
                decoder: Box::new(decoder),
                mime: "image/webp",
            }
        }
        Ok(_) => return Err(Failure::new(Code::Format)),
        Err(_) => return Err(Failure::new(Code::InvalidImage)),
    };
    source.decoder.set_limits(limits).map_err(image_error)?;
    let (w, h) = source.decoder.dimensions();
    if w == 0 || h == 0 || w > MAX_EDGE || h > MAX_EDGE {
        return Err(Failure::new(Code::Limits));
    }
    *pixels = pixels
        .checked_sub(u64::from(w) * u64::from(h))
        .ok_or_else(|| Failure::new(Code::Limits))?;
    Ok(source)
}

impl Source<'_> {
    pub fn decode(mut self) -> Result<RgbImage, Failure> {
        let orientation = self.decoder.orientation().map_err(image_error)?;
        let mut image = DynamicImage::from_decoder(self.decoder).map_err(image_error)?;
        image.apply_orientation(orientation);
        if !image.color().has_alpha() {
            return Ok(image.into_rgb8());
        }

        let rgba = image.into_rgba8();
        Ok(RgbImage::from_fn(rgba.width(), rgba.height(), |x, y| {
            let [r, g, b, alpha] = rgba.get_pixel(x, y).0.map(u16::from);
            let flatten = |c| ((c * alpha + 255 * (255 - alpha) + 127) / 255) as u8;
            Rgb([flatten(r), flatten(g), flatten(b)])
        }))
    }
}

pub(super) fn png(image: &RgbImage) -> Result<Vec<u8>, Failure> {
    let mut result = Vec::new();
    PngEncoder::new_with_quality(&mut result, CompressionType::Fast, FilterType::Adaptive)
        .write_image(
            image.as_raw(),
            image.width(),
            image.height(),
            image::ExtendedColorType::Rgb8,
        )
        .map_err(|_| Failure::new(Code::Encoding))?;
    if result.len() > MAX_PNG_BYTES {
        return Err(Failure::new(Code::Limits));
    }
    Ok(result)
}
