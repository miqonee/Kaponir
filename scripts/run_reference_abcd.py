#!/usr/bin/env python3
"""Run all A/B/C/D for only the new eight, after the six-scheme queue."""
import argparse
from pathlib import Path
import subprocess
import sys
import time

from privacy_abcd_reference import FIELDS, REFERENCE, bind, reference
from privacy_expansion_profiles import GEMMA_IT_MODELS
from run_complete_research import Controller as Base, TERMINAL, terminal_result as original_terminal
from run_legacy_new_models import MODELS, snapshot as scheme_snapshot
from run_privacy_policy_suite import SuiteLock, atomic_json, file_sha, process_identity, runtime_processes, utc
from summarize_privacy_policy import build_report, digest, load_run, render_markdown, require, strict_json

DEFAULT_ORDER = tuple(model for model in MODELS if model != 'lfm2.5-2.6b') + ('lfm2.5-2.6b',)


def snapshot(root):
    value = scheme_snapshot(root)
    value[REFERENCE] = file_sha(Path(root) / REFERENCE)
    return value


def build_jobs(config):
    require(set(config['model_dirs']) == set(MODELS), 'ABCD_requires_exactly_eight_new_models')
    order = tuple(config.get('model_order', DEFAULT_ORDER))
    require(len(order) == len(MODELS) and set(order) == set(MODELS),
            'ABCD_order_requires_each_new_model_exactly_once')
    root, output = Path(config['runner_root']), Path(config['output'])
    old = reference()['signature']['settings']
    jobs = []
    for model in order:
        destination = output / 'runs' / model
        command = [sys.executable, '-u', str(root / 'scripts/evaluate_privacy_expansion.py'),
            '--model', model, '--profile', 'common', '--dataset-kind', 'calibration',
            '--paired-reference', '--variants', 'A', 'B', 'C', 'D',
            '--model-dir', config['model_dirs'][model], '--runtime', config['runtime'],
            '--output', str(destination)]
        for key in FIELDS:
            if key not in ('threads', 'slots', 'gpu_layers', 'expected_runtime_sha256'):
                command += ['--' + key.replace('_', '-'), str(old[key])]
        if model in GEMMA_IT_MODELS:
            command += ['--new-catalog', str(root / 'docs/experiments/gemma-it-artifacts-v1.json')]
        jobs.append({'id': model, 'model': model, 'kind': 'privacy', 'profile': 'common',
                     'dataset': 'calibration', 'output': str(destination), 'command': command})
    return jobs


def prepare_jobs(config):
    rows = []
    for job in build_jobs(config):
        output = Path(config['output']) / 'prepared' / job['model']
        command = list(job['command']);command[command.index('--output') + 1] = str(output)
        result = subprocess.run(command + ['--prepare-only'], cwd=config['runner_root'], capture_output=True, text=True)
        require(result.returncode == 0, 'ABCD_prepare_failed:' + job['model'] + ':' + result.stderr[-250:])
        plan = strict_json((output / 'plan.json').read_text());binding = bind(plan)
        rows.append({'model': job['model'], 'path': str(output / 'plan.json'),
                     'plan_sha256': plan['plan_sha256'], 'reference': binding,
                     'planned_requests': len(plan['requests'])})
    value = {'models': 8, 'variants': ['A', 'B', 'C', 'D'], 'cases_per_variant': 32,
             'model_order': [row['model'] for row in rows],
             'outcomes': 1024, 'unique_cases': 32, 'planned_requests': sum(r['planned_requests'] for r in rows),
             'new_inference_calls': 0, 'old_models_repeated': 0, 'plans': rows}
    atomic_json(Path(config['output']) / 'prepared-index.json', value)
    return value


def terminal_result(job):
    output = Path(job['output']);diagnostic = output / 'native-budget-observations.json'
    if not (output / 'result.json').exists() and diagnostic.exists():
        data = strict_json(diagnostic.read_text())
        if data.get('measurement_request_slots_recorded') == 0 and data.get('error') in (
                'paired_reference_batch_plan_differs', 'runtime_json_schema_not_supported',
                'single_case_context_budget_exceeded'):
            sessions = [strict_json(p.read_text()) for p in output.glob('session-*.json')]
            require(sessions and all(s.get('finished_at_utc') and
                    'runtime_cleanup' in s.get('phases_seconds', {}) for s in sessions), 'ABCD_cleanup_missing')
            return {'status': 'unavailable_context', 'reason': data['error'], 'measurement_requests': 0,
                    'budget_observations_sha256': file_sha(diagnostic)}
    result = original_terminal(job)
    if result['status'].startswith('completed'):
        plan = strict_json((output / 'plan.json').read_text())
        require(plan['signature']['paired_reference'] == bind(plan), 'ABCD_reference_changed')
        load_run(output / 'result.json')
    return result


def dependency_ready(config):
    source = Path(config['wait_schemes']);state = strict_json(source.read_text())
    require(state['status'] != 'blocked', 'six_scheme_queue_blocked')
    require(state['config_sha256'] == config['scheme_config_digest'], 'six_scheme_config_changed')
    if state['status'] not in ('measurements_finished', 'measurements_finished_with_limitations'):
        return False
    require(set(state['jobs']) == set(MODELS) and all(r['status'] in TERMINAL for r in state['jobs'].values()),
            'six_scheme_queue_not_terminal')
    owner = state['controller'];actual = process_identity(owner['pid'])
    if actual and actual['start_ticks'] == owner['identity']['start_ticks'] and actual['state'] != 'Z':
        return False
    applied = strict_json(Path(config['wait_scheme_applications']).read_text())
    require(applied['status'] != 'blocked', 'six_scheme_application_blocked')
    return applied.get('terminal_jobs') == 48 and not runtime_processes(config['runtime'])


class Controller(Base):
    def __init__(self, config, resume=False):
        super().__init__(config, resume, build_jobs(config), terminal_result)
        self.state['model_order'] = [job['model'] for job in self.jobs]
        self.save()

    def check_source(self):
        require(snapshot(self.root) == self.config['producer_inputs_sha256'], 'frozen_ABCD_source_changed')
        require(file_sha(Path(self.config['scheme_config'])) == self.config['scheme_config_sha256'],
                'six_scheme_config_file_changed')

    def refresh_reports(self, force=False):
        paths = [Path(row['output']) / 'result.json' for row in self.state['jobs'].values()
                 if row['status'].startswith('completed')]
        report = build_report(paths)
        output = self.output / 'reports';output.mkdir(exist_ok=True)
        atomic_json(output / 'report.json', report)
        (output / 'REPORT.md').write_text(render_markdown(report))
        self.state['reports'] = {'completed_models': len(paths), 'variants': ['A', 'B', 'C', 'D'],
            'old_models_repeated': 0, 'new_inference_calls_for_report': 0,
            'rejected_inputs': report['rejected_inputs']};self.save()

    def run(self):
        try:
            self.check_source();self.refresh_reports();self.save('waiting_six_schemes')
            while not dependency_ready(self.config):
                self.check_source();self.save();time.sleep(15)
            for job in self.jobs:
                self.model(job)
            self.state.pop('current_job', None);self.state['finished_at_utc'] = utc()
            self.save('measurements_finished' if all(r['status'] == 'completed' for r in self.state['jobs'].values())
                      else 'measurements_finished_with_limitations');self.refresh_reports()
        except BaseException as error:
            self.state.update(error=type(error).__name__, blocked_reason=str(error)[:300]);self.save('blocked');raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args();config = strict_json(args.config.read_text())
    if args.prepare_only:
        print(prepare_jobs(config));return
    with SuiteLock(Path(config['output']) / '.ABCD.lock'):
        Controller(config, args.resume).run()


if __name__ == '__main__':
    main()
