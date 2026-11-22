"""Verify pinned Qwen tokenization, export label-free evaluation windows, or join window answers."""

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import tempfile

from dataset import (
    ROOT,
    SPLITS,
    check_snapshot,
    dumps,
    export,
    jsonl,
    offsets,
    prompt,
    read_jsonl,
    target_spans,
)
from evaluate import AnswerError, ground, index_records
from grounding import ground_client_label
from kaponir_inference import plan_text

MODEL = "Qwen/Qwen3.5-2B"
REVISION = "15852e8c16360a2fea060d615a32b45270f8a8fc"
ASSETS = {
    "tokenizer.json": "5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42",
    "chat_template.jinja": "273d8e0e683b885071fb17e08d71e5f2a5ddfb5309756181681de4f5a1822d80",
}
CONTEXT = 1024
MAX_NEW = 256


class TokenBudget:
    def __init__(self, directory):
        from jinja2.sandbox import ImmutableSandboxedEnvironment
        from tokenizers import Tokenizer

        for name, expected in ASSETS.items():
            if sha256((directory / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f"{name}: assets must match {MODEL}@{REVISION}")
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.template = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True).from_string(
            (directory / "chat_template.jinja").read_text(encoding="utf-8")
        )

    def ids(self, messages, generating):
        text = self.template.render(
            messages=messages, add_generation_prompt=generating, enable_thinking=False
        )
        return self.tokenizer.encode(text, add_special_tokens=False).ids


def model_windows(row, version, budget):
    text = row["text"]

    def fits(fragment, _context):
        return len(budget.ids(prompt({**row, "text": fragment}, version), True)) + MAX_NEW <= CONTEXT

    parts = plan_text(text, fits)
    windows, start = [], 0
    digest = sha256(text.encode()).hexdigest()
    for i, part in enumerate(parts):
        fragment = text[part["start"] : part["end"]]
        end = start + len(fragment.encode())
        windows.append(
            {
                "id": f"{row['id']}#{i + 1}",
                "document_id": row["id"],
                "source_sha256": digest,
                "start_byte": start,
                "end_byte": end,
                "prompt": prompt({**row, "text": fragment}, version),
            }
        )
        start = end
    return windows


def join_answers(windows, answers, grounder=ground):
    groups = defaultdict(list)
    for window in windows:
        groups[window["document_id"]].append(window)
    result = []
    for row_id, parts in groups.items():
        pieces, hits, error, position = [], [], None, 0
        for part in sorted(parts, key=lambda p: p["start_byte"]):
            text = json.loads(part["prompt"][-1]["content"])["text"]
            raw = text.encode()
            if part["start_byte"] != position or part["end_byte"] != position + len(raw):
                raise ValueError(f"{row_id}: non-contiguous document windows")
            try:
                for a, b, k, act in grounder(text, answers.get(part["id"])):
                    hits.append((position + a, raw[a:b].decode(), k, act))
            except AnswerError as exc:
                error = error or str(exc)
            pieces.append(raw)
            position += len(raw)
        original = b"".join(pieces)
        digest = sha256(original).hexdigest()
        if any(p["source_sha256"] != digest for p in parts):
            raise ValueError(f"{row_id}: window source hash mismatch")
        if error:
            result.append({"id": row_id, "error": error})
        else:
            spans = [
                {
                    "value": value,
                    "kind": k,
                    "action": act,
                    "occurrence": list(offsets(original, value)).index(a) + 1,
                }
                for a, value, k, act in hits
            ]
            result.append({"id": row_id, "spans": spans})
    return result


def preflight(rows, version, budget, split):
    stats = {
        "documents": len(rows),
        "windows": 0,
        "split_documents": 0,
        "max_document_tokens": 0,
        "max_window_tokens": 0,
        "max_completion_tokens": 0,
    }
    all_windows = []
    for row in rows:
        completion = {"role": "assistant", "content": dumps({"spans": target_spans(row)})}
        full_length = len(budget.ids(prompt(row, version) + [completion], False))
        stats["max_document_tokens"] = max(stats["max_document_tokens"], full_length)
        windows = model_windows(row, version, budget)
        if split in {"train", "validation"} and len(windows) != 1:
            raise ValueError(f"{row['id']}: training/validation export would require windowed SFT")
        stats["split_documents"] += int(len(windows) > 1)
        stats["windows"] += len(windows)
        oracle = {}
        for part in windows:
            a, b = part["start_byte"], part["end_byte"]
            text = json.loads(part["prompt"][-1]["content"])["text"]
            spans = []
            for s in row["spans"]:
                if s["start_byte"] < b and a < s["end_byte"]:
                    if not (a <= s["start_byte"] and s["end_byte"] <= b):
                        raise ValueError(f"{row['id']}: a gold span crosses an input-only window boundary")
                    spans.append({**s, "start_byte": s["start_byte"] - a, "end_byte": s["end_byte"] - a})
            target = target_spans({"text": text, "spans": spans})
            oracle[part["id"]] = {"spans": target}
            message = {"role": "assistant", "content": dumps({"spans": target})}
            question = budget.ids(part["prompt"], True)
            whole = budget.ids(part["prompt"] + [message], False)
            tail = whole[len(question) :]
            if (
                whole[: len(question)] != question
                or len(whole) > CONTEXT
                or len(tail) > MAX_NEW
                or budget.tokenizer.token_to_id("<|im_end|>") not in tail
            ):
                raise ValueError(f"{row['id']}: token budget, completion prefix or EOS mismatch")
            stats["max_window_tokens"] = max(stats["max_window_tokens"], len(whole))
            stats["max_completion_tokens"] = max(stats["max_completion_tokens"], len(tail))
        if join_answers(windows, oracle)[0]["spans"] != target_spans(row):
            raise ValueError(f"{row['id']}: global occurrences did not round-trip")
        all_windows.extend(windows)
    return stats, all_windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="cmd", required=True)
    check = cmds.add_parser("check")
    check.add_argument("--assets", required=True, type=Path)
    check.add_argument("--data", type=Path, default=ROOT / "data" / "v3")
    check.add_argument("--prompt-version", choices=("v0", "v3"), default="v3")
    check.add_argument("--output", type=Path, help="new directory for SFT data, windows and audit")
    join = cmds.add_parser("join")
    join.add_argument("--windows", required=True, type=Path)
    join.add_argument("--answers", required=True, type=Path)
    join.add_argument("--output", required=True, type=Path)
    join.add_argument("--grounding", choices=("strict", "client-label"), default="strict")
    args = parser.parse_args()
    if args.cmd == "join":
        windows = read_jsonl(args.windows)
        grounder = ground_client_label if args.grounding == "client-label" else ground
        joined = join_answers(windows, index_records(args.answers, windows), grounder)
        with args.output.open("xb") as f:
            f.write(jsonl(joined))
        return
    manifest = check_snapshot(args.data)
    budget = TokenBudget(args.assets)
    report = {
        "model": MODEL,
        "revision": REVISION,
        "assets": ASSETS,
        "dataset": manifest["version"],
        "prompt_version": args.prompt_version,
        "enable_thinking": False,
        "context": CONTEXT,
        "max_new": MAX_NEW,
        "splits": {},
    }
    data = {}
    for split in SPLITS:
        rows = read_jsonl(args.data / f"{split}.jsonl")
        stats, windows = preflight(rows, args.prompt_version, budget, split)
        report["splits"][split] = stats
        if split != "train":
            data[f"{split}-windows.jsonl"] = jsonl(windows)
    if args.output:
        if args.output.exists():
            parser.error("output already exists")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.output.parent) as tmp:
            out = Path(tmp) / "export"
            out.mkdir()
            if manifest["version"] in {"v3", "v4"}:
                export(args.data, out, args.prompt_version)
            for name, payload in data.items():
                (out / name).write_bytes(payload)
            (out / "token-audit.json").write_text(dumps(report) + "\n", encoding="utf-8")
            out.rename(args.output)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
