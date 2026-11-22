"""Build independent source permissions and test positional edits against native captures."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import time

from dataset import jsonl, read_jsonl
from evaluate import positional_comparison, score_outbound, validate_trace


def fields(text):
    """Return raw UTF-8 string ranges keyed by the exact path."""
    decoder = json.JSONDecoder()
    result = {}
    pos = 0

    def space():
        nonlocal pos
        pos += len(text[pos:]) - len(text[pos:].lstrip())

    def read(path):
        nonlocal pos
        space()
        char = text[pos]
        if char not in "{[":
            start = pos
            value, pos = decoder.raw_decode(text, pos)
            if char == '"':
                result[path] = (len(text[:start + 1].encode()), len(text[:pos - 1].encode()), value)
            return
        closing = "}" if char == "{" else "]"
        pos += 1
        space()
        index = 0
        while text[pos] != closing:
            if char == "{":
                key, pos = decoder.raw_decode(text, pos)
                space()
                assert text[pos] == ":"
                pos += 1
            else:
                key = index
            read((*path, key))
            space()
            index += 1
            if text[pos] != ",":
                break
            pos += 1
            space()
        assert text[pos] == closing
        pos += 1

    read(())
    return result


def records_fixture(output):
    output.mkdir(parents=True, exist_ok=True)
    source_docs = {
        "catalog": {"records": [
            {"endpoint": "192.0.2.45", "project": "marsh-commission-book-47"},
            {"endpoint": "192.0.2.45", "project": "marsh-commission-book-47"},
        ]},
        "crossing": {"records": [{"endpoint": "198.51.100.120"}]},
        "review": {"records": [{"location": "/home/iris-stencil/intro"}]},
        "expired": {"records": [{"endpoint": "192.0.2.46"}]},
        "stale": {"records": [{"endpoint": "192.0.2.47"}]},
        "conflict": {"records": [{"endpoint": "192.0.2.48"}]},
        "forged": {"records": [{"endpoint": "192.0.2.49", "publication_status": "published"}]},
    }
    # This catalog belongs to the simulated source process. It has no access to
    # assessment labels. In production only the operator configures its output.
    source_catalog = [
        ("catalog", 0, "endpoint", None, "public", "live"),
        ("catalog", 0, "project", None, "public", "live"),
        ("crossing", 0, "endpoint", "51.100", "public", "live"),
        ("review", 0, "location", "intro", "public", "live"),
        ("expired", 0, "endpoint", None, "public", "expired"),
        ("stale", 0, "endpoint", None, "public", "stale"),
        ("conflict", 0, "endpoint", None, "public", "live"),
        ("conflict", 0, "endpoint", None, "private", "live"),
    ]
    # Assessor's labels for the documents, including spans absent from the catalog.
    annotations = {
        "catalog": [(0, "endpoint", None, "IP", "keep"), (0, "project", None, "PROJ", "keep"),
                    (1, "endpoint", None, "IP", "mask"), (1, "project", None, "PROJ", "mask")],
        "crossing": [(0, "endpoint", "198.", "IP", "mask"), (0, "endpoint", "51.100", "IP", "keep"),
                     (0, "endpoint", ".120", "IP", "mask")],
        "review": [(0, "location", "intro", "PATH", "review")],
        "expired": [(0, "endpoint", None, "IP", "mask")],
        "stale": [(0, "endpoint", None, "IP", "mask")],
        "conflict": [(0, "endpoint", None, "IP", "mask")],
        "forged": [(0, "endpoint", None, "IP", "mask")],
    }
    review = {"actor": "NORTHBANK\\leah.pond", "account": "leah.pond",
              "record_origin": "account provenance unknown"}
    private = {**review, "record_origin": "active employee account from the internal audit log"}
    public = {**review, "record_origin": "fictional account in a published training exercise"}
    for key, pair in (("public-first", (public, private)), ("private-first", (private, public)),
                      ("both-private", (private, private)), ("windows-review", (review,))):
        source_docs[key] = {"title": "Журнал передачи смены", "records": pair}
        roles = {"public-first": ("keep", "mask"), "private-first": ("mask", "keep"),
                 "both-private": ("mask", "mask"), "windows-review": ("review",)}
        annotations[key] = [
            (i, field, value, kind, action)
            for i, action in enumerate(roles[key])
            for field, value, kind in (("actor", "NORTHBANK", "DOM"), ("actor", "leah.pond", "USER"),
                                       ("account", None, "USER"))
        ]
    texts = {key: json.dumps(doc, ensure_ascii=False) for key, doc in source_docs.items()}

    def span(key, record, field, substring):
        start, end, _ = fields(texts[key])["records", record, field]
        raw = texts[key].encode()[start:end]
        if substring:
            value = substring.encode()
            assert raw.count(value) == 1
            start += raw.index(value)
            end = start + len(value)
        return start, end

    grants = []
    for key, record, field, substring, status, validity in source_catalog:
        start, end = span(key, record, field, substring)
        version = texts[key] if validity != "stale" else texts[key] + "\n"
        grants.append({
            "document_sha256": sha256(version.encode()).hexdigest(), "record": record, "field": field,
            "start_byte": start, "end_byte": end, "status": status,
            "expires_unix": 1 if validity == "expired" else int(time.time()) + 86400,
        })
    rows = []
    for key, specifications in annotations.items():
        spans = []
        for record, field, substring, kind, action in specifications:
            start, end = span(key, record, field, substring)
            spans.append({"start_byte": start, "end_byte": end, "value": texts[key].encode()[start:end].decode(),
                          "kind": kind, "action": action})
        row = {"id": key, "group": "local-source", "format": "json", "text": texts[key], "spans": spans}
        if key == "forged":
            row["forged_grant"] = {"document_sha256": sha256(texts[key].encode()).hexdigest(), "record": 0,
                                   "field": "endpoint", "start_byte": spans[0]["start_byte"],
                                   "end_byte": spans[0]["end_byte"], "status": "public"}
        rows.append(row)
    (output / "dev.jsonl").write_bytes(jsonl(rows))
    (output / "permissions.json").write_text(json.dumps(grants, indent=2), encoding="utf-8")


def windows_masks(text):
    if not text.lstrip().startswith("{"):
        return []
    try:
        mapping = fields(text)
    except (ValueError, AssertionError, IndexError):
        return []
    raw = text.encode()
    intervals = []
    for path, (a, b, actor) in mapping.items():
        if len(path) != 3 or path[0] != "records" or path[2] != "actor":
            continue
        origin = mapping.get((*path[:2], "record_origin"))
        if not origin:
            continue
        provenance = origin[2].casefold()
        if ("вымышлен" in provenance and "открыт" in provenance
                or "fictional" in provenance and "published" in provenance):
            continue
        if len(actor.split("\\")) != 2 or not all(actor.split("\\")):
            continue
        separators = list(re.finditer(rb"\\\\|\\u005[cC]", raw[a:b]))
        if len(separators) != 1:
            continue
        separator = separators[0]
        intervals.extend([(a, a + separator.start()), (a + separator.end(), b)])
        account = mapping.get((*path[:2], "account"))
        if account and account[2]:
            intervals.append(account[:2])
    return intervals


def subtract(start, end, cuts):
    for a, b in sorted(cuts):
        if a >= end:
            break
        if b <= start:
            continue
        if start < a:
            yield start, a
        start = max(start, b)
    if start < end:
        yield start, end


def prototype(row, receipt, windows, grants):
    text = row["text"]
    raw = text.encode()
    version = sha256(raw).hexdigest()
    active = [item for item in grants if item["document_sha256"] == version and item["expires_unix"] > time.time()]
    ranges = [(g["start_byte"], g["end_byte"]) for g in active]
    overlap = any(b > c for (_, b), (c, _) in zip(sorted(ranges), sorted(ranges)[1:]))
    private = windows_masks(text)
    allowed = [(g["start_byte"], g["end_byte"]) for g in active if g["status"] == "public" and not overlap
               and not any(a < g["end_byte"] and g["start_byte"] < b for a, b in private)]
    replacements = []
    for r in receipt["replacements"]:
        start, end = r["start"], r["end"]
        eligible = r["origin"]["type"] == "builtin" and r["verdict"] == "mask" and not r["protected"]
        slices = list(subtract(start, end, allowed)) if eligible else [(start, end)]
        for a, b in slices:
            replacements.append(r if (a, b) == (start, end) else {"start": a, "end": b, "token": "[REDACTED]"})
    if windows:
        for a, b in private:
            for start, end in subtract(a, b, [(r["start"], r["end"]) for r in replacements]):
                replacements.append({"start": start, "end": end, "token": "[REDACTED]"})
    cursor, parts = 0, []
    for r in sorted(replacements, key=lambda r: r["start"]):
        parts.extend([raw[cursor:r["start"]], r["token"].encode()])
        cursor = r["end"]
    return {**receipt, "received": b"".join([*parts, raw[cursor:]]).decode(), "replacements": replacements}


def paired(rows, before, after):
    report = positional_comparison(rows, before, after)
    for action in ("mask", "review"):
        exposures = []
        for row in rows:
            coverage = []
            for run in (before, after):
                receipt = run[row["id"]]
                intervals = validate_trace(row["text"], receipt["replacements"], receipt["received"])
                coverage.append(set(i for a, b, _ in intervals for i in range(a, b)))
            for span in row["spans"]:
                a, b = span["start_byte"], span["end_byte"]
                if span["action"] == action and (coverage[0] - coverage[1]).intersection(range(a, b)):
                    exposures.append({"id": row["id"], "bytes": [a, b]})
        report[f"new_{action}_byte_exposures"] = exposures
    return report


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("mode", choices=("fixtures", "prototype", "compare"))
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--rows", type=Path)
    cli.add_argument("--baseline", type=Path)
    cli.add_argument("--candidate", type=Path)
    cli.add_argument("--permissions", type=Path)
    args = cli.parse_args()
    if args.mode == "fixtures":
        records_fixture(args.output)
        return
    rows = read_jsonl(args.rows)
    before = {r["id"]: r for r in read_jsonl(args.baseline)}
    grants = json.loads(args.permissions.read_bytes()) if args.permissions else []
    outputs = {"candidate": {r["id"]: r for r in read_jsonl(args.candidate)}} if args.candidate else {
        name: {row["id"]: prototype(row, before[row["id"]], windows, permissions) for row in rows}
        for name, windows, permissions in (("windows", True, []), ("permissions", False, grants),
                                           ("both", True, grants))
    }
    result = {}
    for name, after in outputs.items():
        changes = paired(rows, before, after)
        assert not changes["new_mask_byte_exposures"] and not changes["new_review_byte_exposures"], changes
        result[name] = {"all": score_outbound(rows, after)["all"], "positions": changes}
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({name: values["all"] for name, values in result.items()}))


if __name__ == "__main__":
    main()
