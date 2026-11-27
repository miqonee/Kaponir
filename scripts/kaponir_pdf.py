"""Rasterize untrusted PDF attachments or build a fresh PDF from masked PNGs."""
from ctypes import c_int
from io import BytesIO
import json
import math
import struct
import sys


SCALE = 2
MAX_EDGE = 8192
MAX_TEXT_BYTES = 2 << 20


class LimitError(Exception):
    pass


def page_text(pdfium, page, width, height):
    raw = pdfium.raw
    text_object = getattr(raw, "FPDFText_GetTextObject", None)
    render_mode = getattr(raw, "FPDFTextObj_GetTextRenderMode", None)
    device_x, device_y = c_int(), c_int()

    def pixel(x, y):
        if not raw.FPDF_PageToDevice(page, 0, 0, width, height, 0, x, y, device_x, device_y):
            raise ValueError("invalid page coordinates")
        return device_x.value, device_y.value

    try:
        textpage = page.get_textpage()
    except pdfium.PdfiumError:
        # A rasterized page can still be checked if its text layer is unavailable.
        return b""
    text, boxes = [], []
    try:
        count = textpage.count_chars()
        if count > 50_000:
            raise LimitError
        for index in range(count):
            code = raw.FPDFText_GetUnicode(textpage, index)
            char = chr(code) if 0 < code <= 0x10ffff and not 0xd800 <= code <= 0xdfff else "\ufffd"
            rect = None
            if not char.isspace() and char.isprintable():
                obj = text_object(textpage, index) if text_object and render_mode else None
                # Ignore invisible and clipping-only text. The raster pass still
                # checks any visible text in graphics below these layers.
                if not obj or render_mode(obj) not in (3, 7):
                    try:
                        left, bottom, right, top = textpage.get_charbox(index)
                        if all(math.isfinite(n) for n in (left, bottom, right, top)):
                            x, y = pixel(left, bottom)
                            a, b = pixel(right, top)
                            x0, y0 = max(0, min(x, a)), max(0, min(y, b))
                            x1, y1 = min(width, max(x, a)), min(height, max(y, b))
                            if x0 < x1 and y0 < y1:
                                rect = (x0, y0, x1, y1)
                    except (pdfium.PdfiumError, ValueError):
                        pass
            text.append(char if rect is not None or char in "\r\n" else " ")
            boxes.append(rect)
    finally:
        textpage.close()
    if not any(boxes):
        return b""
    data = json.dumps({"text": "".join(text), "boxes": boxes}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise LimitError
    return data


def render(pdfium, source, output, page_limit, pixel_limit, page_pixels, byte_length):
    data = source.read(byte_length)
    if len(data) != byte_length:
        raise ValueError("incomplete document")
    with pdfium.PdfDocument(data) as document:
        count = len(document)
        if count == 0:
            raise ValueError("empty document")
        if count > page_limit:
            raise LimitError

        sizes = []
        for index in range(count):
            page = document[index]
            try:
                width, height = page.get_size()
            finally:
                page.close()
            if not all(math.isfinite(size) and size > 0 for size in (width, height)):
                raise ValueError("invalid page size")
            w, h = math.ceil(width * SCALE), math.ceil(height * SCALE)
            pixel_limit -= w * h
            if max(w, h) > MAX_EDGE or w * h > page_pixels or pixel_limit < 0:
                raise LimitError
            sizes.append((w, h))

        output.write(struct.pack(">BB", 0, count))
        for size in sizes:
            output.write(struct.pack(">II", *size))
        for index, (w, h) in enumerate(sizes):
            # The proxy requests each raster when it has space to process it.
            if source.read(1) != b"\x01":
                raise ValueError("missing page request")
            page = document[index]
            try:
                # Use static appearances. No interactive form environment is loaded.
                bitmap = page.render(scale=SCALE, rev_byteorder=True, draw_annots=True,
                                     may_draw_forms=False, limit_image_cache=True)
                try:
                    with bitmap.to_pil().convert("RGB") as image:
                        if image.size != (w, h):
                            raise ValueError("unexpected raster size")
                        text = page_text(pdfium, page, w, h)
                        pixels = image.tobytes()
                        output.write(struct.pack(">BI", 0, len(text)))
                        output.write(text)
                        output.write(pixels)
                        del text, pixels
                finally:
                    bitmap.close()
            finally:
                page.close()


def assemble(pdfium, Image, source, output, byte_limit):
    count, = struct.unpack(">B", source.read(1))
    with pdfium.PdfDocument.new() as document:
        for _ in range(count):
            length, = struct.unpack(">I", source.read(4))
            with Image.open(BytesIO(source.read(length)), formats=["PNG"]) as image:
                # Use the rendered grid to avoid re-interpolating mask edges
                # when the source page size was fractional.
                width, height = image.width / SCALE, image.height / SCALE
                bitmap = pdfium.PdfBitmap.from_pil(image)
                page = document.new_page(width, height)
                try:
                    obj = pdfium.PdfImage.new(document)
                    obj.set_bitmap(bitmap)
                    obj.set_matrix(pdfium.PdfMatrix(width, 0, 0, height, 0, 0))
                    page.insert_obj(obj)
                    page.gen_content()
                finally:
                    page.close()
                    bitmap.close()
        with BytesIO() as data:
            document.save(data)
            if data.tell() > byte_limit:
                raise LimitError
            blob = data.getbuffer()
            try:
                output.write(struct.pack(">BI", 0, len(blob)))
                output.write(blob)
            finally:
                blob.release()


def main():
    source, output = sys.stdin.buffer, sys.stdout.buffer
    # Keep parser diagnostics off the binary output and bound native allocations.
    sys.stdout = sys.stderr
    try:
        if sys.platform == "linux":
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            limit = min(768 << 20, soft) if soft != resource.RLIM_INFINITY else 768 << 20
            resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        import pypdfium2 as pdfium
        from PIL import Image
    except (ImportError, OSError, ValueError):
        output.write(b"\x01")
        return
    try:
        limits = map(int, sys.argv[2:])
        if sys.argv[1] == "render":
            render(pdfium, source, output, *limits)
        else:
            assemble(pdfium, Image, source, output, *limits)
    except (LimitError, MemoryError):
        output.write(b"\x03")
    except Exception:
        output.write(b"\x02")


if __name__ == "__main__":
    main()
