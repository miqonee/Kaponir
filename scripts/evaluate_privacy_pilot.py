#!/usr/bin/env python3
"""Small, preregistered calibration pilot; never substitutes for the full study."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from check_learning_corpus import load_corpus
from compare_local_models import ROOT
from evaluate_privacy_expansion import configure, parser
from evaluate_privacy_policy import execute, file_sha, make_plan, save_plan
from experiment_inference import prompts_and_kinds
from privacy_expansion_profiles import (GEMMA_IT_CATALOG, GEMMA_IT_MODELS, NEW_CATALOG,
                                       ProfiledClient, research_registration)
from privacy_policy_experiment import common_batches, metrics, prompts
from recover_privacy_transport import wait_for_verified_model
from run_privacy_policy_suite import atomic_json, cleanup_durable, runtime_processes
from summarize_privacy_policy import (_attempts, _delivered_generation, _performance,
    _validate_source, catalog_models, digest, require, strict_json)
from summarize_privacy_recovery import rescore_entry
from validation_pipeline import Candidate


MANIFEST = ROOT / 'docs/experiments/eight-model-pilot-v1.json'
KIND = 'eight-model-pilot-v1'


def manifest(path=MANIFEST):
    path = Path(path)
    data = strict_json(path.read_text())
    require(data['version'] == 1 and data['experiment_kind'] == KIND
            and data['variants'] == ['B', 'D'] and len(data['case_ids']) == len(set(data['case_ids'])) == 8
            and data['reference_private'] == data['reference_public'] == 4
            and data['documents'] == ['01-cost-estimate.md', '02-application.log'], 'invalid_pilot_manifest')
    source = ROOT / data['source_cases']
    require(source.resolve() == (ROOT / 'tests/fixtures/context-control-candidates.json').resolve()
            and file_sha(source) == data['source_cases_sha256'], 'pilot_original_cases_changed')
    return data


def selected_data(spec):
    original = strict_json((ROOT / spec['source_cases']).read_text())
    by_id = {row['id']: row for row in original['candidates']}
    cases = [Candidate(**by_id[ident]) for ident in spec['case_ids']]
    _, expected, public, sources, _ = load_corpus()
    sources = {name: sources[name] for name in spec['documents']}
    for case in cases:
        require(case.file in sources and not case.protected
                and sources[case.file][case.start:case.end] == case.value, 'pilot_target_not_grounded')
        require((case.file, case.value) in expected or case.value in public.get(case.file, []),
                'pilot_reference_class_unknown')
    require(sum((case.file, case.value) in expected for case in cases) == 4, 'pilot_reference_balance_changed')
    require(all(sum(case.file == name and (case.file, case.value) in expected for case in cases) == 2
                and sum(case.file == name for case in cases) == 4 for name in spec['documents']),
            'pilot_document_balance_changed')
    source_hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()}
    require(all(original['source_sha256'][name] == value for name, value in source_hashes.items()),
            'pilot_documents_changed')
    case_data = {**copy.deepcopy(original), 'limit': 8, 'reference_private': 4, 'reference_public': 4,
        'candidates': [case.json() for case in cases], 'source_sha256': source_hashes,
        'selection': {'kind': KIND, 'original_case_fixture_sha256': spec['source_cases_sha256'],
                      'known_calibration_not_fresh': True}}
    return SimpleNamespace(cases=cases, case_data=case_data, sources=sources, expected=expected,
                           documents=spec['documents'])


def prepare(args, spec):
    require(args.model in spec['models'] and args.variants == spec['variants'], 'unregistered_pilot_model_or_variant')
    for attribute, key in [('output_tokens', 'output_tokens'), ('retry_output_tokens', 'retry_output_tokens'),
                           ('timeout', 'timeout_seconds')]:
        require(getattr(args, attribute) == spec[key], 'pilot_budget_changed:' + attribute)
    native_without_thinking = args.model_profile['thinking_mode'] == 'native_no_thinking'
    require(args.reasoning_budget == spec['requested_reasoning_budget']
            or (native_without_thinking and args.reasoning_budget == -1), 'pilot_reasoning_budget_changed')
    args.reasoning_budget = -1 if native_without_thinking else spec['requested_reasoning_budget']
    args.thinking_mode = ('native_no_thinking' if native_without_thinking else
                          'pilot_bounded_requested_native_support_recorded_separately')
    args.annotations_path = ROOT / 'tests/docs/expected.json'
    args.extra_producer_files = ['privacy_expansion_profiles.py', 'evaluate_privacy_expansion.py',
                                'evaluate_privacy_pilot.py', 'summarize_privacy_policy.py',
                                'summarize_privacy_recovery.py', 'recover_privacy_transport.py']
    args.historical_reference = None
    args.quality_contract = {'dataset': KIND, 'manifest_sha256': file_sha(MANIFEST),
        'reference_classes': ['mask', 'keep'], 'reference_class_counts': {'mask': 4, 'keep': 4},
        'known_calibration_not_fresh': True, 'does_not_exclude_models_from_full_study': True}
    dataset = selected_data(spec)
    instructions = prompts(args.policy.read_text())
    _, _, kinds = prompts_and_kinds()
    plan = make_plan(args, dataset.cases, dataset.case_data, dataset.sources, instructions, kinds,
                     common_batches(dataset.cases, dataset.documents, args.batch_size))
    return dataset, instructions, kinds, plan


class PilotClient(ProfiledClient):
    def __init__(self, worker, args):
        # Initialization only: do not shorten or change the measured HTTP calls.
        wait_for_verified_model(worker, args.model, timeout=600)
        super().__init__(worker, args)


def load_pilot_run(path):
    path = Path(path).resolve()
    raw = path.read_bytes();state = strict_json(raw.decode())
    plan = strict_json(path.with_name('plan.json').read_text())
    spec = manifest();dataset = selected_data(spec)
    model, ids, indexed = _validate_source(state, plan, catalog_models(),
        control_validator=lambda value: value == dataset.case_data, variants=('B', 'D'), control_count=8)
    require(model in spec['models'] and plan['signature']['quality_contract']['dataset'] == KIND
            and plan['signature']['quality_contract']['manifest_sha256'] == file_sha(MANIFEST),
            'pilot_contract_changed')
    settings = plan['signature']['settings']
    require(settings['output_tokens'] == spec['output_tokens']
            and settings['retry_output_tokens'] == spec['retry_output_tokens']
            and settings['timeout'] == spec['timeout_seconds']
            and settings['reasoning_budget'] == (-1 if model in ('lfm2.5-1.2b-instruct', 'tiny-aya-global')
                                                else spec['requested_reasoning_budget'])
            and settings['context_tokens'] == (8192 if model == 'tiny-aya-global' else 16384)
            and (settings['threads'], settings['slots'], settings['gpu_layers']) == (2, 1, 0),
            'pilot_measured_budget_changed')
    require(plan['signature']['source_sha256'] == dataset.case_data['source_sha256']
            and plan['signature']['policy_sha256'] == file_sha(ROOT / 'docs/experiments/privacy-policy-v1.txt'),
            'pilot_source_or_policy_changed')
    for name, expected in plan['signature']['code_sha256'].items():
        require(Path(name).name == name and file_sha(ROOT / 'scripts' / name) == expected,
                'pilot_reader_producer_changed:' + name)
    require(plan['signature']['kind_source_sha256'] == file_sha(ROOT / 'crates/kaponir-core/src/kinds.rs'),
            'pilot_kind_source_changed')
    entries = _attempts(state, plan, indexed)
    _, _, kinds = prompts_and_kinds()
    rows = []
    for variant in ('B', 'D'):
        decisions = {};scored = set()
        for entry in entries[variant]:
            if entry['status'] == 'pending':
                continue
            selected = indexed[(variant, entry['batch_index'])]
            checked, _ = rescore_entry(entry, selected, dataset.cases, dataset.sources, dataset.expected, kinds)
            decisions.update(checked);scored.update(entry['case_ids'])
        stored = state['stages'].get(variant, {}).get('decisions', {})
        require(stored == decisions, 'pilot_stored_decisions_mismatch')
        quality = metrics(dataset.cases, decisions, dataset.expected, variant != 'D')
        require(not stored or quality == state['metrics'][variant], 'pilot_stored_metrics_mismatch')
        received = {ident for entry in entries[variant] if _delivered_generation(entry) for ident in entry['case_ids']}
        if state['status'] == 'completed':
            require(scored == set(ids) and not any(e['status'] == 'pending' for e in entries[variant]),
                    'completed_pilot_missing_measurements')
        rows.append({'variant': variant, 'quality': quality, 'observed_generation_cases': len(received),
                     'unobserved_generation_cases': len(set(ids) - received),
                     'performance': _performance(entries[variant], state, plan, variant)})
    return {'experiment_kind': KIND, 'model': model, 'status': state['status'],
            'result_path': str(path), 'result_sha256': hashlib.sha256(raw).hexdigest(),
            'plan_sha256': plan['plan_sha256'], 'rows': rows,
            'cleanup_durable': cleanup_durable(state), 'new_inference_calls_by_reader': 0,
            'is_fresh': False, 'final_model_selection_allowed': False,
            'models_excluded_from_full_study': [], 'different_budget_from_full_study': True}


def main(argv=None):
    command = parser()
    spec = manifest()
    command.set_defaults(variants=spec['variants'], dataset_kind='calibration', output_tokens=spec['output_tokens'],
                         retry_output_tokens=spec['retry_output_tokens'], timeout=spec['timeout_seconds'],
                         reasoning_budget=spec['requested_reasoning_budget'])
    args = command.parse_args(argv)
    if args.model in GEMMA_IT_MODELS and args.new_catalog == NEW_CATALOG:
        args.new_catalog = GEMMA_IT_CATALOG
    args = configure(args, command)
    require(args.profile == 'native' and args.dataset_kind == 'calibration', 'pilot_requires_native_calibration')
    require(file_sha(args.policy) == 'a6ce52bb481ae186d81c00e1df0d1171ebac91834e16c59b45fce0e2f1d6a144',
            'pilot_policy_must_remain_v1')
    with research_registration(args.model, args.new_catalog):
        dataset, instructions, kinds, plan = prepare(args, spec)
        require(not (args.output / 'result.json').exists(), 'pilot_measurement_exists_no_automatic_replay')
        args.output.mkdir(parents=True, exist_ok=True)
        save_plan(args.output / 'plan.json', plan)
        if args.prepare_only:
            print(json.dumps({'status': 'prepared', 'cases': 8, 'documents': 2, 'variants': ['B', 'D'],
                'planned_requests': len(plan['requests']), 'new_inference_calls': 0,
                'plan_sha256': plan['plan_sha256']}))
            return
        require(not runtime_processes(args.runtime), 'runtime_busy_no_parallel_inference')
        execute(args, plan, dataset.cases, dataset.sources, dataset.expected, instructions, kinds,
                documents=dataset.documents, client_factory=PilotClient)
        if not args.preflight_only:
            report = load_pilot_run(args.output / 'result.json')
            atomic_json(args.output / 'pilot-summary.json', report)
            print(json.dumps({'status': report['status'], 'model': report['model'], 'rows': report['rows']}))


if __name__ == '__main__':
    main()
