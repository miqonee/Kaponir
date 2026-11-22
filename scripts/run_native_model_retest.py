#!/usr/bin/env python3
"""Run the independent manufacturer retest with an auditable, sequential queue."""

import argparse
from pathlib import Path
import subprocess
import sys
import time

from apply_measured_privacy import run as apply_abcd
from apply_scheme_masks import run_scheme
from calibrate_native_models import select_option
from compare_local_models import ROOT
from evaluate_model_schemes import METHODS
from evaluate_privacy_policy import RUNTIME_SHA256
from native_model_profiles import MODEL_ORDER, catalog_for, profiles_for
from privacy_policy_experiment import VARIANTS
from run_complete_research import terminal_result
from run_privacy_policy_suite import SuiteLock, atomic_json, file_sha, runtime_processes, utc
from summarize_privacy_policy import require, strict_json


DEFAULT_STUDY = ROOT / 'artifacts/privacy-policy-phase2/native-retest-v1'
ENTRYPOINTS = {'schemes': 'evaluate_model_schemes.py', 'abcd': 'evaluate_privacy_expansion.py'}


def command(script, model, output, args):
    return [sys.executable, '-u', '-B', str(ROOT / 'scripts' / script), '--model', model,
            '--output', str(output), '--model-dir', str(args.model_dir), '--runtime', str(args.runtime)]


def measurement_command(stage, model, output, profile, args):
    result = command(ENTRYPOINTS[stage], model, output, args)
    result += ['--profile', 'native', '--native-profile', str(profile),
               '--new-catalog', str(catalog_for(model))]
    if stage == 'abcd':
        result += ['--dataset-kind', 'calibration', '--variants', *VARIANTS]
    return result


def prepare(args):
    for model in MODEL_ORDER:
        for profile in profiles_for(model):
            directory = args.study / 'prepared' / model / profile['option']
            directory.mkdir(parents=True, exist_ok=True)
            file = directory / 'native-profile.json'
            if file.exists():
                require(strict_json(file.read_text()) == profile, 'native_retest_prepared_profile_changed')
            else:
                atomic_json(file, profile)
            for stage in ENTRYPOINTS:
                cmd = measurement_command(stage, model, directory / stage, file, args)
                subprocess.run([*cmd, '--prepare-only'], cwd=ROOT, check=True, capture_output=True)
    print('Prepared 32 plans for eight models. Inference calls: 0.', flush=True)


def calibration_option(directory, model):
    result = strict_json((directory / 'result.json').read_text())
    require(result['model'] == model and result['runtime_cleanup_verified'], 'native_retest_calibration_incomplete')
    require([row['profile'] for row in result['options']] == list(profiles_for(model))
            and result['selected'] == select_option(result['options']),
            'native_retest_calibration_selection_changed')
    if result['selected'] is None:
        return None
    path = directory / 'selected-profile.json'
    require(strict_json(path.read_text()) == result['selected'], 'native_retest_selected_profile_changed')
    return path


def available(result):
    return result.get('status') in ('completed', 'completed_technical_incomplete')


class Study:
    def __init__(self, args):
        self.args = args
        self.path = args.study / 'status.json'
        self.snapshot = {name: file_sha(ROOT / 'scripts' / name) for name in (
            *ENTRYPOINTS.values(), 'native_model_profiles.py', 'calibrate_native_models.py',
            'run_native_model_retest.py', 'evaluate_privacy_policy.py',
            'evaluate_validation_schemes.py', 'kaponir_worker.py', 'experiment_inference.py',
            'privacy_expansion_profiles.py', 'privacy_policy_experiment.py',
        )}
        self.binary_sha = file_sha(args.binary)
        if self.path.exists():
            self.state = strict_json(self.path.read_text())
            require(self.state['model_order'] == list(MODEL_ORDER)
                    and self.state['producers'] == self.snapshot
                    and self.state['binary_sha256'] == self.binary_sha,
                    'native_retest_resume_incompatible')
        else:
            self.state = {'version': 1, 'status': 'running', 'model_order': list(MODEL_ORDER),
                          'producers': self.snapshot, 'binary_sha256': self.binary_sha, 'jobs': {}}

    def record(self, key, **receipt):
        self.state['jobs'][key] = receipt
        self.state['updated_at_utc'] = utc()
        atomic_json(self.path, self.state)
        print(key, receipt['status'], receipt.get('reason') or '', flush=True)
        return receipt

    def launch(self, key, cmd):
        require(not runtime_processes(self.args.runtime), 'runtime_busy_no_parallel_inference')
        self.record(key, status='running')
        log = self.args.study / 'logs' / (key.replace('/', '--') + '.log')
        log.parent.mkdir(exist_ok=True)
        with log.open('ab') as stream:
            return subprocess.run(cmd, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT).returncode

    def calibrate(self, model):
        key = f'calibration/{model}'
        output = self.args.study / key
        if not output.exists():
            rc = self.launch(key, command('calibrate_native_models.py', model, output, self.args))
            if rc:
                self.record(key, status='failed', reason=f'calibration_exit_{rc}')
                return None
        # A calibration can already be active when this queue is attached.
        while runtime_processes(self.args.runtime):
            time.sleep(5)
        if not (output / 'result.json').is_file():
            self.record(key, status='interrupted', reason='calibration_result_missing')
            return None
        chosen = calibration_option(output, model)
        self.record(key, status='completed' if chosen else 'incompatible',
                    result_sha256=file_sha(output / 'result.json'),
                    reason=None if chosen else 'no_usable_manufacturer_profile')
        return chosen

    def measure(self, stage, model, profile):
        key = f'{stage}/{model}'
        output = self.args.study / key
        if not output.exists():
            self.launch(key, measurement_command(stage, model, output, profile, self.args))
        require(not runtime_processes(self.args.runtime), 'runtime_cleanup_missing')
        receipt = terminal_result({'output': output, 'kind': 'privacy' if stage == 'abcd' else 'schemes',
                                   'dataset': 'calibration', 'profile': 'native', 'model': model})
        plan_file = 'plan.json' if stage == 'abcd' else 'scheme-plan.json'
        path = output / plan_file
        if path.is_file():
            plan = strict_json(path.read_text())
            source = plan['signature']['model_profile_source'] if stage == 'abcd' else plan['profile']
            require(source['native_retest'] == strict_json(profile.read_text())['profile_sha256'],
                    'native_retest_measurement_profile_mismatch')
        self.record(key, **receipt)
        return output / 'result.json' if available(receipt) else None

    def apply(self, stage, model, source, methods):
        for method in methods:
            key = f'application/{stage}/{model}/{method}'
            output = self.args.study / key
            try:
                if not output.exists():
                    if stage == 'schemes':
                        run_scheme(source, self.args.binary, output, method)
                    else:
                        apply_abcd(source, self.args.binary, output, method)
                path = output / 'result.json'
                report = strict_json(path.read_text())
                require(report['status'] == 'completed' and report['exact_roundtrips'] == 6
                        and report['source_result_sha256'] == file_sha(source)
                        and report['binary_sha256'] == self.binary_sha,
                        'native_retest_application_binding_changed')
                self.record(key, status='completed', result_sha256=file_sha(path))
            except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
                self.record(key, status='failed', reason=str(exc)[:200])

    def run(self):
        for model in MODEL_ORDER:
            profile = self.calibrate(model)
            if profile is None:
                continue
            for stage, methods in (('schemes', METHODS), ('abcd', VARIANTS)):
                source = self.measure(stage, model, profile)
                if source is not None:
                    self.apply(stage, model, source, methods)
        completed = all(available(self.state['jobs'].get(f'{stage}/{model}', {}))
            for model in MODEL_ORDER for stage in ENTRYPOINTS)
        applications = sum(job['status'] == 'completed' for key, job in self.state['jobs'].items()
                           if key.startswith('application/'))
        self.state['status'] = 'completed' if completed and applications == 80 else 'completed_with_failures'
        atomic_json(self.path, self.state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, default=DEFAULT_STUDY)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    args.study = args.study.resolve()
    args.model_dir = (args.model_dir or args.study / 'models').resolve()
    args.runtime = (args.runtime or args.study / 'runtime/llama-b12038/llama-server').resolve()
    args.binary = (args.binary or args.study / 'build/release/kaponir').resolve()
    args.study.mkdir(parents=True, exist_ok=True)
    with SuiteLock(args.study / '.native-retest.lock'):
        if args.prepare_only:
            prepare(args)
        else:
            require(file_sha(args.runtime) == RUNTIME_SHA256, 'native_retest_runtime_checksum_mismatch')
            Study(args).run()


if __name__ == '__main__':
    main()
