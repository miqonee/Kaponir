#!/usr/bin/env python3
"""Join the completed model and API studies into one row per model."""
import argparse
import csv
from pathlib import Path

from evaluate_model_schemes import METHODS
from report_complete_research import METHOD_NAMES, table
from run_legacy_new_models import MODELS
from run_privacy_policy_suite import MODEL_ORDER, VARIANTS, atomic_json, file_sha
from scheme_reference import REQUESTED_REFERENCE
from summarize_privacy_policy import compare_quality, require, strict_json

MEASUREMENTS_CLOSED = {'measurements_finished', 'measurements_finished_with_limitations'}
APPLICATIONS_CLOSED = {'applications_finished', 'applications_finished_with_source_limitations'}
NO_QUALITY_RESULT = {'failed', 'unavailable_context'}
REASONS = {
    'native_thinking_off_not_supported': 'Модель требует рассуждений в шести схемах',
    'missing_generation_outcomes': 'В A–D есть цели без ответа',
}
SUMMARY_FIELDS = (
    'model', 'scheme_profile', 'schemes_measured', 'scheme_reason', 'documents_processed', 'private_found', 'private_added',
    'private_full', 'private_partial', 'private_untouched', 'public_lost', 'public_replacements',
    'scheme_choices', 'scheme_request_errors', 'scheme_http_minutes', 'scheme_peak_rss_gib',
    'abcd_profile', 'abcd_variants_with_all_responses', 'abcd_responses', 'abcd_reason',
    'd_private_mask', 'd_private_keep', 'd_public_mask', 'd_review', 'd_inapplicable', 'd_missing',
    'abcd_choices', 'abcd_request_errors', 'abcd_http_minutes', 'abcd_peak_rss_gib',
)


def ready(schemes, abcd):
    return (schemes['study_status'] in MEASUREMENTS_CLOSED
            and schemes.get('application_status') in APPLICATIONS_CLOSED
            and abcd['status'] in MEASUREMENTS_CLOSED
            and abcd['application_status'] in APPLICATIONS_CLOSED)


def indexed(rows, keys, expected):
    values = {tuple(row[key] for key in keys): row for row in rows}
    require(len(values) == len(rows) and values.keys() == expected, 'study_model_matrix_changed')
    return values


def scheme_choices(rows):
    candidates = {}
    for row in rows:
        application = row['application']
        if application is not None:
            candidates[row['method']] = (-application['private_spans_fully_covered'],
                                        application['public_occurrences_lost'],
                                        application['public_reference_replacements'])

    def dominates(other, current):
        return all(left <= right for left, right in zip(other, current)) and other != current

    return [method for method, axes in candidates.items()
            if not any(dominates(other, axes) for other in candidates.values())]


def scheme_summary(rows, run, unavailable):
    summary = {'schemes_measured': sum(row['measurement'] is not None for row in rows),
               'scheme_profile': rows[0]['group'], 'scheme_choices': scheme_choices(rows)}
    union = next(row for row in rows if row['method'] == 'current_union')
    if run is None:
        require(unavailable and unavailable['status'] in NO_QUALITY_RESULT
                and summary['schemes_measured'] == 0, 'study_scheme_run_unaccounted')
        summary['scheme_reason'] = unavailable.get('reason') or unavailable['status']
        return summary
    require(summary['schemes_measured'] == len(METHODS) and all(row['application'] for row in rows),
            'study_scheme_application_incomplete')
    outcome = union['application']
    peak = next((run.get(key) for key in ('full_session_peak_rss_bytes', 'model_sampled_peak_rss_bytes',
                 'model_snapshot_peak_rss_bytes') if run.get(key) is not None), None)
    summary.update(documents_processed=run['details']['documents_processed'],
        private_found=union['measurement']['quality']['proposal_matches'],
        private_added=union['details']['new_private_over_source_pool'],
        private_full=outcome['private_spans_fully_covered'], private_partial=outcome['private_spans_partly_covered'],
        private_untouched=outcome['private_spans_untouched'], public_lost=outcome['public_occurrences_lost'],
        public_replacements=outcome['public_reference_replacements'],
        scheme_request_errors=run['details']['requests_with_recorded_error'],
        scheme_http_minutes=round(run['http_seconds_known'] / 60, 2) if run['http_seconds_complete'] else None,
        scheme_peak_rss_gib=round(peak / 2**30, 2) if peak is not None else None)
    return summary


def abcd_summary(rows, profile):
    require(tuple(row['variant'] for row in rows) == VARIANTS, 'study_ABCD_variants_changed')
    require(all(row['protocol_complete'] and row['run_finalized'] for row in rows), 'study_ABCD_run_incomplete')
    evidence = [row['outcome_evidence'] for row in rows]
    observed = sum(entry['delivered_scored_cases'] for entry in evidence)
    full_variants = sum(entry['all_generation_outcomes_observed'] for entry in evidence)
    request_errors = [row.get('request_errors') for row in rows]
    timing = [row['performance']['http_wall_seconds_including_retries'] for row in rows]
    peaks = [row['performance'].get('hardware_planning_model_observed_peak_rss_bytes') for row in rows]
    peak = max((value for value in peaks if value is not None), default=None)
    summary = {'abcd_profile': profile, 'abcd_responses': observed,
        'abcd_variants_with_all_responses': full_variants, 'abcd_choices': [],
        'abcd_request_errors': sum(request_errors) if all(value is not None for value in request_errors) else None,
        'abcd_http_minutes': round(sum(timing) / 60, 2) if all(t is not None for t in timing) else None,
        'abcd_peak_rss_gib': round(peak / 2**30, 2) if peak is not None else None}
    if observed < 128:
        summary['abcd_reason'] = 'missing_generation_outcomes'
    else:
        frontier = compare_quality(rows)['frontier']
        summary['abcd_choices'] = [row['variant'] for row in rows if row['row_id'] in frontier]
    decision = rows[-1]
    delivered = decision['outcome_evidence']
    summary['d_missing'] = delivered['unobserved_scored_cases']
    if delivered['delivered_scored_cases']:
        quality = decision['quality']
        summary.update(d_private_mask=quality['private_mask'], d_private_keep=quality['private_keep'],
            d_public_mask=quality['public_mask'], d_review=quality['explicit_review'],
            d_inapplicable=delivered['delivered_inapplicable_cases'])
    return summary


def build(schemes, old, abcd):
    require(ready(schemes, abcd), 'study_queues_have_not_finished')
    require(schemes['reference_mode'] == REQUESTED_REFERENCE and not schemes['rejected_inputs'],
            'study_scheme_report_is_not_verified_auto')
    require(abcd['unique_cases'] == 32, 'study_ABCD_case_count_changed')
    models = (*MODEL_ORDER, *MODELS)
    method_rows = indexed(schemes['rows'], ('model', 'method'), {(m, method) for m in models for method in METHODS})
    variant_rows = indexed(abcd['rows'], ('model', 'variant'), {(m, v) for m in MODELS for v in VARIANTS})
    archived = indexed(old['runs'], ('model',), {(m,) for m in MODEL_ORDER})
    measured_schemes = {row['model']: row for row in schemes['runs']}
    unavailable_schemes = {row['model']: row for row in schemes['pending_models']}
    abcd_runs = {row['model']: row for row in abcd['runs']}
    result = []
    for model in models:
        methods = [method_rows[model, method] for method in METHODS]
        summary = {'model': model, **scheme_summary(methods, measured_schemes.get(model), unavailable_schemes.get(model))}
        if model in MODEL_ORDER:
            variants = archived[model,]['rows']
            summary.update(abcd_summary(variants, 'archived_gemma_reference' if model == 'gemma4-e4b-qat' else 'archived_original'))
        else:
            variants = [variant_rows[model, v] for v in VARIANTS]
            if model in abcd_runs:
                require(all(row.get('exact_roundtrips') == 6 for row in variants), 'study_ABCD_application_incomplete')
                summary.update(abcd_summary(abcd_runs[model]['rows'], 'gemma_reference'))
            else:
                require(all(row['status'] in NO_QUALITY_RESULT for row in variants), 'study_ABCD_run_unaccounted')
                summary.update(abcd_profile='gemma_reference', abcd_choices=[], abcd_responses=0,
                    abcd_variants_with_all_responses=0, d_missing=abcd['unique_cases'],
                    abcd_reason=variants[0].get('reason') or variants[0]['status'])
        result.append(summary)
    return {'models': result, 'corpus': schemes['corpus'], 'unique_ABCD_cases': abcd['unique_cases'],
            'verified_scheme_applications': schemes['application_jobs_complete'],
            'verified_new_ABCD_applications': abcd['application_jobs_completed'],
            'new_inference_calls': 0}


def render(report):
    def rate(value, denominator):
        return f'{value}/{denominator}' if value is not None else None

    corpus = report['corpus']
    total_cases = report['unique_ABCD_cases'] * len(VARIANTS)
    values = []
    for row in report['models']:
        added = row.get('private_added')
        private = f"{row['private_found']}/{corpus['private_values']} (+{added})" if added is not None else None
        values.append([row['model'], rate(row['schemes_measured'], len(METHODS)), private,
            rate(row.get('private_full'), corpus['private_occurrences']),
            rate(row.get('public_lost'), corpus['public_occurrences']),
            rate(row['abcd_responses'], total_cases), rate(row.get('d_private_mask'), 16),
            rate(row.get('d_public_mask'), 16), row.get('d_inapplicable'), row.get('d_missing')])
    lines = ['# Сводка исследования всех 15 моделей Kaponir', '',
        'Три документа содержат 92 приватных значения, 193 приватных и 87 публичных контрольных вхождений. '
        'Столбцы поиска показывают одну и ту же схему добавления к детекторам. Прибавка в скобках относится '
        'к уникальным значениям сверх исходного пула. Скрытие и потери измерены через API.', '',
        'A–D проверяют по 32 одинаковые цели на вариант. В основной таблице показано также решение D '
        'по 16 приватным и 16 публичным целям. Прочерк обозначает отсутствующее измерение. '
        'Полные таблицы схем и вариантов A–D лежат рядом со сводкой в пакете исследования.', '']
    lines += table(['Модель', 'Схем', 'Найдено (+новых)', 'Полностью скрыто', 'Публичных потерь',
        'Целей с ответом A–D', 'D приватных MASK', 'D публичных MASK', 'D непригодных', 'D без ответа'], values)
    lines += ['', '## Подходы для дальнейшей проверки', '',
        'Варианты в строке сохраняют измеримый компромисс. Для схем сравнивается полное скрытие и сохранность открытого текста. '
        'Для A–D учитываются ошибочные KEEP и MASK, ручная проверка и технические ошибки. '
        'Неполные ответы и затраты ресурсов учитываются отдельно.', '']
    lines += table(['Модель', 'Схемы', 'Варианты A–D', 'Ограничения'], [
        [row['model'], ', '.join(METHOD_NAMES[key] for key in row['scheme_choices']) or None, ', '.join(row['abcd_choices']) or None,
         ', '.join(REASONS.get(row[key], row[key]) for key in ('scheme_reason', 'abcd_reason') if row.get(key)) or None]
        for row in report['models']])
    lines += ['', '## Запросы и ресурсы', '',
        'Время HTTP включает запросы с повторами, когда они были записаны. RSS показывает наблюдавшийся '
        'пиковый объём оперативной памяти модели. Ресурсы приведены для подбора оборудования.', '']
    lines += table(['Модель', 'Документов', 'Ошибок запросов схем', 'Ошибок запросов A–D',
        'Шесть схем, мин', 'A–D, мин', 'RSS схем, ГиБ', 'RSS A–D, ГиБ'], [
        [row['model'], *(row.get(key) for key in ('documents_processed', 'scheme_request_errors', 'abcd_request_errors',
            'scheme_http_minutes', 'abcd_http_minutes', 'scheme_peak_rss_gib', 'abcd_peak_rss_gib'))]
        for row in report['models']])
    lines += ['', 'Строгий профиль шести схем `auto` объединяет новые модели с архивными Gemma, Granite и Nanbeige. '
        'В A–D новые восемь используют профиль сохранённой Gemma. Остальные архивные профили сохранены '
        'в CSV. Выводы по качеству относятся к проверенным контрольным документам.', '']
    return '\n'.join(lines)


def write_report(report, output):
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / 'all-models.json', report)
    (output / 'ALL_MODELS.md').write_text(render(report), encoding='utf-8')
    with (output / 'all-models.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS, lineterminator='\n')
        writer.writeheader()
        writer.writerows({**row, 'scheme_choices': '; '.join(row['scheme_choices']),
                         'abcd_choices': '; '.join(row['abcd_choices'])} for row in report['models'])


def from_checkpoint(checkpoint):
    manifest = strict_json((checkpoint / 'manifest.json').read_text())
    inputs = []
    for name in ('reports/schemes.json', 'reports/old-abcd.json', 'reports/new-abcd/report.json'):
        path = checkpoint / name
        require(file_sha(path) == manifest['files'][name]['sha256'], 'study_checkpoint_report_changed')
        inputs.append(strict_json(path.read_text()))
    return build(*inputs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'study_summary_output_exists')
    write_report(from_checkpoint(args.checkpoint), args.output)


if __name__ == '__main__':
    main()
