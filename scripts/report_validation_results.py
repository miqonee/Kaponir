#!/usr/bin/env python3
"""Publish the document matrix separately from diagnostic probes and ablations."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from check_learning_corpus import load_corpus
from compare_local_models import ROOT
from evaluate_scheme_matrix import COHORT, METHODS, summarize, validation_quality
from evaluate_validation_schemes import atomic_json

DIAGNOSTICS = [
    'minicpm-balanced', 'minicpm-probes', 'minicpm-probes-single',
    'minicpm-probes-prompt-single', 'nanbeige-probes',
    'nanbeige-probes-prompt-batch8-timeout', 'nanbeige-probes-prompt-single',
    'granite-probes-prompt-single', 'gemma-probes-prompt-single',
]
ADDITIONAL = {
    'nanbeige42-3b': 'nanbeige-calibration',
    'granite42-3b': 'granite-calibration',
    'gemma4-e4b-qat': 'gemma-calibration',
}
CONTEXT_RUNS = [
    'qwen08-common', 'qwen08-native-document', 'qwen2-common',
    'qwen2-native-document', 'minicpm-common', 'minicpm-native-document',
    'minicpm-card-document',
    'nanbeige-card-document', 'granite-card-document', 'gemma-card-document',
    'qwen08-prompt-document', 'qwen2-prompt-document', 'minicpm-prompt-document',
    'qwen08-single-record', 'qwen2-single-record', 'minicpm-single-record',
    'qwen08-native-prompt',
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference_coverage(state, expected):
    """Coverage grouped by reference type, regardless of the model's guessed type."""
    totals=Counter(kind for kind,_ in expected.values())
    result={}
    for method in METHODS:
        stage=state.get('stages',{}).get(method)
        if stage is None:continue
        proposed=set();visible=set();confirmed=set()
        for candidate in stage['candidates']:
            key=(candidate['file'],candidate['value'])
            if key not in expected:continue
            proposed.add(key)
            row=stage['decisions'].get(candidate['id'],{})
            protected=candidate.get('protected',False)
            if protected or not row.get('valid') or row.get('verdict')!='keep':visible.add(key)
            if protected or row.get('valid') and row.get('verdict')=='mask':confirmed.add(key)
        result[method]={kind:{'expected':total,
            'proposed':sum(expected[key][0]==kind for key in proposed),
            'visible':sum(expected[key][0]==kind for key in visible),
            'confirmed_including_protection':sum(expected[key][0]==kind for key in confirmed),
            'filtered':sum(expected[key][0]==kind for key in proposed-visible)}
            for kind,total in sorted(totals.items())}
    return result


def run_summary(path):
    if not path.is_file():
        return {'status': 'not_run'}
    snapshot = path.read_bytes()
    state = json.loads(snapshot)
    signature = state['signature']
    result = {key: state[key] for key in [
        'status', 'structured_output', 'runtime_supports_json_schema', 'metrics',
        'resources_before', 'resources_after', 'error', 'runtime_exit_code',
        'cache_origin', 'active_runtime_memory_policy', 'runtime_cache_history', 'artifact',
        'thinking_template_probe', 'runtime_reasoning', 'reasoning_format',
        'runtime_restarts', 'discovery_checkpoint_policy', 'discovery_checkpoint_initial_calls',
    ] if key in state}
    result.update(result_sha256=hashlib.sha256(snapshot).hexdigest(), signature=signature,
        calls=len(state.get('calls', [])), shadow=state['shadow'], rules_changed=state['rules_changed'],
        request_errors=dict(Counter(c['error'] for c in state.get('calls',[]) if c.get('error'))),
        inference_errors=dict(Counter(r['error'] for r in state['responses'].values() if 'error' in r)),
        finish_reasons=dict(Counter(c.get('finish_reason', c.get('error', 'unknown')) for c in state['calls'])))
    if any(method in state.get('stages',{}) for method in ['point','regions','union_point','double']):
        _,expected,_,_,_=load_corpus()
        result['validation_quality']=validation_quality(state,expected)
    if state.get('control_cases'):
        from analyze_reason_limits import analyze
        try:
            diagnostics=analyze(state)
        except (ValueError,KeyError,IndexError,TypeError) as exc:
            # Optional post-hoc diagnostics must not stop the benchmark queue.
            result['reason_length_diagnostics_error']=type(exc).__name__
        else:
            if diagnostics:result['reason_length_diagnostics']=diagnostics
    if 'probes' in state:
        result['probes'] = state['probes']['summary']
        # These fixed probes have two complete passes. Preserve raw-verdict
        # diagnostics: a malformed reason can hide a semantically unsafe KEEP.
        responses = list(state['responses'].values())
        if len(responses) % 2 == 0 and 'audit' in result['probes']:
            middle = len(responses)//2
            cases = state['probes']['rows']
            known = {row['id'] for row in cases}
            maps = []
            for group in [responses[:middle], responses[middle:]]:
                rows = {}
                for response in group:
                    reply = response.get('reply', {})
                    decisions = reply.get('decisions') if isinstance(reply, dict) else None
                    for row in decisions if isinstance(decisions, list) else []:
                        if isinstance(row, dict):
                            ident = row.get('id')
                            if isinstance(ident, str) and ident in known:
                                rows[ident] = row if ident not in rows else None
                maps.append(rows)
            result['raw_verdict_diagnostics'] = {
                label: {
                    'private_kept': sum('expected_kind' in r['case'] and
                        (rows.get(r['id']) or {}).get('verdict') == 'keep' for r in cases),
                    'missing_or_unparseable': sum(not rows.get(r['id']) for r in cases),
                } for label, rows in zip(['point', 'audit'], maps)
            }
    return result


def additive_region_pool(path, expected):
    """Set union of saved proposals; no new inference or decision evaluation."""
    if not path.is_file():return {'status':'not_run'}
    state=json.loads(path.read_text());stage=state.get('stages',{}).get('regions')
    if stage is None:return {'status':'region_stage_not_completed'}
    original={(c['file'],c['value']) for c in state['candidate_pool']}
    regional={(c['file'],c['value']) for c in stage['candidates']}
    combined=original|regional
    gold={key for key in expected if key[0] in state['signature']['source_sha256']}
    return {'status':'derived_from_saved_pools','posthoc_analysis':True,'new_inference_calls':0,
        'expected':len(gold),'visible_matches':len(combined&gold),'visible_outside':len(combined-gold),
        'added_matches':len((regional-original)&gold),'added_outside':len((regional-original)-gold),
        'original_proposals_retained':True,'privacy_kind_scope_decisions_not_evaluated':True,
        'source_region_errors':sum(bool(row.get('error')) for row in state.get('regions',[])),
        'source_pools_sha256':hashlib.sha256(json.dumps([state['candidate_pool'],stage],
            sort_keys=True,ensure_ascii=False).encode()).hexdigest()}


def build_report(directory):
    _, expected, _, sources, _ = load_corpus()
    matrix = summarize(directory)
    calibration_gold={key:value for key,value in expected.items() if key[0] in matrix['documents']}
    report = {
        'version': 3,
        'unit': matrix['unit'],
        'full_corpus_expected_values': len(expected),
        'calibration_expected_values': matrix['expected_values'],
        'holdout_expected_values': len(expected)-matrix['expected_values'],
        'source_sha256': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()},
        'annotations_sha256': digest(ROOT/'tests/docs/expected.json'),
        'probe_sha256': digest(ROOT/'tests/fixtures/validation-review-heldout.json'),
        'primary_matrix': matrix,
        'primary_coverage_by_reference_kind': {
            model:reference_coverage(json.loads((directory/name/'result.json').read_text()),calibration_gold)
            for model,name in COHORT.items() if (directory/name/'result.json').is_file()},
        'diagnostic_runs': {name: run_summary(directory/name/'result.json') for name in DIAGNOSTICS},
        'additional_document_runs': {model: run_summary(directory/name/'result.json')
            for model,name in ADDITIONAL.items()},
        'derived_additive_region_pools': {model:additive_region_pool(directory/name/'result.json',expected)
            for model,name in {**COHORT,**ADDITIONAL}.items()},
        'selected_holdout': run_summary(directory/'selected-holdout/result.json'),
        'context_controls': {name:run_summary(directory/'context-controls'/name/'result.json')
            for name in sorted(set(CONTEXT_RUNS) | {
                path.parent.name for path in (directory/'context-controls').glob('*/result.json')})},
        'limitations': [
            'Synthetic documents; exact unique values per document, not actual masking leakage.',
            'Outside-gold values include incorrect span boundaries and are not all proven false positives.',
            'Detector and current_union scope are placeholders. Candidate-only confirmed fields measure the proposal pool, not validated privacy decisions.',
            'Diagnostic 20-case probes, thinking and context ablations do not replace the common document matrix.',
            'Old RAM-state cache was disabled after Qwen request 67; all comparison requests forbid prompt reuse. No speed or cold-memory ranking.',
            'No permanent rules, training labels or user vault decisions were changed by the experiments.',
        ],
    }
    for name, key in [('holdout-selection', 'holdout_selection'),
                      ('runtime-cache-diagnostic', 'runtime_cache_diagnostic'),
                      ('runtime-cache-replay', 'runtime_cache_replay'),
                      ('context-budget-probe', 'context_budget_probe'),
                      ('context-budget-probe-nanbeige', 'context_budget_probe_nanbeige'),
                      ('classifier-audit/result', 'internal_classifier_audit')]:
        path = directory/(name+'.json')
        if path.is_file(): report[key] = json.loads(path.read_text())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT/'artifacts/validation-schemes')
    parser.add_argument('--output', type=Path, default=ROOT/'docs/VALIDATION_RESULTS.json')
    parser.add_argument('--tables-output',type=Path,help='Readable tables; defaults beside the JSON report')
    args = parser.parse_args()
    report = build_report(args.directory)
    atomic_json(args.output, report)
    from render_validation_tables import write_tables
    write_tables(report,args.tables_output or args.output.with_name('VALIDATION_TABLES.md'))
    print(json.dumps({model: row['status'] for model, row in report['primary_matrix']['models'].items()}))


if __name__ == '__main__':
    main()
