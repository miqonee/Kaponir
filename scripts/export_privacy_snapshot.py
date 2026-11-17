#!/usr/bin/env python3
"""Archive completed synthetic phase2 runs and frozen producers, without inference.

Raw bytes are preserved, including invalid replies and timeout attempts. Running
or unclean runs remain pending. No weights, Vaults, credentials, download logs or
arbitrary directories are eligible. Existing snapshots are never overwritten.
"""
import argparse
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile

from compare_local_models import ROOT
from privacy_policy_clarification import load_clarification_run
from report_complete_research import legacy_summary
from report_policy_clarification import read_pair
from run_complete_research import build_jobs
from run_policy_clarification import build_jobs as policy_jobs
from run_privacy_policy_suite import (MODEL_ORDER, atomic_json, cleanup_durable, snapshot_inputs, utc)
from summarize_privacy_holdout import load_fresh_run
from summarize_privacy_policy import load_run, require, strict_json
from summarize_privacy_recovery import load_supplement


STUDY = ROOT / 'artifacts/privacy-policy-phase2'
RUN_FILES = ('plan.json', 'result.json', 'scheme-plan.json', 'transports.json',
             'policy-pair.json', 'preflight.json', 'recovery-plan.json', 'recovery-result.json',
             'native-budget-observations.json', 'continuation-source/recovery-plan.json',
             'continuation-source/recovery-result.json')
MAX_RAW_BYTES = 256 * 1024 * 1024


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def contained(path, root):
    path, root = Path(path), Path(root).resolve()
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root),
            'snapshot_source_outside_allowed_tree')
    return path


def safe_relative(value):
    require(isinstance(value, str) and bool(value) and '\\' not in value
            and value != '.'
            and not PurePosixPath(value).is_absolute() and '..' not in PurePosixPath(value).parts
            and PurePosixPath(value).as_posix() == value, 'unsafe_snapshot_relative_path')
    return value


def decompress(raw):
    with gzip.GzipFile(fileobj=BytesIO(raw), mode='rb') as archive:
        result = archive.read(MAX_RAW_BYTES + 1)
    require(len(result) <= MAX_RAW_BYTES, 'snapshot_member_exceeds_limit')
    return result


def verify_producer_binding(row):
    plan_name = ('scheme-plan.json' if row['kind'] == 'schemes' else
                 'recovery-plan.json' if row['kind'] == 'recovery' else 'plan.json')
    plan = strict_json(row['path'].with_name(plan_name).read_text())
    signature = plan.get('original_signature', plan.get('signature', {}))
    hashes = plan.get('producer_sha256', signature.get('code_sha256', {}))
    require(bool(hashes), 'measured_producer_hashes_required')
    for name, expected in hashes.items():
        require(Path(name).name == name and name.endswith('.py'), 'unsafe_producer_code_path')
        raw = contained(row['producer'] / 'scripts' / name, row['producer']).read_bytes()
        require(sha(raw) == expected, 'measured_producer_source_changed')


def records(study, recovery_queue=None):
    config = strict_json(contained(study / 'complete-v1/config.json', study).read_text())
    result = []
    for model, path in config['reference_results'].items():
        require(model in MODEL_ORDER, 'unknown_original_model')
        producer = study / ('gemma-v2-producer' if model == 'gemma4-e4b-qat' else 'suite-v1-producer')
        result.append({'id': 'calibration-original/' + model, 'model': model, 'kind': 'calibration',
                       'path': Path(path), 'producer': producer})
    states = strict_json((study / 'complete-v1/status.json').read_text())['jobs']
    for job in build_jobs(config):
        result.append({'id': job['id'], 'model': job['model'],
            'kind': 'schemes' if job['kind'] == 'schemes' else job['dataset'],
            'path': Path(job['output']) / 'result.json', 'producer': Path(config['runner_root']),
            'terminal_status': states.get(job['id'], {}).get('status')})
    policy_config = study / 'policy-v2/config.json'
    if policy_config.is_file():
        policy = strict_json(contained(policy_config, study).read_text())
        for job in policy_jobs(policy):
            result.append({'id': 'policy-v2/' + job['model'], 'model': job['model'], 'kind': 'policy_v2',
                'path': Path(job['output']) / 'result.json', 'producer': Path(policy['runner_root']),
                'reference': job['reference_result']})
    extension_config = study / 'gemma-it-v1/config.json'
    if extension_config.is_file():
        from run_gemma_it_research import build_jobs as gemma_jobs
        extension = strict_json(contained(extension_config, study).read_text())
        state_path = study / 'gemma-it-v1/status.json'
        states = strict_json(contained(state_path, study).read_text())['jobs'] if state_path.is_file() else {}
        for job in gemma_jobs(extension):
            result.append({'id': 'gemma-it/' + job['id'], 'model': job['model'],
                'kind': ('policy_v2' if job['kind'] == 'policy_clarification' else
                         'schemes' if job['kind'] == 'schemes' else job['dataset']),
                'path': Path(job['output']) / 'result.json', 'producer': Path(extension['runner_root']),
                'terminal_status': states.get(job['id'], {}).get('status'),
                **({'reference': job['reference_result']} if 'reference_result' in job else {})})
    queue = Path(recovery_queue) if recovery_queue is not None else study / 'recovery-v1/queue.json'
    if queue.is_file():
        for row in strict_json(contained(queue, study).read_text())['runs']:
            original = Path(row['result'])
            model = next((item['model'] for item in result if item['path'] == original), None)
            require(model is not None, 'unknown_recovery_original')
            result.append({'id': 'transport-recovery/' + model, 'model': model, 'kind': 'recovery',
                'path': Path(row['output']) / 'recovery-result.json', 'producer': Path(row['producer_root']),
                'reference': str(original)})
    require(len({row['id'] for row in result}) == len(result), 'duplicate_snapshot_run_id')
    return result


def validate_record(row, path=None, reference=None):
    path = Path(path or row['path'])
    if row['kind'] == 'calibration':
        value = load_run(path)
    elif row['kind'] == 'holdout':
        value = load_fresh_run(path)
    elif row['kind'] == 'schemes':
        value = legacy_summary(path)
    elif row['kind'] == 'policy_v2':
        value = load_clarification_run(path)
        read_pair(reference or row['reference'], path)
    else:
        value = load_supplement(path, reference or row['reference'])
    require(value['model'] == row['model'], 'snapshot_model_mismatch')
    return value


def producer_bundle(producer, study):
    producer = Path(producer).resolve()
    require(producer.is_relative_to(Path(study).resolve()) and producer.name.endswith('-producer'),
            'snapshot_producer_outside_study')
    names = set(snapshot_inputs(producer))
    names.update(path.relative_to(producer).as_posix() for path in (producer / 'scripts').glob('*.py'))
    for name in ('crates/kaponir-proxy/src/learning.rs', 'docs/experiments/privacy-policy-v2.txt',
                 'docs/experiments/POLICY_V2_ABLATION.md', 'docs/experiments/phase2-new-model-artifacts.json',
                 'docs/experiments/gemma-it-artifacts-v1.json', 'docs/experiments/GEMMA_IT_EXTENSION.md',
                 'benchmark/evidence/artifacts/validation-schemes/gemma-calibration/detectors.json.gz'):
        if (producer / name).is_file():
            names.add(name)
    holdout = producer / 'tests/fixtures/privacy-policy-holdout-v1'
    if holdout.is_dir():
        names.update(path.relative_to(producer).as_posix() for path in holdout.rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts)
    contents = {name: contained(producer / name, producer).read_bytes() for name in sorted(names)}
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, raw in contents.items():
            info = tarfile.TarInfo(name);info.size = len(raw);info.mode = 0o644;info.mtime = 0
            archive.addfile(info, BytesIO(raw))
    compressed = gzip.compress(stream.getvalue(), mtime=0)
    return compressed, {name: {'sha256': sha(raw), 'bytes': len(raw)} for name, raw in contents.items()}


def export(study, output, recovery_queue=None):
    study, output = Path(study).resolve(), Path(output).resolve()
    require(study == STUDY.resolve(), 'only_explicit_synthetic_phase2_study_allowed')
    require(not output.exists(), 'snapshot_output_exists_no_overwrite')
    require(not output.is_relative_to((ROOT / 'benchmark').resolve()), 'old_benchmark_must_stay_immutable')
    manifest = {'version': 1, 'created_at_utc': utc(), 'new_inference_calls': 0, 'rules_changed': 0,
        'source': 'pinned synthetic calibration and privacy-policy-holdout-v1',
        'raw_response_reinterpretation': False, 'runs': [], 'diagnostics': [], 'pending': [], 'producers': {}}
    blobs = {}
    for row in records(study, recovery_queue):
        safe_relative(row['id'])
        path = row['path']
        if row['kind'] == 'recovery' and not path.is_file() and path.with_name('recovery-plan.json').is_file():
            plan = strict_json(contained(path.with_name('recovery-plan.json'), study).read_text())
            if not plan['requests']:
                raw = path.with_name('recovery-plan.json').read_bytes();compressed = gzip.compress(raw, mtime=0)
                target = 'diagnostics/' + row['id'] + '/recovery-plan.json.gz';blobs[target] = compressed
                manifest['diagnostics'].append({'id': row['id'], 'model': row['model'],
                    'status': 'not_applicable_no_eligible_timeouts', 'quality_score_available': False,
                    'files': {'recovery-plan.json': {'archive': target, 'raw_sha256': sha(raw),
                        'archive_sha256': sha(compressed), 'raw_bytes': len(raw)}}})
                continue
        if row.get('terminal_status') in ('unavailable_context', 'failed'):
            source_files = [source for source in path.parent.iterdir() if source.is_file()
                and (source.name in RUN_FILES or re.fullmatch(r'session-[0-9]+\.json', source.name))]
            files = {}
            for source in source_files:
                raw = contained(source, study).read_bytes();compressed = gzip.compress(raw, mtime=0)
                if source.name == 'result.json':
                    failed = strict_json(raw.decode('utf-8'))
                    require(failed.get('shadow') is True and failed.get('rules_changed') == 0,
                            'only_shadow_runs_may_be_archived')
                target = 'diagnostics/' + row['id'] + '/' + source.name + '.gz';blobs[target] = compressed
                files[source.name] = {'archive': target, 'raw_sha256': sha(raw),
                    'archive_sha256': sha(compressed), 'raw_bytes': len(raw)}
            if path.with_name('scheme-plan.json' if row['kind'] == 'schemes' else 'plan.json').is_file():
                verify_producer_binding(row)
            manifest['diagnostics'].append({'id': row['id'], 'model': row['model'],
                'status': row['terminal_status'], 'quality_score_available': False, 'files': files})
            continue
        if not path.is_file():
            manifest['pending'].append({'id': row['id'], 'reason': 'no_result'})
            continue
        raw = contained(path, study).read_bytes()
        state = strict_json(raw.decode('utf-8'))
        if state.get('status') != 'completed':
            manifest['pending'].append({'id': row['id'], 'reason': state.get('status', 'not_completed')})
            continue
        if row['kind'] in ('calibration', 'holdout', 'policy_v2') and not cleanup_durable(state):
            manifest['pending'].append({'id': row['id'], 'reason': 'cleanup_pending'})
            continue
        if row['kind'] == 'schemes' and state.get('research_execution', {}).get('runtime_cleanup_verified') is not True:
            manifest['pending'].append({'id': row['id'], 'reason': 'cleanup_pending'})
            continue
        require(state.get('shadow') is True and state.get('rules_changed') == 0, 'only_shadow_runs_may_be_archived')
        verified = validate_record(row)
        require(verified['result_sha256'] == sha(raw), 'snapshot_changed_while_reading_retry_later')
        verify_producer_binding(row)
        files = {}
        for name in RUN_FILES:
            source = path.parent / name
            if not source.is_file():
                continue
            original = contained(source, study).read_bytes()
            target = 'runs/' + row['id'] + '/' + name + '.gz'
            compressed = gzip.compress(original, mtime=0)
            blobs[target] = compressed
            files[name] = {'archive': target, 'raw_sha256': sha(original), 'archive_sha256': sha(compressed),
                           'raw_bytes': len(original)}
        result_name = 'recovery-result.json' if row['kind'] == 'recovery' else 'result.json'
        require(files[result_name]['raw_sha256'] == sha(raw), 'snapshot_changed_while_collecting_retry_later')
        producer = row['producer'].name
        if producer not in manifest['producers']:
            archive, members = producer_bundle(row['producer'], study)
            target = 'producers/' + producer + '.tar.gz';blobs[target] = archive
            manifest['producers'][producer] = {'archive': target, 'archive_sha256': sha(archive), 'files': members}
        manifest['runs'].append({'id': row['id'], 'model': row['model'], 'kind': row['kind'],
            'producer': producer, 'status': 'completed', 'files': files,
            'reference_result_sha256': verified.get('original_result_sha256') or (
                sha(contained(row['reference'], study).read_bytes()) if 'reference' in row else None)})
    output.mkdir(parents=True)
    for name, raw in blobs.items():
        path = output / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(raw)
    atomic_json(output / 'manifest.json', manifest)
    verify(output, rescore=True)
    return manifest


def verify(directory, rescore=False):
    directory = Path(directory).resolve()
    manifest = strict_json(contained(directory / 'manifest.json', directory).read_text())
    require(manifest['version'] == 1 and manifest['new_inference_calls'] == 0 and manifest['rules_changed'] == 0,
            'not_readonly_privacy_snapshot')
    expected = {'manifest.json'}
    for producer in manifest['producers'].values():
        expected.add(producer['archive'])
        compressed = contained(directory / producer['archive'], directory).read_bytes()
        require(sha(compressed) == producer['archive_sha256'], 'snapshot_producer_archive_changed')
        with tarfile.open(fileobj=BytesIO(decompress(compressed)), mode='r:') as archive:
            require(set(archive.getnames()) == set(producer['files']), 'snapshot_producer_file_set_changed')
            for member in archive.getmembers():
                require(member.isfile() and not Path(member.name).is_absolute() and '..' not in Path(member.name).parts,
                        'unsafe_producer_member')
                raw = archive.extractfile(member).read()
                require(sha(raw) == producer['files'][member.name]['sha256'], 'snapshot_producer_member_changed')
    with tempfile.TemporaryDirectory(prefix='kaponir-privacy-snapshot-') as temporary:
        root = Path(temporary);by_sha, expanded = {}, []
        for row in manifest['runs']:
            safe_relative(row['id'])
            target = root / row['id'];target.mkdir(parents=True)
            for name, record in row['files'].items():
                require(name in RUN_FILES, 'unknown_snapshot_run_file')
                expected.add(record['archive'])
                compressed = contained(directory / record['archive'], directory).read_bytes()
                raw = decompress(compressed)
                require(sha(compressed) == record['archive_sha256'] and sha(raw) == record['raw_sha256']
                        and len(raw) == record['raw_bytes'], 'snapshot_raw_or_archive_changed')
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                if name in ('result.json', 'recovery-result.json'):
                    by_sha[record['raw_sha256']] = target / name
            name = 'recovery-result.json' if row['kind'] == 'recovery' else 'result.json'
            expanded.append((row, target / name))
        for row in manifest.get('diagnostics', []):
            safe_relative(row['id'])
            require(row['quality_score_available'] is False, 'diagnostic_cannot_be_a_quality_score')
            for name, record in row['files'].items():
                require(name in RUN_FILES or re.fullmatch(r'session-[0-9]+\.json', name), 'unknown_diagnostic_file')
                expected.add(record['archive'])
                compressed = contained(directory / record['archive'], directory).read_bytes();raw = decompress(compressed)
                require(sha(compressed) == record['archive_sha256'] and sha(raw) == record['raw_sha256']
                        and len(raw) == record['raw_bytes'], 'snapshot_diagnostic_changed')
        if rescore:
            for row, path in expanded:
                reference = by_sha.get(row.get('reference_result_sha256'))
                require(row['kind'] not in ('policy_v2', 'recovery') or reference is not None,
                        'snapshot_reference_not_archived')
                validate_record(row, path, reference)
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob('*') if path.is_file()}
    require(actual == expected, 'snapshot_file_set_changed')
    return {'runs': len(manifest['runs']), 'pending': len(manifest['pending']),
            'diagnostics': len(manifest.get('diagnostics', [])),
            'files': len(expected), 'rescored': bool(rescore), 'new_inference_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, default=STUDY)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--recovery-queue', type=Path,
                        help='Explicit canonical recovery queue after an environment restart')
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--rescore', action='store_true')
    args = parser.parse_args()
    require((args.output is None) != (args.verify is None), 'choose_export_or_verify')
    if args.verify:
        print(json.dumps(verify(args.verify, args.rescore)))
    else:
        manifest = export(args.study, args.output, args.recovery_queue)
        print(json.dumps({'archived_runs': len(manifest['runs']), 'pending': len(manifest['pending']),
                          'new_inference_calls': 0}))


if __name__ == '__main__':
    main()
