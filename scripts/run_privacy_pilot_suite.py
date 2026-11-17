#!/usr/bin/env python3
"""Run the fixed eight-model pilot after the inspected current runtime exits."""
import argparse
from pathlib import Path
import sys
import time

from evaluate_privacy_pilot import load_pilot_run
from report_complete_research import _csv, table
from run_complete_research import Controller as ResearchController
from run_gemma_it_research import snapshot as research_snapshot
from run_privacy_policy_suite import SuiteLock, atomic_json, file_sha, process_identity, runtime_processes, utc
from summarize_privacy_policy import require, strict_json


def snapshot(root):
    root = Path(root)
    return {**research_snapshot(root),
            **{name: file_sha(root / name) for name in ('docs/experiments/eight-model-pilot-v1.json',
                                                       'docs/experiments/EIGHT_MODEL_PILOT.md')}}


def build_jobs(config):
    spec = strict_json((Path(config['runner_root']) / 'docs/experiments/eight-model-pilot-v1.json').read_text())
    return [{'id': model, 'model': model, 'kind': 'pilot', 'dataset': 'pilot', 'profile': 'native-pilot',
             'output': str(Path(config['output']) / 'runs' / model),
             'command': [sys.executable, '-u', str(Path(config['runner_root']) / 'scripts/evaluate_privacy_pilot.py'),
                         '--model', model, '--output', str(Path(config['output']) / 'runs' / model),
                         '--model-dir', config['model_dirs'][model], '--runtime', config['runtime']]}
            for model in spec['models']]


def ready(config):
    owner = config['paused_recovery_supervisor']
    actual = process_identity(owner['pid'])
    require(actual and actual['start_ticks'] == owner['start_ticks'] and actual['state'] == 'T',
            'inspected_recovery_supervisor_not_paused')
    child = config['wait_current_child'];actual = process_identity(child['pid'])
    if actual and actual['start_ticks'] == child['start_ticks'] and actual['state'] != 'Z':
        return False
    require(not runtime_processes(config['runtime']), 'previous_child_left_a_live_runtime')
    result = Path(config['wait_current_result'])
    if result.exists():
        state = strict_json(result.read_text())
        if state.get('status') == 'completed':
            require(state.get('finished_at_utc') and type(state.get('runtime_cleanup_seconds')) in (int, float),
                    'previous_recovery_cleanup_not_durable')
    return True


def terminal_result(job):
    path = Path(job['output']) / 'result.json'
    if not path.exists():
        diagnostic = path.with_name('native-budget-observations.json')
        if diagnostic.exists():
            value = strict_json(diagnostic.read_text())
            if value.get('measurement_request_slots_recorded') == 0 and value.get('error') in (
                    'single_case_context_budget_exceeded', 'model_context_budget_exceeded'):
                return {'status': 'unavailable_context', 'budget_observations_sha256': file_sha(diagnostic)}
        return {'status': 'failed', 'reason': 'pilot_result_not_recorded'}
    run = load_pilot_run(path)
    if run['status'] != 'completed' or not run['cleanup_durable']:
        return {'status': 'failed', 'reason': 'pilot_completion_or_cleanup_not_durable',
                'result_sha256': run['result_sha256']}
    require(run['model'] == job['model'], 'pilot_result_model_mismatch')
    technical = any(row['quality']['technical_review'] or row['unobserved_generation_cases'] for row in run['rows'])
    return {'status': 'completed_technical_incomplete' if technical else 'completed',
            'technical_incomplete': technical, 'result_sha256': run['result_sha256']}


class Controller(ResearchController):
    def __init__(self, config, resume=False):
        super().__init__(config, resume, build_jobs(config), terminal_result)

    def check_source(self):
        require(snapshot(self.root) == self.config['producer_inputs_sha256'], 'immutable_pilot_source_changed')

    def refresh_reports(self, force=False):
        runs, pending, rejected, rows = [], [], [], []
        for job in self.jobs:
            status = self.state['jobs'].get(job['id'], {}).get('status', 'pending')
            path = Path(job['output']) / 'result.json'
            if status not in ('completed', 'completed_technical_incomplete'):
                pending.append({'model': job['model'], 'status': status})
                continue
            try:
                require(file_sha(path) == self.state['jobs'][job['id']]['result_sha256'], 'pilot_result_changed')
                run = load_pilot_run(path);runs.append(run)
                for row in run['rows']:
                    p, q = row['performance'], row['quality'];typing = q['typing'] or {}
                    rows.append({'model': run['model'], 'variant': row['variant'],
                        **{key: q[key] for key in ('private_mask', 'private_keep', 'public_mask', 'public_keep',
                                                  'explicit_review', 'technical_review')},
                        'observed_cases': row['observed_generation_cases'],
                        'unobserved_cases': row['unobserved_generation_cases'],
                        'kind_and_scope_correct': typing.get('kind_and_scope_correct'),
                        'typed_private_masks': typing.get('private_mask_cases'),
                        'http_seconds': p['http_wall_seconds_including_retries'],
                        'model_cpu_seconds': p['known_model_cpu_seconds'],
                        'model_peak_rss_bytes': p['hardware_planning_model_observed_peak_rss_bytes'],
                        'gguf_bytes': p['model_disk_bytes'], 'context_tokens': p['settings']['context_tokens'],
                        'reasoning_budget': p['settings']['reasoning_budget'], 'reference_cost_only': True})
            except (ValueError, KeyError, TypeError, OSError) as error:
                rejected.append({'model': job['model'], 'error': str(error)})
        output = self.output / 'reports';output.mkdir(parents=True, exist_ok=True)
        report = {'created_at_utc': utc(), 'runs': runs, 'pending': pending, 'rejected_inputs': rejected,
                  'new_inference_calls_by_report': 0, 'final_selection_allowed': False,
                  'models_excluded_from_full_study': []}
        atomic_json(output / 'report.json', report)
        fields = ['model', 'variant', 'private_mask', 'private_keep', 'public_mask', 'public_keep',
                  'explicit_review', 'technical_review', 'observed_cases', 'unobserved_cases',
                  'kind_and_scope_correct', 'typed_private_masks', 'http_seconds', 'model_cpu_seconds',
                  'model_peak_rss_bytes', 'gguf_bytes', 'context_tokens', 'reasoning_budget', 'reference_cost_only']
        _csv(output / 'pilot.csv', rows, fields)
        lines = ['# Малый срез восьми моделей', '',
            'Два прежних документа, 4 приватные и 4 публичные цели. B/D, меньший бюджет ответа. '
            'Это знакомая калибровка; свежая и полная проверка остаются впереди. '
            'Ни одна модель не исключается из полной проверки.', '']
        lines += table(['Модель', 'Вариант', 'Private mask / 4', 'Private keep / 4', 'Public mask / 4',
                        'Public keep / 4', 'Review', 'Тех. review', 'Получено / 8'],
                       [[row[key] for key in fields[:8]] + [row['observed_cases']] for row in rows])
        lines += ['', '## Время и ресурсы, справочно', '',
                  'RSS включает общую сессию с загрузкой и повторяется для B/D; его нельзя суммировать '
                  'между вариантами. HTTP показан за вариант, а не за один ответ. '
                  'Размер GGUF и наблюдённый RSS не задают универсальную минимальную RAM.', '']
        lines += table(['Модель', 'Вариант', 'HTTP, мин', 'RSS с загрузкой, GiB', 'GGUF, GiB', 'Контекст'],
            [[row['model'], row['variant'], None if row['http_seconds'] is None else round(row['http_seconds']/60, 2),
              None if row['model_peak_rss_bytes'] is None else round(row['model_peak_rss_bytes']/2**30, 2),
              round(row['gguf_bytes']/2**30, 2), row['context_tokens']] for row in rows])
        lines += ['', 'Ожидают: ' + str(len(pending)) + '; отклонённых входов: ' + str(len(rejected)) + '.', '']
        (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
        self.state['reports'] = {'output': str(output), 'completed_models': len(runs),
                                 'pending_models': len(pending), 'rejected_inputs': rejected}
        self.save()

    def run(self):
        try:
            self.check_source();self.save('waiting_current_granite');self.refresh_reports(force=True)
            while not ready(self.config):
                self.check_source();self.save();time.sleep(15)
            for job in self.jobs:
                self.model(job)
            self.state.pop('current_job', None)
            self.state['finished_at_utc'] = utc()
            self.save('measurements_finished' if all(row['status'] == 'completed' for row in self.state['jobs'].values())
                      else 'measurements_finished_with_limitations')
            self.refresh_reports(force=True)
        except BaseException as error:
            self.state.update(error=type(error).__name__, blocked_reason=str(error)[:250]);self.save('blocked');raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args();config = strict_json(args.config.read_text())
    if args.check_only:
        require(snapshot(config['runner_root']) == config['producer_inputs_sha256'], 'pilot_source_changed')
        print({'jobs': len(build_jobs(config)), 'runtime_boundary_ready': ready(config), 'new_inference_calls': 0})
        return
    with SuiteLock(Path(config['output']) / '.pilot.lock'):
        Controller(config, args.resume).run()


if __name__ == '__main__':
    main()
