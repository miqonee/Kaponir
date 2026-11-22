#!/usr/bin/env python3
"""Put verified manufacturer measurements next to the corresponding reference rows."""

import argparse
import csv
import gzip
from pathlib import Path
import time

from apply_scheme_masks import complete_discovery_snapshot
from compare_local_models import ROOT
from evaluate_model_schemes import METHODS
from native_model_profiles import MODEL_ORDER, SOURCES
from native_retest_baselines import CHECKPOINTS, base_scheme_source, reapply_baselines, restore_snapshot
from privacy_policy_experiment import VARIANTS
from report_complete_research import METHOD_NAMES, legacy_summary, table
from report_new_models_first import application_summary
from run_native_model_retest import DEFAULT_STUDY, available, calibration_option
from run_privacy_policy_suite import SuiteLock, file_sha
from run_tiny_aya_context_supplement import (ABCD as TINY_ABCD, RECEIPT as TINY_RECEIPT,
    MODEL as TINY_AYA, SUPPLEMENT as TINY_SUPPLEMENT, original as tiny_original)
from summarize_privacy_policy import load_run, require, strict_json


DOCS = ROOT / 'docs/experiments'
MAIN = DOCS / 'NEW_MODELS_RESULTS.md'
DETAIL = DOCS / 'NATIVE_MODEL_RETEST.md'
BEGIN = '<!-- native-model-retest:start -->'
END = '<!-- native-model-retest:end -->'
LFM_CHECKPOINTS = (
    ('native-retest-lfm12-schemes', 'Шесть схем LFM1.2 с сырыми ответами и проверками API'),
    ('native-retest-lfm12-discovery', 'Срез добавляющего поиска LFM1.2'),
)
SCHEME_COVERAGE = ('private_full', 'private_partial', 'private_untouched', 'public_control_losses')
SCHEME_FIELDS = ('private_proposed', *SCHEME_COVERAGE, 'technical_review')
COVERAGE_FIELDS = (
    'private_spans_fully_covered', 'private_spans_partly_covered',
    'private_spans_untouched', 'public_occurrences_lost',
)
ABCD_FIELDS = ('private_mask', 'private_keep', 'public_mask', 'explicit_review', 'inapplicable_received', 'unobserved')
STATES = {
    'pending': 'ожидает', 'prepared': 'подготовлен', 'running': 'идёт', 'completed': 'завершён',
    'completed_technical_incomplete': 'есть технические отказы', 'failed': 'ошибка',
    'interrupted': 'прерван', 'incompatible': 'профиль не прошёл калибровку',
    'unavailable_context': 'N/A по окну',
}


def read_csv(path, group, fields):
    with path.open(encoding='utf-8', newline='') as stream:
        return {(row['model'], row[group]): {key: int(row[key]) if row[key] else None for key in fields}
                for row in csv.DictReader(stream)}


def verified_application(directory, measured, source, method, binary_sha, *, archived=False, phase='six_scheme_calibration'):
    path = directory / 'result.json'
    if not path.is_file():
        return None
    result = strict_json(path.read_text())
    if result['status'] != 'completed':
        return None
    evidence = {item.relative_to(directory).as_posix(): file_sha(item)
                for item in directory.glob('*/*/*') if item.name in ('mask.json', 'restore.json', 'masked.txt')}
    receipt = {'output': str(directory), 'result_sha256': file_sha(path),
               'source_file_sha256': file_sha(source), 'evidence_files_sha256': evidence, 'exact_roundtrips': 6}
    job = {'source': str(source), 'model': measured['model'], 'method': method, 'archived': archived}
    return application_summary(job, receipt, measured, binary_sha, phase=phase)


def scheme_values(directory, model, source, binary_sha, archived=False, *, discovery_snapshot=False):
    if source is None:
        return {}
    measured = legacy_summary(source, archived)
    require(measured['model'] == model, 'native_retest_scheme_identity_changed')
    if discovery_snapshot:
        complete_discovery_snapshot(strict_json(source.read_text()),
            strict_json(source.with_name('scheme-plan.json').read_text()),
            strict_json(source.with_name('transports.json').read_text()))
    else:
        require(measured['status'] == 'completed', 'native_retest_incomplete_schemes')
    values = {}
    for row in measured['rows']:
        method = row['method']
        if discovery_snapshot and method != 'current_union':
            continue
        app = verified_application(directory / method, measured, source, method, binary_sha, archived=archived)
        values[method] = {'private_proposed': row['quality']['proposal_matches'],
                          'technical_review': row['technical_review_values'],
                          **{field: (app or {}).get(key) for field, key in zip(SCHEME_COVERAGE, COVERAGE_FIELDS)},
                          'documents': app['documents'] if app else [],
                          'applied': app is not None}
    return values


def abcd_values(measured):
    values = {}
    for row in measured['rows']:
        evidence = row['outcome_evidence']
        values[row['variant']] = {**{key: row['quality'][key] for key in ABCD_FIELDS[:4]},
            'inapplicable_received': evidence['delivered_inapplicable_cases'],
            'unobserved': evidence['unobserved_scored_cases'],
            'typing': row['typing']}
    return values


def old_abcd_plan():
    checkpoint = CHECKPOINTS / '2026-11-17'
    key = 'legacy/runs/calibration-original/gemma4-e4b-qat/plan.json.gz'
    archive = checkpoint / key
    files = strict_json((checkpoint / 'manifest.json').read_text())['files']
    require(file_sha(archive) == files[key]['sha256'], 'native_retest_baseline_plan_changed')
    return strict_json(gzip.decompress(archive.read_bytes()).decode())


def same_abcd_cohort(base, native):
    left, right = base['signature'], native['signature']
    keys = ('model_artifact', 'case_data_sha256', 'source_sha256', 'annotations_sha256',
            'policy_sha256', 'kinds_sha256')
    return all(left[key] == right[key] for key in keys)


def same_abcd_profile(base, native):
    settings = ('context_tokens', 'reasoning_budget', 'timeout', 'retry_output_tokens')
    if any(base['signature']['settings'][key] != native['signature']['settings'][key] for key in settings):
        return False
    return [row['request_sha256'] for row in base['requests']] == [
        row['request_sha256'] for row in native['requests']]


def differences(base, native, fields, paired):
    if not paired or base is None or native is None:
        return [None] * len(fields)
    return [native[key] - base[key] if base[key] is not None and native[key] is not None else None
            for key in fields]


def resources(path, stage):
    if not path.is_file():
        return None
    result = strict_json(path.read_text())
    records = (result.get('calls', []) if stage == 'schemes'
               else [entry['record'] for entry in result.get('responses', {}).values()])
    if not records:
        return None
    peaks = [entry.get('sampled_resources', {}).get('roles', {}).get('model', {}).get('observed_peak_rss_bytes')
             for entry in records]
    timed = [entry['http_wall_seconds'] for entry in records if entry.get('http_wall_seconds') is not None]
    usage = [entry['usage'] for entry in records if entry.get('usage')]
    return {
        'calls': len(records), 'received_usage': len(usage),
        'seconds': sum(timed) if timed else None,
        'prompt_tokens': sum(row['prompt_tokens'] for row in usage) if usage else None,
        'completion_tokens': sum(row['completion_tokens'] for row in usage) if usage else None,
        'reasoning_tokens': sum(entry.get('reasoning_text_tokens') or 0 for entry in records) if usage else None,
        'truncated': sum(entry.get('finish_reason') == 'length' for entry in records),
        'peak_gib': max(peaks, key=lambda value: value or 0) / 1024**3 if any(peaks) else None,
    }


def tiny_context_receipt(study):
    path = study / TINY_RECEIPT
    if not path.is_file():
        return None
    receipt = strict_json(path.read_text())
    _, provenance = tiny_original(study)
    require(receipt['provenance'] == provenance and set(receipt['applications']) <= set(VARIANTS),
            'tiny_aya_supplement_provenance_changed')
    if available(receipt):
        path = study / TINY_SUPPLEMENT / TINY_ABCD / 'plan.json'
        plan = strict_json(path.read_text())
        signature = plan['signature']
        require(file_sha(path) == receipt['plan_file_sha256']
                and signature['quality_contract']['context_supplement'] == provenance
                and signature['settings']['output_tokens'] == provenance['output_tokens'],
                'tiny_aya_supplement_plan_receipt_changed')
    return receipt


def build(study):
    state_path = study / 'status.json'
    status = strict_json(state_path.read_text()) if state_path.is_file() else {}
    jobs = status.get('jobs', {})
    binary_sha = status.get('binary_sha256')
    old_schemes = read_csv(DOCS / 'new-models-reference-v2-comparison.csv', 'method', SCHEME_FIELDS)
    old_abcd = read_csv(DOCS / 'old-models-abcd.csv', 'variant', ABCD_FIELDS)
    tiny_supplement = tiny_context_receipt(study)
    rows = []
    for model in MODEL_ORDER:
        pilot_dir = study / 'calibration' / model
        pilot = strict_json((pilot_dir / 'result.json').read_text()) if (pilot_dir / 'result.json').is_file() else None
        chosen = calibration_option(pilot_dir, model) if pilot else None
        last_calibration = jobs.get(f'calibration/{model}', {})
        blocked = last_calibration if last_calibration.get('status') in ('failed', 'incompatible', 'interrupted') else {}
        schemes = jobs.get(f'schemes/{model}', blocked)
        abcd = jobs.get(f'abcd/{model}', blocked)
        supplement = tiny_supplement if model == TINY_AYA else None
        abcd_root = study / TINY_SUPPLEMENT if supplement else study
        if supplement:
            abcd = supplement
        abcd_source = abcd_root / 'abcd' / model / 'result.json'
        native_schemes = {}
        baseline_schemes = {}
        baseline_resources = None
        native_abcd = {}
        base_abcd = {v: old_abcd.get((model, v)) for v in VARIANTS}
        abcd_paired = False
        abcd_repetition = False
        snapshot = study / 'snapshots/current_union' / model
        discovery_snapshot = False
        if available(schemes):
            source = study / 'schemes' / model / 'result.json'
            require(file_sha(source) == schemes['result_sha256'], 'native_retest_scheme_receipt_changed')
            native_schemes = scheme_values(study / 'application/schemes' / model, model, source, binary_sha)
        if (not native_schemes.get('current_union', {}).get('applied')
                and (snapshot / 'application/current_union/result.json').is_file()):
            native_schemes.update(scheme_values(snapshot / 'application', model, snapshot / 'result.json',
                                                binary_sha, discovery_snapshot=True))
            discovery_snapshot = True
        applied = study / 'baseline-application' / model
        if applied.is_dir() and binary_sha:
            source, archived = base_scheme_source(study, model)
            baseline_schemes = scheme_values(applied, model, source, binary_sha, archived)
            if source is not None and not archived:
                baseline_resources = resources(source, 'schemes')
        if available(abcd):
            source = abcd_source
            require(file_sha(source) == abcd['result_sha256'], 'native_retest_abcd_receipt_changed')
            plan = strict_json(source.with_name('plan.json').read_text())
            measured = load_run(source)
            native_abcd = abcd_values(measured)
            for variant, values in native_abcd.items():
                directory = abcd_root / 'application/abcd' / model / variant
                if supplement:
                    expected = supplement['applications'].get(variant)
                    if expected is None:
                        continue
                    require(file_sha(directory / 'result.json') == expected,
                            'tiny_aya_supplement_application_receipt_changed')
                values['application'] = verified_application(
                    directory, measured, source, variant, binary_sha, phase='calibration_controls')
            if model == 'gemma4-e4b-qat':
                reference = old_abcd_plan()
                abcd_paired = same_abcd_cohort(reference, plan)
                abcd_repetition = abcd_paired and same_abcd_profile(reference, plan)
            else:
                former = study.parent / 'abcd-reference-v2/runs' / model / 'result.json'
                if former.is_file():
                    prior = load_run(former)
                    if prior['run_status'] == 'completed':
                        base_abcd = abcd_values(prior)
                        reference = strict_json(former.with_name('plan.json').read_text())
                        abcd_paired = same_abcd_cohort(reference, plan)
                        abcd_repetition = abcd_paired and same_abcd_profile(reference, plan)
        rows.append({
            'model': model, 'profile': strict_json(chosen.read_text()) if chosen else None,
            'pilot': pilot, 'schemes_status': schemes, 'abcd_status': abcd,
            'context_supplement': supplement,
            'discovery_snapshot': discovery_snapshot,
            'abcd_repetition': abcd_repetition,
            'baseline_scheme_resources': baseline_resources,
            'resources': {'schemes': resources(study / 'schemes' / model / 'result.json', 'schemes'),
                          'abcd': resources(abcd_source, 'abcd')},
            'schemes': {method: {
                'base': baseline_schemes.get(method) or old_schemes.get((model, method)),
                'native': native_schemes.get(method),
                'paired': baseline_schemes.get(method, {}).get('applied', False)
                          and native_schemes.get(method, {}).get('applied', False)}
                for method in METHODS},
            'abcd': {variant: {
                'base': base_abcd[variant], 'native': native_abcd.get(variant),
                'paired': abcd_paired and base_abcd[variant]['unobserved'] == 0
                          and native_abcd[variant]['unobserved'] == 0}
                for variant in VARIANTS},
        })
    return rows


def numbers(values, fields):
    if not values or all(values[key] is None for key in fields):
        return '—'
    return ' / '.join('—' if values[key] is None else str(values[key]) for key in fields)


def deltas(row, fields):
    changes = differences(row['base'], row['native'], fields, row['paired'])
    if all(value is None for value in changes):
        return '—'
    return ' / '.join('—' if value is None else f'{value:+d}' if value else '0' for value in changes)


def result_cells(row, fields):
    return [numbers(row['base'], fields), numbers(row['native'], fields), deltas(row, fields)]


def status_text(receipt):
    return STATES.get(receipt.get('status', 'pending'), 'ожидает')


def typing_score(row):
    value = (row or {}).get('typing')
    if value:
        return f"{value['kind_and_scope_correct']} / {value['private_mask_cases']}"
    return '—'


def conclusion(row):
    fields = ('private_full', 'private_untouched', 'public_control_losses')
    full, untouched, public = differences(row['base'], row['native'], fields, row['paired'])
    if full is None or untouched is None or public is None:
        return '—'
    if full >= 0 and untouched <= 0 and public <= 0 and any((full, untouched, public)):
        return 'улучшение'
    if full <= 0 and untouched >= 0 and public >= 0 and any((full, untouched, public)):
        return 'ухудшение'
    return 'одинаковые показатели' if (full, untouched, public) == (0, 0, 0) else 'компромисс'


def render_main(rows):
    fields = SCHEME_COVERAGE
    lines = [BEGIN, '## Основной и нативный профили', '',
        'Добавляющий поиск. В каждой ячейке четыре числа в одном порядке — полностью скрыто, '
        'частично скрыто, не затронуто из 193 приватных вхождений и потеряно из 87 публичных контролей. '
        'Δ рассчитана после применения обоих результатов одним бинарником.', '']
    summary = []
    for row in rows:
        search = row['schemes']['current_union']
        interpretation = conclusion(search) if search['paired'] else status_text(row['schemes_status'])
        if (search['native'] and search['native'].get('applied')
                and (search['base'] or {}).get('private_full') is None):
            interpretation = 'первый полный замер'
        if row['discovery_snapshot']:
            interpretation += ' (добавляющий поиск проверен через API)'
        summary.append([row['model'], *result_cells(search, fields), interpretation])
    lines += table(['Модель', 'Основной', 'Нативный', 'Δ нативный − основной', 'Итог / статус'], summary)
    lines += ['', 'Шесть схем, все варианты A/B/C/D и точные настройки каждой модели '
        'собраны в [отчёте повторного прогона](NATIVE_MODEL_RETEST.md).', END, '']
    return '\n'.join(lines)


def render_detail(rows):
    lines = ['# Повторный прогон моделей на настройках производителей', '',
        'Три прежних документа, шесть схем и 32 цели A/B/C/D. Калибровка профиля использует '
        'отдельные русский и английский документы. Сводка по добавляющему поиску находится '
        'в [основном отчёте](NEW_MODELS_RESULTS.md#основной-и-нативный-профили).', '',
        'Окно 16 384 токена, у Tiny Aya 8 192. В основной очереди выход 4 096, один повтор с лимитом до 8 192 при '
        'усечении и наличии контекста. Ожидание HTTP 1 200 секунд. Один слот, два CPU-потока, llama.cpp b12038.', '',
        '## Выбранные профили', '']
    profiles = []
    for row in rows:
        profile = row['profile']
        key = row['model']
        pilot = ', '.join(f"{option['profile']['option']} "
                  f"{sum(stage['valid'] for stage in option['stages'])}/4"
                  for option in row['pilot']['options']) if row['pilot'] else '—'
        settings = profile['settings'] if profile else None
        sampler = (' / '.join(str(settings[field]) for field in ('temperature', 'top_p', 'top_k', 'min_p'))
                   if settings else '—')
        penalties = (' / '.join(str(settings['extra_sampling'].get(field, '—'))
                     for field in ('repeat_penalty', 'presence_penalty')) if settings else '—')
        budget = settings['reasoning_budget'] if settings and settings['reasoning_budget'] > 0 else '—'
        abcd = 'повтор того же профиля' if row['abcd_repetition'] else status_text(row['abcd_status'])
        if row['context_supplement']:
            cap = f"{row['context_supplement']['provenance']['output_tokens']:,}".replace(',', ' ')
            abcd += f' · выход {cap}'
        profiles.append([f"[{key}]({SOURCES[key]})", profile['option'] if profile else '—', sampler, penalties,
                         budget, pilot, status_text(row['schemes_status']), abcd])
    lines += table(['Модель и источник', 'Профиль', 'temp / top-p / top-k / min-p', 'repeat / presence',
                    'Бюджет рассуждений', 'Калибровка, пригодных ответов', 'Шесть схем', 'A/B/C/D'], profiles)
    calibration = [[row['model'], option['profile']['option'],
                    sum(stage['private_missed'] for stage in option['stages'] if stage['stage'] == 'discovery')
                    if option['stages'] else None,
                    sum(stage['private_missed'] for stage in option['stages'] if stage['stage'] == 'D')
                    if option['stages'] else None,
                    option.get('public_masked'), 'да' if option['eligible'] else 'нет']
                   for row in rows if row['pilot'] for option in row['pilot']['options']]
    if calibration:
        lines += ['', '### Как выбран профиль', '',
            'Среди пригодных режимов выбирается профиль с наименьшей суммой пропусков в поиске и D. '
            'При равенстве учитываются публичные MASK, затем заранее заданный порядок профилей. '
            'Непригодный ответ засчитывается как пропуск всех приватных целей запроса.', '']
        lines += table(['Модель', 'Профиль', 'Пропуски в поиске', 'Пропуски в D',
                        'Публичные MASK', 'Пригоден'], calibration)
    lines += ['', '## Шесть схем', '',
        'Порядок чисел — найдено приватных значений из 92, полностью скрыто, частично скрыто, '
        'не затронуто из 193 приватных вхождений, потеряно из 87 публичных контрольных вхождений '
        'и технически непригодных решений. Для итога лучше больше полных скрытий, '
        'меньше нетронутых приватных вхождений и меньше потерь публичных контролей. '
        'Если эти показатели изменились в разные стороны, итог — компромисс.', '']
    lines += table(['Модель', 'Схема', 'Основной', 'Нативный', 'Δ нативный − основной', 'Итог покрытия'],
        [[row['model'], METHOD_NAMES[method], *result_cells(row['schemes'][method], SCHEME_FIELDS),
          conclusion(row['schemes'][method])]
         for row in rows for method in METHODS])
    document_rows = []
    for row in rows:
        search = row['schemes']['current_union']
        if search['paired']:
            baseline = {doc['file']: doc for doc in search['base']['documents']}
            for doc in search['native']['documents']:
                pair = {'base': baseline[doc['file']], 'native': doc, 'paired': True}
                document_rows.append([row['model'], doc['file'], *result_cells(pair, COVERAGE_FIELDS)])
    if document_rows:
        lines += ['', '### Добавляющий поиск по документам', '',
                  'Полностью скрыто / частично скрыто / не затронуто / публичных потерь.', '']
        lines += table(['Модель', 'Документ', 'Основной', 'Нативный', 'Δ нативный − основной'], document_rows)
    lines += ['', '## A/B/C/D', '',
        'Порядок чисел — приватных MASK, приватных KEEP, публичных MASK, явных REVIEW, '
        'непригодных решений и целей без ответа. В каждом варианте 16 приватных и 16 публичных целей. '
        'Для применения через API указано число полностью скрытых и нетронутых приватных вхождений, '
        'потери публичных контролей и число точных восстановлений.', '']
    if any(row['context_supplement'] for row in rows):
        lines += ['У Tiny Aya исходная попытка с резервом 4 096 остановилась на проверке окна '
                  'до запросов оценки. Отдельный повтор использует начальный лимит 2 048 '
                  'и полные документы. План и проверка исходного окна связаны контрольными суммами.', '']
    app_fields = ('private_spans_fully_covered', 'private_spans_untouched', 'public_occurrences_lost', 'exact_roundtrips')
    lines += table(['Модель', 'Вариант', 'Основной', 'Нативный', 'Δ нативный − основной', 'Δ нативный − свой A',
                    'Верны тип и scope у приватных MASK', 'Нативный через API'],
        [[row['model'], variant, *result_cells(row['abcd'][variant], ABCD_FIELDS),
          deltas({'base': row['abcd']['A']['native'], 'native': row['abcd'][variant]['native'],
                  'paired': variant != 'A'}, ABCD_FIELDS),
          typing_score(row['abcd'][variant]['native']),
          numbers((row['abcd'][variant]['native'] or {}).get('application'), app_fields)]
         for row in rows for variant in VARIANTS])
    paired_times = []
    for row in rows:
        base = row['baseline_scheme_resources']
        native = row['resources']['schemes']
        if (available(row['schemes_status']) and base and native
                and base['seconds'] is not None and native['seconds'] is not None):
            paired_times.append([row['model'], round(base['seconds'] / 60, 2),
                round(native['seconds'] / 60, 2), base['calls'], native['calls']])
    if paired_times:
        lines += ['', '## HTTP-время шести схем в обоих прогонах', '',
                  'Суммарное время запросов из записей двух прогонов, включая повторы. '
                  'Калибровка и применение через API выполняются отдельно.', '']
        lines += table(['Модель', 'Основной, мин', 'Нативный, мин', 'Запросов в основном',
                        'Запросов в нативном'], paired_times)
    measured = [[row['model'], 'Шесть схем' if stage == 'schemes' else 'A/B/C/D', res['calls'],
                 res['received_usage'], round(res['seconds'], 2) if res['seconds'] is not None else None,
                 res['prompt_tokens'], res['completion_tokens'], res['reasoning_tokens'], res['truncated'],
                 round(res['peak_gib'], 2) if res['peak_gib'] is not None else None]
                for row in rows for stage, res in row['resources'].items() if res]
    if measured:
        lines += ['', '## Ресурсы нативных прогонов', '',
            'Токены суммированы по ответам, для которых сервер вернул счётчики. '
            'Токены рассуждений входят в общий выход.', '']
        lines += table(['Модель', 'Блок', 'Запросов', 'Ответов со счётчиками', 'HTTP, с', 'Вход', 'Выход',
                        'Рассуждения', 'Усечений', 'Пик RSS, GiB'], measured)
    limitations = [[row['model'], stage, row.get(key, {}).get('reason')] for row in rows
                   for stage, key in (('Шесть схем', 'schemes_status'), ('A/B/C/D', 'abcd_status'))
                   if row.get(key, {}).get('reason')]
    if limitations:
        lines += ['', '## Причины неполных прогонов', '']
        lines += table(['Модель', 'Блок', 'Причина'], limitations)
    lines += ['', '## Файлы прогона', '',
        'Планы, ответы и результаты API лежат в `artifacts/privacy-policy-phase2/native-retest-v1`. '
        'Отчёт пересчитывается командой `python3 scripts/report_native_model_retest.py --write-docs`. '
        'Флаг `--reapply-baselines` повторно применяет готовые основные решения тем же бинарником. '
        'Флаг `--follow` обновляет оба документа по мере завершения заданий.', '']
    completed_checkpoints = tuple((f'native-retest-{model}', f'Полный срез {model}') for model in MODEL_ORDER)
    for checkpoint, title in (*completed_checkpoints, *LFM_CHECKPOINTS):
        if (CHECKPOINTS / checkpoint / 'manifest.json').is_file():
            lines += [f'[{title}](../../research/privacy-policy-phase2/checkpoints/{checkpoint}/manifest.json).', '']
    return '\n'.join(lines)


def update_main(source, section):
    if BEGIN in source:
        start, finish = source.index(BEGIN), source.index(END) + len(END)
        return source[:start] + section.rstrip() + source[finish:]
    anchor = '## Что именно проверяется'
    start = source.index(anchor)
    return source[:start] + section + '\n' + source[start:]


def write_documents(rows):
    for path, contents in ((DETAIL, render_detail(rows)),
                           (MAIN, update_main(MAIN.read_text(), render_main(rows)))):
        if path.is_file() and path.read_text() == contents:
            continue
        pending = path.with_suffix('.md.part')
        pending.write_text(contents, encoding='utf-8')
        pending.replace(path)


def reapply_ready(study, binary, status):
    require(file_sha(binary) == status['binary_sha256'], 'native_retest_application_binary_changed')
    models = [model for model in MODEL_ORDER if available(status['jobs'].get(f'schemes/{model}', {}))]
    reapply_baselines(study, binary, models)


def follow(study, binary):
    watch = study / 'status.json'
    watch_supplement = study / TINY_RECEIPT
    last = (None, None)
    with SuiteLock(study / '.native-report.lock'):
        while True:
            if watch.is_file():
                raw = watch.read_text()
                extra = watch_supplement.read_text() if watch_supplement.is_file() else None
                if (raw, extra) != last:
                    status = strict_json(raw)
                    if raw != last[0]:
                        try:
                            reapply_ready(study, binary, status)
                        except (OSError, ValueError) as error:
                            print(f'Baseline application needs attention: {error}', flush=True)
                    rows = build(study)
                    write_documents(rows)
                    print('Updated both reports.', status['status'], flush=True)
                    last = raw, extra
                    supplement = strict_json(extra) if extra else {}
                    pending = (supplement.get('status') in ('prepared', 'running')
                               or available(supplement) and len(supplement['applications']) != len(VARIANTS))
                    if status['status'] in ('completed', 'completed_with_failures') and not pending:
                        return
            time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, default=DEFAULT_STUDY)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--reapply-baselines', action='store_true')
    parser.add_argument('--write-docs', action='store_true')
    parser.add_argument('--follow', action='store_true', help='Refresh both documents as the current queue progresses')
    parser.add_argument('--restore-snapshot', type=Path, help='Restore a read-only report snapshot to --study')
    args = parser.parse_args()
    args.study = args.study.resolve()
    binary = (args.binary or args.study / 'build/release/kaponir').resolve()
    if args.restore_snapshot:
        require(not args.follow and not args.reapply_baselines, 'native_retest_snapshot_is_not_a_resume_source')
        restore_snapshot(args.restore_snapshot, args.study)
    if args.follow:
        follow(args.study, binary)
        return
    if args.reapply_baselines:
        status = strict_json((args.study / 'status.json').read_text())
        reapply_ready(args.study, binary, status)
    rows = build(args.study)
    if args.write_docs:
        write_documents(rows)
    else:
        print(render_detail(rows), end='')


if __name__ == '__main__':
    main()
