"""Rasterize untrusted PDF attachments or build a fresh PDF from masked PNGs."""
from io import BytesIO
import math
import struct
import sys


SCALE = 2
MAX_EDGE = 8192


class LimitError(Exception):
    pass


def render(pdfium, source, output, page_limit, pixel_limit, byte_limit):
    data = source.read(byte_limit + 1)
    if len(data) > byte_limit:
        raise LimitError
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
            if max(w, h) > MAX_EDGE or pixel_limit < 0:
                raise LimitError
            sizes.append((width, height, w, h))

        output.write(struct.pack(">BB", 0, count))
        for index, (width, height, w, h) in enumerate(sizes):
            page = document[index]
            try:
                # Use static appearances. No interactive form environment is loaded.
                bitmap = page.render(scale=SCALE, rev_byteorder=True, draw_annots=True,
                                     may_draw_forms=False, limit_image_cache=True)
                try:
                    with bitmap.to_pil().convert("RGB") as image:
                        if image.size != (w, h):
                            raise ValueError("unexpected raster size")
                        output.write(struct.pack(">ddII", width, height, w, h))
                        output.write(image.tobytes())
                finally:
                    bitmap.close()
            finally:
                page.close()


def assemble(pdfium, Image, source, output, byte_limit):
    count, = struct.unpack(">B", source.read(1))
    with pdfium.PdfDocument.new() as document:
        for _ in range(count):
            width, height, length = struct.unpack(">ddI", source.read(20))
            with Image.open(BytesIO(source.read(length)), formats=["PNG"]) as image:
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
