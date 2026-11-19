#!/usr/bin/env python3
"""Read the full A/B/C/D results and verify their mask and restore evidence."""
import argparse
import csv
from pathlib import Path

from compare_local_models import ROOT
from evaluate_privacy_policy import parse_response
from privacy_abcd_reference import bind
from report_complete_research import STATUS, table
from report_new_models_first import application_summary
from run_legacy_new_models import MODELS
from run_privacy_policy_suite import atomic_json, cleanup_durable, file_sha, utc, VARIANTS
from summarize_privacy_policy import digest, load_run, require, strict_json

STUDY_ROOT = ROOT / 'artifacts/privacy-policy-phase2'
FIELDS = (
    ('model', 'Модель'), ('variant', 'Вариант'), ('status', 'Статус'),
    ('delivered_cases', 'Целей с ответом /32'), ('private_mask', 'Приватных MASK /16'),
    ('private_keep', 'Приватных KEEP /16'), ('public_mask', 'Публичных MASK /16'),
    ('explicit_review', 'Ручная проверка'), ('inapplicable_received', 'Непригодных решений'),
    ('missing_cases', 'Целей без ответа'), ('request_errors', 'Ошибок запросов'),
    ('received_requests', 'Ответов HTTP'), ('attempted_requests', 'Запросов HTTP'),
    ('private_full', 'Полностью скрыто вхождений /193'),
    ('private_partial', 'Частично скрыто вхождений /193'),
    ('private_untouched', 'Не затронуто вхождений /193'),
    ('public_lost', 'Публичных потерь /87'), ('public_replacements', 'Замен в публичном документе'),
    ('exact_roundtrips', 'Точных восстановлений'), ('http_seconds', 'HTTP, с'),
    ('cpu_seconds', 'CPU модели, с'), ('observed_rss_bytes', 'Наблюдённый RSS, байт'),
    ('typed_private_masks', 'Приватных MASK с типизацией'),
    ('correct_kind_and_scope', 'Верны тип и область действия'),
    ('reason', 'Причина ограничения'),
)


def completed_run(path, model, result_sha256):
    """Verify a terminal outcome against the full, pinned Gemma request plan."""
    run = load_run(path)
    require(run['model'] == model and run['result_sha256'] == result_sha256
            and run['run_status'] == 'completed' and all(row['protocol_complete'] for row in run['rows']),
            'ABCD_completed_result_binding_changed')
    plan = strict_json(Path(run['plan_path']).read_text())
    require(plan['signature']['paired_reference'] == bind(plan), 'ABCD_recorded_reference_changed')
    state = strict_json(Path(path).read_text())
    require(cleanup_durable(state), 'ABCD_completed_cleanup_missing')
    for row in run['rows']:
        row['request_errors'] = sum(bool(parse_response(entry)[1]) for entry in state['responses'].values()
                                    if entry['variant'] == row['variant'])
    return run


def build(study, applications):
    study, applications = Path(study), Path(applications)
    config = strict_json((study / 'config.json').read_text())
    state = strict_json((study / 'status.json').read_text())
    require(digest(config) == state['config_sha256'], 'ABCD_configuration_changed')
    order = state['model_order']
    require(order == config['model_order'] and len(order) == len(MODELS) and set(order) == set(MODELS),
            'ABCD_order_changed')
    app_config = strict_json((applications / 'config.json').read_text())
    app_state = strict_json((applications / 'status.json').read_text())
    require(file_sha(applications / 'config.json') == app_state['config_sha256'], 'ABCD_application_configuration_changed')
    binary = app_config['binary_sha256']
    jobs = {(job['model'], job['method']): job for job in app_config['jobs']}
    require(len(jobs) == len(app_config['jobs']) and jobs.keys() == {(m, v) for m in MODELS for v in VARIANTS},
            'ABCD_application_plan_changed')
    rows, runs = [], []
    for model in order:
        receipt = state['jobs'].get(model, {})
        status = receipt.get('status', 'pending')
        result_path = study / 'runs' / model / 'result.json'
        measured = {}
        if status.startswith('completed'):
            run = completed_run(result_path, model, receipt['result_sha256'])
            runs.append(run)
            measured = {row['variant']: row for row in run['rows']}
        for variant in VARIANTS:
            row = {'model': model, 'variant': variant, 'status': status, 'reason': receipt.get('reason')}
            if variant in measured:
                entry = measured[variant]
                quality, evidence, perf = entry['quality'], entry['outcome_evidence'], entry['performance']
                row.update({key: quality[key] for key in ('private_mask', 'private_keep', 'public_mask', 'explicit_review')})
                row.update(delivered_cases=evidence['delivered_scored_cases'],
                    missing_cases=evidence['unobserved_scored_cases'],
                    inapplicable_received=evidence['delivered_inapplicable_cases'],
                    request_errors=entry['request_errors'], received_requests=entry['received_responses'],
                    attempted_requests=entry['registered_request_slots'],
                    http_seconds=perf['http_wall_seconds_including_retries'],
                    cpu_seconds=perf['known_model_cpu_seconds'],
                    observed_rss_bytes=perf['hardware_planning_model_observed_peak_rss_bytes'])
                if entry['typing'] is not None:
                    row.update(typed_private_masks=entry['typing']['private_mask_cases'],
                        correct_kind_and_scope=entry['typing']['kind_and_scope_correct'])
                job = jobs[model, variant]
                applied = app_state['jobs'].get(job['id'], {})
                if applied.get('status') == 'completed':
                    require(Path(job['source']).resolve() == result_path.resolve(), 'ABCD_application_source_changed')
                    coverage = application_summary(job, applied, run, binary, phase='calibration_controls')
                    row.update(private_full=coverage['private_spans_fully_covered'],
                        private_partial=coverage['private_spans_partly_covered'],
                        private_untouched=coverage['private_spans_untouched'], public_lost=coverage['public_occurrences_lost'],
                        public_replacements=coverage['public_reference_replacements'],
                        exact_roundtrips=coverage['exact_roundtrips'])
            rows.append(row)
    return {'created_at_utc': utc(), 'status': state['status'], 'application_status': app_state['status'],
            'model_order': order, 'unique_cases': 32, 'models_completed': len(runs),
            'application_jobs_completed': sum('exact_roundtrips' in row for row in rows),
            'exact_roundtrips': sum(row.get('exact_roundtrips', 0) for row in rows),
            'new_inference_calls': 0, 'rows': rows, 'runs': runs}


def render(report):
    rows = report['rows']
    all_observed = sum(all(row['outcome_evidence']['all_generation_outcomes_observed'] for row in run['rows'])
                       for run in report['runs'])
    lines = ['# Полный A/B/C/D новых моделей', '',
        f"Завершены все четыре варианта у {report['models_completed']} из 8 моделей. "
        f"Ответы на все {report['unique_cases'] * len(VARIANTS)} целей есть у {all_observed} из них. "
        f"Проверено применений {report['application_jobs_completed']} из 32, "
        f"восстановлений {report['exact_roundtrips']}.", '',
        'Каждый вариант получает те же 32 цели, поровну приватных и публичных. '
        'A использует исходный промпт. B добавляет политику приватности. '
        'C выделяет проверяемое вхождение. D запрашивает решение и его причину. '
        'Подробные данные по ошибкам, ресурсам и фактическим заменам доступны в CSV.', '']
    quality_fields = FIELDS[:11]
    lines += table([label for key, label in quality_fields], [
        [STATUS.get(row[key], row[key]) if key == 'status' else row.get(key) for key, _ in quality_fields]
        for row in rows])
    coverage_fields = FIELDS[:2] + FIELDS[13:19]
    lines += ['', '## Фактическое скрытие', '',
        'API применяет пригодные MASK к трём документам. '
        'Проверяем 193 приватных и 87 публичных контрольных вхождений.', '']
    applied = [row for row in rows if 'exact_roundtrips' in row]
    if applied:
        lines += table([label for key, label in coverage_fields], [
            [row.get(key) for key, _ in coverage_fields] for row in applied])
    else:
        lines += ['Применение ожидает завершённых решений модели.']
    reasons = dict.fromkeys((row['model'], row['reason']) for row in rows if row.get('reason'))
    if reasons:
        lines += ['', '## Ограничения', '']
        lines += [f'- {model} — `{reason}`' for model, reason in reasons]
    return '\n'.join(lines) + '\n'


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True)
    atomic_json(output / 'report.json', report)
    (output / 'REPORT.md').write_text(render(report), encoding='utf-8')
    with (output / 'comparison.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=[key for key, _ in FIELDS], lineterminator='\n')
        writer.writeheader()
        writer.writerows(report['rows'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, default=STUDY_ROOT / 'abcd-reference-v2')
    parser.add_argument('--applications', type=Path, default=STUDY_ROOT / 'abcd-reference-v2-application')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = build(args.study, args.applications)
    write_report(report, args.output)
    print({key: report[key] for key in ('models_completed', 'application_jobs_completed', 'exact_roundtrips')})


if __name__ == '__main__':
    main()
