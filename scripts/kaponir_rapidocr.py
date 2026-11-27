"""Multilingual RapidOCR for Kaponir's local image paths.

The stdio protocol is a big-endian u32 payload length followed by a PNG.
The high bit requests optional character geometry for URL masks.
An empty payload probes availability. The reply is a status byte, a u32
length and a TSV payload. Nothing in this protocol is written to logs.
"""
import math
from pathlib import Path
import re
import struct
import sys


MAX_PNG = 32 << 20
MAX_TSV = 2 << 20
MAX_WORDS = 10_000
CHARACTER_BOXES = 1 << 31
MIN_BOUNDARY_CONFIDENCE = 0.85
TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Treat fullwidth config separators and ASCII glyphs as their OCR equivalents.
PUNCTUATION = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}


class OcrImageError(Exception):
    pass


class OcrLimitError(Exception):
    pass


def clean(text):
    return " ".join(text.translate(PUNCTUATION).split())


def bounds(points, width, height):
    if points is None or len(points) != 4:
        return None
    xs, ys = zip(*points)
    if not all(math.isfinite(float(v)) for v in (*xs, *ys)):
        return None
    x0 = max(0, math.floor(min(xs)))
    y0 = max(0, math.floor(min(ys)))
    x1 = min(width, math.ceil(max(xs)))
    y1 = min(height, math.ceil(max(ys)))
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def union(rects):
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def duplicated_separator(rect, labels):
    x0, y0, x1, y1 = rect
    area = (x1 - x0) * (y1 - y0)
    for a, b, c, d in labels:
        overlap = max(0, min(x1, c) - max(x0, a)) * max(0, min(y1, d) - max(y0, b))
        if 3 * overlap >= 2 * area:
            return True
    return False


def word_boxes(line, pieces, width, height, character_boxes=False):
    """Join character boxes at the whitespace boundaries of the transcription.

    RapidOCR can return one box per character when a line mixes scripts. A
    group spanning several words keeps its text intact and uses one rectangle.
    """
    compact = "".join(line.split())
    ends = {}
    offset = 0
    for word in re.finditer(r"\S+", line):
        offset += len(word[0])
        ends[offset] = word.end()
    groups = []
    rects = []
    scores = []
    offset = start = 0
    for text, score, points in pieces:
        rect = bounds(points, width, height)
        part = "".join(clean(text).split())
        if not part:
            continue
        if rect is None or not compact.startswith(part, offset):
            return []
        rects.append(rect)
        scores.append(float(score))
        offset += len(part)
        if offset in ends:
            end = ends[offset]
            word = line[start:end].strip()
            centers = []
            looks_like_url = "://" in word or ("." in word and ("/" in word or word.lower().startswith("www.")))
            if (character_boxes and looks_like_url and word.isascii()
                    and not any(ch.isspace() for ch in word) and len(rects) == len(word)):
                centers = [str((rect[0] + rect[2]) // 2) if score >= MIN_BOUNDARY_CONFIDENCE else "-"
                           for rect, score in zip(rects, scores, strict=True)]
            groups.append((word, min(scores), union(rects), centers))
            start = end
            rects = []
            scores = []
    return groups if offset == len(compact) else []


class KaponirRapidOCR:
    def __init__(self, threads):
        import cv2
        import numpy
        import onnxruntime
        import rapidocr

        models = Path(rapidocr.__file__).resolve().parent / "models"
        paths = {
            "Det.model_path": models / "PP-OCRv6_det_small.onnx",
            "Rec.model_path": models / "PP-OCRv6_rec_small.onnx",
        }
        if not all(path.is_file() for path in paths.values()):
            raise OSError("rapidocr_models_unavailable")
        cv2.setNumThreads(1)
        onnxruntime.disable_telemetry_events()
        self.cv2 = cv2
        self.numpy = numpy
        self.engine = rapidocr.RapidOCR(params={
            "Global.max_side_len": 8192,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            **{key: str(path) for key, path in paths.items()},
        })

    def tsv(self, png, character_boxes=False):
        if len(png) > MAX_PNG:
            raise OcrLimitError
        if len(png) < 33 or not png.startswith(PNG_MAGIC) or png[12:16] != b"IHDR":
            raise OcrImageError
        width, height = struct.unpack(">II", png[16:24])
        if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 32_000_000):
            raise OcrLimitError
        pixels = self.cv2.imdecode(self.numpy.frombuffer(png, dtype=self.numpy.uint8),
                                   self.cv2.IMREAD_COLOR)
        if pixels is None or pixels.shape[:2] != (height, width):
            raise OcrImageError

        # The detector rescales for localization, then recognition crops the
        # source raster. Preserve the small-image profile for agent thumbnails.
        det = self.engine._load_det_model()
        if max(width, height) > 2000:
            det.limit_type, det.limit_side_len = "max", 1800
        else:
            det.limit_type, det.limit_side_len = "min", 736
        result = self.engine(pixels, use_cls=False, return_word_box=True,
                             return_single_char_box=character_boxes)

        words_in_image = []
        for index, (line, score, polygon) in enumerate(
            zip(result.txts or (), result.scores or (),
                result.boxes if result.boxes is not None else (), strict=True),
            1,
        ):
            line = clean(line)
            if not line:
                continue
            rect = bounds(polygon, width, height)
            if rect is None:
                raise OcrImageError
            pieces = result.word_results or ()
            words = (word_boxes(line, pieces[index - 1], width, height, character_boxes)
                     if index <= len(pieces) else [])
            for order, (word, confidence, rect, centers) in enumerate(
                words or [(line, float(score), rect, [])], 1
            ):
                if len(words_in_image) == MAX_WORDS or len(word.encode("utf-8")) > 1024:
                    raise OcrLimitError
                if not math.isfinite(confidence):
                    raise OcrImageError
                confidence = max(0, min(100, 100 * confidence))
                words_in_image.append((index, order, word, confidence, rect, centers))

        # The detector sometimes puts the same colon in both the label box
        # and the value line. Redundant separators obscure the label boundary.
        labels = {":": [], "=": []}
        for _, _, word, _, rect, _ in words_in_image:
            if len(word) > 1 and word[-1] in labels:
                labels[word[-1]].append(rect)
        records = [TSV_HEADER]
        geometry_budget = MAX_TSV // 4
        for index, order, word, confidence, rect, centers in words_in_image:
            if word in labels and duplicated_separator(rect, labels[word]):
                continue
            x0, y0, x1, y1 = rect
            records.append(
                f"5\t1\t1\t1\t{index}\t{order}\t{x0}\t{y0}\t{x1-x0}\t{y1-y0}\t{confidence:.3f}\t{word}\n"
            )
            if centers and geometry_budget > 0:
                record = "#char-centers\t" + ",".join(centers) + "\n"
                geometry_budget -= len(record)
                if geometry_budget >= 0:
                    records.append(record)
        data = "".join(records).encode("utf-8")
        if len(data) > MAX_TSV and character_boxes:
            data = "".join(row for row in records if not row.startswith("#char-centers\t")).encode("utf-8")
        if len(data) > MAX_TSV:
            raise OcrLimitError
        return data


def ocr_pipe(threads):
    receive = sys.stdin.buffer
    send = sys.stdout.buffer
    # Dependency logs must stay off the framed stream.
    sys.stdout = sys.stderr
    engine = None
    while True:
        header = receive.read(4)
        if not header:
            return 0
        if len(header) != 4:
            return 1
        length, = struct.unpack(">I", header)
        character_boxes = bool(length & CHARACTER_BOXES)
        length &= ~CHARACTER_BOXES
        if length > MAX_PNG:
            return 1
        png = receive.read(length)
        if len(png) != length:
            return 1
        status, data = 0, b""
        try:
            if engine is None:
                engine = KaponirRapidOCR(threads)
            if png:
                data = engine.tsv(png, character_boxes)
        except OcrImageError:
            status = 2
        except OcrLimitError:
            status = 3
        except Exception:
            status = 1
        send.write(struct.pack(">BI", status, len(data)))
        send.write(data)
        send.flush()
        del png, data
        if status == 1:
            return 1


if __name__ == "__main__":
    if sys.argv[1] == "--check":
        engine = KaponirRapidOCR(1).engine
        engine._load_det_model()
        engine._load_rec_model()
    else:
        sys.exit(ocr_pipe(int(sys.argv[1])))
