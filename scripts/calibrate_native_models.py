#!/usr/bin/env python3
"""Test each manufacturer's two presets on the same independent RU/EN controls."""

import argparse
from collections import Counter
import hashlib
from pathlib import Path
import tempfile

from check_learning_corpus import load_corpus, occurrences
from compare_local_models import DOCUMENTS, ROOT
from evaluate_model_schemes import MeasuredClient, scheme_profile
from evaluate_privacy_policy import RUNTIME_SHA256
from experiment_inference import extraction_schema, prompts_and_kinds
from kaponir_worker import FILES, Worker, configuration
from native_model_profiles import MODEL_ORDER, catalog_for, profiles_for
from privacy_expansion_profiles import research_registration
from privacy_policy_experiment import payload, prompts, schema, validate
from run_privacy_policy_suite import atomic_json, file_sha, runtime_processes
from summarize_privacy_policy import digest, require, strict_json
from validation_pipeline import Candidate, opaque_id


PILOT_DOCUMENTS = ('04-incident-report.md', '07-operations-handover-en.md')
SAMPLERS = ('temperature', 'top_p', 'top_k', 'min_p', 'seed')
MODE_ERRORS = {'native_retest_thinking_ignored_off', 'native_retest_reasoning_budget_exceeded'}


def calibration_documents():
    _, expected, public, sources, _ = load_corpus()
    used = '\n'.join(sources[name] for name in DOCUMENTS)
    for name in PILOT_DOCUMENTS:
        source = sources[name]
        private = sorted((match.start(), match.end(), value)
            for file, value in expected if file == name and value not in used
            if (match := next(iter(occurrences(source, value)), None)) is not None)
        public_controls = sorted((match.start(), match.end(), value)
            for value in public[name] if value not in used
            if (match := next(iter(occurrences(source, value)), None)) is not None)
        require(len(private) >= 2 and public_controls, 'native_retest_pilot_controls_changed')
        cases = [Candidate(id=opaque_id(name, value), file=name, value=value, start=start, end=end)
                 for start, end, value in private[:2] + public_controls[:1]]
        yield {
            'file': name, 'source': source, 'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'private': {value for _, _, value in private},
            'public': {value for _, _, value in public_controls}, 'cases': cases,
        }


def check_extraction(answer, source, kinds):
    require(isinstance(answer, dict) and set(answer) == {'candidates'}, 'invalid_calibration_extraction')
    values = answer['candidates']
    require(isinstance(values, list) and len(values) <= 24, 'invalid_calibration_extraction')
    found = set()
    for row in values:
        require(isinstance(row, dict) and set(row) == {'value', 'kind', 'confidence'},
                'invalid_calibration_extraction')
        value, confidence = row['value'], row['confidence']
        require(isinstance(value, str) and row['kind'] in kinds[:16]
                and type(confidence) in (int, float) and 0 <= confidence <= 1,
                'invalid_calibration_extraction')
        if 3 <= len(value.encode()) <= 512 and value.strip() == value and value in source:
            found.add(value)
    return found


def measure(worker, profile, directory, documents):
    settings = profile['settings']
    effective = scheme_profile(profile['model'], 'native', catalog_for(profile['model']))
    effective.update(settings, native_retest=profile['profile_sha256'],
                     transport_timeout_seconds=settings['timeout'])
    history = []
    stages = []
    extract, _, kinds = prompts_and_kinds()
    policy = prompts((ROOT / 'docs/experiments/privacy-policy-v1.txt').read_text())
    try:
        client = MeasuredClient(worker, profile['model'], settings['context_tokens'],
            settings['output_tokens'], settings['response_format'],
            {key: settings[key] for key in SAMPLERS}, effective, directory, history)
    except (ValueError, OSError) as exc:
        return {'profile': profile, 'stages': stages, 'error': str(exc), 'eligible': False}
    for doc in documents:
        name = doc['file']
        case_input, contexts = payload('D', doc['cases'], {name: doc['source']})
        requests = (
            ('discovery', extract, {'context': '', 'source': doc['source']}, extraction_schema(kinds, 'typed', 24)),
            ('D', policy['D'], case_input, schema('D', doc['cases'], kinds)),
        )
        for stage, prompt, body, contract in requests:
            targets = doc['private'] if stage == 'discovery' else {case.value for case in doc['cases'][:2]}
            controls = doc['public'] if stage == 'discovery' else {doc['cases'][-1].value}
            result = {'document': name, 'stage': stage, 'valid': False,
                      'private_missed': len(targets), 'public_masked': 0}
            before = len(history)
            client.measurement_stage = f'{name}/{stage}'
            try:
                client.one(prompt, body, contract, settings['output_tokens'])
                answer = strict_json(history[-1]['raw_reply']['choices'][0]['message']['content'])
                if stage == 'discovery':
                    found = check_extraction(answer, doc['source'], kinds)
                else:
                    decisions = validate(answer, doc['cases'], contexts, kinds, 'D')
                    require(all(decision['valid'] for decision in decisions.values()), 'invalid_calibration_decisions')
                    found = {case.value for case in doc['cases'] if decisions[case.id]['verdict'] == 'mask'}
                result.update(valid=True, private_missed=len(targets - found), public_masked=len(controls & found))
            except (ValueError, TypeError, KeyError, OSError) as exc:
                result['error'] = str(exc)[:200]
            result['request_index'] = before if len(history) > before else None
            stages.append(result)
    valid = Counter(row['stage'] for row in stages if row['valid'])
    eligible = valid['discovery'] > 0 and valid['D'] > 0 and not any(
        row.get('error') in MODE_ERRORS for row in stages)
    return {'profile': profile, 'stages': stages, 'eligible': eligible,
            'private_missed': sum(row['private_missed'] for row in stages),
            'public_masked': sum(row['public_masked'] for row in stages)}


def select_option(options):
    eligible = [row for row in options if row['eligible']]
    if not eligible:
        return None
    best = min(eligible, key=lambda row: (row['private_missed'], row['public_masked']))
    return best['profile']


def run(model, model_dir, runtime, output):
    require(not runtime_processes(runtime), 'runtime_busy_no_parallel_inference')
    require(file_sha(runtime) == RUNTIME_SHA256, 'native_retest_runtime_checksum_mismatch')
    require(not output.exists(), 'native_retest_calibration_exists')
    documents = list(calibration_documents())
    with research_registration(model, catalog_for(model)):
        filename, size, sha, _ = FILES[model]
        path = model_dir / filename
        require(path.stat().st_size == size and file_sha(path) == sha, 'native_retest_model_checksum_mismatch')
        output.mkdir(parents=True)
        plan = {
            'model': model, 'artifact_sha256': sha, 'runtime_sha256': RUNTIME_SHA256,
            'annotations_sha256': file_sha(ROOT / 'tests/docs/expected.json'),
            'producer_sha256': file_sha(Path(__file__)),
            'documents': {doc['file']: {'sha256': doc['source_sha256'],
                                       'case_ids': [case.id for case in doc['cases']]} for doc in documents},
            'profiles': profiles_for(model),
        }
        atomic_json(output / 'plan.json', {**plan, 'plan_sha256': digest(plan)})
        reports = []
        with tempfile.TemporaryDirectory(prefix='kaponir-native-pilot-') as home:
            home = Path(home)
            worker = Worker(home, configuration(home, {
                'model_dir': str(model_dir), 'runtime': str(runtime), 'ocr_timeout_seconds': 1200,
            }), 'unused')
            try:
                for profile in plan['profiles']:
                    directory = output / profile['option']
                    directory.mkdir()
                    reports.append(measure(worker, profile, directory, documents))
                    atomic_json(directory / 'result.json', reports[-1])
            finally:
                worker.stop.set()
                worker.stop_runtime()
    selected = select_option(reports)
    result = {'model': model, 'plan_sha256': digest(plan), 'options': reports,
              'selected': selected, 'runtime_cleanup_verified': not runtime_processes(runtime)}
    atomic_json(output / 'result.json', result)
    if selected is not None:
        atomic_json(output / 'selected-profile.json', selected)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', choices=MODEL_ORDER, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.model, args.model_dir.resolve(), args.runtime.resolve(), args.output.resolve())
    print({'model': args.model, 'selected': (result['selected'] or {}).get('option')}, flush=True)


if __name__ == '__main__':
    main()
