#!/usr/bin/env python3
"""Append two requested Gemma-it variants after the immutable thirteen-model study.

The original producers, order and answers are left intact. This controller owns
only twelve new jobs and combines read-only reports into a fifteen-model view.
"""
import argparse
from pathlib import Path
import sys
import time

from privacy_expansion_profiles import GEMMA_IT_MODELS, NEW_MODELS
from run_complete_research import (Controller as ResearchController,
    build_jobs as original_jobs, producer_snapshot, terminal_result as original_terminal)
from run_policy_clarification import build_jobs as original_policy_jobs, terminal_result as policy_terminal
from run_privacy_policy_suite import MODEL_ORDER, SuiteLock, file_sha, process_identity, runtime_processes, utc
from summarize_privacy_policy import require, strict_json


ALL_MODELS = (*MODEL_ORDER, *NEW_MODELS, *GEMMA_IT_MODELS)


def snapshot(root):
    root = Path(root)
    result = producer_snapshot(root)
    for name in ('gemma-it-artifacts-v1.json', 'GEMMA_IT_EXTENSION.md',
                 'privacy-policy-v2.txt', 'POLICY_V2_ABLATION.md'):
        key = 'docs/experiments/' + name
        result[key] = file_sha(root / key)
    return result


def build_jobs(config):
    root, output = Path(config['runner_root']), Path(config['output'])
    catalog = root / 'docs/experiments/gemma-it-artifacts-v1.json'
    jobs = []
    groups = [('calibration-new', 'privacy', 'native', 'calibration'),
              ('schemes-common-new', 'schemes', 'common', None),
              ('schemes-native-new', 'schemes', 'native', None),
              ('fresh-native-new', 'privacy', 'native', 'holdout'),
              ('fresh-common', 'privacy', 'common', 'holdout')]
    for stage, kind, profile, dataset in groups:
        for model in GEMMA_IT_MODELS:
            destination = output / stage / model
            script = 'evaluate_privacy_expansion.py' if kind == 'privacy' else 'evaluate_model_schemes.py'
            command = [sys.executable, '-u', str(root / 'scripts' / script), '--model', model,
                '--new-catalog', str(catalog), '--profile', profile, '--model-dir', config['model_dir'],
                '--runtime', config['runtime'], '--output', str(destination)]
            if kind == 'privacy':
                command += ['--dataset-kind', dataset, '--timeout', '3600', '--variants', 'A', 'B', 'C', 'D']
            jobs.append({'id': stage + '/' + model, 'stage': stage, 'kind': kind, 'model': model,
                         'profile': profile, 'dataset': dataset, 'output': str(destination), 'command': command})
    for model in GEMMA_IT_MODELS:
        reference = output / 'fresh-native-new' / model / 'result.json'
        destination = output / 'policy-v2' / model
        command = [sys.executable, '-u', str(root / 'scripts/evaluate_policy_clarification.py'),
            '--model', model, '--new-catalog', str(catalog), '--reference-result', str(reference),
            '--output', str(destination), '--model-dir', config['model_dir'], '--runtime', config['runtime']]
        jobs.append({'id': 'policy-v2/' + model, 'stage': 'policy-v2', 'kind': 'policy_clarification',
            'model': model, 'profile': 'native', 'dataset': 'holdout', 'variants': ['D'],
            'output': str(destination), 'reference_result': str(reference), 'command': command})
    return jobs


def ready(config):
    path = Path(config['wait_policy_status'])
    if not path.is_file():
        return False
    state = strict_json(path.read_text())
    require(state.get('status') != 'blocked', 'original_policy_comparison_blocked')
    if state.get('status') not in ('measurements_finished', 'measurements_finished_with_limitations'):
        return False
    owner = state['controller']
    identity = process_identity(owner['pid'])
    if identity and identity['start_ticks'] == owner['identity']['start_ticks'] and identity['state'] != 'Z':
        return False
    artifact_path = Path(config['artifacts_status'])
    if not artifact_path.is_file():
        return False
    artifacts = strict_json(artifact_path.read_text())
    require(artifacts.get('status') not in ('failed', 'blocked_existing_file'), 'gemma_artifact_preparation_failed')
    return artifacts.get('status') == 'completed' and not runtime_processes(config['runtime'])


def terminal_result(job):
    return policy_terminal(job) if job['kind'] == 'policy_clarification' else original_terminal(job)


class Controller(ResearchController):
    def __init__(self, config, resume=False):
        super().__init__(config, resume, build_jobs(config), terminal_result)

    def check_source(self):
        require(snapshot(self.root) == self.config['producer_inputs_sha256'], 'immutable_gemma_it_producer_changed')
        for name in ('reference_complete_config', 'reference_policy_config'):
            require(file_sha(Path(self.config[name])) == self.config[name + '_sha256'],
                    'original_reference_configuration_changed')

    def refresh_reports(self, force=False):
        complete = strict_json(Path(self.config['reference_complete_config']).read_text())
        policy = strict_json(Path(self.config['reference_policy_config']).read_text())
        jobs = original_jobs(complete) + original_policy_jobs(policy) + self.jobs
        complete_state = strict_json((Path(complete['output']) / 'status.json').read_text())
        policy_state = strict_json((Path(policy['output']) / 'status.json').read_text())
        versions = {}
        sources = [Path(job['output']) / 'result.json' for job in jobs]
        sources.extend(Path(path) for path in complete.get('reference_results', {}).values())
        sources.extend([Path(complete['output']) / 'status.json', Path(policy['output']) / 'status.json'])
        for path in sources:
            if path.is_file():
                versions[str(path)] = [path.stat().st_mtime_ns, path.stat().st_size]
        if not force and versions == self.last_reports:
            return
        state = {'status': self.state['status'], 'jobs': {
            **complete_state.get('jobs', {}), **policy_state.get('jobs', {}), **self.state['jobs']}}
        try:
            from report_complete_research import write_reports
            from report_policy_clarification import write_reports as policy_reports
            settings = {**complete, 'output': str(self.output), 'expected_models': list(ALL_MODELS)}
            self.state['reports'] = write_reports(settings, state, jobs)
            self.state['reports']['policy_comparison'] = policy_reports(
                {'output': str(self.output / 'policy-comparison'), 'expected_models': list(ALL_MODELS)},
                state, [job for job in jobs if job['kind'] == 'policy_clarification'])
            self.last_reports = versions
        except Exception as exc:
            self.state['reports'] = {'status': 'failed', 'error': type(exc).__name__, 'reason': str(exc)[:240]}
        self.save()

    def run(self):
        try:
            self.check_source()
            self.save('waiting_original_policy_comparison')
            self.refresh_reports(force=True)
            while not ready(self.config):
                self.check_source()
                self.refresh_reports()
                time.sleep(15)
            with SuiteLock(Path(self.config['reference_complete_config']).parent / '.complete.lock'), \
                    SuiteLock(Path(self.config['reference_policy_config']).parent / '.ablation.lock'), \
                    SuiteLock(Path(self.config['original_suite_output']) / '.suite.lock'), \
                    SuiteLock(Path(self.config['recovery_output']) / '.queue.lock'):
                require(ready(self.config), 'original_study_became_active')
                for job in self.jobs:
                    self.model(job)
                self.state.pop('current_job', None)
                self.state['finished_at_utc'] = utc()
                self.save('measurements_finished' if all(row['status'] == 'completed'
                    for row in self.state['jobs'].values()) else 'measurements_finished_with_limitations')
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
    with SuiteLock(Path(config['output']) / '.gemma-it.lock'):
        Controller(config, args.resume).run()


if __name__ == '__main__':
    main()
