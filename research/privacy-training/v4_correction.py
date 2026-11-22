"""One validation-only fallback for the explicitly public CSV amount cell."""

from hashlib import sha256
from pathlib import Path
import sys
import tempfile

from context_policy import context_edits
from dataset import ROOT, check_snapshot, jsonl, read_jsonl
from evaluate import index_records, positional_comparison, score_model, score_outbound
from v4_audit import write_json


FIELD = ROOT.parents[1] / "field-experiment"
sys.path.insert(0, str(FIELD))
from field_policy import project, render
from replay_fields import compare, run_pair


CORRECTION = "csv-explicit-public-amount"


def release_public_amount(text, fmt, status, decisions, builtin, merged, schemas):
    projected = project(text, fmt, status, decisions, builtin, merged, schemas)
    if fmt != "csv" or fmt not in schemas or status in {"internal", "unreleased", "unknown"}:
        return projected
    payload, trace, inverse, released = projected
    keeps = {(a, b) for a, b, kind, action in decisions if kind == "AMOUNT" and action == "keep"}
    allowed = [
        edit for edit in context_edits({"text": text, "format": fmt}, builtin["replacements"], decisions,
                                      ("csv-amount",), valid_answer=True)
        if edit.action == "keep" and (edit.start, edit.end) in keeps
    ]
    hard = {(r["start"], r["end"]): r["token"] for r in builtin["replacements"]}
    retained, extra = [], []
    for item in trace:
        a, b = item["start"], item["end"]
        if hard.get((a, b)) == item["token"] and any(edit.start <= a < b <= edit.end for edit in allowed):
            extra.append((a, b))
        else:
            retained.append(item)
    if not extra:
        return projected
    return render(text.encode(), retained), retained, inverse, released + extra


def replay_correction(data, directories, binary, split="validation", name=None):
    if name not in (None, CORRECTION):
        raise ValueError(f"Unsupported frozen correction: {name}")
    manifest = check_snapshot(data)
    rows = read_jsonl(data / f"{split}.jsonl")
    source_hash = manifest["splits"][split]["sha256"]
    for directory in directories:
        output = directory / "correction"
        if (output / "report.json").is_file():
            continue
        original = index_records(directory / "fields/after.jsonl", rows)
        prediction_path = directory / "joined/predictions.jsonl"
        predictions = index_records(prediction_path, rows)
        before, after, events = run_pair(rows, predictions, binary, {"csv", "yaml", "log"}, release_public_amount)
        result = compare(rows, original, after, events)
        positions = positional_comparison(rows, original, after)
        result.update(
            dataset=manifest["version"], split=split, source_sha256=source_hash,
            model=score_model(rows, predictions), outbound=score_outbound(rows, after),
            positions=positions, correction=CORRECTION,
            predictions_sha256=sha256(prediction_path.read_bytes()).hexdigest(),
        )
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            staging = Path(tmp) / "report"
            staging.mkdir()
            (staging / "before.jsonl").write_bytes(jsonl(before.values()))
            (staging / "after.jsonl").write_bytes(jsonl(after.values()))
            (staging / "released.jsonl").write_bytes(jsonl(events))
            write_json(staging / "report.json", result)
            staging.rename(output)
    return CORRECTION
