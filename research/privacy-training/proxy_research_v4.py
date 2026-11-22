"""Recreate the frozen epoch-2 CSV control and check record-local epoch-2 predictions."""

import argparse
from hashlib import file_digest, sha256
import json
from pathlib import Path

from dataset import PILOT_PROMPT, ROOT, jsonl, prompt, read_jsonl
from evaluate import AnswerError, ground, index_records, score_outbound
from v4_correction import release_public_amount, run_pair


RESULTS = ROOT / "results/v4"
EPOCH = RESULTS / "measurements/v4/validation/new-epoch-2"
ADAPTER_SHA256 = "a730758be6c676fcbf4f8842bd2487c9da8d159ceb2323c300d633d5f5b96280"
CASES = {"v4/shift-handover/mixed", "v4/shift-handover/both-private"}


def record_windows(rows):
    for row in rows:
        if row["id"] not in CASES:
            continue
        original = row["text"]
        doc = json.loads(original)
        records = doc["records"]
        cursor = original.index('[')
        for i, record in enumerate(records):
            record_text = json.dumps(record, ensure_ascii=False)
            start = original.index(record_text, cursor)
            cursor = start + len(record_text)
            # Only this record contains account values. Document metadata and the
            # provenance of neighboring records remain available as context.
            fragment = json.dumps({**doc, "records": []}, ensure_ascii=False)
            start_in_fragment = fragment.index('"records": [') + len('"records": [')
            fragment = fragment[:start_in_fragment] + record_text + fragment[start_in_fragment:]
            messages = prompt({**row, "text": fragment}, PILOT_PROMPT)
            context = json.dumps([item["record_origin"] for item in records], ensure_ascii=False)
            messages[0]["content"] += f" Document context: record provenance in source order is {context}. Classify only the supplied record at source index {i}."
            yield {
                "id": f"{row['id']}#{i}", "document_id": row["id"],
                "original_record_start": len(original[:start].encode()),
                "fragment_record_start": len(fragment[:start_in_fragment].encode()),
                "fragment_record_end": len(fragment[:start_in_fragment].encode()) + len(record_text.encode()),
                "prompt": messages,
            }


def compare_records(rows, windows, answers):
    archived = index_records(EPOCH / "joined/predictions.jsonl", rows)
    baseline = {row["id"]: set(ground(row["text"], archived[row["id"]])) for row in rows if row["id"] in CASES}
    projected = {key: set() for key in baseline}
    errors = {}
    for window in windows:
        fragment = json.loads(window["prompt"][-1]["content"])["text"]
        start, end = window["fragment_record_start"], window["fragment_record_end"]
        shift = window["original_record_start"] - start
        try:
            projected[window["document_id"]].update(
                (a + shift, b + shift, kind, act)
                for a, b, kind, act in ground(fragment, answers.get(window["id"]))
                if start <= a < b <= end
            )
        except AnswerError as exc:
            errors[window["id"]] = str(exc)
    return {
        "errors": errors,
        "positions": [
            {"id": row["id"], "range": [a, b], "kind": kind, "gold": act,
             "whole_document_match": (a, b, kind, act) in baseline[row["id"]],
             "record_local_match": (a, b, kind, act) in projected[row["id"]]}
            for row in rows if row["id"] in CASES for span in row["spans"]
            for a, b, kind, act in [(span["start_byte"], span["end_byte"], span["kind"], span["action"])]
        ],
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("mode", choices=("control", "windows", "record-results"))
    cli.add_argument("--binary", type=Path)
    cli.add_argument("--adapter", type=Path)
    cli.add_argument("--answers", type=Path)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    rows = read_jsonl(ROOT / "data/v4/validation.jsonl")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.mode == "control":
        predictions = index_records(EPOCH / "joined/predictions.jsonl", rows)
        _, after, _ = run_pair(rows, predictions, args.binary, {"csv", "yaml", "log"}, release_public_amount)
        report = score_outbound(rows, after)["all"]
        if (report["private_full"], report["public_touched"], report["restored"]) != (53, 25, 71):
            raise AssertionError(report)
        (args.output / "control.jsonl").write_bytes(jsonl(after.values()))
        (args.output / "control.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    elif args.mode == "windows":
        checkpoint = args.adapter / "adapter_model.safetensors"
        with checkpoint.open("rb") as f:
            if file_digest(f, sha256).hexdigest() != ADAPTER_SHA256:
                raise ValueError("unexpected epoch-2 adapter SHA-256")
        (args.output / "record-windows.jsonl").write_bytes(jsonl(record_windows(rows)))
    else:
        answers = {row["id"]: row for row in read_jsonl(args.answers)}
        report = compare_records(rows, read_jsonl(args.output / "record-windows.jsonl"), answers)
        (args.output / "record-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
