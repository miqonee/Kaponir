#!/usr/bin/env python3
"""Exercise the real proxy against recording Chat and Vision HTTP servers.

Requires Pillow, DejaVu Sans Mono, RapidOCR and a Kaponir binary.
All screenshots and credentials are synthetic.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, PngImagePlugin
except ImportError:
    Image = None

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "crates/kaponir-proxy/assets/ocr-example.png"
TOKEN = "kaponir-synthetic-inspector-token"
QUESTION = "Проверь конфигурацию"
FIELDS = [
    ('secretKey: ', '"FAKE_O0Il1-Aa9_8xQ2"'),
    ('accessKey: ', '"DEMO_7Rq0-I1lO_9Un"'),
]


@dataclass
class Fixture:
    raw: bytes
    reference: Image.Image
    secrets: list
    public: list
    words: list
    mime: str = "image/png"


def fixture(lines, size=(1000, 262), font_size=44, jpeg=False, photo=False, webp=False):
    """Pixel labels come from the drawing, independently of OCR."""
    background = (249, 250, 252)
    image = Image.new("RGB", size, background)
    font = ImageFont.truetype(os.environ.get(
        "KAPONIR_TEST_MONO", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"), font_size)
    ink = (24, 35, 52)
    draw = ImageDraw.Draw(image)
    public, secrets, words = [], [], []
    for row, (x, y, parts) in enumerate(lines, 1):
        written = ""
        for text, sensitive in parts:
            left = x + draw.textlength(written, font=font)
            draw.text((left, y), text, fill=ink, font=font)
            region = tuple(round(v) for v in draw.textbbox((left, y), text.rstrip(), font=font))
            if region[2] > region[0] and region[3] > region[1]:
                (secrets if sensitive else public).append(region)
            written += text
        for order, word in enumerate(written.split(), 1):
            start = written.index(word) if order == 1 else written.index(word, end)
            end = start + len(word)
            left = x + draw.textlength(written[:start], font=font)
            region = draw.textbbox((left, y), word, font=font)
            words.append({"line": row, "order": order, "text": word,
                          "box": [n / size[i % 2] for i, n in enumerate(region)]})
    buffer = BytesIO()
    if photo:
        # Synthetic phone-like capture with an illumination gradient and JPEG artifacts.
        shade = Image.linear_gradient("L").resize(size).point(lambda v: 218 + v // 9).convert("RGB")
        image = ImageChops.multiply(image, shade)
    if jpeg or photo:
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "synthetic private comment"
        image.transpose(Image.Transpose.ROTATE_90).save(buffer, "JPEG", quality=94, exif=exif)
        full = ImageOps.exif_transpose(Image.open(BytesIO(buffer.getvalue()))).convert("RGB")
        return Fixture(buffer.getvalue(), full, secrets, public, words, "image/jpeg")
    if webp:
        image.save(buffer, "WEBP", lossless=True, xmp=b"synthetic private comment")
        return Fixture(buffer.getvalue(), image, secrets, public, words, "image/webp")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "synthetic private comment")
    image.save(buffer, "PNG", pnginfo=metadata)
    return Fixture(buffer.getvalue(), image, secrets, public, words)


def helm(**options):
    rows = [(64, 32 + index * 68, [(key, False), (value, True)])
            for index, (key, value) in enumerate(FIELDS)]
    rows.append((64, 168, [("replicas: 3", False)]))
    return fixture(rows, **options)


def base64_image(source):
    return base64.b64encode(source).decode("ascii")


def openai_image(fixture_):
    return {"type": "image_url", "image_url": {
        "url": "data:" + fixture_.mime + ";base64," + base64_image(fixture_.raw), "detail": "high"}}


def anthropic_image(fixture_):
    return {"type": "image", "source": {"type": "base64", "media_type": fixture_.mime,
                                      "data": base64_image(fixture_.raw)}}


def binary_parts(payload):
    def visit(blocks):
        for block in blocks if isinstance(blocks, list) else []:
            if block["type"] == "image_url":
                assert block["image_url"]["url"].startswith("data:image/png;base64,")
                yield base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
            elif block["type"] == "image":
                assert block["source"]["media_type"] == "image/png"
                yield base64.b64decode(block["source"]["data"])
            elif block["type"] == "file" and block.get("file", {}).get("file_data"):
                assert block["file"]["file_data"].startswith("data:image/png;base64,")
                yield base64.b64decode(block["file"]["file_data"].split(",", 1)[1])
            else:
                yield from visit(block.get("content"))
    yield from visit(payload.get("system"))
    for message in payload["messages"]:
        yield from visit(message["content"])


def assert_pixels(raw, sample):
    image = Image.open(BytesIO(raw))
    assert image.format == "PNG" and image.size == sample.reference.size
    assert image.mode == "RGB", image.mode
    assert not image.info, image.info
    for box in sample.secrets:
        covered = image.crop(box)
        assert covered.getextrema() == ((0, 0),) * 3, f"incomplete value at {box}"
    for box in sample.public:
        original = sample.reference.crop(box)
        cut = image.crop(box)
        pixels = getattr(original, "get_flattened_data", original.getdata)()
        protected = getattr(cut, "get_flattened_data", cut.getdata)()
        if sample.mime == "image/jpeg":
            # Different JPEG decoders round the IDCT to slightly different RGB values.
            assert all(max(abs(x-y) for x, y in zip(before, after)) <= 9
                       for before, after in zip(pixels, protected) if min(before) < 185), box
        else:
            assert all(after == before for before, after in zip(pixels, protected)
                       if before != (249, 250, 252)), f"public glyph pixels altered at {box}"
    return len(sample.secrets), len(sample.public)


class Recorder(ThreadingHTTPServer):
    def __init__(self, vision=False):
        super().__init__(("127.0.0.1", 0), RecordingHandler)
        self.vision = vision
        self.mode = "good"
        self.restarts = 0
        self.words = helm().words
        self.records = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def close(self):
        self.shutdown()
        self.server_close()


class RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, code, body, content_type="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/__fixture_digest":
            payload = self.server.records[-1][2] if self.server.records else {"messages": []}
            self.respond(200, {"calls": len(self.server.records), "restarts": self.server.restarts, "images": [
                hashlib.sha256(item).hexdigest() for item in binary_parts(payload)]})
        else:
            self.respond(200, {"data": [{"id": "fixture-vision"}, {"id": "fixture-chat"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.records.append((time.perf_counter_ns(), self.path, body))
        if self.server.vision:
            if self.server.mode == "timeout":
                time.sleep(1.6)
            if self.server.mode == "no_images":
                self.respond(400, {"error": {"message": "image unsupported"}})
                return
            words = self.server.words if self.server.mode != "invalid" else [
                {"line": 1, "order": 1, "text": "bad", "box": [0.9, 0.0, 0.5, 0.6]}]
            self.respond(200, {"choices": [{"message": {"content": json.dumps({"words": words})}}]})
        elif self.server.mode == "error":
            self.respond(502, {"error": {"message": "Synthetic upstream failure"}})
        elif self.path.endswith("count_tokens"):
            self.respond(200, {"input_tokens": 42})
        elif body.get("stream"):
            self.respond(200, b'data: {"choices":[{"index":0,"delta":{"content":"fixture"}}]}\n\ndata: [DONE]\n\n',
                         "text/event-stream")
        elif self.path.endswith("/messages"):
            self.respond(200, {"content": [{"type": "text", "text": "Проверено. replicas: 3."}]})
        else:
            self.respond(200, {"choices": [{"index": 0, "message": {
                "role": "assistant", "content": "Проверено. replicas: 3."}}]})


class Reply:
    def __init__(self, response):
        with response:
            self.status = response.status
            self.headers = response.headers
            self.body = response.read()

    def json(self):
        return json.loads(self.body)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Proxy:
    def __init__(self, binary, home, upstream, env=None, legacy=False, worker=None):
        self.binary, self.home, self.port = binary, Path(home), free_port()
        self.env = {**os.environ, **(env or {}), "KAPONIR_HOME": str(home),
                    "KAPONIR_API_TOKEN": TOKEN}
        self.env.setdefault("KAPONIR_OCR_PYTHON", sys.executable)
        subprocess.run([binary, "init"], env=self.env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        cfg = self.home / "config.toml"
        config = cfg.read_text().replace("https://api.openai.com", upstream).replace("https://api.anthropic.com", upstream)
        if not legacy:
            if worker:
                config = config.replace("[serve.ocr]", f'worker_url = "{worker}"\n\n[serve.ocr]')
        else:
            config = config.split("[serve.ocr]", 1)[0]
        cfg.write_text(config)
        self.url = f"http://127.0.0.1:{self.port}"
        self.child = None
        self.log = self.home / "server.log"

    def start(self):
        with self.log.open("ab") as log:
            self.child = subprocess.Popen([self.binary, "serve", "--listen", f"127.0.0.1:{self.port}"],
                                          env=self.env, stdout=log, stderr=log)
        for _ in range(150):
            try:
                if self.call("/healthz").status == 200:
                    return
            except (URLError, OSError):
                if self.child.poll() is not None:
                    break
                time.sleep(0.035)
        raise RuntimeError(self.log.read_text())

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()

    def call(self, path, payload=None, method=None, *, auth=True, headers=None):
        headers = {**({"Authorization": "Bearer " + TOKEN} if auth else {}), **(headers or {})}
        if isinstance(payload, dict):
            headers.setdefault("Content-Type", "application/json")
            payload = json.dumps(payload, ensure_ascii=False).encode()
        req = Request(self.url + path, data=payload, headers=headers, method=method)
        try:
            return Reply(urlopen(req, timeout=35))
        except HTTPError as error:
            return Reply(error)

    def settings(self, **kw):
        status = self.call("/api/v1/ui/ocr/settings").json()
        if kw:
            result = self.call("/api/v1/ui/ocr/settings", {**status["settings"], **kw}, "PUT")
            assert result.status == 200, result.json()
            return result.json()
        return status

    def event(self, id_=None):
        if id_ is None:
            id_ = self.call("/api/v1/ui/events").json()["events"][0]["id"]
        reply = self.call(f"/api/v1/ui/events/{id_}")
        assert reply.status == 200
        return reply.json()

    def connection(self, base, kind="openai", pool=True):
        response = self.call("/api/v1/connections", {
            "kind": kind, "name": "Synthetic " + kind, "base_url": base, "api_key": "fixture-key",
            "enabled": True, "pool": pool, "all_models": True, "auto_models": False})
        assert response.status == 200, response.json()
        return response.json()["id"]


@contextmanager
def proxy(binary, upstream, **kw):
    with tempfile.TemporaryDirectory(prefix="kaponir-ocr-") as home:
        instance = Proxy(binary, home, upstream, **kw)
        try:
            instance.start()
            yield instance
        finally:
            instance.stop()


def user_message(*parts):
    return {"role": "user", "content": [{"type": "text", "text": QUESTION}, *parts]}


def request(proxy_, content, path="/v1/chat/completions", stream=False, session="synthetic"):
    return proxy_.call(path, {"model": "fixture-chat", "messages": content, "stream": stream},
                       headers={"x-kaponir-session": session})


def attachment_url(event, attachment, variant):
    return (f"/api/v1/ui/ocr/images/{event['session_id']}/{event['id']}/"
            f"{attachment['id']}/{variant}")


def accepted(p, chat, messages, samples, *, path="/v1/chat/completions", stream=False,
             expected=None, session="synthetic"):
    before = len(chat.records)
    response = request(p, messages, path, stream, session)
    assert response.status == 200, response.body[:250]
    assert len(chat.records) == before + 1
    assert chat.records[-1][1] in ("/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens")
    parts = list(binary_parts(chat.records[-1][2]))
    event = p.event()
    inspector = event["inspector"]
    audit = inspector["images"]
    assert audit["stage"] == "sent", audit
    assert len(parts) == len(samples) == len(audit["attachments"])
    for raw, sample, attachment in zip(parts, samples, audit["attachments"]):
        if expected is None:
            assert_pixels(raw, sample)
        else:
            expected(raw, sample, attachment)
        reply = p.call(attachment_url(event, attachment, "sent"))
        assert reply.status == 200 and reply.body == raw
        assert reply.headers["Cache-Control"] == "private, no-store"
        assert base64_image(sample.raw)[:500] not in json.dumps(inspector)
        assert base64_image(raw)[:500] not in json.dumps(inspector)
    return event


def blocked(p, chat, messages, codes, status, *, path="/v1/chat/completions"):
    before = len(chat.records)
    response = request(p, messages, path, stream=True)
    assert response.status == status, response.body[:250]
    details = response.json()["error"]["details"]
    assert details["code"] in codes, details
    assert len(chat.records) == before
    event = p.event()
    assert not event["sent"] and event["inspector"]["images"]["stage"] == "blocked"
    return details


def controlled_tsv(sample, replacements=None, joined=False, omit_lines=()):
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    lines = [header]
    width, height = sample.reference.size
    words = sample.words.copy()
    if joined:
        a, b = words[:2]
        words = [{**a, "text": a["text"] + b["text"], "box": a["box"][:2] + b["box"][2:]}] + words[2:]
    for word in words:
        row, order = word["line"], word["order"]
        if row in omit_lines:
            continue
        coords = [round(n * (width if index % 2 == 0 else height)) for index, n in enumerate(word["box"])]
        text = word["text"]
        if replacements and (row, order) in replacements:
            for index, (part, start, finish) in enumerate(replacements[(row, order)]):
                size = coords[2] - coords[0]
                x = coords[0] + round(start * size)
                w = coords[0] + round(finish * size) - x
                lines.append(f"5\t1\t1\t1\t{row}\t{order+index}\t{x}\t{coords[1]}\t{w}\t{coords[3]-coords[1]}\t1\t{part}")
            continue
        x, y, x1, y1 = coords
        lines.append(f"5\t1\t1\t1\t{row}\t{order}\t{x}\t{y}\t{x1-x}\t{y1-y}\t95\t{text}")
    return "\n".join(lines) + "\n"


def run_controlled(binary, chat, sample, wrap, yaml, repeated):
    with tempfile.TemporaryDirectory(prefix="kaponir-tsv-") as location:
        bin_path, tsv_path = Path(location, "ocr-fixture"), Path(location, "selected.tsv")
        delay = Path(location, "delay")
        running = Path(location, "pid")
        bin_path.write_text(f"#!{sys.executable}\n" + """\
import os
from pathlib import Path
import struct
import sys
import time

source = Path(os.environ["KAPONIR_TEST_TSV"])
delay = source.with_name("delay")
while header := sys.stdin.buffer.read(4):
    size, = struct.unpack(">I", header)
    data = b""
    if size:
        sys.stdin.buffer.read(size)
        if delay.exists():
            delay.with_name("pid").write_text(str(os.getpid()))
            time.sleep(float(delay.read_text()))
        data = source.read_bytes()
    sys.stdout.buffer.write(struct.pack(">BI", 0, len(data)) + data)
    sys.stdout.buffer.flush()
""")
        bin_path.chmod(0o700)
        env = {"KAPONIR_OCR_PYTHON": str(bin_path), "KAPONIR_TEST_TSV": str(tsv_path)}
        with proxy(binary, chat.url, env=env) as p:
            call = [user_message(openai_image(sample))]
            tsv_path.write_text(controlled_tsv(sample, {
                (1, 2): [('AKE_001', 0.12, 0.48), ('11-Аа9_8x0', 0.52, 0.89)],
                (2, 2): [('DEMO_7RqQ-I110_9U', 0.06, 0.93)],
            }))
            accepted(p, chat, call, [sample])
            tsv_path.write_text(controlled_tsv(sample, joined=True))
            def row_coverage(raw, original, audit):
                assert audit["areas"][0]["reason"] == "field_row"
                assert audit["areas"][0]["rectangles"][0]["x0"] == 0
                copy = Fixture(original.raw, original.reference, original.secrets + [original.public[0]],
                               original.public[1:], original.words)
                assert_pixels(raw, copy)
            accepted(p, chat, call, [sample], expected=row_coverage)
            for sample_ in (wrap, yaml):
                tsv_path.write_text(controlled_tsv(sample_))
                accepted(p, chat, [user_message(openai_image(sample_))], [sample_])
            # The second value is legible, but the OCR missed its field separator.
            tsv_path.write_text(controlled_tsv(repeated, {(2, 1): [("accessKey", 0.0, 1.0)]}))
            event = accepted(p, chat, [user_message(openai_image(repeated))], [repeated])
            attachment = event["inspector"]["images"]["attachments"][0]
            assert len(attachment["areas"]) == 2
            preview = p.call("/api/v1/ui/ocr/preview", repeated.raw,
                             headers={"content-type": repeated.mime})
            assert preview.status == 200
            result = preview.json()
            assert result["image"]["areas"] == attachment["areas"]
            assert base64.b64decode(result["data"].split(",", 1)[1]) == next(binary_parts(chat.records[-1][2]))
            tsv_path.write_text(controlled_tsv(yaml, omit_lines=(2,)))
            details = blocked(p, chat, [user_message(openai_image(yaml), openai_image(sample))], {"boundaries"}, 422)
            assert details["field"] == "secretKey"
            if sys.platform == "linux":
                tsv_path.write_text(controlled_tsv(sample))
                delay.write_text("8")
                p.settings(parallelism=1, queue_limit=0)
                before = len(chat.records)
                payload = json.dumps({"model":"fixture-chat","messages":call}).encode()
                with socket.create_connection(("127.0.0.1", p.port)) as client:
                    header = ("POST /v1/chat/completions HTTP/1.1\r\n"
                              f"Host: 127.0.0.1:{p.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
                              f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n")
                    client.sendall(header.encode() + payload)
                    for _ in range(100):
                        if running.exists() and running.stat().st_size:
                            break
                        time.sleep(0.03)
                    child = Path("/proc") / running.read_text()
                    try:
                        blocked(p, chat, call, {"queue_full"}, 429)
                        assert request(p, [{"role": "user", "content": QUESTION}]).status == 200
                    finally:
                        delay.unlink()
                    assert child.exists()
                for _ in range(100):
                    if not child.exists():
                        break
                    time.sleep(0.02)
                assert not child.exists(), "disconnect did not stop the OCR child"
                assert len(chat.records) == before + 1  # The independent text request.
                accepted(p, chat, call, [sample])
    return (6, 2) if sys.platform == "linux" else (5, 1)


def exercise_worker(binary, chat, sample):
    port = free_port()
    worker_url = f"http://127.0.0.1:{port}"
    key = "synthetic-worker-key"
    with tempfile.TemporaryDirectory(prefix="kaponir-worker-ocr-") as home:
        env = {**os.environ, "KAPONIR_HOME": home, "KAPONIR_WORKER_PYTHON": sys.executable,
               "KAPONIR_WORKER_TOKEN": key}
        with open(Path(home, "worker.log"), "wb") as log:
            worker = subprocess.Popen([binary, "worker", "--listen", f"127.0.0.1:{port}", "serve", "--idle"],
                                      env=env, stdout=log, stderr=log)
        try:
            for _ in range(120):
                try:
                    req = Request(worker_url + "/ocr/status", headers={"Authorization": "Bearer " + key})
                    with urlopen(req, timeout=2) as response:
                        assert {"rus", "eng"}.issubset(json.load(response))
                    break
                except (URLError, OSError):
                    if worker.poll() is not None:
                        raise RuntimeError(Path(home, "worker.log").read_text())
                    time.sleep(0.05)
            else:
                raise RuntimeError("standalone OCR worker did not start")
            with proxy(binary, chat.url, worker=f"http://127.0.0.1:{free_port()}",
                       env={"KAPONIR_WORKER_TOKEN": key, "KAPONIR_WORKER_URL": worker_url}) as p:
                assert p.settings()["worker"] == worker_url + "/ocr/tsv"
                settings = p.settings(node="worker", approved_recipient=worker_url + "/ocr/tsv")
                assert settings["engine"] == "rapidocr" and settings["engine_state"] == "ready"
                accepted(p, chat, [user_message(openai_image(sample))], [sample])
                worker.terminate()
                worker.wait(timeout=10)
                blocked(p, chat, [user_message(openai_image(sample))], {"unavailable"}, 503)
                assert request(p, [{"role": "user", "content": QUESTION}]).status == 200
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)


def exercise_managed_worker(binary, chat, sample):
    address = f"127.0.0.1:{free_port()}"
    recipient = f"http://{address}/ocr/tsv"
    with proxy(binary, chat.url, env={"KAPONIR_WORKER_LISTEN": address,
                                      "KAPONIR_WORKER_PYTHON": sys.executable}) as p:
        current = p.settings()
        assert current["worker_managed"] and current["worker"] == recipient

        def worker_status():
            return p.call("/api/v1/ui/worker").json()

        assert worker_status()["state"] == "disabled"
        declined = p.call("/api/v1/ui/ocr/settings", {**current["settings"], "node": "worker"}, "PUT")
        assert declined.status == 503
        assert worker_status()["state"] == "disabled"

        settings = p.settings(node="worker", approved_recipient=recipient)
        assert settings["engine"] == "rapidocr" and settings["engine_state"] == "ready", settings
        state = worker_status()
        assert state["online"] and not state["enabled"]
        attachment = [user_message(openai_image(sample))]
        accepted(p, chat, attachment, [sample])

        p.stop()
        p.start()
        assert worker_status()["state"] == "disabled"
        accepted(p, chat, attachment, [sample])
        state = worker_status()
        assert state["online"] and not state["enabled"]


def exercise_review_and_history(binary, chat):
    value = "10.23.45.67"
    source = fixture([(64, 32, [("Address: ", False), (value, True)]),
                      (64, 100, [("replicas: 3", False)])])
    # Generic detector areas cover the word's ink. Trim the font's blank side
    # bearing from the independent ground truth for this generic IP fixture.
    box = source.secrets[0]
    crop = source.reference.crop(box)
    ink = ImageChops.difference(crop, Image.new("RGB", crop.size, (249, 250, 252))).getbbox()
    source.secrets[0] = (box[0] + ink[0], box[1] + ink[1], box[0] + ink[2], box[1] + ink[3])
    messages = [user_message(openai_image(source))]
    with proxy(binary, chat.url) as p:
        assert request(p, [{"role": "user", "content": "Host address " + value}]).status == 200
        text = p.event()
        index = next(i for i, item in enumerate(text["usage"]["masked_items"]) if item["kind"] == "IP")
        decision = p.call("/api/v1/ui/review/from-event", {"event_id": text["id"], "indexes": [index]})
        assert decision.status == 200, decision.body
        journal_id = decision.json()["keys"][0].split(":")[1]
        image = accepted(p, chat, messages, [source])
        attachment = image["inspector"]["images"]["attachments"][0]
        old_preview = attachment_url(image, attachment, "sent")
        first = p.call(old_preview).body
        assert p.call("/api/v1/ui/ocr/review-links/" + journal_id).json() == [{
            "event_id": image["id"], "session_id": image["session_id"], "turn_id": image["turn"]["id"],
            "message_id": attachment["message_id"], "attachment_id": attachment["id"], "area_id": 1,
        }]
        result = p.call("/api/v1/rules", {"scope": "team", "rule": {
            "id": "synthetic-image-policy", "kind": "CUSTOM", "action": "mask", "value": "replicas"}})
        assert result.status == 200, result.body
        def verify_new_rule(raw, sample, audit):
            assert raw != first
            assert any(area["scope"] == "team" and area["kind"] == "CUSTOM" for area in audit["areas"])
            public_address = Fixture(sample.raw, sample.reference, sample.secrets, sample.public[:1], sample.words)
            assert_pixels(raw, public_address)
        new = accepted(p, chat, messages, [source], expected=verify_new_rule)
        assert new["turn"]["id"] == image["turn"]["id"]
        assert p.call(old_preview).body == first

        masked_preview = attachment_url(new, new["inspector"]["images"]["attachments"][0], "sent")
        sent_with_rule, = binary_parts(chat.records[-1][2])
        team = next(s for s in p.call("/api/v1/rules").json() if s["scope"] == "team")
        index = next(i for i, rule in enumerate(team["file"]["rule"])
                     if rule.get("id") == "synthetic-image-policy")
        removed = p.call(f"/api/v1/rules/team/{index}?revision={team['revision']}", method="DELETE")
        assert removed.status == 200 and removed.json()["removed"]["id"] == "synthetic-image-policy"

        # Replaying the same turn uses current rules. Its earlier preview keeps the pixels sent.
        replay = accepted(p, chat, messages, [source])
        assert replay["turn"]["id"] == new["turn"]["id"]
        assert p.call(masked_preview).body == sent_with_rule


def run_tests(binary):
    chat, vision = Recorder(), Recorder(vision=True)
    sample = helm()
    public = fixture([(64, 32, [("Проверка: выполнено", False)]),
                      (64, 100, [("replicas: 3", False)])])
    resized = helm(size=(1920, 1080))
    rotated = helm(jpeg=True)
    webp = helm(webp=True)
    columns = fixture([
        (64, 32, [(FIELDS[0][0], False), (FIELDS[0][1], True)]),
        (64, 100, [(FIELDS[1][0], False), (FIELDS[1][1], True)]),
        (64, 168, [("replicas: 3", False)]),
        (1270, 32, [("status: ready", False)]),
        (1270, 100, [("public: available", False)]),
        (1270, 168, [("version: 3", False)]),
    ], size=(1920, 320))
    repeated = fixture([
        (64, 32, [(FIELDS[0][0], False), (FIELDS[0][1], True)]),
        (64, 100, [(FIELDS[1][0], False), (FIELDS[0][1], True)]),
        (64, 168, [("replicas: 3", False)]),
    ])
    wrap = fixture([(64, 32, [("secretKey: ", False), ('"FAKE_O0Il1-', True)]),
                    (160, 100, [('Aa9_8xQ2"', True)]), (64, 168, [("replicas: 3", False)])])
    yaml = fixture([(64, 32, [("secretKey:", False), (" |", False)]),
                    (160, 100, [("FAKE_O0Il1-Aa9", True)]), (160, 168, [("DEMO_7Rq0-I1lO", True)]),
                    (64, 236, [("replicas: 3", False)])], size=(1000, 330))
    mentions = fixture([(64, 32, [("Check the secretKey field", False)]),
                        (64, 100, [("secretKeyRef: storage", False)]),
                        (64, 168, [("replicas: 3", False)])])
    tiny = fixture([(2, 3, [("secretKey: word", True)])], size=(110, 17), font_size=5)
    blank = fixture([])
    bad = Fixture(b"\x89PNG\r\n\x1a\ninvalid", sample.reference, [], [], [])
    animated_bytes = BytesIO()
    sample.reference.save(animated_bytes, "PNG", save_all=True, append_images=[public.reference], duration=50)
    animated = Fixture(animated_bytes.getvalue(), sample.reference, [], [], [])
    out = {"accepted_images": 0, "fully_covered": 0, "public_preserved": 0, "blocked": 0}
    def report(*values):
        for value in values:
            out["accepted_images"] += 1
            out["fully_covered"] += len(value.secrets)
            out["public_preserved"] += len(value.public)
    def refused(p, messages, codes, status, **kwargs):
        blocked(p, chat, messages, codes, status, **kwargs)
        out["blocked"] += 1
    try:
        with proxy(binary, chat.url, env={"KAPONIR_OCR_PYTHON": str(ROOT / "missing-ocr-python")}) as p:
            status = p.settings()
            assert status["engine"] == "rapidocr" and status["engine_state"] == "unavailable"
            refused(p, [user_message(openai_image(sample))], {"unavailable"}, 503)
            assert request(p, [{"role": "user", "content": QUESTION}]).status == 200
        with proxy(binary, chat.url) as p:
            assert p.settings()["settings"]["enabled"]
            assert p.settings()["engine"] == "rapidocr"
            assert {"rus", "eng"}.issubset(p.settings()["installed_languages"])
            first = accepted(p, chat, [user_message(openai_image(sample))], [sample])
            assert len(first["inspector"]["images"]["attachments"][0]["areas"]) == 2
            report(sample)
            assert chat.records[-1][2]["messages"][0]["content"][1]["image_url"]["detail"] == "high"
            managed = p.connection(chat.url)
            managed_anthropic = p.connection(chat.url, "anthropic")
            accepted(p, chat, [user_message(openai_image(resized), openai_image(public))],
                     [resized, public], path=f"/p/{managed}/v1/chat/completions", stream=True)
            report(resized, public)
            accepted(p, chat, [user_message(anthropic_image(rotated))], [rotated],
                     path=f"/p/{managed_anthropic}/v1/messages")
            accepted(p, chat, [user_message(anthropic_image(webp))], [webp], path="/v1/messages/count_tokens")
            report(rotated, webp)
            wrapped = {"type": "file", "file": {"filename": "values.png",
                        "file_data": "data:image/png;base64," + base64_image(sample.raw)}}
            accepted(p, chat, [user_message(wrapped)], [sample])
            document = {"type": "document", "source": {"type": "base64",
                        "media_type": "image/png", "data": base64_image(sample.raw)}}
            accepted(p, chat, [user_message(document)], [sample], path="/v1/messages")
            report(sample, sample)
            for source in (columns, repeated, wrap, yaml, mentions, public):
                ev = accepted(p, chat, [user_message(openai_image(source))], [source])
                if not source.secrets:
                    assert not ev["inspector"]["images"]["attachments"][0]["areas"]
                report(source)
            turns = [user_message(openai_image(sample)),
                     {"role": "assistant", "content": "", "tool_calls": [
                         {"type": "function", "id": "file-1", "function": {"name": "read", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "file-1", "content": [openai_image(public)]}]
            replay = accepted(p, chat, turns, [sample, public])
            assert first["turn"]["id"] == replay["turn"]["id"]
            origin, tool = replay["inspector"]["images"]["attachments"]
            assert origin["id"] == first["inspector"]["images"]["attachments"][0]["id"]
            assert tool["tool_id"] == "file-1"
            report(sample, public)
            turns = [user_message(anthropic_image(sample)),
                     {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "read", "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1",
                                                   "content": [anthropic_image(public)]}]}]
            ev = accepted(p, chat, turns, [sample, public], path="/v1/messages")
            assert ev["inspector"]["images"]["attachments"][1]["tool_id"] == "tool-1"
            report(sample, public)
            for source, codes in [(bad, {"invalid_image"}), (tiny, {"empty", "unreadable"}),
                                  (blank, {"empty", "unreadable"}), (animated, {"animated"})]:
                refused(p, [user_message(openai_image(sample), openai_image(source))], codes, 422)
            refused(p, [user_message({"type": "image_url", "image_url": {"url": "https://example.invalid/pic.png"}})],
                    {"inline_required"}, 422)
            refused(p, [user_message({"type": "file", "file": {"file_id": "fixture-only"}})], {"inline_required"}, 422)
            assert p.settings(languages="rus+eng+unknown")["engine_state"] == "ready"
            p.settings(languages="rus+eng", save_originals=True)
            saved = accepted(p, chat, [user_message(openai_image(sample))], [sample], session="retention")
            report(sample)
            attachment = saved["inspector"]["images"]["attachments"][0]
            sent_url = attachment_url(saved, attachment, "sent")
            url = attachment_url(saved, attachment, "original")
            assert p.call(url, auth=False).status == 401
            assert p.call(url.replace(saved["session_id"], "other-session")).status == 404
            assert p.call(url).body == sample.raw
            # Expire only the encrypted original. Its subsequent read cannot return data.
            with sqlite3.connect(p.home / "vault.db") as db:
                db.execute("UPDATE traffic_images SET expires=1 WHERE event=? AND original=1", (saved["id"],))
            assert p.call(url).status == 410
            p.stop()
            p.start()
            assert p.settings()["settings"]["save_originals"]
            assert p.call(sent_url).status == 200
            with sqlite3.connect(p.home / "vault.db") as db:
                assert db.execute("SELECT count(*) FROM traffic_images WHERE event=? AND original=1", (saved["id"],)).fetchone()[0] == 0
            p.settings(enabled=False, save_originals=False)
            before = len(chat.records)
            assert request(p, [user_message(openai_image(sample))]).status == 200
            assert len(chat.records) == before + 1
            sent = chat.records[-1][2]["messages"][0]["content"][1]["image_url"]["url"]
            assert sent.endswith(base64_image(sample.raw))
            assert p.event()["inspector"]["images"]["stage"] == "disabled"
            before = len(chat.records)
            preview = p.call("/api/v1/ui/ocr/preview", sample.raw, headers={"content-type": "image/png"})
            assert preview.status == 200 and len(chat.records) == before
            assert_pixels(base64.b64decode(preview.json()["data"].split(",", 1)[1]), sample)
            no_ocr_history = p.event()
            assert no_ocr_history["inspector"]["images"]["stage"] == "disabled"
            before = len(chat.records)
            p.settings(enabled=True)
            chat.mode = "error"
            assert request(p, [user_message(openai_image(sample))]).status == 502
            assert p.event()["inspector"]["images"]["stage"] == "provider_error"
            assert len(chat.records) == before + 1
            assert_pixels(list(binary_parts(chat.records[-1][2]))[0], sample)
            report(sample)
            chat.mode = "good"

            vision_id = p.connection(vision.url, pool=False)
            selection = {"connection_id": vision_id, "model": "fixture-vision"}
            probe = p.call("/api/v1/ui/ocr/check", selection).json()
            assert probe["reachable"] and probe["images"] and probe["coordinates"], probe
            assert len(vision.records) == 1
            p.settings(enabled=True, mode="vision", **selection, approved_recipient=probe["recipient"])
            accepted(p, chat, [user_message(openai_image(sample))], [sample], path=f"/p/{managed}/v1/chat/completions")
            report(sample)
            assert len(vision.records) == 2
            vision.mode = "invalid"
            refused(p, [user_message(openai_image(sample))], {"vision_coordinates"}, 503,
                    path=f"/p/{managed}/v1/chat/completions")
            vision.mode = "no_images"
            check = p.call("/api/v1/ui/ocr/check", selection).json()
            assert check["reachable"] and check["images"] is False and check["coordinates"] is None
            p.settings(timeout_ms=500)
            vision.mode = "timeout"
            refused(p, [user_message(openai_image(sample))], {"timeout"}, 504,
                    path=f"/p/{managed}/v1/chat/completions")
            p.settings(timeout_ms=20000)
            new_address = f"http://127.0.0.1:{free_port()}"
            changed = p.call(f"/api/v1/connections/{vision_id}", {"kind": "openai", "base_url": new_address,
                             "api_key": "synthetic-key", "pool": False, "auto_models": False}, "PUT")
            assert changed.status == 200
            old_count = len(vision.records)
            refused(p, [user_message(openai_image(sample))], {"consent"}, 503)
            assert len(vision.records) == old_count
            check = p.call("/api/v1/ui/ocr/check", selection).json()
            assert check["reachable"] is False and check["images"] is None
        with proxy(binary, chat.url, legacy=True) as old:
            assert not old.settings()["settings"]["enabled"]
            assert request(old, [user_message(openai_image(sample))]).status == 200
        exercise_worker(binary, chat, sample)
        exercise_managed_worker(binary, chat, sample)
        report(sample)
        out["blocked"] += 1
        controlled, rejected = run_controlled(binary, chat, sample, wrap, yaml, repeated)
        exercise_review_and_history(binary, chat)
        out["review_and_history"] = "passed"
        out["controlled_tsv_accepted"] = controlled
        out["controlled_tsv_blocked"] = rejected
        print(json.dumps(out, ensure_ascii=False))
    finally:
        chat.close()
        vision.close()


def percentiles(samples):
    values = sorted(samples)
    return {key: round(values[max(0, math.ceil(fraction * len(values)) - 1)], 3)
            for key, fraction in (("p50", 0.5), ("p95", 0.95))}


@contextmanager
def resource_samples(pid):
    """Sample the proxy and its children. The recorder and driver are excluded."""
    import psutil

    stop = threading.Event()
    measurements = {"peak_rss_mib": 0.0, "peak_cpu_percent": 0.0}
    cpu = {}

    def sample():
        parent = psutil.Process(pid)
        while not stop.is_set():
            try:
                processes = [parent, *parent.children(recursive=True)]
            except psutil.Error:
                break
            rss, usage = 0, 0
            for process in processes:
                try:
                    rss += process.memory_info().rss
                    handle = cpu.setdefault(process.pid, process)
                    usage += handle.cpu_percent()
                except psutil.Error:
                    pass
            live = {process.pid for process in processes}
            for identity in list(cpu):
                if identity not in live:
                    del cpu[identity]
            measurements["peak_rss_mib"] = max(measurements["peak_rss_mib"], round(rss / (1024 ** 2), 2))
            measurements["peak_cpu_percent"] = max(measurements["peak_cpu_percent"], round(usage, 1))
            stop.wait(0.04)

    thread = threading.Thread(target=sample)
    thread.start()
    try:
        yield measurements
    finally:
        stop.set()
        thread.join()


def run_benchmark(binary, runs):
    chat = Recorder()
    images = {
        "screenshot_1920x1080": helm(size=(1920, 1080)),
        "simulated_phone_jpeg_1440x1080": helm(size=(1440, 1080), photo=True),
    }
    results = {"runs": runs, "concurrent_image_requests": 2,
               "languages": "multilingual", "gpu": "not used", "live_vision": "not measured",
               "rapidocr": version("rapidocr"), "onnxruntime": version("onnxruntime")}
    try:
        with proxy(binary, chat.url) as p:
            route = "/v1/chat/completions"

            base = {key: [user_message(openai_image(sample))] for key, sample in images.items()}
            base["text"] = [{"role": "user", "content": QUESTION}]

            def payload(key, index):
                return json.dumps({"model": "fixture-chat", "messages": base[key],
                                   "metadata": {"ocr_benchmark": index}}, ensure_ascii=False).encode()

            headers = {"Content-Type": "application/json", "x-kaponir-session": "benchmark"}
            def timed(key, index):
                message = payload(key, index)
                start = time.perf_counter_ns()
                response = p.call(route, message, "POST", headers=headers)
                finish = time.perf_counter_ns()
                assert response.status == 200, response.body[:250]
                recorded = next(rec for rec in reversed(chat.records) if rec[2]["metadata"]["ocr_benchmark"] == index)
                return (recorded[0] - start) / 1e6, (finish - start) / 1e6

            p.stop()
            p.start()
            cold_boundary, cold_total = timed("screenshot_1920x1080", "cold")
            results["cold_proxy_first_image"] = {"before_upstream_ms": cold_boundary, "total_ms": cold_total}
            for enabled in (False, True):
                p.settings(enabled=enabled)
                for key in images:
                    mode = "local" if enabled else "disabled"
                    boundary, total, stage = [], [], []
                    timed(key, key + "-" + mode + "-warm")
                    with resource_samples(p.child.pid) as resources:
                        for i in range(runs):
                            before, after = timed(key, f"{key}-{mode}-{i}")
                            boundary.append(before)
                            total.append(after)
                            if enabled:
                                stage.append(p.event()["inspector"]["images"]["timings"])
                    # Throughput from client round trips without the inspector fetches.
                    results.setdefault(key, {})[mode] = {
                        "before_upstream_ms": percentiles(boundary), "total_ms": percentiles(total),
                        "images_per_second_serial": round(1000 * runs / sum(total), 2),
                        "pipeline_stage_ms": {name: percentiles([row[name] for row in stage])
                                              for name in stage[0]} if stage else {},
                        **resources,
                    }
            text_times = [timed("text", f"text-isolated-{i}") for i in range(runs)]
            overlapped = []
            started = time.perf_counter()
            with resource_samples(p.child.pid) as resources, ThreadPoolExecutor(max_workers=2) as pool:
                for i in range(runs // 2):
                    tasks = [pool.submit(timed, "screenshot_1920x1080", f"parallel-{i}-{j}") for j in range(2)]
                    time.sleep(0.015)
                    overlapped.append(timed("text", f"text-overlap-{i}"))
                    for task in tasks:
                        task.result()
            results["parallel"] = {
                "throughput_images_per_second": round((runs - runs % 2) / (time.perf_counter() - started), 2),
                "text_baseline_before_upstream_ms": percentiles([record[0] for record in text_times]),
                "text_with_images_before_upstream_ms": percentiles([record[0] for record in overlapped]),
                **resources,
            }
    finally:
        chat.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/debug/kaponir")
    parser.add_argument("--browser-fixture", action="store_true", help="start the proxy and a recording upstream for Playwright")
    parser.add_argument("--benchmark", type=int, metavar="RUNS")
    args = parser.parse_args()
    if Image is None:
        parser.error("install Pillow to generate test fixtures")
    binary = str(args.binary.resolve())
    if args.browser_fixture:
        chat = Recorder()
        running = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: running.set())
        restart = threading.Event()
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, lambda *_: restart.set())
        try:
            with proxy(binary, chat.url, env={
                "KAPONIR_WORKER_LISTEN": f"127.0.0.1:{free_port()}",
                "KAPONIR_WORKER_PYTHON": sys.executable,
            }) as p:
                # Simulate an existing installation so the walkthrough explicitly enables OCR.
                p.settings(enabled=False)
                print(json.dumps({"url": p.url, "token": TOKEN, "upstream": chat.url,
                                  "fixture_pid": os.getpid() if hasattr(signal, "SIGHUP") else None}), flush=True)
                while not running.wait(0.1):
                    if restart.is_set():
                        restart.clear()
                        p.stop()
                        p.start()
                        chat.restarts += 1
        finally:
            chat.close()
    else:
        if args.benchmark:
            if args.benchmark < 4:
                parser.error("use at least 4 benchmark runs")
            run_benchmark(binary, args.benchmark)
        else:
            run_tests(binary)


if __name__ == "__main__":
    main()
