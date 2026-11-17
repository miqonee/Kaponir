#!/usr/bin/env python3
"""Finish the agreed thirteen-model matrix after the active reference queue.

Uses immutable producers and explicit jobs; no uncertain launch or terminal
failure is automatically replayed. Existing running children can be adopted by
PID plus start ticks. The original queues stay locked during continuation.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from privacy_expansion_profiles import NEW_MODELS
from run_privacy_fresh_suite import snapshot as fresh_snapshot
from run_privacy_policy_suite import (MODEL_ORDER, SuiteLock, atomic_json, cleanup_durable,
    file_sha, process_group_alive, process_identity, runtime_processes, utc)
from summarize_privacy_policy import digest, require, strict_json


TERMINAL = {'completed', 'completed_technical_incomplete', 'unavailable_context', 'failed'}


def producer_snapshot(root):
    root = Path(root).resolve()
    result = fresh_snapshot(root)
    names = ['crates/kaponir-proxy/src/learning.rs', 'benchmark/manifest.json',
             'benchmark/evidence/artifacts/validation-schemes/gemma-calibration/detectors.json.gz']
    names.extend(path.relative_to(root).as_posix()
                 for path in (root / 'benchmark/data/validation').glob('*calibration*--result.json.gz'))
    result.update({name: file_sha(root / name) for name in names})
    return result


def build_jobs(config):
    root, output = Path(config['runner_root']), Path(config['output'])
    jobs = []
    groups = [('fresh-native-old', 'privacy', MODEL_ORDER, 'native', 'holdout'),
              ('calibration-new', 'privacy', NEW_MODELS, 'native', 'calibration'),
              ('schemes-common-new', 'schemes', NEW_MODELS, 'common', None),
              ('schemes-native-new', 'schemes', NEW_MODELS, 'native', None),
              ('fresh-native-new', 'privacy', NEW_MODELS, 'native', 'holdout'),
              ('fresh-common', 'privacy', (*MODEL_ORDER, *NEW_MODELS), 'common', 'holdout')]
    for stage, kind, models, profile, dataset in groups:
        for model in models:
            ident = stage + '/' + model
            destination = output / stage / model
            model_dir = config['old_model_dir'] if model in MODEL_ORDER else config['new_model_dir']
            script = 'evaluate_privacy_expansion.py' if kind == 'privacy' else 'evaluate_model_schemes.py'
            command = [sys.executable, '-u', str(root / 'scripts' / script), '--model', model,
                '--profile', profile, '--model-dir', model_dir, '--runtime', config['runtime'],
                '--output', str(destination)]
            if kind == 'privacy':
                command += ['--dataset-kind', dataset, '--timeout', '3600', '--variants', 'A', 'B', 'C', 'D']
            jobs.append({'id': ident, 'stage': stage, 'kind': kind, 'model': model,
                         'profile': profile, 'dataset': dataset, 'output': str(destination), 'command': command})
    return jobs


def dependency_ready(config, state):
    require(state.get('status') != 'blocked', 'reference_recovery_blocked')
    if state.get('status') not in ('completed', 'completed_with_failures'):
        return False
    owner = config['recovery_controller']
    identity = process_identity(owner['pid'])
    if identity and identity['start_ticks'] == owner['start_ticks'] and identity['state'] != 'Z':
        return False
    return not runtime_processes(config['runtime'])


def terminal_result(job):
    output = Path(job['output'])
    path = output / 'result.json'
    mode = output / 'native-mode-observations.json'
    if mode.is_file():
        observed = strict_json(mode.read_text())
        require(observed.get('model') == job['model']
                and job['kind'] == 'schemes'
                and observed.get('error') == 'native_thinking_off_not_supported'
                and observed.get('measurement_request_slots_recorded') == 0
                and observed.get('control_data_used') is False,
                'native_mode_diagnostic_invalid')
        require(digest(observed['profile']) == observed['profile_sha256']
                and observed['profile']['reasoning'] == 'off'
                and observed['profile']['mode'] == job['profile']
                and observed['profile']['model'] == job['model']
                and digest(observed['native_template_probe']) == observed['native_template_probe_sha256']
                and observed['native_template_probe']['requested']['ends_with_think_prefix'] is True
                and observed['scheme_plan_file_sha256'] == file_sha(output / 'scheme-plan.json'),
                'native_mode_diagnostic_binding_changed')
        if path.is_file():
            partial = strict_json(path.read_text())
            require(not partial.get('calls') and not partial.get('responses'),
                    'native_mode_diagnostic_has_measurements')
        execution = strict_json((output / 'execution.json').read_text())
        require(execution.get('runtime_cleanup_verified') is True, 'native_mode_cleanup_not_durable')
        return {'status': 'failed', 'reason': 'native_thinking_off_not_supported',
                'native_mode_observations_sha256': file_sha(mode), 'measurement_requests': 0,
                **({'result_sha256': file_sha(path)} if path.is_file() else {})}
    if not path.is_file():
        budget = output / 'native-budget-observations.json'
        if budget.is_file():
            observed = strict_json(budget.read_text())
            if (observed.get('error') == 'single_case_context_budget_exceeded'
                    and observed.get('measurement_request_slots_recorded') == 0):
                return {'status': 'unavailable_context', 'reason': 'whole_document_does_not_fit_native_profile',
                        'budget_observations_sha256': file_sha(budget), 'measurement_requests': 0}
        return {'status': 'failed', 'reason': 'result_not_recorded'}
    state = strict_json(path.read_text())
    if state.get('status') != 'completed':
        return {'status': 'failed', 'reason': 'runner_failed', 'result_sha256': file_sha(path)}
    if job['kind'] == 'privacy':
        plan = strict_json(path.with_name('plan.json').read_text())
        require(plan['signature']['settings']['model'] == job['model']
                and plan['signature']['model_profile_source']['mode'] == job['profile'], 'privacy_job_identity_mismatch')
        contract = plan['signature']['quality_contract']
        require((contract.get('phase') == 'fresh_holdout') == (job['dataset'] == 'holdout'), 'privacy_job_dataset_mismatch')
        complete = cleanup_durable(state) and set(state.get('stages', {})) == {'A', 'B', 'C', 'D'}
        complete = complete and all(len(stage.get('decisions', {})) == 32 for stage in state.get('stages', {}).values())
        technical = bool(state.get('technical_incomplete'))
    else:
        plan = strict_json(path.with_name('scheme-plan.json').read_text())
        require(state['signature']['model'] == job['model'] and plan['profile']['mode'] == job['profile'],
                'scheme_job_identity_mismatch')
        methods = ('detector_proposals', 'current_union', 'point', 'regions', 'union_point', 'double')
        complete = state.get('research_execution', {}).get('runtime_cleanup_verified') is True
        complete = complete and all(method in state.get('metrics', {}) for method in methods)
        technical = any(not row.get('valid') for method in methods
                        for row in state.get('stages', {}).get(method, {}).get('decisions', {}).values())
    return {'status': ('completed_technical_incomplete' if technical else 'completed') if complete else 'failed',
            'reason': None if complete else 'completion_or_cleanup_not_durable',
            'result_sha256': file_sha(path), 'technical_incomplete': technical}


class Controller:
    def __init__(self, config, resume=False, jobs=None, result_function=None):
        self.config, self.root = config, Path(config['runner_root']).resolve()
        self.output = Path(config['output']).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.path = self.output / 'status.json'
        self.jobs = build_jobs(config) if jobs is None else jobs
        self.result_function = result_function or terminal_result
        self.signature = digest(config)
        if self.path.exists():
            require(resume, 'complete_queue_exists_use_explicit_resume_after_inspection')
            self.state = strict_json(self.path.read_text())
            require(self.state['config_sha256'] == self.signature, 'complete_queue_config_changed')
        else:
            self.state = {'version': 1, 'status': 'waiting_reference_research',
                'config_sha256': self.signature, 'jobs': {}, 'started_at_utc': utc(),
                'shadow': True, 'rules_changed': 0, 'planned_jobs': len(self.jobs)}
        self.state['controller'] = {'pid': os.getpid(), 'identity': process_identity(os.getpid()), 'started_at_utc': utc()}
        self.last_reports = None
        self.save()

    def save(self, status=None):
        if status:
            self.state['status'] = status
        self.state['updated_at_utc'] = utc()
        atomic_json(self.path, self.state)

    def check_source(self):
        require(producer_snapshot(self.root) == self.config['producer_inputs_sha256'], 'immutable_complete_producer_changed')

    def refresh_reports(self, force=False):
        versions = {}
        for job in self.jobs:
            path = Path(job['output']) / 'result.json'
            if path.is_file():
                versions[str(path)] = [path.stat().st_mtime_ns, path.stat().st_size]
        for source in self.config.get('reference_results', {}).values():
            path = Path(source)
            if path.is_file():
                versions[str(path)] = [path.stat().st_mtime_ns, path.stat().st_size]
        if not force and versions == self.last_reports:
            return
        from report_complete_research import write_reports
        try:
            self.state['reports'] = write_reports(self.config, self.state, self.jobs)
            self.last_reports = versions
        except Exception as exc:
            self.state['reports'] = {'status': 'failed', 'error': type(exc).__name__, 'reason': str(exc)[:240]}
        self.save()

    def wait_child(self, job, row, child=None):
        require(row.get('pid') and (child is not None or row.get('identity')), 'recorded_child_identity_missing')
        last_report = 0
        while True:
            identity = process_identity(row['pid'])
            if identity and row.get('identity'):
                require(identity['start_ticks'] == row['identity']['start_ticks'], 'recorded_child_pid_reused')
            if child is not None:
                try:
                    code = child.wait(timeout=15)
                    break
                except subprocess.TimeoutExpired:
                    pass
            elif not identity or identity['state'] == 'Z':
                code = None
                break
            else:
                time.sleep(15)
            if time.monotonic() - last_report >= 60:
                self.refresh_reports()
                last_report = time.monotonic()
        require(not process_group_alive(row['pid']) and not runtime_processes(self.config['runtime']),
                'child_exited_with_live_runtime_no_automatic_kill')
        result = self.result_function(job)
        row.update(result, exit_code=code, finished_at_utc=utc())
        self.save()
        self.refresh_reports(force=True)

    def model(self, job):
        self.check_source()
        row = self.state['jobs'].get(job['id'])
        if row:
            require(row['command'] == job['command'], 'recorded_job_command_changed')
            if row['status'] in TERMINAL:
                if row.get('result_sha256'):
                    require(file_sha(Path(job['output']) / 'result.json') == row['result_sha256'], 'completed_result_changed')
                if row.get('budget_observations_sha256'):
                    require(file_sha(Path(job['output']) / 'native-budget-observations.json')
                            == row['budget_observations_sha256'], 'completed_budget_evidence_changed')
                if row.get('native_mode_observations_sha256'):
                    require(file_sha(Path(job['output']) / 'native-mode-observations.json')
                            == row['native_mode_observations_sha256'], 'completed_native_mode_evidence_changed')
                return
            require(row['status'] != 'launching', 'launch_outcome_unknown_no_automatic_replay')
            require(row['status'] == 'running', 'unknown_recorded_job_state')
            self.wait_child(job, row)
            return
        require(not runtime_processes(self.config['runtime']), 'runtime_busy_no_parallel_inference')
        output = Path(job['output'])
        require(not output.exists(), 'unmanaged_job_output_no_automatic_replay')
        output.mkdir(parents=True)
        row = {**job, 'status': 'launching', 'started_at_utc': utc()}
        self.state['jobs'][job['id']] = row
        self.state['current_job'] = job['id']
        self.save('running')
        with (output / 'runner.log').open('a') as log:
            child = subprocess.Popen(job['command'], cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            row.update(status='running', pid=child.pid, identity=process_identity(child.pid))
            self.save()
            self.wait_child(job, row, child)

    def run(self):
        try:
            self.check_source()
            last_report = 0
            while not dependency_ready(self.config, strict_json(Path(self.config['wait_recovery']).read_text())):
                if time.monotonic() - last_report >= 60:
                    self.refresh_reports()
                    last_report = time.monotonic()
                time.sleep(15)
            with SuiteLock(Path(self.config['original_suite_output']) / '.suite.lock'), \
                    SuiteLock(Path(self.config['wait_recovery']).parent / '.queue.lock'):
                require(dependency_ready(self.config, strict_json(Path(self.config['wait_recovery']).read_text())),
                        'reference_became_active')
                self.refresh_reports(force=True)
                for index, job in enumerate(self.jobs):
                    if job['model'] in NEW_MODELS:
                        while True:
                            artifact = strict_json(Path(self.config['new_artifacts_status']).read_text())
                            require(artifact.get('status') != 'failed', 'new_artifact_download_failed')
                            if artifact.get('status') == 'completed':
                                break
                            self.save('waiting_new_artifacts')
                            time.sleep(15)
                    if index == len(MODEL_ORDER):
                        from report_complete_research import write_old_model_checkpoint
                        write_old_model_checkpoint(self.config, self.jobs[:index])
                    self.model(job)
                self.state.pop('current_job', None)
                self.state['finished_at_utc'] = utc()
                self.save('measurements_finished' if all(row['status'] == 'completed' for row in self.state['jobs'].values())
                          else 'measurements_finished_with_limitations')
                self.refresh_reports(force=True)
        except BaseException as exc:
            self.state.update(error=type(exc).__name__, blocked_reason=str(exc)[:240])
            self.save('blocked')
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    config = strict_json(args.config.read_text())
    with SuiteLock(Path(config['output']) / '.complete.lock'):
        Controller(config, args.resume).run()


if __name__ == '__main__':
    main()
