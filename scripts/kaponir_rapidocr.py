"""Multilingual RapidOCR for Kaponir's local image paths.

The stdio protocol is a big-endian u32 payload length followed by a PNG.
The two high bits request URL character geometry and the Russian-English profile.
An empty payload probes availability. The reply is a status byte, a u32
length and a TSV payload. Nothing in this protocol is written to logs.
"""
import hashlib
import math
from pathlib import Path
import re
import struct
import sys
from tempfile import NamedTemporaryFile
from urllib.request import urlopen


MAX_PNG = 32 << 20
MAX_TSV = 2 << 20
MAX_WORDS = 10_000
CHARACTER_BOXES = 1 << 31
RUSSIAN_ENGLISH = 1 << 30
MIN_BOUNDARY_CONFIDENCE = 0.85
TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CYRILLIC_MODEL = "cyrillic_PP-OCRv5_rec_mobile.onnx"
CYRILLIC_URL = ("https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/"
                "onnx/PP-OCRv5/rec/" + CYRILLIC_MODEL)
CYRILLIC_SIZE = 8_074_092
CYRILLIC_SHA256 = "90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711"
CYRILLIC = re.compile(r"[\u0400-\u052f]")
LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9]")

# Treat fullwidth config separators and ASCII glyphs as their OCR equivalents.
PUNCTUATION = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}


class OcrImageError(Exception):
    pass


class OcrLimitError(Exception):
    pass


class OcrProfileError(Exception):
    pass


def verified_cyrillic(path):
    try:
        with path.open("rb") as model:
            data = model.read(CYRILLIC_SIZE + 1)
        return len(data) == CYRILLIC_SIZE and hashlib.sha256(data).hexdigest() == CYRILLIC_SHA256
    except OSError:
        return False


def install_cyrillic(engine):
    path = engine.cyrillic_path
    if not verified_cyrillic(path):
        with urlopen(CYRILLIC_URL, timeout=30) as response:
            data = response.read(CYRILLIC_SIZE + 1)
        if len(data) != CYRILLIC_SIZE or hashlib.sha256(data).hexdigest() != CYRILLIC_SHA256:
            raise OSError("Cyrillic RapidOCR model failed SHA-256 verification")
        temporary = NamedTemporaryFile(dir=path.parent, delete=False)
        staged = Path(temporary.name)
        try:
            with temporary:
                temporary.write(data)
            staged.chmod(0o644)
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
    engine.load_cyrillic()


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
        self.cyrillic_path = models / CYRILLIC_MODEL
        self.cyrillic = None
        self.params = {
            "Global.max_side_len": 8192,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            **{key: str(path) for key, path in paths.items()},
        }
        self.engine = rapidocr.RapidOCR(params=self.params)

    def load_cyrillic(self):
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

        if self.cyrillic is None:
            if not verified_cyrillic(self.cyrillic_path):
                raise OcrProfileError("cyrillic_model_unavailable")
            self.cyrillic = RapidOCR(params={
                **self.params,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.lang_type": LangRec.CYRILLIC,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.model_path": str(self.cyrillic_path),
            })._load_rec_model()
        return self.cyrillic

    def russian_english(self, pixels, character_boxes):
        from rapidocr.ch_ppocr_rec.typings import TextRecInput
        from rapidocr.main import RapidOCRError, RapidOCROutput, TextClsOutput

        recognizer = self.load_cyrillic()
        engine = self.engine
        engine.return_word_box = True
        engine.return_single_char_box = character_boxes
        raster, operations = engine.preprocess_img(pixels)
        try:
            crops, detection = engine.detect_and_crop(raster, operations)
        except RapidOCRError:
            return RapidOCROutput()

        reading = recognizer(TextRecInput(img=crops, return_word_box=True))
        # Keep the V6 reading and its character geometry on Latin-only lines.
        latin = [i for i, line in enumerate(reading.txts) if not CYRILLIC.search(line)]
        if latin:
            v6 = engine.recognize_txt([crops[i] for i in latin])
            text, scores, words = list(reading.txts), list(reading.scores), list(reading.word_results)
            for i, line, score, pieces in zip(latin, v6.txts, v6.scores, v6.word_results, strict=True):
                if line.strip() and score >= engine.text_score:
                    text[i], scores[i], words[i] = line, score, pieces
            reading.txts, reading.scores, reading.word_results = tuple(text), scores, tuple(words)
        return engine.build_final_output(pixels, detection, TextClsOutput(), reading, crops, operations)

    def latin_alternates(self, pixels, words):
        from rapidocr.ch_ppocr_rec.typings import TextRecInput

        height, width = pixels.shape[:2]
        crops, positions = [], []
        for index, (_, _, word, _, (x0, y0, x1, y1), _, mixed) in enumerate(words):
            if not mixed or not LATIN_OR_DIGIT.search(word):
                continue
            pad = max(2, min(5, (y1 - y0) // 9))
            crop = pixels[max(0, y0 - pad):min(height, y1 + pad),
                          max(0, x0 - pad):min(width, x1 + pad)]
            crops.append(crop)
            positions.append(index)
        if not crops:
            return {}
        result = self.engine._load_rec_model()(TextRecInput(img=crops, return_word_box=False))
        alternatives = {}
        for i, text, score in zip(positions, result.txts, result.scores, strict=True):
            text = clean(text)
            if (text.isascii() and LATIN_OR_DIGIT.search(text) and len(text) <= 1024
                    and text != words[i][2] and score >= self.engine.text_score):
                alternatives[i] = text
        return alternatives

    def tsv(self, png, character_boxes=False, russian_english=False):
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
        result = (self.russian_english(pixels, character_boxes) if russian_english else
                  self.engine(pixels, use_cls=False, return_word_box=True,
                              return_single_char_box=character_boxes))

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
            mixed = russian_english and bool(CYRILLIC.search(line))
            for order, (word, confidence, rect, centers) in enumerate(
                words or [(line, float(score), rect, [])], 1
            ):
                if len(words_in_image) == MAX_WORDS or len(word.encode("utf-8")) > 1024:
                    raise OcrLimitError
                if not math.isfinite(confidence):
                    raise OcrImageError
                confidence = max(0, min(100, 100 * confidence))
                words_in_image.append((index, order, word, confidence, rect, centers, mixed))

        # The detector sometimes puts the same colon in both the label box
        # and the value line. Redundant separators obscure the label boundary.
        labels = {":": [], "=": []}
        for _, _, word, _, rect, _, _ in words_in_image:
            if len(word) > 1 and word[-1] in labels:
                labels[word[-1]].append(rect)
        records = [TSV_HEADER]
        geometry_budget = MAX_TSV // 4
        alternatives = self.latin_alternates(pixels, words_in_image) if russian_english else {}
        for position, (index, order, word, confidence, rect, centers, _) in enumerate(words_in_image):
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
            if position in alternatives:
                records.append(f"#alternate-text\t{alternatives[position]}\n")
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
        russian_english = bool(length & RUSSIAN_ENGLISH)
        length &= ~(CHARACTER_BOXES | RUSSIAN_ENGLISH)
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
                data = engine.tsv(png, character_boxes, russian_english)
            elif russian_english:
                engine.load_cyrillic()
        except OcrImageError:
            status = 2
        except OcrLimitError:
            status = 3
        except OcrProfileError:
            status = 4
        except Exception:
            status = 1
        send.write(struct.pack(">BI", status, len(data)))
        send.write(data)
        send.flush()
        del png, data
        if status == 1:
            return 1


if __name__ == "__main__":
    if sys.argv[1] == "--install-cyrillic":
        install_cyrillic(KaponirRapidOCR(1))
    elif sys.argv[1] == "--check":
        engine = KaponirRapidOCR(1).engine
        engine._load_det_model()
        engine._load_rec_model()
    else:
        sys.exit(ocr_pipe(int(sys.argv[1])))
