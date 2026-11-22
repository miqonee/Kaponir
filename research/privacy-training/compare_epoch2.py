#!/usr/bin/env python3
"""Run the original Kaponir search or A–D benchmark with one Qwen LoRA arm."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


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


def compare_abcd_plans(archived, candidate, variants):
    for key in ("source_sha256", "annotations_sha256", "case_data_sha256", "policy_sha256", "kinds_sha256"):
        assert candidate["signature"][key] == archived["signature"][key], key
    for key in ("control_cases", "batches"):
        assert candidate[key] == archived[key], key
    assert candidate["variant_order"] == list(variants)
    assert [r["request_sha256"] for r in candidate["requests"]] == [
        r["request_sha256"] for r in archived["requests"] if r["variant"] in variants
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
    variants = args.variants or tuple(plan["variant_order"])
    original_prepare = benchmark.make_plan

    def make_plan(parameters, *inputs):
        parameters.variants = variants
        return original_prepare(parameters, *inputs)

    benchmark.make_plan = make_plan
    argv = ["--model", "qwen3.5-2b", "--model-dir", str(args.model_dir),
            "--runtime", str(args.runtime)]
    for name, value in original.items():
        if name in {"context_tokens", "output_tokens", "retry_output_tokens", "reserve_tokens",
                    "reasoning_budget", "batch_size", "timeout", "response_format",
                    "temperature", "top_p", "top_k", "min_p", "seed"} and value is not None:
            argv.extend(["--" + name.replace("_", "-"), str(value)])
    with TemporaryDirectory(prefix="kaponir-abcd-plan-") as directory:
        invoke_abcd(benchmark, [*argv, "--output", directory, "--prepare-only"])
        compare_abcd_plans(plan, json.loads((Path(directory) / "plan.json").read_bytes()), variants)

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
    compare_abcd_plans(plan, current, variants)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("search", "abcd"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--binary", type=Path, help="Kaponir executable used for the search backtest")
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--variants", nargs="+", choices=tuple("ABCD"), help="Run an ordered A–D subset")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.suite == "search" and args.binary is None:
        parser.error("Search requires --binary")
    if args.variants and (args.suite != "abcd" or args.variants != sorted(set(args.variants))):
        parser.error("A–D variants must be unique and ordered")
    for name in ("repo", "runtime", "model_dir", "binary", "adapter", "output"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.repo / "scripts"))
    import kaponir_worker
    launcher = RuntimeLauncher(args.runtime, args.adapter)
    kaponir_worker.subprocess = launcher
    suite = {"search": run_search, "abcd": run_abcd}
    suite[args.suite](args, launcher)


if __name__ == "__main__":
    main()
