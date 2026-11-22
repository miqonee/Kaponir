"""Recheck the stored LFM timings, response contracts and applied masks without inference."""

import base64
import hashlib
import math
from pathlib import Path
import sys
import tarfile


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from apply_measured_privacy import coverage, span_coverage
from summarize_privacy_policy import digest, require, strict_json


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def valid_candidates(answer, schema):
    if not isinstance(answer, dict) or set(answer) != {"candidates"}:
        return False
    rows = answer["candidates"]
    if not isinstance(rows, list) or len(rows) > schema["maxItems"]:
        return False
    fields = schema["items"]["properties"]
    allowed = fields["confidence"].get("enum")
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(schema["items"]["required"]):
            return False
        confidence = row["confidence"]
        if (not isinstance(row["value"], str) or row["kind"] not in fields["kind"]["enum"]
                or type(confidence) not in (int, float) or not 0 <= confidence <= 1
                or (allowed is not None and confidence not in allowed)):
            return False
    return True


def main():
    manifest = strict_json((HERE / "manifest.json").read_text())
    info = manifest["archive"]
    archive = HERE / info["path"]
    require(archive.stat().st_size == info["bytes"] and sha256(archive.read_bytes()) == info["sha256"],
            "archive_checksum_mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        files = {member.name: tar.extractfile(member).read() for member in tar if member.isfile()}
    require(len(files) == info["members"], "archive_member_count_mismatch")
    checksum_lines = files["SHA256SUMS.txt"].decode().splitlines()
    checks = {name: value for value, name in (line.split("  ", 1) for line in checksum_lines)}
    require(set(checks) == set(files) - {"SHA256SUMS.txt"} and
            all(sha256(files[name]) == value for name, value in checks.items()), "member_checksum_mismatch")

    def read(name):
        return strict_json(files[name].decode())

    labels = read("measurement/annotations.json")
    count = 0
    for profile, run in manifest["verified_profiles"].items():
        prefix = f"runs/{profile}/"
        seconds, requests, valid = 0.0, 0, 0
        for path in files:
            if not path.startswith(prefix + "journal-") or not path.endswith(".json"):
                continue
            for entry in read(path)["requests"]:
                raw = entry["raw_transport"]
                reply = strict_json(base64.b64decode(raw["body_base64"], validate=True).decode())
                choice = reply["choices"][0]
                require(raw["status"] == 200 and reply == entry["raw_reply"] and choice["finish_reason"] == "stop"
                        and digest(entry["request"]) == entry["request_sha256"], "response_mismatch")
                array = entry["request"]["response_format"]["json_schema"]["schema"]["properties"]["candidates"]
                valid += valid_candidates(strict_json(choice["message"]["content"]), array)
                seconds += entry["record"]["http_wall_seconds"]
                requests += 1
        require(requests == run["complete_json_requests"] and valid == run["schema_valid_requests"]
                and math.isclose(seconds, run["http_seconds"], abs_tol=0.0001), "timing_or_contract_mismatch")
        count += requests
        for doc, expected in run["documents"].items():
            source = labels["sources"][doc]
            for mode in ("baseline", "model_masks_added"):
                applied = read(prefix + f"applied/{mode}/{doc}.json")
                require(applied["source_sha256"] == applied["restored_sha256"] == sha256(source.encode()),
                        "source_or_restore_mismatch")
                score = span_coverage(source, labels["private"][doc], applied["replacements"], applied["masked_text"])
                if mode == "model_masks_added":
                    score.update(coverage(source, applied["masked_text"], labels["private"][doc], labels["public"][doc]))
                    require(all(expected[k] == v for k, v in score.items()) and
                            expected["replacements"] == len(applied["replacements"]), "coverage_mismatch")
    print(f"Verified {len(manifest['verified_profiles'])} profiles and {count} completed requests")


if __name__ == "__main__":
    main()
