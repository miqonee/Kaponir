"""Run the registered Qwen LoRA pilot on one BF16 CUDA GPU, or select a validation checkpoint."""

import argparse
import json
import math
from pathlib import Path
import random
import subprocess
import sys
from time import monotonic

from dataset import MAX_PUBLIC_PERCENT, ROOT, check_snapshot, dumps, jsonl, read_jsonl, target_spans
from evaluate import AnswerError, ground
from prepare import CONTEXT, MAX_NEW, MODEL, REVISION, TokenBudget

SEED = 42
LORA_TARGETS = (
    r"model\.language_model\.layers\.\d+\.(?:self_attn\.(?:q_proj|k_proj|v_proj|o_proj)"
    r"|linear_attn\.(?:in_proj_qkv|in_proj_z|in_proj_a|in_proj_b|out_proj)"
    r"|mlp\.(?:gate_proj|up_proj|down_proj))"
)


def gpu():
    import torch

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("This pilot requires a CUDA GPU with BF16 support")
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    torch.cuda.reset_peak_memory_stats()
    return torch


def base_model():
    import torch
    from transformers import Qwen3_5ForConditionalGeneration

    return Qwen3_5ForConditionalGeneration.from_pretrained(
        MODEL, revision=REVISION, dtype=torch.bfloat16, device_map={"": "cuda:0"}, attn_implementation="sdpa"
    )


def attach_lora(base):
    from peft import LoraConfig, get_peft_model

    config = LoraConfig(
        task_type="CAUSAL_LM",
        revision=REVISION,
        target_modules=LORA_TARGETS,
        r=16,
        lora_alpha=32,
        lora_dropout=0.0,
        bias="none",
    )
    model = get_peft_model(base, config)
    trainable = [name for name, p in model.named_parameters() if p.requires_grad]
    if not trainable or any(".language_model.layers." not in n or "lora_" not in n for n in trainable):
        raise ValueError("Expected trainable adapters only in the language decoder")
    return model


def encoded_training(data, budget):
    from dataset import prompt

    batches = []
    for row in data:
        messages = prompt(row, "v3")
        question = budget.ids(messages, True)
        completion = {"role": "assistant", "content": dumps({"spans": target_spans(row)})}
        tokens = budget.ids(messages + [completion], False)
        if (
            tokens[: len(question)] != question
            or len(tokens) > CONTEXT
            or not 0 < len(tokens) - len(question) <= MAX_NEW
        ):
            raise ValueError(f"{row['id']}: invalid training sequence")
        batches.append((tokens, [-100] * len(question) + tokens[len(question) :]))
    return batches


def generate(model, budget, window, device):
    import torch

    question = budget.ids(window["prompt"], True)
    if len(question) + MAX_NEW > CONTEXT:
        raise ValueError(f"{window['id']}: input exceeds generation budget")
    input_ids = torch.tensor([question], device=device)
    stop = budget.tokenizer.token_to_id("<|im_end|>")
    with torch.inference_mode():
        result = model.generate(
            input_ids=input_ids,
            attention_mask=torch.ones_like(input_ids),
            do_sample=False,
            max_new_tokens=MAX_NEW,
            eos_token_id=stop,
            pad_token_id=stop,
            use_cache=True,
        )
    tail = result[0, len(question) :].tolist()
    if stop not in tail:
        return {"id": window["id"], "error": "truncated_answer"}
    response = budget.tokenizer.decode(tail[: tail.index(stop)], skip_special_tokens=False)
    return {"id": window["id"], "response": response}


def choose(reports):
    expected = check_snapshot(ROOT / "data" / "v3")["splits"]["validation"]["sha256"]
    if len(reports) != 2 or any(
        r.get("dataset") != "v3" or r.get("split") != "validation" or r.get("source_sha256") != expected
        for r in reports
    ):
        raise ValueError("Use two reports on the same frozen v3 validation split")
    eligible = []
    for epoch, report in enumerate(reports, 1):
        o, m = report["outbound"]["all"], report["model"]
        recall = m["by_action"]["mask"]["recall"]["rate"]
        if (
            o["complete"]
            and o["restored"] == o["documents"]
            and o["public"]
            and 100 * o.get("public_touched", 0) <= MAX_PUBLIC_PERCENT * o["public"]
            and recall is not None
        ):
            eligible.append((recall, -epoch))
    return -max(eligible)[1] if eligible else None


def train(args):
    import torch
    from peft import PeftModel

    manifest = check_snapshot(args.data)
    if manifest["version"] != "v3":
        raise ValueError("The registered training corpus is v3")
    if args.output.exists():
        raise ValueError("Use a new directory for each smoke or full training run")
    budget = TokenBudget(args.assets)
    batches = encoded_training(read_jsonl(args.data / "train.jsonl"), budget)
    model = attach_lora(base_model())
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=1e-4, weight_decay=0.0)
    args.output.mkdir(parents=True)
    # Written first so an interrupted run is identifiable; checkpoint selection uses complete runs only.
    audit = {
        "stage": args.stage,
        "status": "running",
        "model": MODEL,
        "revision": REVISION,
        "training_sha256": manifest["splits"]["train"]["sha256"],
        "prompt_sha256": manifest["prompt_sha256"],
        "seed": SEED,
        "bf16": True,
        "batch": 1,
        "accumulation": 4,
        "context": CONTEXT,
        "rank": 16,
        "alpha": 32,
        "lr": 1e-4,
        "targets": LORA_TARGETS,
        "schedule": "constant",
        "gpu": torch.cuda.get_device_name(0),
        "cuda": torch.version.cuda,
    }
    path = args.output / "run.json"
    path.write_text(dumps(audit) + "\n", encoding="utf-8")
    (args.output / "packages.txt").write_bytes(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"])
    )
    steps, start = 0, monotonic()
    for epoch in range(1, 3):
        order = list(range(len(batches)))
        random.Random(SEED + epoch).shuffle(order)
        for i in range(0, len(order), 4):
            micro = order[i : i + 4]
            optimizer.zero_grad(set_to_none=True)
            for index in micro:
                tokens, labels = batches[index]
                ids = torch.tensor([tokens], device="cuda")
                output = model(
                    input_ids=ids,
                    attention_mask=torch.ones_like(ids),
                    labels=torch.tensor([labels], device="cuda"),
                    use_cache=False,
                )
                loss = output.loss
                if not math.isfinite(loss.detach().item()):
                    raise ValueError("Non-finite training loss")
                (loss / len(micro)).backward()
                del output, loss
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            steps += 1
            if args.stage == "smoke" and steps == 20:
                break
        checkpoint = args.output / ("adapter-smoke" if args.stage == "smoke" else f"epoch-{epoch}")
        model.save_pretrained(checkpoint)
        if args.stage == "smoke":
            break
    torch.cuda.synchronize()
    audit.update(
        steps=steps,
        seconds_per_step=(monotonic() - start) / steps,
        peak_allocated_bytes=torch.cuda.max_memory_allocated(),
    )
    if args.stage == "smoke":
        del model, optimizer, params
        torch.cuda.empty_cache()
        reloaded = PeftModel.from_pretrained(base_model(), checkpoint).eval()
        from dataset import prompt

        sample = read_jsonl(args.data / "validation.jsonl")[0]
        answer = generate(reloaded, budget, {"id": sample["id"], "prompt": prompt(sample, "v3")}, "cuda")
        try:
            ground(sample["text"], answer)
            audit["smoke_answer"] = "valid_grounded_json"
        except AnswerError as exc:
            audit["smoke_answer"] = str(exc)
    audit["status"] = "complete"
    path.write_text(dumps(audit) + "\n", encoding="utf-8")
    print(dumps(audit))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    training = subs.add_parser("train")
    training.add_argument("--data", default=ROOT / "data" / "v3", type=Path)
    training.add_argument("--stage", choices=("smoke", "full"), required=True)
    predicting = subs.add_parser("predict")
    predicting.add_argument("--windows", type=Path, required=True)
    predicting.add_argument("--adapter", type=Path)
    for p in [training, predicting]:
        p.add_argument("--assets", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
    selection = subs.add_parser("select")
    selection.add_argument("--epoch1", required=True, type=Path)
    selection.add_argument("--epoch2", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "select":
        print(
            dumps(
                {"selected_epoch": choose([json.loads(p.read_bytes()) for p in (args.epoch1, args.epoch2)])}
            )
        )
        return
    gpu()
    if args.command == "train":
        train(args)
        return
    if args.output.exists():
        parser.error("output already exists")
    from peft import PeftModel

    model = base_model()
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter)
    model.eval()
    budget = TokenBudget(args.assets)
    answers = [generate(model, budget, window, "cuda") for window in read_jsonl(args.windows)]
    with args.output.open("xb") as f:
        f.write(jsonl(answers))


if __name__ == "__main__":
    main()
