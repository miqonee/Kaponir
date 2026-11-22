"""Evaluate pilot model answers and independently observed outbound replacements."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import islice
import json
from pathlib import Path
from random import Random
import sys

from dataset import (
    ACTIONS,
    KINDS,
    MAX_PUBLIC_PERCENT,
    MIN_FULL_PERCENT,
    ROOT,
    SPLITS,
    check_snapshot,
    contrast_sets,
    offsets,
    read_jsonl,
)

sys.path.insert(0, str(ROOT.parents[1] / "scripts"))
from kaponir_inference import strict_json


class AnswerError(ValueError):
    """A recorded model failure, distinct from a damaged evaluation file."""


def index_records(path, rows):
    ids = {r["id"] for r in rows}
    records = {}
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = strict_json(line)
                key = row["id"]
                if key not in ids or key in records:
                    raise ValueError("unknown or duplicate ID")
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: invalid run envelope: {exc}") from exc
            records[key] = row
    return records


def ground(text, record):
    """Interpret literal occurrence numbers in the source, independently of gold."""
    if record is None:
        raise AnswerError("missing_answer")
    if "error" in record:
        code = record["error"]
        known = (
            "missing_answer",
            "invalid_json",
            "invalid_schema",
            "ungrounded_occurrence",
            "duplicate_or_overlap",
            "truncated_answer",
        )
        raise AnswerError(code if code in known else "invalid_schema")
    if "response" in record:
        try:
            answer = strict_json(record["response"])
        except (ValueError, TypeError):
            raise AnswerError("invalid_json") from None
        if not isinstance(answer, dict) or set(answer) != {"spans"}:
            raise AnswerError("invalid_schema")
        spans = answer["spans"]
    else:
        spans = record.get("spans")
    if not isinstance(spans, list):
        raise AnswerError("invalid_schema")
    raw = text.encode()
    positions = []
    for s in spans:
        if not isinstance(s, dict) or set(s) != {"value", "kind", "action", "occurrence"}:
            raise AnswerError("invalid_schema")
        value, kind, action, occurrence = (s[k] for k in ("value", "kind", "action", "occurrence"))
        if not (
            isinstance(value, str)
            and value
            and isinstance(kind, str)
            and kind in KINDS
            and isinstance(action, str)
            and action in ACTIONS
            and type(occurrence) is int
        ):
            raise AnswerError("invalid_schema")
        if not 1 <= occurrence <= len(text):
            raise AnswerError("ungrounded_occurrence")
        try:
            start = next(islice(offsets(raw, value), occurrence - 1, occurrence), None)
        except UnicodeEncodeError:
            raise AnswerError("ungrounded_occurrence") from None
        if start is None:
            raise AnswerError("ungrounded_occurrence")
        positions.append((start, start + len(value.encode()), kind, action))
    positions.sort()
    if any(a[1] > b[0] for a, b in zip(positions, positions[1:])):
        raise AnswerError("duplicate_or_overlap")
    return positions


def rate(n, d):
    return {"n": n, "d": d, "rate": n / d if d else None}


def prf(counts):
    tp, fp, fn = (counts[k] for k in ("tp", "fp", "fn"))
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": rate(tp, tp + fp),
        "recall": rate(tp, tp + fn),
        "f1": rate(2 * tp, 2 * tp + fp + fn),
    }


def stratum(row):
    return "with_status" if "publication_status" in row else "text_only"


def score_model(rows, predictions):
    total, errors, mismatches = Counter(), Counter(), Counter()
    actions, kinds, cases, correct = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter), {}
    for r in rows:
        gold = {(s["start_byte"], s["end_byte"], s["kind"], s["action"]) for s in r["spans"]}
        ok = True
        try:
            found = set(ground(r["text"], predictions.get(r["id"])))
        except AnswerError as exc:
            found, ok = set(), False
            errors[str(exc)] += 1
        correct[r["id"]] = ok and gold == found
        cases[stratum(r)]["exact"] += correct[r["id"]]
        cases[stratum(r)]["total"] += 1
        for metric, items in (("tp", found & gold), ("fp", found - gold), ("fn", gold - found)):
            total[metric] += len(items)
            for _, _, kind, action in items:
                actions[action][metric] += 1
                kinds[kind][metric] += 1
        locations = {(a, b): (k, act) for a, b, k, act in found}
        for a, b, kind, action in gold - found:
            if (a, b) in locations:
                k, act = locations[a, b]
                mismatches["wrong_kind"] += k != kind
                mismatches["wrong_action"] += act != action
            elif any(a < right and left < b for left, right, _, _ in found):
                mismatches["inexact_boundary"] += 1
            else:
                mismatches["no_span"] += 1
    paired = defaultdict(Counter)
    for (contrast, _), members in contrast_sets(rows).items():
        paired[contrast]["total"] += 1
        paired[contrast]["exact"] += all(correct[r["id"]] for r in members)
    return {
        "documents": len(rows),
        "exact_documents": rate(sum(correct.values()), len(rows)),
        "valid_documents": rate(len(rows) - sum(errors.values()), len(rows)),
        "errors": dict(errors),
        "diagnostics": dict(mismatches),
        "all_spans": prf(total),
        "by_action": {k: prf(actions[k]) for k in sorted(ACTIONS)},
        "by_kind": {k: prf(kinds[k]) for k in sorted(KINDS)},
        "by_input": {k: rate(v["exact"], v["total"]) for k, v in cases.items()},
        "contrast_sets": {k: rate(v["exact"], v["total"]) for k, v in paired.items()},
    }


def validate_trace(text, replacements, received):
    """Verify every replacement against the payload captured at the recipient."""
    raw = text.encode()
    ordered = []
    for r in replacements:
        a, b, token = r["start"], r["end"], r["token"]
        if not (
            type(a) is int and type(b) is int and 0 <= a < b <= len(raw) and isinstance(token, str) and token
        ):
            raise ValueError("invalid replacement")
        raw[:a].decode()
        original = raw[a:b].decode()
        if original in token:
            raise ValueError("replacement exposes its original")
        ordered.append((a, b, token))
    pos, pieces = 0, []
    for a, b, token in sorted(ordered):
        if a < pos:
            raise ValueError("overlapping replacements")
        pieces.extend((raw[pos:a], token.encode()))
        pos = b
    if b"".join(pieces + [raw[pos:]]) != received.encode():
        raise ValueError("trace differs from received text")
    return sorted(ordered)


def span_intersections(span, intervals):
    a, b = span["start_byte"], span["end_byte"]
    touching = [(l, h, t) for l, h, t in intervals if l < b and a < h]
    covered = sum(min(b, h) - max(a, l) for l, h, _ in touching)
    return touching, covered


def private_level(raw, span, touching, covered):
    a, b = span["start_byte"], span["end_byte"]
    if not covered:
        return "open"
    # A literal can survive a substitution when the token and adjacent bytes
    # form the original value. Check within this occurrence's outbound slice.
    cursor, visible = a, []
    for left, right, token in touching:
        visible.extend((raw[cursor : max(cursor, left)], token.encode()))
        cursor = min(b, right)
    if span["value"].encode() in b"".join(visible + [raw[cursor:b]]):
        return "open"
    return "full" if covered == b - a else "partial"


def outbound_counts(row, observation, by_kind=None):
    spans = row["spans"]
    counts = Counter(
        documents=1,
        private=sum(s["action"] == "mask" for s in spans),
        public=sum(s["action"] == "keep" for s in spans),
        review=sum(s["action"] == "review" for s in spans),
        clean=int(not spans),
    )
    counts["private_documents"] = int(counts["private"] > 0)
    if by_kind is not None:
        for s in spans:
            by_kind[s["kind"]][s["action"]] += 1
    if observation is None:
        counts["missing_observation"] = 1
        counts["private_unobserved"] = counts["private"]
        counts["public_unobserved"] = counts["public"]
        return counts
    original = row["text"]
    if observation["source_sha256"] != sha256(original.encode()).hexdigest():
        raise ValueError("observation belongs to a different source")
    intervals = validate_trace(original, observation["replacements"], observation["received"])
    counts["observed"] = 1
    counts["restored"] = int(original == observation["restored"])
    counts["changed_clean"] = int(not spans and original != observation["received"])
    annotated_bytes = 0
    raw = original.encode()
    for s in spans:
        touching, n = span_intersections(s, intervals)
        annotated_bytes += n
        if s["action"] == "keep":
            counts["public_touched"] += bool(n)
            if by_kind is not None:
                by_kind[s["kind"]]["public_touched"] += bool(n)
        else:
            level = private_level(raw, s, touching, n)
            prefix = "private" if s["action"] == "mask" else "review"
            counts[f"{prefix}_{level}"] += 1
            if by_kind is not None:
                by_kind[s["kind"]][f"{prefix}_{level}"] += 1
    counts["collateral_bytes"] = sum(h - l for l, h, _ in intervals) - annotated_bytes
    counts["documents_with_leak"] = int(counts["private_full"] < counts["private"])
    return counts


def positional_comparison(rows, baseline, candidate):
    """Compare exposure at the same source positions, without cancelling regressions."""
    report = {
        "recovered_public": [],
        "new_public_losses": [],
        "new_private_disclosures": [],
        "private_remaining": [],
    }
    for row in rows:
        key, text = row["id"], row["text"]
        raw = text.encode()
        before = validate_trace(text, baseline[key]["replacements"], baseline[key]["received"])
        after = validate_trace(text, candidate[key]["replacements"], candidate[key]["received"])
        for span in row["spans"]:
            before_hits, before_bytes = span_intersections(span, before)
            after_hits, after_bytes = span_intersections(span, after)
            item = {k: span[k] for k in ("start_byte", "end_byte", "kind")}
            item.update(id=key, masked_bytes_before=before_bytes, masked_bytes_after=after_bytes)
            if span["action"] == "keep":
                if before_bytes and not after_bytes:
                    report["recovered_public"].append(item)
                elif after_bytes and not before_bytes:
                    report["new_public_losses"].append(item)
            elif span["action"] == "mask":
                old = private_level(raw, span, before_hits, before_bytes)
                new = private_level(raw, span, after_hits, after_bytes)
                if new != "full":
                    item.update(before=old, after=new)
                    report["private_remaining"].append(item)
                    if old == "full":
                        report["new_private_disclosures"].append(item)
    return report


def outbound_summary(c):
    complete = c["observed"] == c["documents"]

    def measured(n, d):
        result = rate(n, d)
        if not complete:
            result["rate"] = None
        return result

    coverage = {
        f"{action}_{level}": c[f"{action}_{level}"]
        for action in ("private", "review")
        for level in ("full", "partial", "open")
    }
    return {
        **coverage,
        **dict(c),
        "full_privacy": rate(c["private_full"], c["private"]),
        "public_losses": measured(c["public_touched"], c["public"]),
        "leaking_requests": measured(c["documents_with_leak"], c["private_documents"]),
        "changed_empty_requests": measured(c["changed_clean"], c["clean"]),
        "exact_restore": rate(c["restored"], c["documents"]),
        "complete": complete,
        "pass": bool(
            complete
            and c["private"]
            and c["public"]
            and 100 * c["private_full"] >= MIN_FULL_PERCENT * c["private"]
            and 100 * c["public_touched"] <= MAX_PUBLIC_PERCENT * c["public"]
            and c["restored"] == c["documents"]
        ),
    }


def score_outbound(rows, observations):
    total, groups, slices = Counter(), defaultdict(Counter), defaultdict(Counter)
    kinds, languages, formats = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    for row in rows:
        try:
            c = outbound_counts(row, observations.get(row["id"]), kinds)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"{row['id']}: invalid outbound evidence: {exc}") from exc
        total.update(c)
        groups[row["group"]].update(c)
        slices[stratum(row)].update(c)
        languages[row.get("language", "unspecified")].update(c)
        formats[row["format"]].update(c)
    return {
        "all": outbound_summary(total),
        "by_input": {k: outbound_summary(v) for k, v in slices.items()},
        "by_language": {k: outbound_summary(v) for k, v in languages.items()},
        "by_format": {k: outbound_summary(v) for k, v in formats.items()},
        "by_kind": {k: dict(v) for k, v in kinds.items()},
        "by_group": {k: dict(v) for k, v in groups.items()},
    }


def paired_difference(base, candidate, replicates=2000):
    if not base["all"]["complete"] or not candidate["all"]["complete"]:
        raise ValueError("Paired comparison needs complete observations from both runs")
    groups = sorted(base["by_group"])
    rng = Random(42)
    result = {}
    for numerator, denominator in (("private_full", "private"), ("public_touched", "public")):
        # The same draw of source groups is used for both systems within each replicate.
        data = [
            (
                candidate["by_group"][g].get(numerator, 0) - base["by_group"][g].get(numerator, 0),
                base["by_group"][g].get(denominator, 0),
            )
            for g in groups
        ]
        deltas = []
        for _ in range(replicates):
            draw = rng.choices(data, k=len(groups))
            d = sum(v for _, v in draw)
            if d:
                deltas.append(100 * sum(v for v, _ in draw) / d)
        deltas.sort()
        d = sum(v for _, v in data)
        result[numerator] = {
            "delta_pp": 100 * sum(v for v, _ in data) / d if d else None,
            "ci95_pp": [deltas[int((len(deltas) - 1) * q)] for q in (0.025, 0.975)] if deltas else None,
            "groups": len(groups),
            "replicates_used": len(deltas),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "v3")
    parser.add_argument("--split", choices=SPLITS, default="test")
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--baseline-observations", type=Path)
    parser.add_argument("--positions", action="store_true", help="compare matched source occurrences")
    args = parser.parse_args()
    if not (args.predictions or args.observations) or (args.baseline_observations and not args.observations):
        parser.error("provide predictions or observations; a baseline needs candidate observations")
    if args.positions and not args.baseline_observations:
        parser.error("--positions requires a baseline and candidate observations")
    manifest = check_snapshot(args.data)
    rows = read_jsonl(args.data / f"{args.split}.jsonl")
    report = {
        "dataset": manifest["version"],
        "split": args.split,
        "source_sha256": manifest["splits"][args.split]["sha256"],
    }
    if args.predictions:
        report["model"] = score_model(rows, index_records(args.predictions, rows))
    if args.observations:
        observations = index_records(args.observations, rows)
        report["outbound"] = score_outbound(rows, observations)
    if args.baseline_observations:
        baseline_observations = index_records(args.baseline_observations, rows)
        baseline = score_outbound(rows, baseline_observations)
        report["baseline"] = baseline["all"]
        report["paired"] = paired_difference(baseline, report["outbound"])
        if args.positions:
            if not baseline["all"]["complete"] or not report["outbound"]["all"]["complete"]:
                parser.error("--positions requires complete observations in both runs")
            report["positions"] = positional_comparison(rows, baseline_observations, observations)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
