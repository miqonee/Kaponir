#!/usr/bin/env python3
"""Prepare or measure fresh controls with explicit native/common profiles for13models.

New model tensors are admitted only in this standalone process, after pinning.
Actual generation verifies size/SHA and refuses to overlap a running benchmark.
"""
import argparse
import json
import math
from pathlib import Path
from types import SimpleNamespace

from check_learning_corpus import load_corpus
from compare_local_models import ROOT, DOCUMENTS
from evaluate_context_controls import prepare_cases
from evaluate_privacy_holdout import PROFILE_FIELDS, prepare_plan
from evaluate_privacy_policy import execute, save_plan
from experiment_inference import sampling_settings
from privacy_expansion_profiles import (NEW_CATALOG, ProfiledClient, expansion_profile,
                                        model_ids, research_registration)
from privacy_policy_experiment import VARIANTS, strict_json
from privacy_policy_holdout import DEFAULT_DATASET, scoring_callbacks
from run_privacy_policy_suite import runtime_processes


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument('--model', required=True)
    command.add_argument('--profile', choices=['native', 'common'], default='native')
    command.add_argument('--new-catalog', type=Path, default=NEW_CATALOG)
    command.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    command.add_argument('--dataset-kind', choices=['calibration', 'holdout'], default='holdout')
    command.add_argument('--calibration-cases', type=Path,
                         default=ROOT / 'tests/fixtures/context-control-candidates.json')
    command.add_argument('--policy', type=Path, default=ROOT / 'docs/experiments/privacy-policy-v1.txt')
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--model-dir', type=Path)
    command.add_argument('--runtime', type=Path)
    command.add_argument('--variants', nargs='+', choices=VARIANTS, default=list(VARIANTS))
    modes = command.add_mutually_exclusive_group()
    modes.add_argument('--prepare-only', action='store_true')
    modes.add_argument('--preflight-only', action='store_true')
    for name in ('context_tokens', 'output_tokens', 'retry_output_tokens', 'reserve_tokens',
                 'reasoning_budget', 'batch_size', 'timeout', 'top_k', 'seed'):
        command.add_argument('--' + name.replace('_', '-'), type=int)
    for name in ('temperature', 'top_p', 'min_p'):
        command.add_argument('--' + name.replace('_', '-'), type=float)
    command.add_argument('--response-format', choices=['grammar', 'prompt'])
    return command


def configure(args, command):
    if args.model not in model_ids(args.new_catalog):
        command.error('model is outside the explicitly selected pinned matrix')
    profile = expansion_profile(args.model, args.profile, args.new_catalog)
    for name in PROFILE_FIELDS:
        if getattr(args, name) is None:
            setattr(args, name, profile[name])
    args.expected_runtime_sha256 = profile['runtime_sha256']
    args.template_kwargs = dict(profile['template_kwargs'])
    args.extra_sampling = dict(profile['extra_sampling'])
    args.thinking_mode = profile['thinking_mode']
    args.model_profile = profile
    if (args.output_tokens <= 0 or args.context_tokens <= args.output_tokens + args.reserve_tokens
            or args.retry_output_tokens < args.output_tokens or args.reserve_tokens < 0
            or not 1 <= args.batch_size <= 64 or args.timeout <= 0
            or not -1 <= args.reasoning_budget <= args.context_tokens
            or len(set(args.variants)) != len(args.variants)):
        command.error('invalid fixed experiment parameters')
    sampling_settings({key: getattr(args, key) for key in
                       ('temperature', 'top_p', 'top_k', 'min_p', 'seed')})
    if not args.prepare_only and (args.model_dir is None or args.runtime is None):
        command.error('native execution requires --model-dir and --runtime')
    if (args.prepare_only or args.preflight_only) and (args.output / 'result.json').exists():
        command.error('preparation must not overwrite measured results')
    return args


def prepare_calibration(args):
    from experiment_inference import prompts_and_kinds
    from evaluate_privacy_policy import make_plan
    from privacy_policy_experiment import common_batches, prompts
    from evaluate_privacy_policy import file_sha
    if not args.calibration_cases.is_file():
        raise ValueError('frozen_calibration_cases_required')
    _, expected, public, all_sources, _ = load_corpus()
    sources = {name: all_sources[name] for name in DOCUMENTS}
    cases, data = prepare_cases(args.calibration_cases, sources, expected, public, 32)
    if (len(cases) != 32 or data['reference_private'] != 16 or data['reference_public'] != 16
            or any(case.protected for case in cases)):
        raise ValueError('frozen_calibration_cases_changed')
    if file_sha(args.policy) != 'a6ce52bb481ae186d81c00e1df0d1171ebac91834e16c59b45fce0e2f1d6a144':
        raise ValueError('frozen_calibration_policy_changed')
    args.annotations_path = ROOT / 'tests/docs/expected.json'
    args.extra_producer_files = []
    args.quality_contract = {'dataset': 'calibration-controls-v1', 'reference_classes': ['mask', 'keep']}
    args.historical_reference = args.model_profile['historical_reference']
    instructions = prompts(args.policy.read_text())
    _, _, kinds = prompts_and_kinds()
    dataset = SimpleNamespace(cases=cases, sources=sources, documents=DOCUMENTS,
                              expected=expected, case_data=data)
    plan = make_plan(args, cases, data, sources, instructions, kinds,
                     common_batches(cases, DOCUMENTS, args.batch_size))
    return dataset, instructions, kinds, plan


def main(argv=None):
    command = parser()
    args = configure(command.parse_args(argv), command)
    with research_registration(args.model, args.new_catalog):
        dataset, instructions, kinds, _ = (prepare_calibration(args)
            if args.dataset_kind == 'calibration' else prepare_plan(args))
        args.extra_producer_files += ['privacy_expansion_profiles.py', 'evaluate_privacy_expansion.py']
        from evaluate_privacy_policy import make_plan
        from privacy_policy_experiment import common_batches
        plan = make_plan(args, dataset.cases, dataset.case_data, dataset.sources,
                         instructions, kinds, common_batches(dataset.cases, dataset.documents, args.batch_size))
        args.output.mkdir(parents=True, exist_ok=True)
        destination = args.output / 'plan.json'
        if args.prepare_only:
            save_plan(destination, plan)
            print(json.dumps({'status': 'prepared', 'model': args.model,
                'phase': args.dataset_kind, 'profile': args.profile, 'variants': args.variants,
                'context_tokens': args.context_tokens, 'thinking_requested': args.thinking_mode,
                'planned_requests': len(plan['requests']), 'inference_calls': 0,
                'native_capabilities_verified': False, 'plan_sha256': plan['plan_sha256']}))
            return
        if runtime_processes(args.runtime):
            raise ValueError('another_benchmark_runtime_is_active')
        if destination.exists() and strict_json(destination.read_text())['signature'] != plan['signature']:
            raise ValueError('prepared_profile_changed_use_new_output')
        if not destination.exists():
            save_plan(destination, plan)
        if args.dataset_kind == 'calibration':
            execute(args, plan, dataset.cases, dataset.sources, dataset.expected, instructions, kinds,
                    documents=dataset.documents, client_factory=ProfiledClient)
        else:
            metric_function, transition_function = scoring_callbacks(dataset.expected_by_id, args.variants)
            execute(args, plan, dataset.cases, dataset.sources, {}, instructions, kinds,
                metric_function=metric_function, documents=dataset.documents,
                transition_function=transition_function, client_factory=ProfiledClient)


if __name__ == '__main__':
    main()
