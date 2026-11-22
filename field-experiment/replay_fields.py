"""Paired validation replay of the source-field policies using Kaponir and a loopback recipient."""

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from field_policy import project

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research" / "privacy-training"))
from dataset import ROOT, check_snapshot, jsonl, read_jsonl
from evaluate import AnswerError, ground, index_records, outbound_counts, paired_difference, score_model, score_outbound, validate_trace
from replay import apply_positions, receiver, send


def capture(row, status, projection, upstream, kaponir, home):
    text = row["text"]
    payload, trace, lookup = projection
    echoed = send(upstream, payload)
    validate_trace(text, trace, upstream.received)
    if lookup:
        pattern = "|".join(map(re.escape, sorted(lookup, key=len, reverse=True)))
        echoed = re.sub(pattern, lambda m: lookup[m[0]], echoed)
    return {"id": row["id"], "source_sha256": sha256(text.encode()).hexdigest(), "model_status": status,
            "received": upstream.received, "replacements": trace, "restored": kaponir(home, ["restore"], echoed)}


def run_pair(rows, predictions, binary, schemas, projection=project):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAPONIR_")}
    def kaponir(home, args, content=""):
        proc = subprocess.run([str(binary), "--home", str(home), *args], input=content.encode(),
                              check=True, capture_output=True, env=env, timeout=120)
        return proc.stdout.decode()
    before, after, events = {}, {}, []
    with tempfile.TemporaryDirectory() as tmp, receiver() as upstream:
        template = Path(tmp) / "initial"
        kaponir(template, ["init"])
        for row in rows:
            key, text = row["id"], row["text"]
            with tempfile.TemporaryDirectory(dir=tmp) as folder:
                home = Path(folder) / "home"
                shutil.copytree(template, home)
                base = json.loads(kaponir(home, ["mask", "--format", "text", "--no-journal", "--json"], text))
                try:
                    decisions, status = ground(text, predictions.get(key)), "valid"
                except AnswerError as exc:
                    decisions, status = [], str(exc)
                merged = apply_positions(text, base, decisions)
                payload, trace, inverse, released = projection(
                    text, row["format"], row.get("publication_status"), decisions, base, merged, schemas
                )
                before[key] = capture(row, status, merged, upstream, kaponir, home)
                after[key] = capture(row, status, (payload, trace, inverse), upstream, kaponir, home)
                if released:
                    events.append({"id": key, "format": row["format"], "released_byte_ranges": released})
    return before, after, events


def compare(rows, before, after, events):
    scores = [score_outbound(rows, obs) for obs in (before, after)]
    exposed, rescued, new_public_losses = [], Counter(), []
    for r in rows:
        key = r["id"]
        for span in r["spans"]:
            one = {**r, "spans": [span]}
            old, new = (outbound_counts(one, obs[key]) for obs in (before, after))
            if old["private_full"] > new["private_full"] or old["review_full"] > new["review_full"]:
                exposed.append([key, span["start_byte"], span["end_byte"], span["action"]])
            if old["public_touched"] > new["public_touched"]:
                rescued[r["group"]] += 1
            if old["public_touched"] < new["public_touched"]:
                new_public_losses.append([key, span["start_byte"], span["end_byte"]])
    return {"before": scores[0]["all"], "after": scores[1]["all"], "new_exposures": exposed,
            "public_values_recovered_by_family": dict(rescued), "new_public_losses": new_public_losses,
            "remaining_public_losses_by_family": {g: c["public_touched"] for g, c in scores[1]["by_group"].items()
                                                if c.get("public_touched", 0)},
            "windows_with_release": len(events), "paired": paired_difference(*scores)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--predictions", type=Path, required=True, help="Joined predictions for the chosen split.")
    p.add_argument("--split", choices=("validation", "test"), default="validation")
    p.add_argument("--data", type=Path, default=ROOT / "data" / "v3")
    p.add_argument("--schemas", nargs="+", choices=["csv", "yaml", "log"], default=[])
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error("output directory already exists")
    data = args.data
    manifest = check_snapshot(data)
    source = data / (args.split + ".jsonl")
    rows = read_jsonl(source)
    pred = index_records(args.predictions, rows)
    if len(pred) != len(rows):
        p.error("paired experiment requires predictions for every document in the chosen split")
    before, after, events = run_pair(rows, pred, args.binary.resolve(), set(args.schemas))
    stats = compare(rows, before, after, events)
    stats.update(dataset=manifest["version"], split=args.split, schemas=args.schemas, documents=len(rows),
                 model=score_model(rows, pred), outbound=score_outbound(rows, after),
                 source_sha256=sha256(source.read_bytes()).hexdigest(),
                 predictions_sha256=sha256(args.predictions.read_bytes()).hexdigest(),
                 binary_sha256=sha256(args.binary.read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as tmp:
        out = Path(tmp) / "results"
        out.mkdir()
        (out / "before.jsonl").write_bytes(jsonl(before.values()))
        (out / "after.jsonl").write_bytes(jsonl(after.values()))
        (out / "released.jsonl").write_bytes(jsonl(events))
        (out / "report.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.rename(args.output)
    for k in ("before", "after"):
        print(k, {metric: stats[k][metric] for metric in (
            "private_full", "private_partial", "private_open", "public_touched", "restored", "documents_with_leak")})
    print("new exposures", len(stats["new_exposures"]), "public saved", stats["public_values_recovered_by_family"])


if __name__ == "__main__":
    main()
