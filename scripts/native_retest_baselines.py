"""Restore finished reference decisions for the native retest without inference."""

import gzip
import hashlib
from pathlib import Path
import tarfile

from apply_scheme_masks import run_scheme
from compare_local_models import ROOT
from evaluate_model_schemes import METHODS
from report_complete_research import legacy_summary
from run_privacy_policy_suite import file_sha
from scheme_reference import REQUESTED_REFERENCE
from summarize_privacy_policy import require, strict_json


CHECKPOINTS = ROOT / 'research/privacy-policy-phase2/checkpoints'
MINI_CHECKPOINTS = {
    'tiny-aya-global': '2026-11-17-tiny-aya-auto',
    'qwen3.5-4b': '2026-11-18-qwen4b-auto',
    'spark-x2.6-4b': '2026-11-18-spark-auto',
}


def store_exact(destination, contents):
    if destination.exists():
        require(destination.read_bytes() == contents, 'native_retest_baseline_copy_changed')
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.part')
    temporary.write_bytes(contents)
    temporary.replace(destination)


def restore_snapshot(directory, study):
    manifest = strict_json((directory / 'manifest.json').read_text())
    require(manifest['archive'] == 'evidence.tar.gz', 'native_retest_snapshot_archive_changed')
    archive = directory / manifest['archive']
    require(manifest['series'] == 'native-retest-v1' and manifest['not_a_resume_source'] is True
            and file_sha(archive) == manifest['archive_sha256']
            and archive.stat().st_size == manifest['archive_bytes'], 'native_retest_snapshot_changed')
    with tarfile.open(archive, 'r:gz') as package:
        members = package.getmembers()
        require(len(members) == len(manifest['files'])
                and {member.name for member in members} == set(manifest['files']), 'native_retest_snapshot_members_changed')
        for member in members:
            destination = study / member.name
            record = manifest['files'][member.name]
            require(destination.resolve().is_relative_to(study) and member.isfile()
                    and member.size == record['bytes'], 'native_retest_snapshot_member_changed')
            with package.extractfile(member) as source:
                contents = source.read()
            require(hashlib.sha256(contents).hexdigest() == record['sha256'], 'native_retest_snapshot_member_changed')
            store_exact(destination, contents)


def base_scheme_source(study, model):
    if model == 'gemma4-e4b-qat':
        return ROOT / 'benchmark/data/validation/gemma-calibration--result.json.gz', True

    names = ('scheme-plan.json', 'result.json', 'transports.json')
    prefix = f'new-models-reference-v2/runs/{model}'
    directory = study / 'baselines' / model
    if model == 'lfm2.5-1.2b-instruct':
        checkpoint = CHECKPOINTS / '2026-11-17'
        files = strict_json((checkpoint / 'manifest.json').read_text())['files']
        for name in names:
            key = f'evidence/{prefix}/{name}.gz'
            path = checkpoint / key
            require(path.stat().st_size == files[key]['bytes'] and file_sha(path) == files[key]['sha256'],
                    'native_retest_baseline_file_changed')
            store_exact(directory / name, gzip.decompress(path.read_bytes()))
        return directory / 'result.json', False

    if model in MINI_CHECKPOINTS:
        checkpoint = CHECKPOINTS / MINI_CHECKPOINTS[model]
        manifest = strict_json((checkpoint / 'manifest.json').read_text())
        archive = checkpoint / manifest['archive']
        require(manifest['model'] == model and file_sha(archive) == manifest['archive_sha256']
                and archive.stat().st_size == manifest['archive_bytes'], 'native_retest_checkpoint_changed')
        with tarfile.open(archive, 'r:gz') as package:
            for name in names:
                key = f'{prefix}/{name}'
                record = manifest['files'][key]
                member = package.getmember(key)
                require(member.isfile() and member.size == record['bytes'], 'native_retest_checkpoint_member_changed')
                with package.extractfile(member) as source:
                    contents = source.read()
                require(hashlib.sha256(contents).hexdigest() == record['sha256'],
                        'native_retest_checkpoint_member_changed')
                store_exact(directory / name, contents)
        return directory / 'result.json', False

    finished = study.parent / prefix / 'result.json'
    if finished.is_file():
        plan = strict_json(finished.with_name('scheme-plan.json').read_text())
        if plan.get('legacy_reference_mode') == REQUESTED_REFERENCE and legacy_summary(finished)['status'] == 'completed':
            return finished, False
    return None, False


def reapply_baselines(study, binary, models):
    binary_sha = file_sha(binary)
    for model in models:
        source, archived = base_scheme_source(study, model)
        if source is None:
            continue
        require(legacy_summary(source, archived)['model'] == model, 'native_retest_baseline_model_mismatch')
        for method in METHODS:
            directory = study / 'baseline-application' / model / method
            result = directory / 'result.json'
            if not directory.exists():
                run_scheme(source, binary, directory, method, archived=archived)
            state = strict_json(result.read_text())
            require(state['status'] == 'completed' and state['binary_sha256'] == binary_sha
                    and state['source_file_sha256'] == file_sha(source),
                    'native_retest_baseline_application_changed')
