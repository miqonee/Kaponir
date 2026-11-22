"""Run the validation and development matrix on the actual native HTTP path."""

import argparse
from hashlib import sha256
import json
from pathlib import Path

from dataset import ROOT, jsonl, read_jsonl
from evaluate import score_outbound
from proxy_http_v4 import measure
from proxy_occurrences_v4 import fields, paired, prototype, records_fixture


def save(directory, rows, observations):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "http.jsonl").write_bytes(jsonl(observations.values()))
    metrics = score_outbound(rows, observations)
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics["all"]


def verify_development(baseline, windows, grants, both):
    for case in ("public-first", "private-first", "both-private", "windows-review"):
        actual = fields(both[case]["received"])
        original = fields(baseline[case]["received"])
        for path, (_, _, value) in actual.items():
            if len(path) != 3 or path[2] not in {"actor", "account"}:
                continue
            _, i, name = path
            public = (case, i) in {("public-first", 0), ("private-first", 1)}
            assert (value == original[path][2]) == public, (case, name, value)
            if name == "actor" and not public:
                domain, login = value.split("\\")
                assert "ANON_DOM_" in domain and "ANON_USER_" in login
        assert grants[case]["received"] == baseline[case]["received"]
    for case in ("expired", "stale", "conflict", "forged", "review"):
        base = baseline[case]
        expected = [(x["start"], x["end"]) for x in base["replacements"]]
        for run in (windows, grants, both):
            actual = [(x["start"], x["end"]) for x in run[case]["replacements"]]
            assert expected == actual, (case, expected, actual)
    for run in (grants, both):
        assert "51.100" in run["crossing"]["received"]
        assert "198." not in run["crossing"]["received"] and ".120" not in run["crossing"]["received"]
        actual = fields(run["catalog"]["received"])
        assert actual["records", 0, "endpoint"][2] == "192.0.2.45"
        assert actual["records", 0, "project"][2] == "marsh-commission-book-47"
        assert actual["records", 1, "endpoint"][2] != "192.0.2.45"
        assert actual["records", 1, "project"][2] != "marsh-commission-book-47"
        assert set(json.loads(run["catalog"]["wire_body"])) == {"model", "messages"}
        assert set(json.loads(run["catalog"]["wire_body"])["messages"][0]) == {"role", "content"}
        assert "x-kaponir-session" not in {header.lower() for header in run["catalog"]["wire_header_names"]}


def matrix(baseline, candidate, out):
    dev = out / "dev-source"
    records_fixture(dev)
    validation = ROOT / "data/v4/validation.jsonl"
    expected = json.loads((validation.parent / "manifest.json").read_bytes())["splits"]["validation"]["sha256"]
    if sha256(validation.read_bytes()).hexdigest() != expected:
        raise ValueError("validation file differs from the frozen input")
    permissions = dev / "permissions.json"
    grants = json.loads(permissions.read_bytes())
    results = {}
    for label, path in (("validation", validation), ("development", dev / "dev.jsonl")):
        rows = read_jsonl(path)
        runs = {"baseline": measure(baseline, rows)}
        base = runs["baseline"]
        scores = {"baseline": save(out / label / "baseline", rows, base)}
        for name, windows, allowed in (
            ("windows", True, None), ("permissions", False, permissions), ("both", True, permissions),
        ):
            simulated = {r["id"]: prototype(r, base[r["id"]], windows, grants if allowed else []) for r in rows}
            predicted = paired(rows, base, simulated)
            assert not predicted["new_mask_byte_exposures"] and not predicted["new_review_byte_exposures"]
            run = measure(candidate, rows, windows, allowed)
            runs[name] = run
            metrics = save(out / label / name, rows, run)
            compared = paired(rows, base, run)
            assert not compared["new_mask_byte_exposures"] and not compared["new_review_byte_exposures"], compared
            assert metrics["restored"] == len(rows), metrics
            (out / label / name / "positions.json").write_text(json.dumps(compared, indent=2), encoding="utf-8")
            scores[name] = {
                **metrics,
                "additional_private_full": metrics["private_full"] - scores["baseline"]["private_full"],
                "recovered_public": len(compared["recovered_public"]),
                "new_public_losses": len(compared["new_public_losses"]),
                "new_private_exposures": len(compared["new_mask_byte_exposures"]),
                "new_review_exposures": len(compared["new_review_byte_exposures"]),
            }
        if label == "development":
            verify_development(base, runs["windows"], runs["permissions"], runs["both"])
        results[label] = scores
    (out / "native-matrix.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    for label, scores in results.items():
        for mode, result in scores.items():
            print(label, mode, *(f"{key}={result.get(key, 0)}" for key in (
                "private_full", "public_touched", "restored", "review_full", "new_private_exposures",
            )))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    matrix(args.baseline, args.candidate, args.output)
