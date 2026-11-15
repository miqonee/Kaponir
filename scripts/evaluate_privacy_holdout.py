#!/usr/bin/env python3
"""Prepare, preflight or measure frozen fresh three-way disclosure controls.

All A/B/C/D variants use the established prompt/schema/validator and CPU telemetry.
Gold verdicts stay in evaluator callbacks and never enter a model request.
Preparation requires no runtime, weights, network, or inference.
"""
import argparse
import json
from pathlib import Path
import re

from compare_local_models import ROOT
from evaluate_privacy_policy import (RUNTIME_SHA256, execute, file_sha,
                                     make_plan, save_plan)
from experiment_inference import prompts_and_kinds, sampling_settings
from kaponir_worker import FILES
from privacy_policy_experiment import VARIANTS, common_batches, prompts, strict_json
from privacy_policy_holdout import DEFAULT_DATASET, load_holdout, scoring_callbacks
from privacy_policy_profiles import profile as declared_profile


PROFILE_FIELDS = ('context_tokens', 'output_tokens', 'retry_output_tokens', 'reserve_tokens',
                  'reasoning_budget', 'batch_size', 'timeout', 'response_format',
                  'temperature', 'top_p', 'top_k', 'min_p', 'seed')


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    modes = command.add_mutually_exclusive_group()
    modes.add_argument('--prepare-only', action='store_true')
    modes.add_argument('--preflight-only', action='store_true')
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    command.add_argument('--policy', type=Path, default=ROOT / 'docs/experiments/privacy-policy-v1.txt')
    command.add_argument('--model', choices=sorted(FILES), default='gemma4-e4b-qat')
    command.add_argument('--model-dir', type=Path)
    command.add_argument('--runtime', type=Path)
    command.add_argument('--variants', nargs='+', choices=VARIANTS, default=list(VARIANTS))
    command.add_argument('--expected-runtime-sha256')
    for name in ('context_tokens', 'output_tokens', 'retry_output_tokens', 'reserve_tokens',
                 'reasoning_budget', 'batch_size', 'timeout', 'top_k', 'seed'):
        command.add_argument('--' + name.replace('_', '-'), type=int)
    for name in ('temperature', 'top_p', 'min_p'):
        command.add_argument('--' + name.replace('_', '-'), type=float)
    command.add_argument('--response-format', choices=['grammar', 'prompt'])
    return command


def configure(args, command):
    """Use declared per-model defaults, retaining explicit parameter overrides."""
    try:
        profile = declared_profile(args.model)
    except ValueError as exc:
        if str(exc) != 'unknown_phase2_model':
            raise
        command.error('no declared profile for this model; add an explicit reviewed phase2 profile first')
    for name in PROFILE_FIELDS:
        if getattr(args, name) is None:
            setattr(args, name, profile[name])
    if args.expected_runtime_sha256 is None:
        args.expected_runtime_sha256 = profile.get('runtime_sha256', RUNTIME_SHA256)
    if (not 1 <= args.batch_size <= 64 or args.output_tokens <= 0
            or args.context_tokens <= args.output_tokens + args.reserve_tokens
            or args.retry_output_tokens < args.output_tokens or args.reserve_tokens < 0
            or not 0 <= args.reasoning_budget <= args.context_tokens or args.timeout <= 0
            or not re.fullmatch(r'[a-f0-9]{64}', args.expected_runtime_sha256)):
        command.error('invalid experiment budget or runtime checksum')
    if len(set(args.variants)) != len(args.variants):
        command.error('variant selection must be unique')
    sampling_settings({key: getattr(args, key) for key in
                       ('temperature', 'top_p', 'top_k', 'min_p', 'seed')})
    if not args.prepare_only and (args.model_dir is None or args.runtime is None):
        command.error('generation/preflight requires --model-dir and --runtime')
    if (args.prepare_only or args.preflight_only) and (args.output / 'result.json').exists():
        command.error('preparation/preflight requires an output directory without measured results')
    return args


def prepare_plan(args):
    holdout = load_holdout(args.dataset, policy_path=args.policy)
    policy = args.policy.read_text(encoding='utf-8')
    if not policy.strip() or file_sha(args.policy) != holdout.policy_sha256:
        raise ValueError('holdout_policy_checksum_mismatch')
    instructions = prompts(policy)
    _, _, kinds = prompts_and_kinds()
    args.annotations_path = holdout.annotations_path
    args.extra_producer_files = ['privacy_policy_holdout.py', 'evaluate_privacy_holdout.py']
    args.quality_contract = dict(holdout.quality_contract)
    args.historical_reference = None  # Calibration answers are not a fresh-holdout baseline.
    batches = common_batches(holdout.cases, holdout.documents, args.batch_size)
    plan = make_plan(args, holdout.cases, holdout.case_data, holdout.sources,
                     instructions, kinds, batches)
    return holdout, instructions, kinds, plan


def main(argv=None):
    command = parser()
    args = configure(command.parse_args(argv), command)
    holdout, instructions, kinds, plan = prepare_plan(args)
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / 'plan.json'
    if args.prepare_only:
        save_plan(destination, plan)
        print(json.dumps({'status': 'prepared', 'phase': 'fresh_holdout',
            'dataset_id': holdout.quality_contract['dataset_id'],
            'plan_sha256': plan['plan_sha256'], 'cases': len(holdout.cases),
            'reference_class_counts': holdout.quality_contract['reference_class_counts'],
            'variants': list(args.variants), 'batches_per_variant': len(plan['batches']),
            'planned_requests': len(plan['requests']), 'inference_calls': 0,
            'token_budget_validation': 'pending_runtime'}))
        return
    if destination.exists() and strict_json(destination.read_text())['signature'] != plan['signature']:
        raise ValueError('prepared_profile_changed_use_new_output')
    metric_function, transition_function = scoring_callbacks(holdout.expected_by_id, args.variants)
    # The shared binary oracle argument is deliberately empty: neither the native
    # request builder nor shared binary transition logic receives this oracle.
    execute(args, plan, holdout.cases, holdout.sources, {}, instructions, kinds,
            metric_function=metric_function, documents=holdout.documents,
            transition_function=transition_function)


if __name__ == '__main__':
    main()
