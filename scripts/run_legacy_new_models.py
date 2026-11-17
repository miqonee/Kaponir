#!/usr/bin/env python3
"""Test only the eight new models against an existing archived protocol.

Old models are read and rescored, never generated again. The selected reference
fixes prompts, corpus, sampling, JSON mode and both request timeouts. The former
extended queue remains deferred until these comparable results are analysed.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import time

from privacy_expansion_profiles import NEW_MODELS, GEMMA_IT_MODELS
from report_complete_research import _csv, legacy_summary, table
from run_complete_research import Controller as ResearchController, terminal_result as original_terminal
from run_gemma_it_research import snapshot as research_snapshot
from run_privacy_policy_suite import SuiteLock, atomic_json, file_sha, process_identity, runtime_processes, utc
from scheme_reference import ARCHIVES, reference, verify_reference_inputs
from summarize_privacy_policy import require, strict_json

MODELS = (*NEW_MODELS, *GEMMA_IT_MODELS)


def snapshot(root):
    root = Path(root);value = research_snapshot(root)
    path = 'docs/experiments/NEW_MODELS_FIRST.md';value[path] = file_sha(root / path)
    return value


def build_jobs(config):
    root, output = Path(config['runner_root']), Path(config['output'])
    require(set(config['model_dirs']) == set(MODELS), 'legacy_new_queue_requires_exactly_eight_new_models')
    jobs = []
    for model in MODELS:
        destination = output / 'runs' / model
        command = [sys.executable, '-u', str(root / 'scripts/evaluate_model_schemes.py'),
            '--model', model, '--profile', 'common', '--legacy-reference', config['reference_model'],
            '--model-dir', config['model_dirs'][model], '--runtime', config['runtime'], '--output', str(destination)]
        if model in GEMMA_IT_MODELS:
            command += ['--new-catalog', str(root / 'docs/experiments/gemma-it-artifacts-v1.json')]
        jobs.append({'id': model, 'model': model, 'kind': 'schemes', 'profile': 'common',
            'dataset': None, 'output': str(destination), 'command': command})
    return jobs


def prepare_jobs(config):
    rows = []
    for job in build_jobs(config):
        output = Path(config['output']) / 'prepared' / job['model']
        command = list(job['command']);command[command.index('--output') + 1] = str(output)
        result = subprocess.run(command + ['--prepare-only'], cwd=config['runner_root'], capture_output=True, text=True)
        require(result.returncode == 0, 'legacy_new_preparation_failed:' + job['model'] + ':' + result.stderr[-300:])
        plan = strict_json((output / 'scheme-plan.json').read_text())
        require(plan['legacy_reference'] == verify_reference_inputs(plan, config['reference_model']),
                'legacy_new_reference_binding_changed')
        rows.append({'model': job['model'], 'plan': str(output / 'scheme-plan.json'),
                     'plan_sha256': plan['plan_sha256'], 'old_reference': plan['legacy_reference']})
    value = {'new_models': 8, 'old_models_repeated': 0, 'schemes': 48, 'new_model_calls': 0, 'plans': rows}
    atomic_json(Path(config['output']) / 'prepared-index.json', value)
    return value


def terminal_result(job):
    result = original_terminal(job)
    if result['status'].startswith('completed'):
        path = Path(job['output']) / 'result.json';plan = strict_json(path.with_name('scheme-plan.json').read_text())
        require(plan['legacy_reference'] == verify_reference_inputs(plan, plan['legacy_reference']['model']),
                'legacy_new_completed_reference_changed')
        legacy_summary(path)
    return result


class Controller(ResearchController):
    def __init__(self, config, resume=False):
        super().__init__(config, resume, build_jobs(config), terminal_result)

    def check_source(self):
        require(snapshot(self.root) == self.config['producer_inputs_sha256'], 'legacy_new_producer_changed')
        _, binding = reference(self.config['reference_model'])
        require(binding == self.config['reference_binding'], 'legacy_new_archived_reference_changed')
        deferred = self.config['deferred_full'];identity = process_identity(deferred['pid'])
        require(not identity or identity['start_ticks'] != deferred['start_ticks'] or identity['state'] in ('T', 'Z'),
                'deferred_full_queue_must_not_run_during_new_model_comparison')
        for name in ('config', 'diagnostic_config', 'diagnostic_driver'):
            require(file_sha(Path(deferred[name])) == deferred[name + '_sha256'], 'deferred_input_changed:' + name)

    def ready(self):
        value = strict_json(Path(self.config['pilot_status']).read_text())
        require(value['status'] != 'blocked', 'pilot_blocked_inspect_before_full_corpus')
        if value['status'] not in ('measurements_finished', 'measurements_finished_with_limitations'):
            return False
        require(len(value['jobs']) == 8 and all(row['status'] in
            ('completed', 'completed_technical_incomplete', 'failed', 'unavailable_context')
            for row in value['jobs'].values()), 'not_all_eight_pilots_attempted')
        owner = value['controller'];identity = process_identity(owner['pid'])
        return (not identity or identity['state'] == 'Z'
            or identity['start_ticks'] != owner['identity']['start_ticks']) and not runtime_processes(self.config['runtime'])

    def wait_recorded(self, row, child=None):
        while True:
            identity = process_identity(row['pid'])
            if identity and identity['state'] != 'Z':
                require(identity['start_ticks'] == row['start_ticks'], 'diagnostic_child_pid_reused')
            if child is not None:
                try:
                    code = child.wait(timeout=15);break
                except subprocess.TimeoutExpired:
                    self.save();continue
            if not identity or identity['state'] == 'Z':
                code = None;break
            self.save();time.sleep(15)
        require(not runtime_processes(self.config['runtime']), 'diagnostic_left_live_runtime')
        return code

    def diagnostic(self):
        deferred = self.config['deferred_full'];row = self.state.get('nanbeige_diagnostic')
        path = Path(deferred['diagnostic_output']) / 'result.json'
        if row and row['status'] == 'completed':
            require(file_sha(path) == row['result_sha256'], 'registered_diagnostic_changed');return
        if row:
            require(row['status'] == 'running' and row.get('pid'), 'uncertain_diagnostic_launch_no_replay')
            code = self.wait_recorded(row)
        else:
            require(not runtime_processes(self.config['runtime']), 'runtime_busy_before_registered_diagnostic')
            command = [sys.executable, '-u', deferred['diagnostic_driver'], '--config', deferred['diagnostic_config']]
            row = self.state['nanbeige_diagnostic'] = {'status': 'launching', 'command': command};self.save()
            with (self.output / 'nanbeige-diagnostic.log').open('a') as log:
                child = subprocess.Popen(command, cwd=self.root, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                identity = process_identity(child.pid)
                row.update(status='running', pid=child.pid, start_ticks=identity['start_ticks']);self.save('nanbeige_short_diagnostic')
                code = self.wait_recorded(row, child)
        measured = strict_json(path.read_text())
        require(measured.get('cleanup_finished_at_utc'), 'registered_diagnostic_cleanup_unknown')
        row.update(status='completed', measured_status=measured['status'], result_sha256=file_sha(path), exit_code=code)
        self.save()

    def refresh_reports(self, force=False):
        runs, pending, rejected = [], [], []
        for model, name in ARCHIVES.items():
            path = self.root / 'benchmark/data/validation' / name
            run = dict(legacy_summary(path, archived=True));run['role'] = (
                'archived_reference' if model == self.config['reference_model'] else 'archived_other_profile')
            runs.append(run)
        for job in self.jobs:
            row = self.state['jobs'].get(job['id'], {})
            if not row.get('status', '').startswith('completed'):
                pending.append({'model': job['model'], 'status': row.get('status', 'pending')});continue
            path = Path(job['output']) / 'result.json'
            try:
                require(file_sha(path) == row['result_sha256'], 'legacy_new_result_changed')
                plan = strict_json(path.with_name('scheme-plan.json').read_text())
                require(plan['legacy_reference'] == self.config['reference_binding']
                        == verify_reference_inputs(plan, self.config['reference_model']), 'legacy_new_profile_changed')
                run = dict(legacy_summary(path));run['role'] = 'new_matched_reference';runs.append(run)
                state = strict_json(path.read_text())
                peaks = [sample.get('roles', {}).get('model', {}).get('observed_peak_rss_bytes')
                         for sample in state.get('research_execution', {}).get('sampled_resources', [])]
                peaks += [run['model_sampled_peak_rss_bytes']]
                run['full_session_observed_peak_rss_bytes'] = max(
                    (value for value in peaks if isinstance(value, int) and not isinstance(value, bool)), default=None)
            except (ValueError, KeyError, TypeError, OSError) as error:
                rejected.append({'model': job['model'], 'reason': str(error)[:200]})
        output = self.output / 'reports';output.mkdir(exist_ok=True)
        report = {'kind': 'new-models-existing-protocol-v1', 'reference': self.config['reference_binding'],
            'runs': runs, 'pending_new_models': pending, 'rejected_inputs': rejected,
            'old_model_benchmark_inference_repeated': 0, 'report_inference_calls': 0,
            'new_models_complete': sum(r['role'] == 'new_matched_reference' for r in runs),
            'all_fifteen_in_one_identical_profile_claimed': False,
            'other_historical_profiles_are_separate': True, 'performance_used_for_quality': False}
        atomic_json(output / 'report.json', report)
        rows = []
        for run in runs:
            for item in run['rows']:
                q = item['quality']
                rows.append({'model': run['model'], 'method': item['method'], 'role': run['role'],
                    'private_mask': q['confirmed_matches'], 'private_keep': q['correct_values_filtered'],
                    'public_mask_without_protection': item['known_public_mask_without_protection'],
                    'technical_review': item['technical_review_values'], 'protected_values': item['protected_values']})
        fields = ['model', 'method', 'role', 'private_mask', 'private_keep',
                  'public_mask_without_protection', 'technical_review', 'protected_values']
        _csv(output / 'schemes.csv', rows, fields)
        resources = [{'model': run['model'], 'role': run['role'],
            'http_seconds_known': run['http_seconds_known'], 'http_complete': run['http_seconds_complete'],
            'cpu_seconds_known': run['model_cpu_seconds_known'],
            'query_peak_rss_bytes': run['model_sampled_peak_rss_bytes'],
            'query_snapshot_rss_bytes': run['model_snapshot_peak_rss_bytes'],
            'full_session_peak_rss_bytes': run.get('full_session_observed_peak_rss_bytes'),
            'gguf_bytes': run['weight_bytes']} for run in runs]
        resource_fields = ['model', 'role', 'http_seconds_known', 'http_complete', 'cpu_seconds_known',
                           'query_peak_rss_bytes', 'query_snapshot_rss_bytes', 'full_session_peak_rss_bytes', 'gguf_bytes']
        _csv(output / 'resources.csv', resources, resource_fields)
        lines = ['# Новые модели на прежнем корпусе и prompts', '',
            'Старые семь моделей читаются из проверенного архива, без новой генерации. '
            'Новые восемь повторяют закреплённый профиль ' + self.config['reference_model'] + '. '
            'Остальные исторические профили показаны отдельно: их настройки не объявляются одинаковыми.', '']
        lines += table(['Модель', 'Схема', 'Группа', 'Private MASK', 'Private KEEP',
            'Публичных MASK без защиты', 'Тех. REVIEW', 'Защищённых значений'],
            [[row[k] for k in fields] for row in rows])
        lines += ['', 'Завершены новые модели: ' + str(report['new_models_complete'])
                  + '/8; отклонённых входов: ' + str(len(rejected)) + '.', '']
        lines += ['## Время и память, справочно', '',
            'HTTP суммируется по шести схемам, не относится к одному ответу. '
            'Архивные снимки RSS после запроса не являются полным пиком загрузки. '
            'Отсутствующий замер не заменяется нулём; наблюдённый RSS не равен минимальной RAM.', '']
        lines += table(['Модель', 'HTTP шести схем, мин', 'Полный замер HTTP', 'RSS всей сессии, GiB',
                        'Архивный/запросный RSS, GiB', 'GGUF, GiB'],
            [[r['model'], round(r['http_seconds_known']/60, 2), r['http_complete'],
              None if r['full_session_peak_rss_bytes'] is None else round(r['full_session_peak_rss_bytes']/2**30, 2),
              None if (r['query_peak_rss_bytes'] or r['query_snapshot_rss_bytes']) is None else
                  round((r['query_peak_rss_bytes'] or r['query_snapshot_rss_bytes'])/2**30, 2),
              round(r['gguf_bytes']/2**30, 2)] for r in resources])
        (output / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
        self.state['reports'] = {'output': str(output), 'completed_new_models': report['new_models_complete'],
            'pending_new_models': len(pending), 'rejected_inputs': rejected};self.save()

    def run(self):
        try:
            self.check_source();self.refresh_reports();self.save('waiting_eight_model_pilot')
            while not self.ready():
                self.check_source();self.save();time.sleep(15)
            self.diagnostic()
            for job in self.jobs:
                self.model(job)
            self.state.pop('current_job', None);self.state['finished_at_utc'] = utc()
            self.save('measurements_finished' if all(r['status'] == 'completed' for r in self.state['jobs'].values())
                      else 'measurements_finished_with_limitations');self.refresh_reports(force=True)
            # The former 69-job plan stays deferred. First analyse and apply the
            # new, comparable evidence; do not start wholesale old-model reruns.
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
    with SuiteLock(Path(config['output']) / '.reference-new.lock'):
        Controller(config, args.resume).run()


if __name__ == '__main__':
    main()
