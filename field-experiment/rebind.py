"""Apply the epoch-1 grounding experiment to raw window answers from the Kaponir pilot."""

import argparse
from collections import Counter
from itertools import islice
import json
from pathlib import Path
import re
import sys
import tempfile

# Run from the research branch's repository root, or place this file beside prepare.py.
pilot = Path.cwd() / "research" / "privacy-training"
if pilot.is_dir():
    sys.path.insert(0, str(pilot))

from dataset import jsonl, offsets, read_jsonl
from evaluate import AnswerError, ground, index_records, strict_json
from prepare import join_answers


def repair_span(text, raw, span, policy):
    """Called only for a schema-valid span that failed the original positional check."""
    value = span["value"]
    if span["occurrence"] < 1:
        return None, "invalid_ordinal"
    try:
        hits = list(islice(offsets(raw, value), 2))
    except UnicodeEncodeError:
        return None, "invalid_unicode"
    if len(hits) == 1:
        return {**span, "occurrence": 1}, "unique_literal"
    if hits:
        return None, "ambiguous_literal"
    if policy == "client-label" and (span["kind"], span["action"], span["occurrence"]) == ("CLIENT", "mask", 1):
        match = re.fullmatch(r"customer-([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)", value)
        if match:
            suffix = match[1]
            phrase = r"(?<!\S)customer " + re.escape(suffix) + r"(?=$|[\s,;!?)]|\.(?=\s|$))"
            if len(list(islice(offsets(raw, suffix), 2))) == 1 and re.search(phrase, text):
                return {**span, "value": suffix}, "separate_customer_label"
    return None, "absent_literal"


def repair_window(text, record, policy):
    try:
        ground(text, record)
        return record, {"original": "valid", "applied": "original"}
    except AnswerError as exc:
        audit = {"original": str(exc), "applied": "original"}
    if audit["original"] != "ungrounded_occurrence" or not record or "response" not in record or "error" in record:
        return record, audit

    spans = strict_json(record["response"])["spans"]
    raw = text.encode()
    kept, decisions = [], []
    audit["spans"] = decisions
    for index, span in enumerate(spans):
        try:
            ground(text, {"spans": [span]})
            candidate, reason = span, "original_position"
        except AnswerError as exc:
            if str(exc) != "ungrounded_occurrence":
                audit["rejected"] = str(exc)
                return record, audit
            candidate, reason = repair_span(text, raw, span, policy)
        decisions.append({"index": index, "resolution": reason})
        if candidate is not None:
            kept.append(candidate)

    if not kept:
        return record, audit
    corrected = {"id": record["id"], "spans": kept}
    try:
        ground(text, corrected)
    except AnswerError as exc:
        audit["rejected"] = str(exc)
        return record, audit
    audit["applied"] = "all" if len(kept) == len(spans) else "partial"
    return corrected, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=Path, required=True, help="Label-free validation-windows.jsonl from prepare.py.")
    parser.add_argument("--answers", type=Path, required=True, help="Raw windows.jsonl from inference.")
    parser.add_argument("--policy", choices=("exact", "client-label"), required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory for predictions and the per-window audit.")
    args = parser.parse_args()
    windows = read_jsonl(args.windows)
    if not windows or len({w["id"] for w in windows}) != len(windows):
        parser.error("window plan is empty or has duplicate window IDs")
    answers = index_records(args.answers, windows)
    repaired, audit = {}, []
    for w in windows:
        window_id = w["id"]
        answer = answers.get(window_id)
        if answer is not None and "response" not in answer and "error" not in answer:
            parser.error(f"{window_id}: expected a raw window response or generation error")
        text = json.loads(w["prompt"][-1]["content"])["text"]
        fixed, entry = repair_window(text, answer, args.policy)
        if fixed is not None:
            repaired[window_id] = fixed
        audit.append({"id": window_id, **entry})
    joined = join_answers(windows, repaired)
    if args.output.exists():
        parser.error("output already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as tmp:
        out = Path(tmp) / "result"
        out.mkdir()
        (out / "predictions.jsonl").write_bytes(jsonl(joined))
        (out / "audit.jsonl").write_bytes(jsonl(audit))
        out.rename(args.output)
    print(json.dumps(Counter(a["applied"] for a in audit), sort_keys=True))


if __name__ == "__main__":
    main()
