#!/usr/bin/env python3
"""Queue a full-document Tiny Aya A/B/C/D rerun with a smaller output reserve."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from apply_measured_privacy import run as apply_abcd
from evaluate_privacy_expansion import configure, parser, prepare_calibration
from evaluate_privacy_policy import execute, make_plan, save_plan
from native_model_profiles import catalog_for
from privacy_expansion_profiles import ProfiledClient, research_registration
from privacy_policy_experiment import VARIANTS, common_batches
from run_complete_research import terminal_result
from run_native_model_retest import DEFAULT_STUDY, available, calibration_option
from run_privacy_policy_suite import SuiteBlocked, SuiteLock, atomic_json, file_sha, runtime_processes
from summarize_privacy_policy import require, strict_json


MODEL = 'tiny-aya-global'
OUTPUT_TOKENS = 2048
SUPPLEMENT = Path('context-supplements')
ABCD = Path('abcd') / MODEL
RECEIPT = SUPPLEMENT / ABCD / 'receipt.json'
TERMINAL = ('completed', 'completed_with_failures')


def original(study):
    state = strict_json((study / 'status.json').read_text())
    failed = state['jobs'][ABCD.as_posix()]
    directory = study / ABCD
    observations = directory / 'native-budget-observations.json'
    budget = strict_json(observations.read_text())
    require(failed['status'] == 'unavailable_context' and failed['measurement_requests'] == 0
            and failed['budget_observations_sha256'] == file_sha(observations)
            and budget['error'] == 'single_case_context_budget_exceeded'
            and budget['measurement_request_slots_recorded'] == 0
            and budget['whole_document_not_truncated'] is True
            and not (directory / 'result.json').exists(),
            'tiny_aya_supplement_requires_unmeasured_context_limit')
    plan = strict_json((directory / 'plan.json').read_text())
    settings = plan['signature']['settings']
    require(settings['context_tokens'] == budget['context_tokens'] == 8192
            and settings['output_tokens'] == budget['output_tokens'] == 4096
            and settings['reserve_tokens'] == budget['reserve_tokens'] == 64,
            'tiny_aya_original_budget_changed')
    provenance = {
        'original_plan_sha256': plan['plan_sha256'],
        'original_plan_file_sha256': file_sha(directory / 'plan.json'),
        'original_observations_sha256': file_sha(observations),
        'original_output_tokens': settings['output_tokens'],
        'output_tokens': OUTPUT_TOKENS,
        'whole_documents_preserved': True,
    }
    return plan, provenance


def prepare(study, original_plan, provenance):
    profile = calibration_option(study / 'calibration' / MODEL, MODEL)
    require(profile is not None, 'tiny_aya_supplement_requires_calibrated_profile')
    command = parser()
    args = configure(command.parse_args([
        '--model', MODEL, '--profile', 'native', '--native-profile', str(profile),
        '--new-catalog', str(catalog_for(MODEL)), '--dataset-kind', 'calibration',
        '--model-dir', str(study / 'models'),
        '--runtime', str(study / 'runtime/llama-b12038/llama-server'),
        '--output', str(study / SUPPLEMENT / ABCD),
    ]), command)
    args.output_tokens = OUTPUT_TOKENS
    dataset, instructions, kinds, _ = prepare_calibration(args)
    args.quality_contract['context_supplement'] = provenance
    args.extra_producer_files += ['privacy_expansion_profiles.py', 'evaluate_privacy_expansion.py',
                                 'native_model_profiles.py', Path(__file__).name]
    plan = make_plan(args, dataset.cases, dataset.case_data, dataset.sources, instructions, kinds,
                     common_batches(dataset.cases, dataset.documents, args.batch_size))
    signature = plan['signature']
    prior = original_plan['signature']
    require(signature['settings'] == {**prior['settings'], 'output_tokens': OUTPUT_TOKENS}
            and signature['quality_contract'] == {**prior['quality_contract'], 'context_supplement': provenance}
            and all(signature[key] == prior[key] for key in (
                'model_profile_source', 'model_artifact', 'source_sha256', 'case_data_sha256',
                'annotations_sha256', 'policy_sha256', 'kinds_sha256', 'kind_source_sha256'))
            and all(signature['code_sha256'].get(key) == value for key, value in prior['code_sha256'].items()),
            'tiny_aya_supplement_changes_more_than_response_budget')
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / 'plan.json'
    if not path.is_file():
        save_plan(path, plan)
    else:
        require(strict_json(path.read_text())['signature'] == signature, 'tiny_aya_supplement_plan_changed')
    return args, dataset, instructions, kinds, plan


def run(study, state, prepared, provenance):
    args, dataset, instructions, kinds, plan = prepared
    binary = study / 'build/release/kaponir'
    require(file_sha(binary) == state['binary_sha256'], 'tiny_aya_supplement_binary_changed')
    require(not runtime_processes(args.runtime), 'tiny_aya_supplement_runtime_busy')
    job = {'output': args.output, 'kind': 'privacy', 'dataset': 'calibration', 'profile': 'native', 'model': MODEL}
    receipt = {'provenance': provenance, 'applications': {}}
    path = study / RECEIPT
    source = args.output / 'result.json'
    if not source.is_file() or not available(terminal_result(job)):
        atomic_json(path, {**receipt, 'status': 'running'})
        try:
            execute(args, plan, dataset.cases, dataset.sources, dataset.expected, instructions, kinds,
                    documents=dataset.documents, client_factory=ProfiledClient)
        except (OSError, ValueError):
            atomic_json(path, {**receipt, **terminal_result(job)})
            raise
    require(not runtime_processes(args.runtime), 'tiny_aya_supplement_runtime_cleanup_missing')
    receipt.update(terminal_result(job), plan_file_sha256=file_sha(args.output / 'plan.json'))
    atomic_json(path, receipt)
    if available(receipt):
        for variant in VARIANTS:
            directory = study / SUPPLEMENT / 'application' / ABCD / variant
            if not directory.exists():
                directory.parent.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(prefix='.application-', dir=directory.parent) as temporary:
                    staged = Path(temporary) / 'evidence'
                    apply_abcd(source, binary, staged, variant)
                    staged.rename(directory)
            result = directory / 'result.json'
            application = strict_json(result.read_text())
            require(application['status'] == 'completed' and application['exact_roundtrips'] == 6
                    and application['source_result_sha256'] == receipt['result_sha256']
                    and application['binary_sha256'] == state['binary_sha256'],
                    'tiny_aya_supplement_application_binding_changed')
            receipt['applications'][variant] = file_sha(result)
            atomic_json(path, receipt)
    print(receipt['status'], 'applications', len(receipt['applications']), flush=True)


def wait_for_queue(study, prepared, provenance):
    print('Tiny Aya context supplement waiting for the sequential queue.', flush=True)
    while True:
        state = strict_json((study / 'status.json').read_text())
        if state['status'] in TERMINAL:
            try:
                with SuiteLock(study / '.native-retest.lock'):
                    run(study, state, prepared, provenance)
                    return
            except SuiteBlocked as error:
                if str(error) != 'suite_controller_already_running':
                    raise
        time.sleep(30)


def main():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument('--study', type=Path, default=DEFAULT_STUDY)
    command.add_argument('--prepare-only', action='store_true')
    args = command.parse_args()
    study = args.study.resolve()
    with (SuiteLock(study / '.native-context-supplement.lock'),
          research_registration(MODEL, catalog_for(MODEL))):
        plan, provenance = original(study)
        prepared = prepare(study, plan, provenance)
        receipt = study / RECEIPT
        if not receipt.is_file():
            atomic_json(receipt, {'status': 'prepared', 'provenance': provenance, 'applications': {}})
        if args.prepare_only:
            print('Tiny Aya context supplement prepared. Inference calls: 0.', flush=True)
        else:
            wait_for_queue(study, prepared, provenance)


if __name__ == '__main__':
    main()
