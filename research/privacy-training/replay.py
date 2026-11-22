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

from context_policy import RULES, context_edits
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


def subtract(start, end, intervals):
    """Yield the pieces of a byte interval outside ordered masks."""
    for left, right in intervals:
        if right <= start:
            continue
        if left >= end:
            break
        if start < left:
            yield start, left
        start = max(start, right)
        if start >= end:
            return
    if start < end:
        yield start, end


def apply_positions(text, baseline, decisions, edits=()):
    raw = text.encode()
    hard = validate_trace(text, baseline["replacements"], baseline["text"])
    tag = "KPV3_" + sha256(raw).hexdigest()[:12] + "_"
    while tag in text or tag in baseline["text"]:
        tag += "x"
    tokens = {}
    fallback_markers = None

    def replacement(start, end):
        nonlocal fallback_markers
        surface = raw[start:end].decode()
        if surface not in tokens:
            token = f"{tag}{len(tokens)}X"
            if surface in token:
                if fallback_markers is None:
                    available = (chr(i) for i in range(0xE000, 0xF900) if chr(i) not in text and chr(i) not in baseline["text"])
                    fallback_markers = next(available), next(available)
                opening, closing = fallback_markers
                token = opening * (len(tokens) + 1) + closing
            tokens[surface] = token
        return start, end, tokens[surface]

    releases = sorted((edit.start, edit.end) for edit in edits if edit.action == "keep")
    intervals = []
    for a, b, token in hard:
        remaining = list(subtract(a, b, releases))
        if remaining == [(a, b)]:
            intervals.append((a, b, token))
        else:
            intervals.extend(replacement(start, end) for start, end in remaining)

    additional = [(a, b) for a, b, _, action in decisions if action != "keep"]
    additional.extend((edit.start, edit.end) for edit in edits if edit.action == "mask")
    for a, b in additional:
        covered = sorted((start, end) for start, end, _ in intervals)
        intervals.extend(replacement(start, end) for start, end in subtract(a, b, covered))
    pieces, replacements, position = [], [], 0
    for a, b, token in sorted(intervals):
        pieces.extend((raw[position:a], token.encode()))
        replacements.append({"start": a, "end": b, "token": token})
        position = b
    masked = b"".join(pieces + [raw[position:]]).decode()
    return masked, replacements, {token: value for value, token in tokens.items()}


def run(rows, predictions, binary, oracle=False, context_rules=()):
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
                edits = context_edits(
                    {"text": text, "format": row["format"]}, baseline["replacements"], decisions, context_rules,
                    valid_answer=status in {"valid", "oracle"},
                ) if context_rules else ()
                masked, trace, reverse = apply_positions(text, baseline, decisions, edits)
                reply = send(upstream, masked)
                if reverse:
                    reply = re.sub("|".join(map(re.escape, reverse)), lambda m: reverse[m[0]], reply)
                restored = kaponir(home, ["restore"], reply)
                observation = {
                    "id": row["id"],
                    "source_sha256": sha256(text.encode()).hexdigest(),
                    "model_status": status,
                    "received": upstream.received,
                    "replacements": trace,
                    "restored": restored,
                }
                if context_rules:
                    observation["context_edits"] = [edit._asdict() for edit in edits]
                    observation["model_positions"] = [
                        {"start": a, "end": b, "kind": kind, "action": action} for a, b, kind, action in decisions
                    ]
                observations.append(observation)
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
    parser.add_argument("--context-rule", action="append", choices=RULES, default=[])
    args = parser.parse_args()
    check_snapshot(args.data)
    if args.output.exists():
        parser.error("output already exists")
    rows = read_jsonl(args.data / f"{args.split}.jsonl")
    predictions = index_records(args.predictions, rows) if args.predictions else None
    if args.oracle:
        predictions = {r["id"]: {"spans": target_spans(r)} for r in rows}
    observations = run(rows, predictions, args.binary.resolve(), args.oracle, args.context_rule)
    with args.output.open("xb") as f:
        f.write(jsonl(observations))


if __name__ == "__main__":
    main()
