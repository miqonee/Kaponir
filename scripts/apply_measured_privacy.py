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
from summarize_privacy_policy import load_run, require, strict_json
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


def run(result, binary, output, variant='D'):
    result, binary, output = Path(result).resolve(), Path(binary).resolve(), Path(output).resolve()
    require(not output.exists(), 'application_output_exists_no_overwrite')
    plan = strict_json(result.with_name('plan.json').read_text())
    phase = plan['signature'].get('quality_contract', {}).get('phase')
    loader = (load_clarification_run if phase == 'fresh_holdout_policy_clarification' else
              load_fresh_run if phase == 'fresh_holdout' else load_run)
    verified = loader(result)
    row = next(row for row in verified['rows'] if row['variant'] == variant)
    require(verified['run_status'] == 'completed' and row['protocol_complete'], 'completed_measured_variant_required')
    state = strict_json(result.read_text())
    sources, private, public, oracle_scope = reference_data(plan)
    for name, expected in plan['signature']['source_sha256'].items():
        require(hashlib.sha256(sources[name].encode()).hexdigest() == expected, 'application_source_changed')
    decisions = state['stages'][variant]['decisions']
    selected = {}
    for case in plan['control_cases']['candidates']:
        require(sources[case['file']][case['start']:case['end']] == case['value'], 'application_target_not_grounded')
        decision = decisions.get(case['id'], {})
        if decision.get('valid') is True and decision.get('verdict') == 'mask':
            selected.setdefault(case['file'], []).append({'value': case['value'], 'kind': 'CUSTOM'})
    output.mkdir(parents=True)
    report = {'version': 1, 'status': 'running', 'created_at_utc': utc(), 'model': verified['model'],
        'variant': variant, 'source_result_sha256': verified['result_sha256'],
        'source_plan_sha256': verified['plan_sha256'], 'policy_sha256': plan['signature']['policy_sha256'],
        'binary_sha256': file_sha(binary), 'new_inference_calls': 0, 'user_rules_changed': 0,
        'model_predicted_type_or_scope_applied': False, 'application_kind': 'CUSTOM',
        'application_scope': 'personal in a temporary home; no persistent user rule',
        'scheme': 'existing detectors plus valid model MASK values for this document; no automatic KEEP exceptions',
        'oracle_scope': oracle_scope, 'measured_decision_quality': row['quality'], 'documents': []}
    for index, name in enumerate(plan['signature']['source_sha256']):
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
                requested = selected.get(name, []) if mode == 'model_masks_added' else []
                report['documents'].append({'file': name, 'mode': mode,
                    'source_sha256': hashlib.sha256(original.encode()).hexdigest(),
                    'model_mask_values_requested': len(requested),
                    'model_mask_occurrences_requested': sum(len(occurrences(original, item['value'])) for item in requested),
                    'model_mask_occurrences_still_visible': sum(len(occurrences(masked['text'], item['value'])) for item in requested),
                    'replacements': len(masked['replacements']), 'restore_exact': True,
                    'persistent_rules_unchanged': unchanged, **values})
                atomic_json(output / 'result.json', report)
    report.update(status='completed', finished_at_utc=utc(), exact_roundtrips=len(report['documents']))
    atomic_json(output / 'result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', choices=['A', 'B', 'C', 'D'], default='D')
    args = parser.parse_args()
    report = run(args.result, args.binary, args.output, args.variant)
    print(json.dumps({key: report[key] for key in ('status', 'model', 'variant', 'new_inference_calls',
                                                 'exact_roundtrips', 'user_rules_changed')}))


if __name__ == '__main__':
    main()
