#!/usr/bin/env python3
"""Run the original Kaponir search or A–D benchmark with one Qwen LoRA arm."""
import argparse
import base64
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class RuntimeLauncher:
    """Instrument the benchmark worker's launch without changing its process options."""

    def __init__(self, runtime, adapter):
        self.runtime = runtime
        self.adapter = adapter
        self.sha256 = digest_file(adapter) if adapter else None
        self.launches = 0

    def __getattr__(self, key):
        return getattr(subprocess, key)

    def Popen(self, cmd, *args, **kwargs):
        if cmd[0] == str(self.runtime):
            if self.adapter:
                cmd = [*cmd, "--lora", str(self.adapter)]
            self.launches += 1
        return subprocess.Popen(cmd, *args, **kwargs)


def run_search(args, launcher):
    import evaluate_validation_schemes as legacy
    from apply_measured_privacy import apply_documents
    from apply_scheme_masks import select_masks
    from check_learning_corpus import load_corpus
    from evaluate_model_schemes import EXPORT, MeasuredClient
    from scheme_reference import reference

    archive, binding = reference("qwen3.5-2b")
    original = archive["signature"]["settings"]
    profile = {
        "reasoning": "off", "reasoning_format": "auto", "reasoning_budget": -1,
        "template_kwargs": {"enable_thinking": False}, "extra_sampling": {},
        "reserve_tokens": original["reserve_tokens"],
        "retry_output_tokens": original["output_tokens"],
        "transport_timeout_seconds": original["timeout"],
        "worker_configuration_timeout_seconds": original["timeout"],
    }
    history = []

    def client(worker, *parameters):
        from kaponir_worker import http_json

        measured = MeasuredClient(worker, *parameters, profile, args.output, history)
        adapters = http_json(worker.ocr_base + "/lora-adapters")
        check_active_adapter(adapters, launcher)
        legacy.atomic_json(args.output / "active-adapters.json", adapters)
        return measured

    lineage = {"archived_reference": binding, "adapter_sha256": launcher.sha256}
    argv = [
        "--model", "qwen3.5-2b", "--model-dir", str(args.model_dir),
        "--runtime", str(args.runtime), "--output", str(args.output),
        "--frozen-detectors", str(EXPORT), "--schemes", "free",
        "--response-format", archive["structured_output"],
    ]
    for name in ("batch_size", "context_tokens", "output_tokens", "reserve_tokens", "hint_tokens",
                 "max_candidates", "max_fragments", "timeout"):
        argv.extend(["--" + name.replace("_", "-"), str(original[name])])
    for key, value in archive["generation"].items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    legacy.main(argv, client_factory=client, experiment_lineage=lineage)
    assert launcher.launches, "No benchmark model started"
    state = json.loads((args.output / "result.json").read_bytes())
    signature = state["signature"]
    for name in ("artifact", "runtime_sha256", "source_sha256", "prompts"):
        assert signature[name] == archive["signature"][name], name
    for name in ("extraction_prompt_sha256", "kind_schema_sha256", "detector_export_sha256",
                 "annotations_sha256", "candidate_pool", "structured_output", "generation"):
        assert state[name] == archive[name], name
    assert state["status"] == "completed"
    technical_failures = sum(item["status"] != "received" or bool(item.get("parse_error")) for item in history)
    _, expected, public, sources, _ = load_corpus()
    private = defaultdict(list)
    for name, value in expected:
        private[name].append(value)
    selected, _ = select_masks(state["stages"]["current_union"], sources)
    application = apply_documents(
        args.binary, args.output / "application", signature["source_sha256"],
        sources, private, public, selected, lineage,
    )
    scopes = [row for row in application["documents"] if row["mode"] == "model_masks_added"]
    minutes = sum(row["record"]["http_wall_seconds"] for row in history) / 60
    hidden = sum(row["private_spans_fully_covered"] for row in scopes)
    lost = sum(row["public_occurrences_lost"] for row in scopes)
    replacements = next(row["replacements"] for row in scopes if row["file"] == "08-public-reference.md")
    original_score = 50*hidden/193 + 15*(87-lost)/87 + 5/(1+replacements) + 30*11.55/(11.55+minutes)
    final_score = (60*hidden/193 + 25*(87-lost)/87 + 5/(1+replacements) + 15*11.55/(11.55+minutes))/1.05
    summary = dict(private_hidden=hidden, public_lost=lost, guide_replacements=replacements,
                   http_minutes=minutes, score_50_20_30=original_score, score_60_30_15=final_score,
                   requests=len(history), technical_failures=technical_failures,
                   exact_roundtrips=application["exact_roundtrips"], **lineage)
    legacy.atomic_json(args.output / "summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)


def check_active_adapter(adapters, launcher):
    if launcher.adapter:
        assert len(adapters) == 1
        assert Path(adapters[0]["path"]) == launcher.adapter and adapters[0]["scale"] == 1.0
    else:
        assert adapters == []


def compare_abcd_plans(archived, candidate):
    for key in ("source_sha256", "annotations_sha256", "case_data_sha256", "policy_sha256", "kinds_sha256"):
        assert candidate["signature"][key] == archived["signature"][key], key
    for key in ("control_cases", "batches", "variant_order"):
        assert candidate[key] == archived[key], key
    assert [r["request_sha256"] for r in candidate["requests"]] == [
        r["request_sha256"] for r in archived["requests"]
    ], "Historical A–D request changed"


def invoke_abcd(benchmark, arguments):
    original = sys.argv
    try:
        sys.argv = [benchmark.__file__, *arguments]
        benchmark.main()
    finally:
        sys.argv = original


def run_abcd(args, launcher):
    import evaluate_privacy_policy as benchmark
    from kaponir_worker import http_json

    checkpoint = args.repo / "research/privacy-policy-phase2/checkpoints/2026-11-17"
    reference = checkpoint / "evidence/suite-v1/01-qwen3.5-2b/plan.json.gz"
    plan = json.loads(gzip.decompress(reference.read_bytes()))
    original = plan["signature"]["settings"]
    argv = ["--model", "qwen3.5-2b", "--model-dir", str(args.model_dir),
            "--runtime", str(args.runtime)]
    for name, value in original.items():
        if name in {"context_tokens", "output_tokens", "retry_output_tokens", "reserve_tokens",
                    "reasoning_budget", "batch_size", "timeout", "response_format",
                    "temperature", "top_p", "top_k", "min_p", "seed"} and value is not None:
            argv.extend(["--" + name.replace("_", "-"), str(value)])
    with TemporaryDirectory(prefix="kaponir-abcd-plan-") as directory:
        invoke_abcd(benchmark, [*argv, "--output", directory, "--prepare-only"])
        compare_abcd_plans(plan, json.loads((Path(directory) / "plan.json").read_bytes()))

    # Read back the runtime's effective configuration before the benchmark makes its first request.
    original_probe = benchmark.Worker.detect_json_schema

    def probe(worker):
        original_probe(worker)
        adapters = http_json(worker.ocr_base + "/lora-adapters")
        check_active_adapter(adapters, launcher)
        benchmark.atomic_json(args.output / "active-adapters.json", adapters)

    benchmark.Worker.detect_json_schema = probe
    benchmark.atomic_json(args.output / "adapter.json", {"adapter_sha256": launcher.sha256})
    invoke_abcd(benchmark, [*argv, "--output", str(args.output)])
    assert launcher.launches, "No benchmark model started"
    current = json.loads((args.output / "plan.json").read_bytes())
    compare_abcd_plans(plan, current)


def run_pilot(args, launcher):
    from dataset import ROOT, check_snapshot, jsonl, read_jsonl
    from evaluate import score_model, score_outbound
    from evaluate_privacy_policy import request_raw
    from kaponir_worker import Worker, configuration, http_json
    from prepare import CONTEXT, MAX_NEW, TokenBudget, join_answers, model_windows
    from recover_privacy_transport import wait_for_verified_model
    from replay import run as replay
    from run_privacy_policy_suite import atomic_json

    data = ROOT / "data" / args.suite.rsplit("-", 1)[-1]
    manifest = check_snapshot(data)
    rows = read_jsonl(data / "test.jsonl")
    budget = TokenBudget(args.assets)
    windows = [window for row in rows for window in model_windows(row, manifest["version"], budget)]
    (args.output / "windows.jsonl").write_bytes(jsonl(windows))
    answers = {}
    total_seconds = 0.0
    with TemporaryDirectory(prefix="kaponir-pilot-cpu-") as directory:
        home = Path(directory)
        worker = Worker(home, configuration(home, {
            "model_dir": str(args.model_dir), "runtime": str(args.runtime), "threads": 2,
        }), "unused")
        try:
            model = "qwen3.5-2b"
            wait_for_verified_model(worker, model, timeout=600)
            job = worker.submit("evaluate", model=model)
            worker.start_runtime(job["id"], model, 1, context_tokens=CONTEXT)
            adapters = http_json(worker.ocr_base + "/lora-adapters")
            check_active_adapter(adapters, launcher)
            atomic_json(args.output / "active-adapters.json", adapters)
            with (args.output / "transports.jsonl").open("xb") as receipts:
                for window in windows:
                    ident = window["id"]
                    tokens = budget.ids(window["prompt"], True)
                    assert len(tokens) + MAX_NEW <= CONTEXT
                    request = {"prompt": tokens, "n_predict": MAX_NEW, "temperature": 0.0,
                               "cache_prompt": False, "stream": False, "seed": 42}
                    started = time.monotonic()
                    transport = request_raw(worker.ocr_base + "/completion", request, 300)
                    elapsed = time.monotonic() - started
                    total_seconds += elapsed
                    receipts.write(jsonl([{"id": ident, "request": request,
                                           "transport": transport, "http_seconds": elapsed}]))
                    receipts.flush()
                    assert transport["status"] == 200, (ident, transport["status"])
                    reply = json.loads(base64.b64decode(transport["body_base64"]))
                    answers[ident] = ({"response": reply["content"]} if reply["stop_type"] == "eos"
                                      else {"error": "truncated_answer"})
        finally:
            worker.stop.set()
            worker.stop_runtime()
    assert len(answers) == len(windows)
    joined = join_answers(windows, answers)
    (args.output / "predictions.jsonl").write_bytes(jsonl(joined))
    predictions = {row["id"]: row for row in joined}
    observations = replay(rows, predictions, args.binary)
    (args.output / "observations.jsonl").write_bytes(jsonl(observations))
    outbound = score_outbound(rows, {row["id"]: row for row in observations})
    report = {"dataset": manifest["version"], "split": "test",
              "source_sha256": manifest["splits"]["test"]["sha256"],
              "binary_sha256": digest_file(args.binary), "adapter_sha256": launcher.sha256,
              "windows": len(windows), "http_seconds": total_seconds,
              "model": score_model(rows, predictions), "outbound": outbound}
    atomic_json(args.output / "summary.json", report)
    print(json.dumps(outbound["all"], ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("search", "abcd", "pilot-v3", "pilot-v2"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--assets", type=Path, help="Pinned tokenizer/template for the training pilot")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.suite.startswith("pilot-") and args.assets is None:
        parser.error("The pilot requires --assets")
    for name in ("repo", "runtime", "model_dir", "binary", "adapter", "assets", "output"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.repo / "scripts"))
    import kaponir_worker
    launcher = RuntimeLauncher(args.runtime, args.adapter)
    kaponir_worker.subprocess = launcher
    suite = {"search": run_search, "abcd": run_abcd, "pilot-v3": run_pilot, "pilot-v2": run_pilot}
    suite[args.suite](args, launcher)


if __name__ == "__main__":
    main()
