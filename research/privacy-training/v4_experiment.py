"""Reproduce the corpus-only v4 experiment and make the validation gate auditable."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys

from dataset import MAX_PUBLIC_PERCENT, MIN_FULL_PERCENT, PILOT_PROMPT, ROOT, check_snapshot, jsonl, read_jsonl
from evaluate import index_records, positional_comparison
from laptop import base_model, choose, generate, gpu
from prepare import TokenBudget
from v4_audit import audit_v3, compare_private, write_json
from v4_report import render_report


SOURCE = ROOT.parents[1]
FIELD = SOURCE / "field-experiment"
SCHEMAS = ("csv", "yaml", "log")
PROTOCOL = "842d9887f8023403eadaab713248aea34ceeceeb"


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def subprocess_python(script, *args):
    subprocess.run([sys.executable, "-B", str(script), *map(str, args)], cwd=SOURCE, check=True)


def mark(run, stage):
    (run / "study-stage.txt").write_text(stage + "\n", encoding="utf-8")
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), stage, flush=True)


def verify_inputs(run, old, binary):
    for path in (binary, old / "assets/tokenizer.json", old / "assets/chat_template.jinja",
                 old / "full/epoch-1/adapter_model.safetensors"):
        if not path.is_file():
            raise FileNotFoundError(path)
    for version in ("v2", "v3", "v4"):
        check_snapshot(ROOT / "data" / version)
    if not (run / "v3-audit.json").exists():
        rows = read_jsonl(ROOT / "data/v3/test.jsonl")
        write_json(run / "v3-audit.json", audit_v3(rows, old / "archive-field-evaluation"))


def prepare(run, old, binary):
    verify_inputs(run, old, binary)
    mark(run, "preflight")
    data = ROOT / "data/v4"
    manifest = check_snapshot(data)
    if not (run / "inputs/token-audit.json").is_file():
        subprocess_python(ROOT / "prepare.py", "check", "--data", data, "--assets", old / "assets",
                          "--prompt-version", PILOT_PROMPT, "--output", run / "inputs")
    preflight = json.loads((run / "inputs/token-audit.json").read_bytes())
    if preflight["dataset"] != "v4" or preflight["prompt_version"] != PILOT_PROMPT:
        raise ValueError("Expected the frozen v4 inputs with the unchanged v3 prompt")
    preregistration = run / "preregistration.json"
    if not preregistration.exists():
        write_json(preregistration, {
            "protocol": PROTOCOL, "dataset": "v4", "prompt_version": PILOT_PROMPT,
            "test": manifest["splits"]["test"], "validation_sha256": manifest["splits"]["validation"]["sha256"],
            "selection": "Complete validation, exact restore for every document, public loss <= 2%; maximum exact model mask recall, earlier epoch breaks ties.",
            "failure_policy": "One targeted correction on validation. Use the same policy for all variants; reserve test while no checkpoint passes selection.",
            "sha256": {str(path.relative_to(run)): digest(path) for path in sorted((ROOT / "data/v4").glob("*"))},
        })
    registered = json.loads(preregistration.read_bytes())
    if registered["test"] != manifest["splits"]["test"] or registered["validation_sha256"] != manifest["splits"]["validation"]["sha256"]:
        raise ValueError("Evaluation splits changed after preregistration")
    input_hashes = {
        f"{split}_windows_sha256": digest(run / "inputs" / f"{split}-windows.jsonl")
        for split in ("validation", "test")
    }
    input_hashes.update(
        control_adapter_sha256=digest(old / "full/epoch-1/adapter_model.safetensors"),
        binary_sha256=digest(binary),
        field_policy_sha256=digest(FIELD / "field_policy.py"),
        client_label_sha256=digest(FIELD / "rebind.py"),
    )
    assessment_inputs = run / "assessment-inputs.json"
    if not assessment_inputs.exists():
        write_json(assessment_inputs, input_hashes)
    elif json.loads(assessment_inputs.read_bytes()) != input_hashes:
        raise ValueError("A frozen assessment input changed")
    return manifest


def fit(run, old, manifest):
    destination = run / "full"
    if not destination.exists():
        mark(run, "train")
        subprocess_python(ROOT / "laptop.py", "train", "--stage", "full", "--data", ROOT / "data/v4",
                          "--prompt-version", PILOT_PROMPT, "--assets", old / "assets", "--output", destination)
    record = json.loads((destination / "run.json").read_bytes())
    count = manifest["splits"]["train"]["records"]
    if (record.get("status"), record.get("training_sha256"), record.get("steps")) != (
        "complete", manifest["splits"]["train"]["sha256"], 2 * ((count + 3) // 4)
    ):
        raise ValueError("Expected a completed two-epoch v4 run with the preregistered training SHA")
    for epoch in (1, 2):
        if not (destination / f"epoch-{epoch}/adapter_model.safetensors").is_file():
            raise FileNotFoundError(f"Missing v4 epoch {epoch} adapter")


def generate_file(plan, assets, adapter, output):
    windows = read_jsonl(plan)
    if output.exists():
        existing = read_jsonl(output)
        if [row["id"] for row in existing] != [row["id"] for row in windows]:
            raise ValueError(f"Unmatched saved raw output: {output}")
        return
    partial = output.with_suffix(".partial.jsonl")
    if partial.exists():
        with partial.open("r+b") as file:
            saved = file.read()
            if saved and not saved.endswith(b"\n"):
                file.truncate(saved.rfind(b"\n") + 1)
        completed = read_jsonl(partial)
    else:
        completed = []
    if [row["id"] for row in completed] != [row["id"] for row in windows[:len(completed)]]:
        raise ValueError(f"Unmatched partial output: {partial}")
    if len(completed) < len(windows):
        from peft import PeftModel

        gpu()
        model = PeftModel.from_pretrained(base_model(), adapter).eval()
        budget = TokenBudget(assets)
        with partial.open("ab") as output_file:
            for index, window in enumerate(windows[len(completed):], len(completed) + 1):
                answer = generate(model, budget, window, "cuda")
                output_file.write(jsonl([answer]))
                output_file.flush()
                print(f"{index}/{len(windows)} {answer.get('error', 'generated')}", flush=True)
    partial.rename(output)


def cached_reference(old, version, split, plan, output):
    references = {
        ("v3", "validation"): (old / "inputs/validation-windows.jsonl", old / "validation/epoch-1/windows.jsonl"),
        ("v3", "test"): (old / "archive-field-evaluation/export/test-windows.jsonl", old / "archive-field-evaluation/raw.jsonl"),
        ("v2", "test"): (old / "regression/test-windows.jsonl", old / "context-policy-v3/heldouts/v2/candidate/windows.jsonl"),
    }
    if not output.exists() and (version, split) in references:
        original_plan, raw = references[version, split]
        if original_plan.is_file() and raw.is_file() and digest(original_plan) == digest(plan):
            shutil.copyfile(raw, output)


def measure(run, old, binary, version, split, name, adapter, windows=None):
    directory = run / f"measurements/{version}/{split}/{name}"
    directory.mkdir(parents=True, exist_ok=True)
    data = ROOT / "data" / version
    plan = windows or run / "inputs" / f"{split}-windows.jsonl"
    raw = directory / "windows.jsonl"
    if name == "old-epoch-1":
        cached_reference(old, version, split, plan, raw)
    generate_file(plan, old / "assets", adapter, raw)
    joined = directory / "joined/predictions.jsonl"
    if not joined.is_file():
        subprocess_python(FIELD / "rebind.py", "--policy", "client-label", "--windows", plan,
                          "--answers", raw, "--output", joined.parent)
    if not (directory / "fields/report.json").is_file():
        subprocess_python(FIELD / "replay_fields.py", "--data", data, "--split", split, "--binary", binary,
                          "--predictions", joined, "--schemas", *SCHEMAS, "--output", directory / "fields")
    return directory


def report(directory, subdir="fields"):
    return json.loads((directory / subdir / "report.json").read_bytes())


def comparison(data, split, reference, candidate, subdir="fields"):
    rows = read_jsonl(data / f"{split}.jsonl")
    old = index_records(reference / subdir / "after.jsonl", rows)
    new = index_records(candidate / subdir / "after.jsonl", rows)
    private = compare_private(rows, index_records(reference / "joined/predictions.jsonl", rows), old,
                              index_records(candidate / "joined/predictions.jsonl", rows), new)
    return {
        "old_model": report(reference, subdir)["model"], "new_model": report(candidate, subdir)["model"],
        "old": report(reference, subdir)["after"], "new": report(candidate, subdir)["after"],
        "private_decisions": private, "positions": positional_comparison(rows, old, new),
    }


def run_assessment(run, old, binary):
    from v4_correction import replay_correction

    data = ROOT / "data/v4"
    old_adapter = old / "full/epoch-1"
    adapters = (old_adapter, run / "full/epoch-1", run / "full/epoch-2")
    names = ("old-epoch-1", "new-epoch-1", "new-epoch-2")
    directories = []
    for name, adapter in zip(names, adapters, strict=True):
        mark(run, "validation-" + name)
        directories.append(measure(run, old, binary, "v4", "validation", name, adapter))
    reference, first, second = directories
    decision = run / "selection.json"
    if not decision.is_file():
        initial = choose([report(first), report(second)], data)
        mode, selected = "fields", initial
        correction = None
        if initial is None:
            mark(run, "validation-targeted-correction")
            correction = replay_correction(data, directories, binary)
            mode = "correction"
            selected = choose([report(first, mode), report(second, mode)], data)
            if any(report(directory, mode)["new_exposures"] for directory in directories):
                selected = None
        comparisons = {subdir: {
            "epoch-1": comparison(data, "validation", reference, first, subdir),
            "epoch-2": comparison(data, "validation", reference, second, subdir),
        } for subdir in ("fields", "correction") if (first / subdir / "report.json").is_file()}
        comparison_path = run / "validation-comparison.json"
        if comparison_path.is_file():
            if json.loads(comparison_path.read_bytes()) != comparisons:
                raise ValueError("A saved validation comparison differs from the measured receipts")
        else:
            write_json(comparison_path, comparisons)
        write_json(decision, {
            "initial_epoch": initial, "selected_epoch": selected, "mode": mode, "correction": correction,
            "validation_sha256": check_snapshot(data)["splits"]["validation"]["sha256"],
            "test_sha256": check_snapshot(data)["splits"]["test"]["sha256"],
            "adapter_sha256": digest(adapters[selected] / "adapter_model.safetensors") if selected else None,
        })
    frozen = json.loads(decision.read_bytes())
    selected = frozen["selected_epoch"]
    if selected is None:
        render_report(run)
        mark(run, "complete-no-checkpoint")
        return
    if digest(adapters[selected] / "adapter_model.safetensors") != frozen["adapter_sha256"]:
        raise ValueError("Candidate weights differ from the selected checkpoint")
    for version in ("v4", "v2", "v3"):
        dataset = ROOT / "data" / version
        export = run / "inputs" if version == "v4" else run / f"inputs-{version}"
        if not export.is_dir():
            subprocess_python(ROOT / "prepare.py", "check", "--data", dataset, "--assets", old / "assets",
                              "--prompt-version", PILOT_PROMPT, "--output", export)
        for split in (("test",) if version == "v4" else ("test", "validation")):
            mark(run, f"assessment-{version}-{split}")
            evaluated = [
                measure(run, old, binary, version, split, names[index], adapters[index], export / f"{split}-windows.jsonl")
                for index in (0, selected)
            ]
            if frozen["mode"] == "correction":
                replay_correction(dataset, evaluated, binary, split, frozen["correction"])
            output = run / f"comparison-{version}-{split}.json"
            if not output.is_file():
                result = comparison(dataset, split, *evaluated, frozen["mode"])
                observed = result["new"]
                result["thresholds"] = {
                    "private_full_min": (observed["private"] * MIN_FULL_PERCENT + 99) // 100,
                    "public_touched_max": observed["public"] * MAX_PUBLIC_PERCENT // 100,
                    "restored_min": observed["documents"],
                }
                write_json(output, result)
    render_report(run)
    mark(run, "complete-selected")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--old-run", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--phase", choices=("all", "prepare", "train", "assess"), default="all")
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    args.run.mkdir(parents=True, exist_ok=True)
    manifest = prepare(args.run, args.old_run, args.binary)
    if args.phase in {"all", "train", "assess"}:
        fit(args.run, args.old_run, manifest)
    if args.phase in {"all", "assess"}:
        run_assessment(args.run, args.old_run, args.binary)


if __name__ == "__main__":
    main()
