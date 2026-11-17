#!/usr/bin/env python3
"""Apply completed model mask decisions through the real document-only product API.

No model is called. Each document/phase gets an independent temporary home with
S1 off. Existing detectors stay active; model KEEP does not create exceptions.
CUSTOM/personal are explicit application settings, not predicted type or scope.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request

from check_learning_corpus import load_corpus, occurrences
from privacy_policy_clarification import load_clarification_run
from privacy_policy_holdout import load_holdout
from run_privacy_policy_suite import atomic_json, file_sha, utc
from summarize_privacy_holdout import load_fresh_run
from summarize_privacy_policy import load_run, load_selected_run, require, strict_json
from summarize_privacy_recovery import load_supplement
from validate_console import free_port, wait_for


@contextmanager
def application(binary, folder):
    env = {key: value for key, value in os.environ.items() if not key.startswith('KAPONIR_')}
    folder.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix='kaponir-measured-application-') as home:
        subprocess.run([str(binary), '--home', home, 'init'], env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = 'http://127.0.0.1:' + str(free_port())
        with (folder / 'server.log').open('w') as log:
            process = subprocess.Popen([str(binary), '--home', home, 'serve', '--listen', base.split('://')[1]],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env)
            def api(path, body=None):
                request = urllib.request.Request(base + path,
                    data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return strict_json(response.read().decode('utf-8'))
            try:
                wait_for(lambda: api('/api/v1/ui/info'))
                require(api('/api/v1/ui/s1')['mode'] == 'off', 'application_requires_s1_off')
                yield api
            finally:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=10)


def reference_data(plan):
    phase = plan['signature'].get('quality_contract', {}).get('phase')
    if phase in ('fresh_holdout', 'fresh_holdout_policy_clarification'):
        data = load_holdout()
        private, public = {}, {}
        for case in data.cases:
            label = data.expected_by_id[case.id]
            if label in ('mask', 'keep'):
                target = private if label == 'mask' else public
                target.setdefault(case.file, []).append(case.value)
        return data.sources, private, public, 'only the 14 private and 14 public annotated targets; other text is not fully labeled'
    _, expected, public, sources, _ = load_corpus()
    private = {}
    for name, value in expected:
        private.setdefault(name, []).append(value)
    return sources, private, public, 'all private value annotations and known public controls in the selected source documents'


def coverage(original, masked, private, public):
    count = lambda text, value: len(occurrences(text, value))
    return {'private_values_annotated': len(private),
        'private_occurrences_before': sum(count(original, value) for value in private),
        'private_occurrences_visible': sum(count(masked, value) for value in private),
        'public_occurrences_before': sum(count(original, value) for value in public),
        'public_occurrences_lost': sum(max(0, count(original, value) - count(masked, value)) for value in public)}


def span_coverage(original, private, replacements, masked):
    """Geometric coverage of annotated input spans, not a semantic privacy score."""
    raw = original.encode()
    offsets = [0]
    for character in original:
        offsets.append(offsets[-1] + len(character.encode()))
    boundaries = set(offsets)
    ordered = []
    for row in replacements:
        start, end = row['start'], row['end']
        require(type(start) is int and type(end) is int and 0 <= start < end <= len(raw)
                and start in boundaries and end in boundaries
                and isinstance(row.get('token'), str), 'application_replacement_not_grounded')
        ordered.append((start, end, row['token']))
    ordered.sort()
    intervals = [(a, b) for a, b, _ in ordered]
    require(all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:])),
            'application_replacements_overlap')
    # For inflected names, API `original` is a lemma, not the literal source
    # substring. Validate positions by reconstructing the actual masked text.
    reconstructed, position = [], 0
    for start, end, token in ordered:
        reconstructed.extend((raw[position:start], token.encode()))
        position = end
    reconstructed.append(raw[position:])
    require(b''.join(reconstructed) == masked.encode(), 'application_masked_text_not_reconstructed')
    result = {'private_occurrence_spans': 0, 'private_spans_fully_covered': 0,
              'private_spans_partly_covered': 0, 'private_spans_untouched': 0}
    for value in private:
        for match in occurrences(original, value):
            start, end = offsets[match.start()], offsets[match.end()]
            covered = sum(max(0, min(end, b) - max(start, a)) for a, b in intervals)
            key = ('private_spans_fully_covered' if covered == end - start else
                   'private_spans_partly_covered' if covered else 'private_spans_untouched')
            result[key] += 1
            result['private_occurrence_spans'] += 1
    return result


def apply_documents(binary, output, source_hashes, sources, private, public, selected, metadata):
    """Apply an explicit selection; annotations only measure its consequences."""
    require(not output.exists(), 'application_output_exists_no_overwrite')
    for name, expected in source_hashes.items():
        require(hashlib.sha256(sources[name].encode()).hexdigest() == expected, 'application_source_changed')
    output.mkdir(parents=True)
    report = {'version': 2, 'status': 'running', 'created_at_utc': utc(), **metadata,
        'application_producer_sha256': {Path(__file__).name: file_sha(Path(__file__))},
        'binary_sha256': file_sha(binary), 'new_inference_calls': 0, 'user_rules_changed': 0,
        'model_predicted_type_or_scope_applied': False, 'application_kind': 'CUSTOM',
        'application_scope': 'personal in a temporary home; no persistent user rule',
        'confirmation_assumption': 'all selected values explicitly confirmed for this document; not automatic model application',
        'coverage_semantics': {
            'exact_visibility': 'whole annotated value still present; its absence may result from partial replacement',
            'span_coverage': 'geometric coverage of original UTF-8 input spans, not proof of zero information leakage',
            'annotation_occurrence_spans_may_overlap': True},
        'documents': []}
    for index, name in enumerate(source_hashes):
        original = sources[name]
        for mode in ('baseline', 'model_masks_added'):
            folder = output / mode / (str(index + 1).zfill(2) + '-' + Path(name).stem)
            with application(binary, folder) as api:
                before = api('/api/v1/rules')
                masked = api('/api/v1/ui/mask', {'text': original, 'commit': True,
                    'adhoc': selected.get(name, []) if mode == 'model_masks_added' else []})
                restored = api('/api/v1/ui/restore', {'text': masked['text']})
                unchanged = before == api('/api/v1/rules')
                require(unchanged, 'application_created_persistent_rules')
                require(restored['text'] == original, 'application_roundtrip_not_exact')
                atomic_json(folder / 'mask.json', masked)
                atomic_json(folder / 'restore.json', restored)
                (folder / 'masked.txt').write_text(masked['text'], encoding='utf-8')
                values = coverage(original, masked['text'], private.get(name, []), public.get(name, []))
                spans = span_coverage(original, private.get(name, []), masked['replacements'], masked['text'])
                requested = selected.get(name, []) if mode == 'model_masks_added' else []
                report['documents'].append({'file': name, 'mode': mode,
                    'source_sha256': hashlib.sha256(original.encode()).hexdigest(),
                    'mask_values_requested': len(requested),
                    'mask_occurrences_requested': sum(len(occurrences(original, item['value'])) for item in requested),
                    'mask_occurrences_still_visible': sum(len(occurrences(masked['text'], item['value'])) for item in requested),
                    'replacements': len(masked['replacements']), 'restore_exact': True,
                    'persistent_rules_unchanged': unchanged, **values, **spans})
                atomic_json(output / 'result.json', report)
    report.update(status='completed', finished_at_utc=utc(), exact_roundtrips=len(report['documents']))
    atomic_json(output / 'result.json', report)
    return report


def run(result, binary, output, variant='D', *, transport_supplement=None, selected_variants=None):
    result, binary, output = Path(result).resolve(), Path(binary).resolve(), Path(output).resolve()
    require(not output.exists(), 'application_output_exists_no_overwrite')
    plan = strict_json(result.with_name('plan.json').read_text())
    phase = plan['signature'].get('quality_contract', {}).get('phase')
    loader = (load_clarification_run if phase == 'fresh_holdout_policy_clarification' else
              load_fresh_run if phase == 'fresh_holdout' else load_run)
    if selected_variants is None:
        verified = loader(result)
    else:
        require(phase is None and transport_supplement is None,
                'selected_variants_require_calibration_without_transport_supplement')
        verified = load_selected_run(result, variants=selected_variants)
        require(variant in verified['selected_variants'], 'application_variant_not_selected')
    row = next(row for row in verified['rows'] if row['variant'] == variant)
    require(verified['run_status'] == 'completed' and row['protocol_complete'], 'completed_measured_variant_required')
    raw = result.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == verified['result_sha256'], 'application_source_result_changed')
    state = strict_json(raw.decode('utf-8'))
    sources, private, public, oracle_scope = reference_data(plan)
    decisions = state['stages'][variant]['decisions']
    supplement_metadata = ({'decision_view': 'original'} if selected_variants is None else
        {'decision_view': 'selected_variants', 'selected_variants': verified['selected_variants'],
         'abcd_comparison': verified['abcd_comparison']})
    if transport_supplement is not None:
        require(phase not in ('fresh_holdout', 'fresh_holdout_policy_clarification'),
                'transport_application_requires_calibration_source')
        supplement_path = Path(transport_supplement).resolve()
        supplement = load_supplement(supplement_path, result)
        require(supplement['status'] == 'completed', 'completed_transport_supplement_required')
        supplement_raw = supplement_path.read_bytes()
        require(hashlib.sha256(supplement_raw).hexdigest() == supplement['result_sha256'],
                'application_transport_supplement_changed')
        supplement_state = strict_json(supplement_raw.decode('utf-8'))
        cleanup = supplement_state.get('runtime_cleanup_seconds')
        require(bool(supplement_state.get('finished_at_utc')) and type(cleanup) in (int, float)
                and cleanup >= 0, 'transport_application_requires_durable_cleanup')
        supplement_metadata = {'decision_view': 'original_plus_transport_supplement',
            'transport_supplement_result_sha256': supplement['result_sha256'],
            'transport_supplement_plan_sha256': supplement['recovery_plan_sha256'],
            'transport_supplement_plan_file_sha256': supplement['recovery_plan_file_sha256'],
            'original_measured_decision_quality': row['quality'],
            'reinterpreted_received_failures': supplement['reinterpreted_received_failures']}
        row = next(row for row in supplement['effective_rows'] if row['variant'] == variant)
        require(row['protocol_complete'], 'completed_effective_variant_required')
        decisions = supplement['effective_stages'][variant]['decisions']
    selected = {}
    for case in plan['control_cases']['candidates']:
        require(sources[case['file']][case['start']:case['end']] == case['value'], 'application_target_not_grounded')
        decision = decisions.get(case['id'], {})
        if decision.get('valid') is True and decision.get('verdict') == 'mask':
            selected.setdefault(case['file'], []).append({'value': case['value'], 'kind': 'CUSTOM'})
    metadata = {'model': verified['model'], 'variant': variant,
        'source_phase': phase or 'calibration_controls', 'source_settings': plan['signature']['settings'],
        'source_result_sha256': verified['result_sha256'], 'source_plan_sha256': verified['plan_sha256'],
        'policy_sha256': plan['signature']['policy_sha256'],
        'scheme': 'existing detectors plus valid model MASK values for this document; no automatic KEEP exceptions',
        'oracle_scope': oracle_scope, 'measured_decision_quality': row['quality'], **supplement_metadata}
    return apply_documents(binary, output, plan['signature']['source_sha256'],
                           sources, private, public, selected, metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', choices=['A', 'B', 'C', 'D'], default='D')
    parser.add_argument('--transport-supplement', type=Path)
    parser.add_argument('--selected-variants', nargs='+', choices=['A', 'B', 'C', 'D'],
                        help='Explicit calibration subset; omitted A-C remain unmeasured.')
    args = parser.parse_args()
    report = run(args.result, args.binary, args.output, args.variant,
                 transport_supplement=args.transport_supplement, selected_variants=args.selected_variants)
    print(json.dumps({key: report[key] for key in ('status', 'model', 'variant', 'new_inference_calls',
                                                 'exact_roundtrips', 'user_rules_changed')}))


if __name__ == '__main__':
    main()
