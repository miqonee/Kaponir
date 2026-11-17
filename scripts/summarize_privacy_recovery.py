#!/usr/bin/env python3
"""Read a transport supplement without changing original outcomes or running models.

The effective view replaces only definite missing-response timeouts selected by
the frozen recovery plan. Original attempts and their resource cost stay visible.
Received bad types, truncated finals and uncertain sends are never rehabilitated.
"""
import base64
import copy
import hashlib
from pathlib import Path

from check_learning_corpus import load_corpus
from compare_local_models import ROOT
from evaluate_privacy_policy import parse_response
from experiment_inference import prompts_and_kinds
from privacy_policy_experiment import metrics, payload, technical_review, validate
from recover_privacy_transport import selected_timeouts
from summarize_privacy_policy import (VARIANTS, _attempts, _performance, _quality_row,
    compare_quality, digest, load_run, require, strict_json)
from validation_pipeline import Candidate


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rescore_entry(entry, selected, cases, sources, expected, kinds):
    """Validate transport bytes again; cached final JSON is never the authority."""
    require(entry['status'] != 'pending', 'pending_supplement_has_no_decision')
    derived = {'error': entry.get('error')} if entry['status'] != 'received' else {}
    if entry['status'] == 'received':
        transport = entry['raw_transport']
        try:
            derived['raw_reply'] = strict_json(base64.b64decode(
                transport['body_base64'], validate=True).decode('utf-8'))
            if transport['status'] != 200:
                derived['error'] = 'runtime_http_' + str(transport['status'])
        except (ValueError, TypeError, UnicodeError):
            derived['error'] = 'invalid_runtime_json'
        if 'raw_reply' in entry:
            require(entry['raw_reply'] == derived.get('raw_reply'), 'cached_supplement_reply_mismatch')
    reply, error = parse_response(derived)
    indexed = {case.id: case for case in cases}
    batch = [indexed[ident] for ident in selected['case_ids']]
    _, contexts = payload(selected['variant'], batch, sources)
    decisions = ({case.id: technical_review(case, error) for case in batch} if error else
                 validate(reply, batch, contexts, kinds, selected['variant']))
    return decisions, metrics(batch, decisions, expected, selected['variant'] != 'D')


def load_supplement(path, original_path=None):
    path = Path(path).resolve()
    raw = path.read_bytes()
    state = strict_json(raw.decode('utf-8'))
    plan_path = path.with_name('recovery-plan.json')
    plan = strict_json(plan_path.read_text())
    require(plan.get('experiment_kind') == state.get('experiment_kind') == 'transport-recovery-v1'
            and plan.get('version') == state.get('version') == 1, 'not_transport_supplement')
    require(digest({k: v for k, v in plan.items() if k != 'recovery_plan_sha256'})
            == plan.get('recovery_plan_sha256') == state.get('recovery_plan_sha256'),
            'supplement_plan_hash_mismatch')
    require(all(v.get('shadow') is True and v.get('rules_changed') == 0 for v in (plan, state))
            and plan.get('inference_request_parameters_changed') is False,
            'not_unchanged_shadow_supplement')
    source = Path(original_path or plan['original_result_path'])
    original = load_run(source)
    source_plan = source.with_name('plan.json')
    require(sha(source) == plan['original_result_sha256']
            and sha(source_plan) == plan['original_plan_file_sha256'], 'supplement_original_changed')
    original_state = strict_json(source.read_text())
    original_plan = strict_json(source_plan.read_text())
    require(original['run_status'] == 'completed'
            and original_plan['plan_sha256'] == plan['original_plan_sha256']
            and original_plan['signature'] == plan['original_signature']
            and original_plan['control_cases'] == plan['original_control_cases']
            and original_plan['native_template_probe'] == plan['original_template_probe']
            and original_plan['adjacent_runtime_libraries_sha256'] == plan['original_runtime_libraries'],
            'supplement_original_provenance_mismatch')
    settings = original_plan['signature']['settings']
    require(plan['client_timeout_seconds'] > settings['timeout']
            == plan['worker_configuration_timeout_seconds'], 'supplement_timeout_contract_changed')
    require(selected_timeouts(original_state, original_plan) == plan['requests'],
            'supplement_selection_not_definite_timeouts')
    require(state.get('status') != 'completed' or state.get('original_files_unchanged') is True,
            'supplement_original_preservation_not_confirmed')
    if 'native_template_probe' in state:
        require(state['native_template_probe'] == plan['original_template_probe'], 'supplement_template_changed')
    # These pure validators must still implement the original measured contract.
    for name in ('privacy_policy_experiment.py', 'validation_pipeline.py'):
        require(sha(ROOT / 'scripts' / name) == original_plan['signature']['code_sha256'][name],
                'supplement_validator_version_changed')
    require(sha(ROOT / 'crates/kaponir-core/src/kinds.rs')
            == original_plan['signature']['kind_source_sha256'], 'supplement_kind_vocabulary_changed')
    _, expected, _, sources, _ = load_corpus()
    require(all(hashlib.sha256(sources[name].encode()).hexdigest() == value
                for name, value in original_plan['signature']['source_sha256'].items()),
            'supplement_documents_changed')
    cases = [Candidate(**row) for row in plan['original_control_cases']['candidates']]
    require(all(sources[case.file][case.start:case.end] == case.value for case in cases),
            'supplement_case_not_grounded')
    _, _, kinds = prompts_and_kinds()
    indexed = {(row['variant'], row['batch_index']): row for row in original_plan['requests']}
    entries = _attempts(state, original_plan, indexed)
    selected = {(row['variant'], row['batch_index']): row for row in plan['requests']}
    actual = {}
    for group in entries.values():
        for entry in group:
            key = (entry['variant'], entry['batch_index'])
            require(key in selected and key not in actual
                    and entry['request'] == selected[key]['request'], 'unplanned_supplement_attempt')
            actual[key] = entry
    effective = copy.deepcopy(original_state['stages'])
    batch_rows, scored = [], set()
    for batch in state['batches']:
        key = (batch['variant'], batch['batch_index'])
        require(key in actual and key not in scored
                and batch['original_response_key'] == selected[key]['original_response_key']
                and batch['request_sha256'] == selected[key]['request_sha256'],
                'unregistered_or_duplicate_supplement_batch')
        decisions, quality = rescore_entry(actual[key], selected[key], cases, sources, expected, kinds)
        require(decisions == batch['decisions'] and quality == batch['metrics_selected_cases_only'],
                'supplement_stored_decisions_or_metrics_mismatch')
        effective.setdefault(key[0], {'decisions': {}})['decisions'].update(decisions)
        scored.add(key)
        batch_rows.append({'variant': key[0], 'batch_index': key[1], 'case_ids': selected[key]['case_ids'],
                           'quality_selected_cases_only': quality})
    if state['status'] == 'completed':
        require(scored == set(selected) == set(actual), 'completed_supplement_missing_batches')
    original_entries = _attempts(original_state, original_plan, indexed)
    labels = {row['id']: row['reference_private'] for row in original_state['case_transitions']}
    ids = [case.id for case in cases]
    run_id = digest(str(path))[:12]
    rows = []
    sessions = {'execution_sessions': original_state.get('execution_sessions', []) + [{
        'runtime_starts': state.get('runtime_starts', []),
        'sampled_resources': state.get('sampled_resources', {}),
        **({'execution_wall_seconds': state['execution_wall_seconds']} if 'execution_wall_seconds' in state else {})}]}
    for variant in VARIANTS:
        combined_entries = original_entries[variant] + entries[variant]
        row = _quality_row(variant, ids, labels, effective.get(variant, {}).get('decisions', {}), combined_entries)
        row.update(row_id=run_id + ':' + variant, model=original['model'],
            run_finalized=state['status'] == 'completed',
            performance=_performance(combined_entries, sessions, original_plan, variant))
        rows.append(row)
    return {'model': original['model'], 'status': state['status'], 'result_path': str(path),
        'result_sha256': hashlib.sha256(raw).hexdigest(), 'recovery_plan_file_sha256': sha(plan_path),
        'recovery_plan_sha256': plan['recovery_plan_sha256'],
        'original_result_path': str(source), 'original_result_sha256': sha(source),
        'original_plan_sha256': original['plan_sha256'], 'original_rows': original['rows'],
        'supplement_batches': batch_rows, 'effective_rows': rows, 'effective_stages': effective,
        'effective_comparison': compare_quality(rows), 'automatic_production_approval': False,
        'cost_includes_original_failures': True, 'reinterpreted_received_failures': 0,
        'scope': 'original 32 calibration controls plus separately observed transport supplement'}
