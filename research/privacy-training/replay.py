"""Measure the research adapter using real Kaponir detectors, vault and a loopback HTTP recipient."""

import argparse
from contextlib import contextmanager
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Thread

from dataset import ROOT, SPLITS, check_snapshot, jsonl, read_jsonl, target_spans
from evaluate import AnswerError, ground, index_records, validate_trace


class Echo(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received = body["messages"][0]["content"]
        reply = json.dumps({"choices": [{"message": {"content": self.server.received}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@contextmanager
def receiver():
    with HTTPServer(("127.0.0.1", 0), Echo) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def send(server, text):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=15)
    try:
        body = json.dumps({"model": "local-echo", "messages": [{"role": "user", "content": text}]})
        connection.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"Loopback receiver returned {response.status}")
        return json.load(response)["choices"][0]["message"]["content"]
    finally:
        connection.close()


def apply_positions(text, baseline, decisions):
    raw = text.encode()
    hard = validate_trace(text, baseline["replacements"], baseline["text"])
    intervals = list(hard)
    tag = "KPV3_" + sha256(raw).hexdigest()[:12] + "_"
    while tag in text or tag in baseline["text"]:
        tag += "x"
    tokens = {}
    for a, b, _, action in decisions:
        if action == "keep":
            continue
        # Preserve each builtin replacement. Cover only the still-visible pieces.
        left = a
        for start, end, _ in hard:
            if end <= left:
                continue
            if start >= b:
                break
            if left < start:
                surface = raw[left:start].decode()
                intervals.append((left, start, tokens.setdefault(surface, f"{tag}{len(tokens)}X")))
            left = max(left, end)
        if left < b:
            surface = raw[left:b].decode()
            intervals.append((left, b, tokens.setdefault(surface, f"{tag}{len(tokens)}X")))
    pieces, replacements, position = [], [], 0
    for a, b, token in sorted(intervals):
        pieces.extend((raw[position:a], token.encode()))
        replacements.append({"start": a, "end": b, "token": token})
        position = b
    masked = b"".join(pieces + [raw[position:]]).decode()
    return masked, replacements, {token: value for value, token in tokens.items()}


def run(rows, predictions, binary, oracle=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAPONIR_")}

    def kaponir(home, args, content=""):
        p = subprocess.run(
            [str(binary), "--home", str(home), *args],
            input=content.encode(),
            capture_output=True,
            check=True,
            timeout=120,
            env=env,
        )
        return p.stdout.decode()

    observations = []
    with tempfile.TemporaryDirectory(prefix="pilot-v3-") as directory, receiver() as upstream:
        root = Path(directory)
        initialized = root / "initial"
        kaponir(initialized, ["init"])
        for row in rows:
            text = row["text"]
            with tempfile.TemporaryDirectory(dir=root) as tmp:
                home = Path(tmp) / "home"
                shutil.copytree(initialized, home)
                baseline = json.loads(
                    kaponir(home, ["mask", "--format", "text", "--no-journal", "--json"], text)
                )
                decisions, status = [], "detectors"
                if predictions is not None:
                    try:
                        decisions = ground(text, predictions.get(row["id"]))
                        status = "oracle" if oracle else "valid"
                    except AnswerError as exc:
                        status = str(exc)
                masked, trace, reverse = apply_positions(text, baseline, decisions)
                reply = send(upstream, masked)
                if reverse:
                    reply = re.sub("|".join(map(re.escape, reverse)), lambda m: reverse[m[0]], reply)
                restored = kaponir(home, ["restore"], reply)
                observations.append(
                    {
                        "id": row["id"],
                        "source_sha256": sha256(text.encode()).hexdigest(),
                        "model_status": status,
                        "received": upstream.received,
                        "replacements": trace,
                        "restored": restored,
                    }
                )
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "v3")
    parser.add_argument("--split", choices=SPLITS, default="test")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--predictions", type=Path)
    mode.add_argument(
        "--oracle", action="store_true", help="diagnostic replay of gold labels, without a model"
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    check_snapshot(args.data)
    if args.output.exists():
        parser.error("output already exists")
    rows = read_jsonl(args.data / f"{args.split}.jsonl")
    predictions = index_records(args.predictions, rows) if args.predictions else None
    if args.oracle:
        predictions = {r["id"]: {"spans": target_spans(r)} for r in rows}
    observations = run(rows, predictions, args.binary.resolve(), args.oracle)
    with args.output.open("xb") as f:
        f.write(jsonl(observations))


if __name__ == "__main__":
    main()
