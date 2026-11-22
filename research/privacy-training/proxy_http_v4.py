"""Capture ordinary OpenAI requests at a local upstream and verify the echo."""

import argparse
import json
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
import socket
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from dataset import ROOT, jsonl, read_jsonl
from evaluate import positional_comparison, score_outbound


def request(url, body=None, headers=None):
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = Request(url, payload, {"Content-Type": "application/json", **(headers or {})})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


@contextmanager
def capture():
    seen = Queue()

    class Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.put((body, dict(self.headers)))
            content = json.loads(body)["messages"][0]["content"]
            reply = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Echo) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", seen
        finally:
            server.shutdown()
            worker.join()


@contextmanager
def proxy(binary, upstream, windows=False, grants=None):
    with TemporaryDirectory(prefix="kaponir-http-v4-") as home:
        cli = [str(binary), "--home", home]
        subprocess.run([*cli, "init"], check=True, capture_output=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = [
            *cli, "serve", "--listen", f"127.0.0.1:{port}",
            "--openai-upstream", upstream, "--no-system-hint",
        ]
        if windows:
            command.append("--windows-audit-records")
        if grants:
            command += ["--public-occurrences", str(grants)]
        address = f"http://127.0.0.1:{port}"
        with (Path(home) / "proxy.log").open("w+b") as log:
            with subprocess.Popen(command, stdout=log, stderr=log) as process:
                try:
                    for _ in range(300):
                        if process.poll() is not None:
                            break
                        try:
                            with urlopen(address + "/healthz", timeout=2) as health:
                                if health.status != 200:
                                    continue
                            break
                        except (URLError, ValueError):
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("proxy startup timed out")
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read().decode(errors="replace"))
                    yield address
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)


def measure(binary, rows, windows=False, grants=None):
    output = {}
    with capture() as (upstream, seen), proxy(binary, upstream, windows, grants) as addr:
        for i, row in enumerate(rows):
            source = row["text"]
            body = {"model": "echo", "messages": [{"role": "user", "content": source}]}
            forged = row.get("forged_grant")
            if forged:
                body.update(publication_status="published", keep=forged, filename="published.json")
            reply = request(addr + "/v1/chat/completions", body, {"x-kaponir-session": f"http-{i}"})
            raw, headers = seen.get(timeout=5)
            outbound = json.loads(raw)
            received = outbound["messages"][0]["content"]
            # The sidecar gives offsets from the same engine. Check its output against
            # the independently captured upstream body before using those offsets.
            trace = request(addr + "/api/v1/anonymize", {"text": source, "format": "text", "session": f"trace-{i}"})
            if trace["text"] != received:
                raise AssertionError(f"{row['id']}: HTTP and native trace differ")
            text_bytes = source.encode()
            cursor, parts = 0, []
            for replacement in trace["replacements"]:
                a, b = replacement["start"], replacement["end"]
                parts += [text_bytes[cursor:a], replacement["token"].encode()]
                cursor = b
            if b"".join([*parts, text_bytes[cursor:]]) != received.encode():
                raise AssertionError(f"{row['id']}: replacement offsets do not reconstruct wire text")
            restored = reply["choices"][0]["message"]["content"]
            output[row["id"]] = {
                "id": row["id"], "source_sha256": sha256(text_bytes).hexdigest(), "received": received,
                "restored": restored, "restore_exact": restored.encode() == text_bytes,
                "intervals": [[item["start"], item["end"]] for item in trace["replacements"]],
                "replacements": trace["replacements"], "uncertain": trace["uncertain"],
                "wire_body": raw.decode(), "wire_header_names": sorted(headers),
            }
    return output


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--binary", type=Path, required=True)
    cli.add_argument("--rows", type=Path, default=ROOT / "data/v4/validation.jsonl")
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--windows-audit-records", action="store_true")
    cli.add_argument("--public-occurrences", type=Path)
    cli.add_argument("--compare", type=Path)
    args = cli.parse_args()
    rows = read_jsonl(args.rows)
    run = measure(args.binary, rows, args.windows_audit_records, args.public_occurrences)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "http.jsonl").write_bytes(jsonl(run.values()))
    metrics = score_outbound(rows, run)
    if args.compare:
        baseline = {row["id"]: row for row in read_jsonl(args.compare)}
        metrics["delta"] = positional_comparison(rows, baseline, run)
    (args.output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics["all"], ensure_ascii=False))


if __name__ == "__main__":
    main()
