#!/usr/bin/env python3
"""Measure registered D/v2 after its native fresh D/v1 reference, shadow-only."""
import json
from pathlib import Path

from evaluate_privacy_expansion import parser, configure
from evaluate_privacy_policy import execute, file_sha, make_plan, save_plan
from experiment_inference import prompts_and_kinds
from privacy_expansion_profiles import ProfiledClient, research_registration
from privacy_policy_clarification import (POLICY_V2, POLICY_V2_SHA, quality_contract, verify_pair_plans)
from privacy_policy_experiment import common_batches, prompts
from privacy_policy_holdout import load_holdout, scoring_callbacks
from run_privacy_policy_suite import atomic_json, runtime_processes
from summarize_privacy_holdout import load_fresh_run
from summarize_privacy_policy import require, strict_json


def prepare(args):
    require(args.profile == 'native' and args.variants == ['D'] and args.dataset_kind == 'holdout',
            'clarification_requires_native_fresh_D')
    require(file_sha(args.policy) == POLICY_V2_SHA, 'registered_policy_v2_changed')
    # The original package and owner oracle remain v1; the explicit treatment is
    # recorded in a distinct contract, never accepted silently by the v1 reader.
    dataset = load_holdout(args.dataset)
    args.annotations_path = dataset.annotations_path
    args.quality_contract = quality_contract(dataset)
    args.extra_producer_files = ['privacy_policy_holdout.py', 'privacy_expansion_profiles.py',
        'privacy_policy_clarification.py', 'evaluate_policy_clarification.py']
    args.historical_reference = {'included_in_scored_answers': False, 'fresh_v1_reference':
                                str(args.reference_result) if args.reference_result else None}
    instructions = prompts(args.policy.read_text())
    _, _, kinds = prompts_and_kinds()
    plan = make_plan(args, dataset.cases, dataset.case_data, dataset.sources, instructions, kinds,
                     common_batches(dataset.cases, dataset.documents, args.batch_size))
    return dataset, instructions, kinds, plan


def main(argv=None):
    command = parser()
    command.description = __doc__
    command.set_defaults(policy=POLICY_V2, variants=['D'], profile='native', timeout=3600)
    command.add_argument('--reference-result', type=Path)
    args = configure(command.parse_args(argv), command)
    require(not (args.output / 'result.json').exists(), 'clarification_attempt_exists_inspect_before_resume')
    with research_registration(args.model, args.new_catalog):
        dataset, instructions, kinds, plan = prepare(args)
        args.output.mkdir(parents=True, exist_ok=True)
        if args.prepare_only:
            save_plan(args.output / 'plan.json', plan)
            print(json.dumps({'status': 'prepared', 'model': args.model, 'variants': ['D'],
                'policy_sha256': POLICY_V2_SHA, 'inference_calls': 0, 'planned_requests': len(plan['requests'])}))
            return
        require(args.reference_result is not None, 'native_fresh_v1_reference_required')
        reference = load_fresh_run(args.reference_result)
        require(reference['model'] == args.model and reference['run_status'] == 'completed'
                and next(row for row in reference['rows'] if row['variant'] == 'D')['protocol_complete'],
                'native_fresh_v1_reference_not_complete')
        baseline = strict_json(args.reference_result.with_name('plan.json').read_text())
        verified = verify_pair_plans(baseline, plan)
        binding = {'reference_result_sha256': reference['result_sha256'],
            'reference_plan_sha256': reference['plan_sha256'], 'registered_policy_sha256': POLICY_V2_SHA,
            **verified}
        atomic_json(args.output / 'policy-pair.json', binding)
        require(not runtime_processes(args.runtime), 'runtime_busy_no_parallel_policy_ablation')
        metric_function, transition_function = scoring_callbacks(dataset.expected_by_id, ['D'])
        # Check the native packet plan before sending a scored request. Both
        # executions use the same frozen code/weights/template and pinned inputs.
        requested_preflight = args.preflight_only
        args.preflight_only = True
        execute(args, plan, dataset.cases, dataset.sources, {}, instructions, kinds,
            metric_function=metric_function, documents=dataset.documents,
            transition_function=transition_function, client_factory=ProfiledClient)
        native_plan = strict_json((args.output / 'plan.json').read_text())
        binding['native_verification_before_inference'] = verify_pair_plans(baseline, native_plan)
        atomic_json(args.output / 'policy-pair.json', binding)
        if requested_preflight:
            return
        args.preflight_only = False
        execute(args, native_plan, dataset.cases, dataset.sources, {}, instructions, kinds,
            metric_function=metric_function, documents=dataset.documents,
            transition_function=transition_function, client_factory=ProfiledClient)
        binding['postcondition'] = verify_pair_plans(baseline, strict_json((args.output / 'plan.json').read_text()))
        require(file_sha(args.reference_result) == binding['reference_result_sha256'],
                'native_fresh_v1_reference_changed_during_execution')
        binding['treatment_result_sha256'] = file_sha(args.output / 'result.json')
        atomic_json(args.output / 'policy-pair.json', binding)


if __name__ == '__main__':
    main()
