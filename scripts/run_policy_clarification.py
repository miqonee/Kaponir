#!/usr/bin/env python3
"""Run thirteen registered D/v2 jobs after the complete v1 research queue exits."""
import argparse
import sys
import time
from pathlib import Path

from privacy_expansion_profiles import NEW_MODELS
from privacy_policy_clarification import load_clarification_run
from report_policy_clarification import write_reports
from run_complete_research import Controller as ResearchController, producer_snapshot
from run_privacy_policy_suite import (MODEL_ORDER, SuiteLock, cleanup_durable, file_sha,
    process_identity, runtime_processes, utc)
from summarize_privacy_policy import require, strict_json


def snapshot(root):
    root = Path(root)
    result = producer_snapshot(root)
    for name in ('privacy-policy-v2.txt', 'POLICY_V2_ABLATION.md'):
        key = 'docs/experiments/' + name
        result[key] = file_sha(root / key)
    return result


def build_jobs(config):
    jobs = []
    for model in (*MODEL_ORDER, *NEW_MODELS):
        stage = 'fresh-native-old' if model in MODEL_ORDER else 'fresh-native-new'
        reference = Path(config['complete_output']) / stage / model / 'result.json'
        output = Path(config['output']) / model
        directory = config['old_model_dir'] if model in MODEL_ORDER else config['new_model_dir']
        command = [sys.executable, '-u', str(Path(config['runner_root']) / 'scripts/evaluate_policy_clarification.py'),
            '--model', model, '--reference-result', str(reference), '--output', str(output),
            '--model-dir', directory, '--runtime', config['runtime']]
        jobs.append({'id': model, 'model': model, 'kind': 'policy_clarification', 'dataset': 'holdout',
            'profile': 'native', 'variants': ['D'], 'output': str(output), 'reference_result': str(reference),
            'command': command})
    return jobs


def ready(config):
    state = strict_json((Path(config['complete_output']) / 'status.json').read_text())
    require(state['status'] != 'blocked', 'complete_v1_queue_blocked')
    if state['status'] not in ('measurements_finished', 'measurements_finished_with_limitations'):
        return False
    owner = state['controller']
    identity = process_identity(owner['pid'])
    if identity and identity['start_ticks'] == owner['identity']['start_ticks'] and identity['state'] != 'Z':
        return False
    return not runtime_processes(config['runtime'])


def terminal_result(job):
    path = Path(job['output']) / 'result.json'
    if not path.is_file():
        return {'status': 'failed', 'reason': 'result_not_recorded'}
    state = strict_json(path.read_text())
    if state.get('status') != 'completed' or not cleanup_durable(state):
        return {'status': 'failed', 'reason': 'completion_or_cleanup_not_durable', 'result_sha256': file_sha(path)}
    run = load_clarification_run(path)
    row = run['rows'][0]
    require(row['model'] == job['model'] and row['protocol_complete'], 'policy_clarification_identity_incomplete')
    from report_policy_clarification import read_pair
    read_pair(job['reference_result'], path)
    technical = bool(row['quality']['technical_review'])
    return {'status': 'completed_technical_incomplete' if technical else 'completed',
            'technical_incomplete': technical, 'result_sha256': file_sha(path)}


class Controller(ResearchController):
    def __init__(self, config, resume=False):
        super().__init__(config, resume, build_jobs(config), terminal_result)

    def check_source(self):
        require(snapshot(self.root) == self.config['producer_inputs_sha256'], 'immutable_policy_ablation_producer_changed')

    def refresh_reports(self, force=False):
        versions = {}
        for job in self.jobs:
            for name in (Path(job['output']) / 'result.json', Path(job['output']) / 'policy-pair.json',
                         Path(job['reference_result'])):
                if name.is_file():
                    versions[str(name)] = [name.stat().st_mtime_ns, name.stat().st_size]
        if not force and self.last_reports == versions:
            return
        try:
            self.state['reports'] = write_reports(self.config, self.state, self.jobs)
            self.last_reports = versions
        except Exception as exc:
            self.state['reports'] = {'status': 'failed', 'error': type(exc).__name__, 'reason': str(exc)[:240]}
        self.save()

    def run(self):
        try:
            self.check_source()
            self.refresh_reports(force=True)
            self.save('waiting_complete_v1')
            while not ready(self.config):
                self.check_source()
                self.refresh_reports()
                time.sleep(15)
            with SuiteLock(Path(self.config['complete_output']) / '.complete.lock'), \
                    SuiteLock(Path(self.config['original_suite_output']) / '.suite.lock'), \
                    SuiteLock(Path(self.config['recovery_output']) / '.queue.lock'):
                require(ready(self.config), 'complete_v1_became_active')
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
    with SuiteLock(Path(config['output']) / '.ablation.lock'):
        Controller(config, args.resume).run()


if __name__ == '__main__':
    main()
