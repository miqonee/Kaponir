#!/usr/bin/env python3
"""Publish the document matrix separately from diagnostic probes and ablations."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from check_learning_corpus import load_corpus
from compare_local_models import ROOT
from evaluate_scheme_matrix import summarize
from evaluate_validation_schemes import atomic_json

DIAGNOSTICS = [
    'minicpm-balanced', 'minicpm-probes', 'minicpm-probes-single',
    'minicpm-probes-prompt-single', 'nanbeige-probes',
    'nanbeige-probes-prompt-batch8-timeout', 'nanbeige-probes-prompt-single',
    'granite-probes-prompt-single', 'gemma-probes-prompt-single',
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    ] if key in state}
    result.update(result_sha256=hashlib.sha256(snapshot).hexdigest(), signature=signature,
        calls=len(state.get('calls', [])), shadow=state['shadow'], rules_changed=state['rules_changed'],
        inference_errors=dict(Counter(r['error'] for r in state['responses'].values() if 'error' in r)),
        finish_reasons=dict(Counter(c.get('finish_reason', c.get('error', 'unknown')) for c in state['calls'])))
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


def build_report(directory):
    _, expected, _, sources, _ = load_corpus()
    matrix = summarize(directory)
    report = {
        'version': 2,
        'unit': matrix['unit'],
        'full_corpus_expected_values': len(expected),
        'calibration_expected_values': matrix['expected_values'],
        'holdout_expected_values': len(expected)-matrix['expected_values'],
        'source_sha256': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()},
        'annotations_sha256': digest(ROOT/'tests/docs/expected.json'),
        'probe_sha256': digest(ROOT/'tests/fixtures/validation-review-heldout.json'),
        'primary_matrix': matrix,
        'diagnostic_runs': {name: run_summary(directory/name/'result.json') for name in DIAGNOSTICS},
        'selected_holdout': run_summary(directory/'selected-holdout/result.json'),
        'context_controls': {path.parent.name:run_summary(path)
            for path in sorted((directory/'context-controls').glob('*/result.json'))},
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
                      ('classifier-audit/result', 'internal_classifier_audit')]:
        path = directory/(name+'.json')
        if path.is_file(): report[key] = json.loads(path.read_text())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT/'artifacts/validation-schemes')
    parser.add_argument('--output', type=Path, default=ROOT/'docs/VALIDATION_RESULTS.json')
    args = parser.parse_args()
    report = build_report(args.directory)
    atomic_json(args.output, report)
    print(json.dumps({model: row['status'] for model, row in report['primary_matrix']['models'].items()}))


if __name__ == '__main__':
    main()
