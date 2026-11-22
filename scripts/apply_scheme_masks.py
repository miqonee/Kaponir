#!/usr/bin/env python3
"""Verify saved six-scheme selections with real mask/restore, without inference.

All selected MASK values are assumed explicitly confirmed for this document.
Protected detector candidates remain selected even when their model reply fails.
No KEEP exceptions, predicted types/scopes or persistent user rules are applied.
The annotations score application only; they never select values.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

from apply_measured_privacy import apply_documents
from check_learning_corpus import load_corpus
from evaluate_model_schemes import METHODS
from report_complete_research import legacy_summary
from run_privacy_policy_suite import file_sha
from summarize_privacy_policy import digest, require, strict_json


def select_masks(stage, sources):
    selected, seen, protected = {}, set(), set()
    for candidate in stage['candidates']:
        name, value = candidate['file'], candidate['value']
        start, end = candidate['start'], candidate['end']
        require(name in sources and value and sources[name][start:end] == value,
                'application_target_not_grounded')
        decision = stage['decisions'].get(candidate['id'], {})
        guarded = candidate.get('protected') is True
        if guarded or decision.get('valid') is True and decision.get('verdict') == 'mask':
            key = (name, value)
            if key not in seen:
                selected.setdefault(name, []).append({'value': value, 'kind': 'CUSTOM'})
                seen.add(key)
            if guarded:
                protected.add(key)
    return selected, len(protected)


def complete_discovery_snapshot(state, plan, journal):
    """Verify a finished adding-search stage in a still-open native experiment."""
    require(plan['profile'].get('native_retest') and 'current_union' in state['stages'],
            'native_discovery_stage_not_available')
    require(journal['model'] == plan['model'] and journal['profile'] == plan['profile'],
            'native_discovery_journal_profile_changed')
    requests = journal['requests']
    for file in plan['source_sha256']:
        stage = f'discovery:{file}'
        outcome = state['stages'].get(stage, {})
        entries = [row for row in requests if row['record']['measurement_stage'] == stage]
        require(outcome.get('errors') == [] and entries and len(entries) == outcome.get('calls')
                and all(row['status'] == 'received' and not row.get('parse_error')
                        and row['record']['finish_reason'] == 'stop'
                        and row['request_sha256'] == digest(row['request']) for row in entries),
                'native_discovery_stage_incomplete')


def run_scheme(result, binary, output, method, archived=False, *, discovery_snapshot=False):
    result, binary, output = Path(result).resolve(), Path(binary).resolve(), Path(output).resolve()
    require(not output.exists(), 'application_output_exists_no_overwrite')
    require(method in METHODS, 'unknown_six_scheme_method')
    verified = legacy_summary(result, archived=archived)
    if discovery_snapshot:
        require(method == 'current_union' and not archived, 'native_discovery_only_snapshot')
    else:
        require(verified['status'] == 'completed' and len(verified['rows']) == len(METHODS),
                'completed_measured_scheme_required')
    raw = gzip.decompress(result.read_bytes()) if archived else result.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == verified['result_sha256'], 'application_result_changed')
    state = strict_json(raw.decode())
    if discovery_snapshot:
        complete_discovery_snapshot(state, strict_json(result.with_name('scheme-plan.json').read_text()),
                                    strict_json(result.with_name('transports.json').read_text()))
    elif not archived:
        require(state.get('research_execution', {}).get('runtime_cleanup_verified') is True,
                'scheme_cleanup_not_durable')
    _, expected, public, sources, _ = load_corpus()
    private = {}
    for name, value in expected:
        private.setdefault(name, []).append(value)
    selected, protected = select_masks(state['stages'][method], sources)
    row = next(row for row in verified['rows'] if row['method'] == method)
    metadata = {'model': verified['model'], 'variant': method,
        'application_selection_producer_sha256': file_sha(Path(__file__)),
        'source_phase': 'six_scheme_calibration', 'source_settings': verified['settings'],
        'source_model_artifact': state['signature']['artifact'],
        'source_result_sha256': verified['result_sha256'], 'source_file_sha256': file_sha(result),
        'archived_measurement': archived, 'new_model_measurement': False,
        'source_scheme_plan_sha256': None if archived else state['signature']['experiment_lineage']['plan_sha256'],
        'source_prompt_sha256': state['signature'].get('prompts'),
        'scheme': 'existing detectors plus explicit valid stage MASK and protected values; no automatic KEEP exceptions',
        'protected_mask_values_selected': protected,
        'detector_only_proposal_stage': method == 'detector_proposals',
        'oracle_scope': 'all private value annotations and known public controls in the three calibration source documents',
        'measured_decision_quality': row['quality']}
    if discovery_snapshot:
        metadata['completed_scope'] = 'current_union'
    return apply_documents(binary, output, state['signature']['source_sha256'],
                           sources, private, public, selected, metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--method', choices=METHODS, required=True)
    parser.add_argument('--archived', action='store_true')
    args = parser.parse_args()
    report = run_scheme(args.result, args.binary, args.output, args.method, args.archived)
    print(json.dumps({key: report[key] for key in ('status', 'model', 'variant',
        'new_inference_calls', 'exact_roundtrips', 'user_rules_changed')}))


if __name__ == '__main__':
    main()
