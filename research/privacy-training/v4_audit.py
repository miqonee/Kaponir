"""Position-by-position audit of the saved v3 pilot and the new v4 comparison."""

from collections import Counter
from hashlib import sha256
import json

from dataset import read_jsonl
from evaluate import AnswerError, ground, index_records, private_level, span_intersections, validate_trace


def source_key(row, span):
    return {
        "id": row["id"], "group": row["group"], "kind": span["kind"],
        "start_byte": span["start_byte"], "end_byte": span["end_byte"],
    }


def verified_trace(row, observation):
    if observation["source_sha256"] != sha256(row["text"].encode()).hexdigest():
        raise ValueError(f"{row['id']}: receipt is for a different source document")
    return validate_trace(row["text"], observation["replacements"], observation["received"])


def private_outcomes(rows, predictions, observations):
    for row in rows:
        key, text = row["id"], row["text"]
        raw = text.encode()
        trace = verified_trace(row, observations[key])
        try:
            decisions = ground(text, predictions.get(key))
            error = None
        except AnswerError as exc:
            decisions, error = [], str(exc)
        for span in row["spans"]:
            if span["action"] != "mask":
                continue
            start, end = span["start_byte"], span["end_byte"]
            exact = next(((kind, action) for a, b, kind, action in decisions if (a, b) == (start, end)), None)
            if error:
                outcome = "unusable_response"
            elif exact:
                kind, action = exact
                outcome = {"keep": "wrong_keep", "review": "review", "mask": "exact_mask"}[action]
                if action == "mask" and kind != span["kind"]:
                    outcome = "wrong_kind"
            elif any(a < end and start < b for a, b, _, _ in decisions):
                outcome = "non_exact_boundary"
            elif any(raw[a:b] == raw[start:end] and action != "keep" for a, b, _, action in decisions):
                outcome = "different_occurrence"
            else:
                outcome = "omitted"
            hits, covered = span_intersections(span, trace)
            yield {**source_key(row, span), "outcome": outcome, "response_error": error,
                   "outbound_level": private_level(raw, span, hits, covered)}


def compare_private(rows, old_predictions, old_receipts, new_predictions, new_receipts):
    old = list(private_outcomes(rows, old_predictions, old_receipts))
    new = list(private_outcomes(rows, new_predictions, new_receipts))
    transitions = []
    for before, after in zip(old, new, strict=True):
        if any(before[k] != after[k] for k in ("id", "start_byte", "end_byte", "kind")):
            raise ValueError("Matched private occurrence changed identity")
        if before["outcome"] != after["outcome"] or before["outbound_level"] != after["outbound_level"]:
            transitions.append({
                "id": after["id"], "group": after["group"], "kind": after["kind"],
                "start_byte": after["start_byte"], "end_byte": after["end_byte"],
                "old_outcome": before["outcome"], "new_outcome": after["outcome"],
                "old_outbound": before["outbound_level"], "new_outbound": after["outbound_level"],
            })
    return {
        "old_outcomes": dict(Counter(item["outcome"] for item in old)),
        "new_outcomes": dict(Counter(item["outcome"] for item in new)),
        "new_wrong_keep": sum(item["new_outcome"] == "wrong_keep" and item["old_outcome"] != "wrong_keep" for item in transitions),
        "new_omissions": sum(item["new_outcome"] == "omitted" and item["old_outcome"] != "omitted" for item in transitions),
        "newly_exposed": sum(item["old_outbound"] == "full" and item["new_outbound"] != "full" for item in transitions),
        "transitions": transitions,
    }


def overlaps(span, trace):
    touching, _ = span_intersections(span, trace)
    return [[a, b] for a, b, _ in touching]


def audit_v3(rows, archived):
    """Separate model failures and detector/proxy contributions to public loss."""
    paths = {
        "detectors": archived / "detectors-observations.jsonl",
        "before": archived / "measured/before.jsonl",
        "after": archived / "measured/after.jsonl",
        "predictions": archived / "joined/predictions.jsonl",
        "raw": archived / "raw.jsonl",
        "window_plan": archived / "export/test-windows.jsonl",
        "rebind_audit": archived / "joined/audit.jsonl",
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        return {
            "evidence": "user_report", "unavailable_local_logs": missing,
            "user_report": {"private_full": 157, "public_touched": 22, "restored": 72,
                            "unusable_response_private_occurrences": 24},
        }
    observations = {name: index_records(paths[name], rows) for name in ("detectors", "before", "after")}
    predictions = index_records(paths["predictions"], rows)
    windows = read_jsonl(paths["window_plan"])
    raw = index_records(paths["raw"], windows)
    audits = index_records(paths["rebind_audit"], windows)
    by_doc = {}
    for window in windows:
        window_id = window["id"]
        by_doc.setdefault(window["document_id"], []).append({
            "id": window_id, "raw_error": raw.get(window_id, {}).get("error"),
            "strict_error": audits[window_id]["original"],
            "rebind": audits[window_id]["applied"],
        })
    private = [item for item in private_outcomes(rows, predictions, observations["after"])
               if item["outcome"] == "unusable_response" and item["outbound_level"] != "full"]
    for item in private:
        item["windows"] = by_doc[item["id"]]

    public = []
    for row in rows:
        traces = {name: verified_trace(row, docs[row["id"]]) for name, docs in observations.items()}
        detector_positions = {(a, b) for a, b, _ in traces["detectors"]}
        model_only = [(a, b, token) for a, b, token in traces["before"] if (a, b) not in detector_positions]
        for span in row["spans"]:
            if span["action"] != "keep":
                continue
            after = overlaps(span, traces["after"])
            if not after:
                continue
            detector = overlaps(span, traces["detectors"])
            before = overlaps(span, traces["before"])
            public.append({
                **source_key(row, span), "detector_ranges": detector, "model_ranges": overlaps(span, model_only),
                "before_field_ranges": before, "after_field_ranges": after, "field_rules_changed_ranges": before != after,
            })
    return {
        "evidence": "local_receipts",
        "sha256": {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()},
        "unusable_response_private": len(private),
        "unusable_response_by_joined_error": dict(Counter(item["response_error"] for item in private)),
        "unusable_response_positions": private,
        "public_losses": len(public),
        "public_already_touched_by_detectors": sum(bool(item["detector_ranges"]) for item in public),
        "public_touched_only_after_model": sum(not item["detector_ranges"] for item in public),
        "public_changed_by_field_rules": sum(item["field_rules_changed_ranges"] for item in public),
        "public_positions": public,
    }


def write_json(path, payload):
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
