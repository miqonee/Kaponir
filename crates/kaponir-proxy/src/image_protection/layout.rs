//! Translate UTF-8 ranges into the oriented raster. Field assignments additionally use
//! the visible row and column boundaries, including glyphs omitted by OCR.
use super::ocr::{Reading, Word};
use super::{Area, Code, Failure, Reason, Rect, UrlMask, raster};
use image::{Rgb, RgbImage};
use kaponir_core::{
    Category, Engine, Format, Input, Known, MaskOptions, MemoryVault, Scope, Strategy, TokenForm,
    Verdict,
};
use kaponir_recognizers::kv::{
    FieldAssignment, KeyValue, credential_reference, yaml_block_indicator,
};
use std::collections::{BTreeMap, HashSet};
use std::ops::Range;
use std::sync::{
    OnceLock,
    atomic::{AtomicBool, Ordering::Relaxed},
};

const MAX_AREAS: usize = 256;

#[derive(Clone, Copy)]
struct Extent {
    left: u32,
    right: u32,
}

struct Ink {
    width: usize,
    pixels: Vec<u8>,
}
impl Ink {
    fn read(image: &RgbImage, cancelled: &AtomicBool) -> Result<Self, Failure> {
        let width = image.width() as usize;
        let mut pixels = vec![0; width * image.height() as usize];
        let source = image.as_raw();
        for (y, row) in pixels.chunks_exact_mut(width).enumerate() {
            if cancelled.load(Relaxed) {
                return Err(Failure::new(Code::Cancelled));
            }
            for (x, target) in row.iter_mut().enumerate() {
                let at = (y * width + x) * 3;
                let left = (y * width + x.saturating_sub(2)) * 3;
                let above = (y.saturating_sub(2) * width + x) * 3;
                let has_edge = (0..3).any(|channel| {
                    let value = source[at + channel];
                    value.abs_diff(source[left + channel]) >= 20
                        || value.abs_diff(source[above + channel]) >= 20
                });
                *target = u8::from(has_edge);
            }
        }
        Ok(Self { width, pixels })
    }

    fn horizontal(&self, rect: Rect) -> Vec<u32> {
        let mut counts = vec![0; rect.width() as usize];
        for y in rect.y0 as usize..rect.y1 as usize {
            let base = y * self.width + rect.x0 as usize;
            for (sum, bit) in counts
                .iter_mut()
                .zip(&self.pixels[base..base + rect.width() as usize])
            {
                *sum += u32::from(*bit);
            }
        }
        counts
    }

    fn vertical(&self, column: Extent) -> Vec<bool> {
        self.pixels
            .chunks_exact(self.width)
            .map(|row| {
                row[column.left as usize..column.right as usize]
                    .iter()
                    .any(|&b| b != 0)
            })
            .collect()
    }
}

fn runs(values: impl IntoIterator<Item = bool>, merge_gap: u32) -> Vec<Range<u32>> {
    let mut out: Vec<Range<u32>> = Vec::new();
    for (index, _) in values.into_iter().enumerate().filter(|(_, value)| *value) {
        let index = index as u32;
        if let Some(last) = out.last_mut()
            && index.saturating_sub(last.end) <= merge_gap
        {
            last.end = index + 1;
        } else {
            out.push(index..index + 1);
        }
    }
    out
}

struct Row {
    rect: Rect,
    words: Vec<Word>,
    letters: Vec<Range<usize>>,
    text: Range<usize>,
}
impl Row {
    fn new(words: Vec<Word>) -> Self {
        let rect = words
            .iter()
            .map(|w| w.rect)
            .reduce(Rect::union)
            .expect("nonempty OCR line");
        Self {
            rect,
            words,
            letters: Vec::new(),
            text: 0..0,
        }
    }

    fn word_at(&self, byte: usize) -> Option<usize> {
        self.letters.iter().position(|r| r.contains(&byte))
    }
}

fn physical_rows(words: Vec<Word>) -> Vec<Row> {
    let mut groups = BTreeMap::new();
    for word in words {
        groups.entry(word.group).or_insert_with(Vec::new).push(word);
    }
    let mut lines: Vec<Row> = groups.into_values().map(Row::new).collect();
    lines.sort_by_key(|row| (row.rect.y0 + row.rect.y1, row.rect.x0));
    let mut rows: Vec<Row> = Vec::new();
    for line in lines {
        if let Some(previous) = rows.last_mut() {
            let a = previous.rect;
            let b = line.rect;
            let overlap = a.y1.min(b.y1).saturating_sub(a.y0.max(b.y0));
            if overlap * 2 >= a.height().min(b.height())
                && (a.y0 + a.y1).abs_diff(b.y0 + b.y1) < a.height().max(b.height())
            {
                previous.rect = previous.rect.union(line.rect);
                previous.words.extend(line.words);
                continue;
            }
        }
        rows.push(line);
    }
    for row in &mut rows {
        row.words.sort_by_key(|w| (w.rect.x0, w.order));
    }
    rows
}

fn columns(rows: &[Row], ink: &Ink, width: u32, height: u32, glyph: u32) -> Vec<Extent> {
    let projection = ink.horizontal(Rect {
        x0: 0,
        y0: 0,
        x1: width,
        y1: height,
    });
    let mut edges = vec![0];
    for gap in runs(projection.iter().map(|&count| count == 0), 0) {
        if gap.end - gap.start < (glyph * 3).max(44) {
            continue;
        }
        let x = (gap.start + gap.end) / 2;
        let mut evidence = 0;
        for row in rows {
            let left = row.words.iter().rev().find(|w| w.rect.x1 <= x);
            let mut right = row.words.iter().filter(|w| w.rect.x0 >= x);
            let Some((a, b)) = left.zip(right.next()) else {
                continue;
            };
            if row.words.iter().any(|w| w.rect.x0 < x && x < w.rect.x1) {
                continue;
            }
            let heading = b.text.contains([':', '='])
                || right
                    .next()
                    .is_some_and(|w| matches!(w.text.as_str(), ":" | "="));
            // A wide space after a key can be the start of its value.
            if a.text.ends_with([':', '=']) && !heading {
                continue;
            }
            evidence += if heading || a.group != b.group { 2 } else { 1 };
        }
        if evidence >= 2 && edges.last().is_some_and(|last| x - last >= glyph * 3) {
            edges.push(x);
        }
    }
    edges.push(width);
    edges
        .windows(2)
        .map(|x| Extent {
            left: x[0],
            right: x[1],
        })
        .collect()
}

struct Document {
    text: String,
    rows: Vec<Row>,
    column: Extent,
    glyph: u32,
    bands: Vec<Range<u32>>,
}
impl Document {
    fn build(rows: &[Row], column: Extent, glyph: u32, ink: &Ink) -> Option<Self> {
        let mut rows: Vec<_> = rows
            .iter()
            .filter_map(|row| {
                let words: Vec<_> = row
                    .words
                    .iter()
                    .filter(|word| {
                        let center = (word.rect.x0 + word.rect.x1) / 2;
                        (column.left..column.right).contains(&center)
                    })
                    .cloned()
                    .collect();
                (!words.is_empty()).then(|| Row::new(words))
            })
            .collect();
        let left = rows.iter().map(|row| row.rect.x0).min()?;
        let mut text = String::new();
        let unit = (glyph * 3 / 5).max(3);
        for row in &mut rows {
            if !text.is_empty() {
                text.push('\n');
            }
            let start = text.len();
            text.extend(std::iter::repeat_n(
                ' ',
                (row.rect.x0.saturating_sub(left) / unit).min(120) as usize,
            ));
            for (i, word) in row.words.iter().enumerate() {
                if i > 0 {
                    text.push(' ');
                }
                let at = text.len();
                text.push_str(&word.text);
                row.letters.push(at..text.len());
            }
            row.text = start..text.len();
        }
        let bands = runs(ink.vertical(column), (glyph / 5).clamp(3, 9));
        Some(Self {
            text,
            rows,
            column,
            glyph,
            bands,
        })
    }

    fn pad(&self) -> u32 {
        (self.glyph / 9).clamp(2, 6)
    }

    fn line_band(&self, index: usize, height: u32) -> Option<Range<u32>> {
        let row = self.rows[index].rect;
        let hits: Vec<_> = self
            .bands
            .iter()
            .filter(|b| b.start < row.y1 && row.y0 < b.end)
            .collect();
        let top = hits.first()?.start.min(row.y0);
        let bottom = hits.last()?.end.max(row.y1);
        if index > 0 && top < self.rows[index - 1].rect.y1
            || index + 1 < self.rows.len() && bottom > self.rows[index + 1].rect.y0
        {
            return None;
        }
        let pad = self.pad();
        let previous = index.checked_sub(1).map_or(0, |i| self.rows[i].rect.y1);
        let next = self.rows.get(index + 1).map_or(height, |r| r.rect.y0);
        Some(top.saturating_sub(pad).max(previous)..(bottom + pad).min(next))
    }

    fn span_rectangles(
        &self,
        start: usize,
        end: usize,
        image: &RgbImage,
        url_mask: UrlMask,
    ) -> Vec<Rect> {
        self.rows
            .iter()
            .filter_map(|row| {
                row.words
                    .iter()
                    .zip(&row.letters)
                    .filter(|(_, range)| range.start < end && start < range.end)
                    .map(|(word, range)| {
                        let full = word.rect.pad(self.pad(), image.width(), image.height());
                        if url_mask == UrlMask::Partial {
                            let within =
                                start.saturating_sub(range.start)..end.min(range.end) - range.start;
                            partial_url_rect(image, word, within, full).unwrap_or(full)
                        } else {
                            full
                        }
                    })
                    .reduce(Rect::union)
            })
            .collect()
    }
}

fn inline_url(text: &str) -> bool {
    let text = text.trim_matches(['\'', '"', '(', ')', '<', '>', '`']);
    if text.contains("://") {
        return reqwest::Url::parse(text).is_ok_and(|url| url.has_host());
    }
    let bare = text.to_ascii_lowercase();
    (bare.starts_with("www.") || (bare.contains('.') && bare.contains('/')))
        && reqwest::Url::parse(&format!("https://{bare}"))
            .is_ok_and(|url| url.domain().is_some_and(|host| host.contains('.')))
}

// CTC positions can drift into the next glyph in proportional text. A stable
// advance across the word keeps the two raster separators tied to its text.
fn regular_character_spacing(centers: &[Option<u32>]) -> bool {
    let trusted = || {
        centers
            .iter()
            .enumerate()
            .filter_map(|(index, value)| value.map(|x| (index as f64, f64::from(x))))
    };
    let (count, sum_i, sum_x, sum_ii, sum_ix) = trusted().fold(
        (0u32, 0.0, 0.0, 0.0, 0.0),
        |(count, sum_i, sum_x, sum_ii, sum_ix), (i, x)| {
            (
                count + 1,
                sum_i + i,
                sum_x + x,
                sum_ii + i * i,
                sum_ix + i * x,
            )
        },
    );
    if count < 4 || (count as usize) * 4 < centers.len() * 3 {
        return false;
    }
    let count = f64::from(count);
    let advance = (count * sum_ix - sum_i * sum_x) / (count * sum_ii - sum_i * sum_i);
    let origin = (sum_x - advance * sum_i) / count;
    advance >= 4.0 && trusted().all(|(i, x)| (x - (origin + i * advance)).abs() <= advance / 3.0)
}

// CTC centers indicate which glyphs meet at the cut. Look for low ink at
// that boundary in the source raster. If the neighboring strokes cannot be
// separated reliably, the caller covers the entire OCR word.
fn url_cut(
    image: &RgbImage,
    rect: Rect,
    background: Rgb<u8>,
    previous: u32,
    next: u32,
    start: bool,
) -> Option<u32> {
    let gap = next.checked_sub(previous)?;
    if !(4..=rect.height().max(4)).contains(&gap) {
        return None;
    }
    let radius = (gap / 6).max(1);
    let ink: Vec<_> = (previous..=next)
        .map(|x| {
            (rect.y0..rect.y1)
                .filter(|&y| {
                    image
                        .get_pixel(x, y)
                        .0
                        .into_iter()
                        .zip(background.0)
                        .any(|(observed, base)| observed.abs_diff(base) > 16)
                })
                .count() as u32
        })
        .collect();
    let quiet = rect.height() / 12;
    let mut candidates = (radius..=gap - radius).filter(|&index| {
        ink[index as usize] <= quiet
            && ink[..index as usize].iter().any(|&count| count > quiet)
            && ink[index as usize + 1..].iter().any(|&count| count > quiet)
    });
    let cut = if start {
        candidates.next()?
    } else {
        candidates.next_back()?
    };
    Some(if start {
        previous + cut - 1
    } else {
        previous + cut + 1
    })
}

fn partial_url_rect(
    image: &RgbImage,
    word: &Word,
    range: Range<usize>,
    mut full: Rect,
) -> Option<Rect> {
    let centers = &word.character_centers;
    if centers.len() != word.text.len()
        || range.is_empty()
        || range.end > centers.len()
        || (range.start == 0 && range.end == centers.len())
        || !inline_url(&word.text)
        || !regular_character_spacing(centers)
    {
        return None;
    }
    let background = raster::background(image, word.rect)?;
    if range.start > 0 {
        full.x0 = url_cut(
            image,
            word.rect,
            background,
            centers[range.start - 1]?,
            centers[range.start]?,
            true,
        )?;
    }
    if range.end < centers.len() {
        full.x1 = url_cut(
            image,
            word.rect,
            background,
            centers[range.end - 1]?,
            centers[range.end]?,
            false,
        )?;
    }
    (full.x0 < full.x1).then_some(full)
}

fn scalar_reference(value: &str) -> bool {
    let value = value.trim();
    let unquoted = value
        .strip_prefix('"')
        .and_then(|s| s.strip_suffix('"'))
        .or_else(|| value.strip_prefix('\'').and_then(|s| s.strip_suffix('\'')))
        .unwrap_or(value);
    matches!(value, "\"\"" | "''") || credential_reference(unquoted)
}

// Used for line structure only. Sensitivity and placeholder rules come from KeyValue.
fn starts_assignment(text: &str) -> bool {
    let text = text.trim_start();
    let text = text.strip_prefix("- ").unwrap_or(text);
    let Some((key, value)) = text.split_once([':', '=']) else {
        return false;
    };
    !key.trim().is_empty()
        && key
            .trim()
            .trim_matches(['"', '\''])
            .chars()
            .all(|c| c.is_alphanumeric() || "_-.".contains(c))
        && !value.starts_with(['/', '\\'])
}

fn unclosed_quote(value: &str) -> Option<char> {
    let quote = value.chars().next().filter(|q| matches!(q, '\'' | '"'))?;
    let mut chars = value[1..].chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\\' && quote == '"' {
            chars.next();
        } else if c == quote {
            if quote == '\'' && chars.peek() == Some(&'\'') {
                chars.next();
            } else {
                return None;
            }
        }
    }
    Some(quote)
}

fn rectangle_for_field(
    doc: &Document,
    index: usize,
    ink: &Ink,
    height: u32,
    field: &FieldAssignment<'_>,
    separator: Option<usize>,
) -> Result<(Option<Rect>, bool), Failure> {
    let y = doc
        .line_band(index, height)
        .ok_or_else(|| Failure::field(field.key))?;
    let row = &doc.rows[index];
    let cell = Rect {
        x0: doc.column.left,
        x1: doc.column.right,
        y0: y.start,
        y1: y.end,
    };
    let projection = ink.horizontal(cell);
    let has_ink = |x: u32| projection[(x - cell.x0) as usize] != 0;
    let Some(separator) = separator else {
        return Ok((Some(cell), true)); // Entire continuation line remains opaque.
    };
    let sep = &row.words[separator];
    if sep.rect.x1 > cell.x1 || sep.rect.x0 < cell.x0 {
        return Err(Failure::field(field.key));
    }
    // A token covering both sides of the separator cannot certify the caption boundary.
    let after = field.separator + 1;
    if row.letters[separator].end > after
        && !doc.text[after..row.letters[separator].end]
            .trim()
            .is_empty()
    {
        return Ok((Some(cell), true));
    }

    // Raster edges can extend outside the TSV rectangles. Start at the blank
    // column immediately after the separator, before *any* visible value ink.
    let search_start = sep.rect.x1.saturating_sub(doc.pad()).max(sep.rect.x0);
    let search_end = (sep.rect.x1 + doc.pad() + 2).min(cell.x1);
    let following = row.words.get(separator + 1);
    let boundary =
        (search_start..search_end).find(|&x| !has_ink(x) && x >= sep.rect.x1.saturating_sub(1));
    let Some(x0) = boundary else {
        return Ok((Some(cell), true));
    };
    let visible_end = (x0..cell.x1).rev().find(|&x| has_ink(x)).map(|x| x + 1);
    let Some(visible_end) = visible_end else {
        if following.is_some() {
            return Err(Failure::field(field.key));
        }
        return Ok((None, false));
    };
    let value = &doc.text[field.value.clone()];
    let words = &row.words[separator + 1..];
    let bounds = words.iter().map(|w| w.rect).reduce(Rect::union);
    let visible_start = (x0..visible_end).find(|&x| has_ink(x)).unwrap_or(x0);
    if scalar_reference(value)
        && !words.is_empty()
        && words.iter().all(|word| word.confidence >= 65.0)
        && let Some(bounds) = bounds
        && visible_start >= bounds.x0.saturating_sub(2)
        && visible_end <= bounds.x1 + 3
    {
        return Ok((None, false));
    }
    // If the raster shows omitted glyphs, extend through the cell boundary.
    // A verified separation before the first value still preserves the caption.
    let incomplete = bounds
        .is_none_or(|r| visible_start + doc.pad() < r.x0 || visible_end > r.x1 + doc.pad())
        || unclosed_quote(value.trim()).is_some();
    let x1 = if incomplete {
        cell.x1
    } else {
        (visible_end.max(bounds.map_or(0, |r| r.x1)) + doc.pad() + 2).min(cell.x1)
    };
    Ok((Some(Rect { x0, x1, ..cell }), false))
}

fn complete_field(
    doc: &Document,
    ink: &Ink,
    height: u32,
    field: &FieldAssignment<'_>,
) -> Result<(Vec<Rect>, bool), Failure> {
    let i = doc
        .rows
        .iter()
        .position(|r| r.text.contains(&field.separator))
        .ok_or_else(|| Failure::field(field.key))?;
    let row = &doc.rows[i];
    let separator = row
        .word_at(field.separator)
        .ok_or_else(|| Failure::field(field.key))?;
    let value = doc.text[field.value.clone()].trim();
    let block = yaml_block_indicator(value);
    let mut quote = unclosed_quote(value);
    let first = rectangle_for_field(doc, i, ink, height, field, Some(separator))?;
    let (mut rects, mut whole_row) = (Vec::new(), first.1);
    if !block && let Some(rect) = first.0 {
        rects.push(rect);
    }
    let slack = (doc.glyph / 2).max(4);
    let indent = row.rect.x0;
    let mut previous = i;
    let mut ended = false;
    for j in i + 1..doc.rows.len() {
        let next = &doc.rows[j];
        let text = &doc.text[next.text.clone()];
        let indented = next.rect.x0 >= indent + slack;
        let statement = starts_assignment(text);
        let after_previous = doc
            .line_band(previous, height)
            .ok_or_else(|| Failure::field(field.key))?
            .end;
        let before_next = doc.line_band(j, height).map_or(next.rect.y0, |b| b.start);
        let unrecognized = doc.bands.iter().any(|b| {
            b.start >= after_previous && b.end <= before_next && b.end - b.start >= doc.pad()
        });
        if unrecognized {
            return Err(Failure::field(field.key));
        }
        if doc.rows[previous].rect.y1 + doc.glyph * 3 < next.rect.y0 {
            ended = true;
            break;
        }
        if statement && (!block || !indented) {
            ended = true;
            break;
        }
        if indented && (block || quote.is_some() || !statement) {
            let (rect, fallback) = rectangle_for_field(doc, j, ink, height, field, None)?;
            rects.extend(rect);
            whole_row |= fallback;
            previous = j;
            if let Some(q) = quote
                && text.trim_end().ends_with(q)
            {
                quote = None;
            }
        } else {
            // Near an open quote, a block marker, or a value touching the cell edge,
            // a same-indent unlabelled row can be a clipped continuation.
            if quote.is_some()
                || block
                || rects
                    .last()
                    .is_some_and(|r| r.x1 + slack >= doc.column.right)
            {
                return Err(Failure::field(field.key));
            }
            ended = true;
            break;
        }
    }
    if !ended {
        let bottom = doc
            .line_band(previous, height)
            .ok_or_else(|| Failure::field(field.key))?
            .end;
        if doc.bands.iter().any(|b| {
            b.start >= bottom && b.start < bottom + doc.glyph * 2 && b.end - b.start >= doc.pad()
        }) {
            return Err(Failure::field(field.key));
        }
    }
    if (block && rects.is_empty()) || (quote.is_some() && !ended && previous != i) {
        return Err(Failure::field(field.key));
    }
    Ok((rects, whole_row))
}

pub(super) struct Masks {
    pub areas: Vec<Area>,
    pub review_values: Vec<(usize, String)>,
}

pub(super) fn mask(
    engine: &Engine,
    image: &RgbImage,
    reading: Reading,
    scope: Scope,
    cancelled: &AtomicBool,
    link_reviews: bool,
    url_mask: UrlMask,
) -> Result<Masks, Failure> {
    if reading.words.is_empty() {
        return Err(Failure::new(Code::Empty));
    }
    if !reading.words.iter().any(|word| {
        word.confidence >= 52.0 && word.text.chars().filter(|c| c.is_alphanumeric()).count() >= 2
    }) {
        return Err(Failure::new(Code::Unreadable));
    }
    let (width, height) = image.dimensions();
    let mut heights: Vec<_> = reading
        .words
        .iter()
        .map(|word| word.rect.height())
        .collect();
    heights.sort_unstable();
    let glyph = heights[heights.len() / 2].clamp(6, 160);
    let ink = Ink::read(image, cancelled)?;
    let rows = physical_rows(reading.words);
    let columns = columns(&rows, &ink, width, height, glyph);
    let options = MaskOptions {
        scope,
        irreversible: true,
        journal: false,
        explain: true,
        ..MaskOptions::default()
    };
    let detect = |doc: &Document, options: &MaskOptions| {
        if cancelled.load(Relaxed) {
            return Err(Failure::new(Code::Cancelled));
        }
        engine
            .mask_without_s1(
                &Input {
                    text: &doc.text,
                    format: Format::Auto,
                },
                options,
                &mut MemoryVault::new(),
            )
            .map_err(|_| Failure::new(Code::Rules))
    };

    let mut documents = Vec::new();
    for column in columns {
        if cancelled.load(Relaxed) {
            return Err(Failure::new(Code::Cancelled));
        }
        let Some(doc) = Document::build(&rows, column, glyph, &ink) else {
            continue;
        };
        let masked = detect(&doc, &options)?;
        documents.push((doc, masked));
    }
    // OCR can read a repeated value while dropping the separator that identified it
    // as a credential. Remember confirmed values for this image, including other columns.
    let mut seen = HashSet::new();
    let known = documents
        .iter()
        .flat_map(|(_, masked)| &masked.replacements)
        .filter(|r| {
            r.protected && r.category == Category::Credential && seen.insert(r.original.as_str())
        })
        .map(|r| Known {
            text: r.original.clone(),
            canonical: r.original.clone(),
            kind: r.kind.clone(),
            scope: r.scope,
            strategy: Strategy::Hmac,
            form: TokenForm::Snake,
        })
        .collect();
    let repeat_options = MaskOptions {
        known,
        explain: false,
        ..options
    };
    let mut areas: Vec<Area> = Vec::new();
    let mut review_values = BTreeMap::new();
    static KEY_VALUE: OnceLock<KeyValue> = OnceLock::new();
    let key_value = KEY_VALUE.get_or_init(KeyValue::new);
    for (doc, masked) in documents {
        let column = doc.column;
        for field in key_value.field_assignments(engine.lexicon(), &doc.text) {
            let matches = |start: usize, end: usize, key: &str| {
                key.strip_prefix("key:").is_some_and(|key| key == field.key)
                    && start >= field.value.start
                    && (start <= field.value.end || field.value.is_empty())
                    && end > field.separator
            };
            let replacement = masked.replacements.iter().find(|r| {
                r.evidence
                    .iter()
                    .any(|e| matches(r.start, r.end, &e.source))
            });
            let kept = masked.explain.iter().any(|r| {
                r.verdict == Verdict::Keep
                    && r.evidence.iter().any(|e| e.source == "rule-keep")
                    && r.evidence
                        .iter()
                        .any(|e| matches(r.start, r.end, &e.source))
            });
            if replacement.is_none() && (!field.mandatory || kept) {
                continue;
            }
            let (rectangles, whole) = complete_field(&doc, &ink, height, &field)?;
            if rectangles.is_empty() {
                continue;
            }
            if areas.len() == MAX_AREAS {
                return Err(Failure::new(Code::Limits));
            }
            if link_reviews && let Some(replacement) = replacement.filter(|r| !r.protected) {
                review_values.insert(areas.len(), replacement.original.clone());
            }
            areas.push(Area {
                id: areas.len(),
                rectangles,
                category: field.category,
                kind: field.kind.into(),
                scope: replacement.map_or(scope, |r| r.scope),
                reason: if whole {
                    Reason::FieldRow
                } else {
                    Reason::FieldValue
                },
                field: Some(field.key.into()),
                review_id: None,
            });
        }
        let mut replacements = masked.replacements;
        if !repeat_options.known.is_empty() {
            replacements.extend(
                detect(&doc, &repeat_options)?
                    .replacements
                    .into_iter()
                    .filter(|r| r.recognizer == "session")
                    .map(|mut r| {
                        r.category = Category::Credential;
                        r
                    }),
            );
        }
        for replacement in replacements {
            let rectangles =
                doc.span_rectangles(replacement.start, replacement.end, image, url_mask);
            if rectangles.is_empty() {
                return Err(Failure::new(Code::Boundaries));
            }
            let rectangles: Vec<_> = rectangles
                .into_iter()
                .filter(|rect| {
                    !areas.iter().any(|area| {
                        area.rectangles.iter().any(|known| {
                            known.contains(*rect)
                                || (matches!(area.reason, Reason::FieldValue | Reason::FieldRow)
                                    && known.x0 <= rect.x0 + doc.pad()
                                    && known.x1 + doc.pad() >= rect.x1
                                    && known.y0 <= rect.y0 + doc.pad()
                                    && known.y1 + doc.pad() >= rect.y1)
                        })
                    })
                })
                .collect();
            if rectangles.is_empty() {
                continue;
            }
            if areas.len() == MAX_AREAS {
                return Err(Failure::new(Code::Limits));
            }
            if replacement.recognizer == "config-keys" && replacement.protected {
                // Structured formats such as env name/value pairs may not be a
                // simple assignment on the same line. Cover the full visible row.
                let mut full = Vec::new();
                for (i, row) in doc.rows.iter().enumerate() {
                    if row.text.start < replacement.end && replacement.start < row.text.end {
                        let y = doc
                            .line_band(i, height)
                            .ok_or_else(|| Failure::new(Code::Boundaries))?;
                        full.push(Rect {
                            x0: column.left,
                            x1: column.right,
                            y0: y.start,
                            y1: y.end,
                        });
                    }
                }
                if full.is_empty() {
                    return Err(Failure::new(Code::Boundaries));
                }
                areas.push(Area {
                    id: areas.len(),
                    rectangles: full,
                    category: replacement.category,
                    kind: replacement.kind,
                    scope: replacement.scope,
                    reason: Reason::FieldRow,
                    field: replacement
                        .evidence
                        .iter()
                        .find_map(|e| e.source.strip_prefix("key:").map(str::to_owned)),
                    review_id: None,
                });
            } else {
                if link_reviews && !replacement.protected {
                    review_values.insert(areas.len(), replacement.original.clone());
                }
                areas.push(Area {
                    id: areas.len(),
                    rectangles,
                    category: replacement.category,
                    kind: replacement.kind,
                    scope: replacement.scope,
                    reason: Reason::Detector,
                    field: None,
                    review_id: None,
                });
            }
        }
        // Suggestions are optional, and follow the same category decision
        // thresholds as other candidates. They cannot cancel local masks.
        for suggestion in &reading.suggestions {
            if suggestion.rect.x0 < column.left || suggestion.rect.x0 >= column.right {
                continue;
            }
            if areas
                .iter()
                .flat_map(|a| &a.rectangles)
                .any(|r| r.contains(suggestion.rect))
            {
                continue;
            }
            let thresholds = engine.policy().thresholds(suggestion.category);
            if 0.85 < thresholds.keep
                || (0.85 < thresholds.mask
                    && engine.policy().uncertain == kaponir_core::UncertainAction::Keep)
            {
                continue;
            }
            areas.push(Area {
                id: areas.len(),
                rectangles: vec![suggestion.rect.pad(doc.pad(), width, height)],
                category: suggestion.category,
                kind: suggestion.kind.clone(),
                scope,
                reason: Reason::Vision,
                field: None,
                review_id: None,
            });
        }
        if areas.len() > MAX_AREAS {
            return Err(Failure::new(Code::Limits));
        }
    }
    areas.sort_by_key(|a| (a.rectangles[0].y0, a.rectangles[0].x0));
    let links = areas
        .iter_mut()
        .enumerate()
        .filter_map(|(i, area)| {
            let value = review_values.remove(&area.id);
            area.id = i + 1;
            value.map(|value| (area.id, value))
        })
        .collect();
    Ok(Masks {
        areas,
        review_values: links,
    })
}
