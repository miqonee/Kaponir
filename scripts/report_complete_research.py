#!/usr/bin/env python3
"""Readable, provenance-checked progress for the complete thirteen-model study.

Reports never run inference, change rules or promote a partially observed winner.
Historical six-scheme rows and new controls are separate data series.
"""
import csv
import gzip
import hashlib
import json
from pathlib import Path

from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS, ROOT
from evaluate_model_schemes import EXPORT_CANONICAL_SHA, METHODS
from evaluate_scheme_matrix import COHORT, validation_quality
from kaponir_worker import FILES
from privacy_expansion_profiles import NEW_MODELS, pinned_models
from privacy_policy_holdout import load_holdout, score_holdout
from report_validation_results import ADDITIONAL
from run_privacy_policy_suite import MODEL_ORDER, atomic_json, file_sha, utc
from summarize_privacy_holdout import build_fresh_report, load_fresh_run, render_fresh
from summarize_privacy_policy import build_report, digest, render_markdown, require, strict_json
from validation_pipeline import Candidate, score


STATUS = {'pending': 'ожидает', 'running': 'идёт', 'launching': 'запускается',
    'completed': 'завершён', 'completed_technical_incomplete': 'завершён с тех. review',
    'unavailable_context': 'N/A: окно', 'failed': 'ошибка', 'archived': 'проверенный архив',
    'not_planned': 'отдельно не измерялся'}
METHOD_NAMES = dict(zip(METHODS, ['Детекторы', 'Добавляющий поиск', 'Проверка списка',
                                'Перепоиск записей', 'Проверка объединения', 'Два прохода']))
_CACHE = {}


def cell(value):
    return '—' if value is None else str(value).replace('|', '\\|').replace('\n', ' ')


def table(headers, rows):
    return ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join('---' for _ in headers) + '|'] + [
        '| ' + ' | '.join(cell(value) for value in row) + ' |' for row in rows]


def _cached(path, loader):
    path = Path(path)
    key = (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
    if key not in _CACHE:
        # Retain summaries only; raw multi-megabyte replies are not kept here.
        _CACHE[key] = loader(path)
        for previous in list(_CACHE):
            if previous[0] == key[0] and previous != key:
                del _CACHE[previous]
    return _CACHE[key]


def legacy_summary(path, archived=False):
    def load(source):
        raw = source.read_bytes()
        if archived:
            manifest = strict_json((ROOT / 'benchmark/data/validation/manifest.json').read_text())
            record = next(row for row in manifest['files'].values() if row['archive'] == source.name)
            require(hashlib.sha256(raw).hexdigest() == record['archive_sha256'], 'legacy_archive_hash_mismatch')
            raw = gzip.decompress(raw)
            require(hashlib.sha256(raw).hexdigest() == record['json_sha256'], 'legacy_json_hash_mismatch')
        state = strict_json(raw.decode('utf-8'))
        model = state['signature']['model']
        expected_artifact = FILES.get(model)
        pinned, _ = pinned_models()
        match = next((row for row in pinned['models'] if row['id'] == model), None)
        if match:
            expected_artifact = [match['artifact'][key] for key in ('filename', 'bytes', 'sha256', 'url')]
        require(expected_artifact is not None and state['signature']['artifact'] == list(expected_artifact),
                'scheme_model_artifact_mismatch')
        _, expected, public, sources, _ = load_corpus()
        expected = {key: value for key, value in expected.items() if key[0] in DOCUMENTS}
        expected_hashes = {name: hashlib.sha256(sources[name].encode()).hexdigest() for name in DOCUMENTS}
        require(state.get('shadow') is True and state.get('rules_changed') == 0, 'not_shadow_measurement')
        require(state['signature']['source_sha256'] == expected_hashes, 'legacy_source_mismatch')
        require(state['detector_export_sha256'] == EXPORT_CANONICAL_SHA, 'legacy_pool_mismatch')
        require(state['annotations_sha256'] == file_sha(ROOT / 'tests/docs/expected.json'), 'legacy_annotation_mismatch')
        if not archived:
            plan = strict_json(source.with_name('scheme-plan.json').read_text())
            require(digest({key: value for key, value in plan.items() if key != 'plan_sha256'}) == plan['plan_sha256'],
                    'scheme_plan_hash_mismatch')
            require(state['signature']['experiment_lineage']['plan_sha256'] == plan['plan_sha256'],
                    'scheme_lineage_mismatch')
            require(state['signature']['prompts'] == plan['prompt_sha256'], 'scheme_prompt_mismatch')
            require(digest(state['candidate_pool']) == plan['frozen_candidate_pool_sha256'], 'scheme_frozen_pool_mismatch')
            require(state['signature']['artifact'] == plan['model_artifact'], 'scheme_artifact_mismatch')
            if state['status'] == 'completed':
                journal = source.with_name('transports.json')
                require(state.get('transport_journal_sha256') == file_sha(journal), 'scheme_transport_journal_mismatch')
                transports = strict_json(journal.read_text())['requests']
                require(len(transports) == state['measurement_request_slots_recorded'], 'scheme_transport_count_mismatch')
                for entry in transports:
                    require(entry['request_sha256'] == digest(entry['request']), 'scheme_actual_request_hash_mismatch')
        rows = []
        for method in METHODS:
            stage = state.get('stages', {}).get(method)
            if stage is None:
                continue
            candidates = [Candidate(**row) for row in stage['candidates']]
            for candidate in candidates:
                require(candidate.file in sources and sources[candidate.file][candidate.start:candidate.end] == candidate.value,
                        'scheme_candidate_not_grounded')
            quality = score(candidates, stage['decisions'], expected, public)
            require(quality == state['metrics'][method], 'stored_scheme_metrics_mismatch')
            public_masks, technical, protected_masks = set(), set(), set()
            for candidate in candidates:
                decision = stage['decisions'].get(candidate.id, {})
                if candidate.protected:
                    protected_masks.add((candidate.file, candidate.value))
                elif decision.get('valid') and decision.get('verdict') == 'mask' and candidate.value in public.get(candidate.file, []):
                    public_masks.add((candidate.file, candidate.value))
                elif not decision.get('valid'):
                    technical.add((candidate.file, candidate.value))
            rows.append({'method': method, 'quality': quality, 'known_public_mask_without_protection': len(public_masks),
                'technical_review_values': len(technical), 'protected_values': len(protected_masks)})
        calls = state.get('calls', [])
        times = [row['seconds'] for row in calls if isinstance(row.get('seconds'), (int, float))]
        peaks = [row.get('sampled_resources', {}).get('roles', {}).get('model', {}).get('observed_peak_rss_bytes')
                 for row in calls]
        peaks = [value for value in peaks if value is not None]
        off = (state.get('native_template_probe') or {}).get('off', {})
        common_off_supported = not off['ends_with_think_prefix'] if 'ends_with_think_prefix' in off else None
        cpu = [row.get('sampled_resources', {}).get('roles', {}).get('model', {}).get('cpu_seconds') for row in calls]
        cpu = [value for value in cpu if value is not None]
        return {'model': state['signature']['model'], 'status': state['status'], 'archived': archived,
            'source_path': str(source), 'result_sha256': hashlib.sha256(raw).hexdigest(),
            'rows': rows, 'typing': validation_quality(state, expected), 'calls': len(calls),
            'http_seconds_known': sum(times), 'http_seconds_complete': len(times) == len(calls),
            'model_sampled_peak_rss_bytes': max(peaks) if peaks else None,
            'model_cpu_seconds_known': sum(cpu) if cpu else None,
            'weight_bytes': state['signature']['artifact'][1],
            'common_off_supported_by_observed_template': common_off_supported,
            'native_template_probe': state.get('native_template_probe'),
            'settings': state['signature']['settings']}
    return _cached(path, load)


def language_rows(path, profile):
    run = load_fresh_run(path)
    holdout = load_holdout()
    state = strict_json(Path(path).read_text())
    rows = []
    for language, suffix in [('RU', '-ru.md'), ('EN', '-en.md')]:
        cases = [case for case in holdout.cases if case.file.endswith(suffix)]
        expected = {case.id: holdout.expected_by_id[case.id] for case in cases}
        for variant in ('A', 'B', 'C', 'D'):
            decisions = state['stages'].get(variant, {}).get('decisions', {})
            quality = score_holdout(cases, {ident: row for ident, row in decisions.items() if ident in expected}, expected)
            rows.append({'model': run['model'], 'profile': profile, 'variant': variant, 'language': language,
                **{key: quality[key] for key in ('cases', 'attempted_cases', 'unattempted_cases', 'applicable_decisions', 'private_cases', 'public_cases',
                    'reference_review_cases', 'private_keep', 'public_mask', 'unnecessary_explicit_review',
                    'overconfident_on_reference_review', 'technical_review', 'correct_automatic_actions')}})
    return rows


def write_old_model_checkpoint(config, jobs):
    paths = [Path(job['output']) / 'result.json' for job in jobs if (Path(job['output']) / 'result.json').is_file()]
    report = build_fresh_report(paths, list(MODEL_ORDER))
    output = Path(config['output']) / 'before-expansion'
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / 'report.json', report)
    (output / 'report.md').write_text(render_fresh(report), encoding='utf-8')


def _csv(path, rows, fields):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def write_reports(config, state, jobs):
    output = Path(config['output']) / 'reports'
    output.mkdir(parents=True, exist_ok=True)
    all_models = (*MODEL_ORDER, *NEW_MODELS)
    rows = state.get('jobs', {})
    native_jobs = [job for job in jobs if job['kind'] == 'privacy' and job['dataset'] == 'holdout' and job['profile'] == 'native']
    common_jobs = [job for job in jobs if job['kind'] == 'privacy' and job['dataset'] == 'holdout' and job['profile'] == 'common']
    reports = {}
    languages, resources, rejected = [], [], []
    for profile, group in [('native', native_jobs), ('common', common_jobs)]:
        paths = [Path(job['output']) / 'result.json' for job in group if (Path(job['output']) / 'result.json').is_file()]
        report = build_fresh_report(paths, list(all_models))
        reports[profile] = report
        atomic_json(output / ('fresh-' + profile + '.json'), report)
        (output / ('fresh-' + profile + '.md')).write_text(render_fresh(report), encoding='utf-8')
        rejected.extend(report['rejected_inputs'])
        for run in report['runs']:
            try:
                languages.extend(language_rows(run['result_path'], profile))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                rejected.append({'path': run['result_path'], 'error': str(exc)})
            for row in run['rows']:
                performance = row['performance']
                resources.append({'model': run['model'], 'series': 'fresh-' + profile, 'variant': row['variant'],
                    'http_seconds': performance['http_wall_seconds_including_retries'],
                    'model_peak_rss_bytes': performance['model_observed_peak_rss_bytes'],
                    'model_cpu_seconds': performance['known_model_cpu_seconds'],
                    'weight_bytes': performance['model_disk_bytes'],
                    'context_tokens': run['profile']['context_tokens'], 'reference_only': True})
    calibration_paths = [Path(path) for path in config.get('reference_results', {}).values() if Path(path).is_file()]
    calibration_paths.extend(Path(job['output']) / 'result.json' for job in jobs
        if job.get('dataset') == 'calibration' and (Path(job['output']) / 'result.json').is_file())
    calibration = build_report(calibration_paths)
    atomic_json(output / 'calibration.json', calibration)
    (output / 'calibration.md').write_text(render_markdown(calibration), encoding='utf-8')
    rejected.extend(calibration['rejected_inputs'])
    legacy = []
    for model, name in {**COHORT, **ADDITIONAL}.items():
        path = ROOT / 'benchmark/data/validation' / (name + '--result.json.gz')
        try:
            legacy.append({**legacy_summary(path, True), 'profile': 'historical-common'})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            rejected.append({'path': str(path), 'error': str(exc)})
    for job in jobs:
        path = Path(job['output']) / 'result.json'
        if job['kind'] == 'schemes' and path.is_file():
            try:
                legacy.append({**legacy_summary(path), 'profile': job['profile']})
            except (ValueError, KeyError, TypeError, OSError) as exc:
                rejected.append({'path': str(path), 'error': str(exc)})
    for run in legacy:
        resources.append({'model': run['model'], 'series': 'six-schemes-' + run['profile'], 'variant': 'shared-run',
            'http_seconds': run['http_seconds_known'] if run['http_seconds_complete'] else None,
            'model_peak_rss_bytes': run['model_sampled_peak_rss_bytes'],
            'model_cpu_seconds': run['model_cpu_seconds_known'], 'weight_bytes': run['weight_bytes'],
            'context_tokens': run['settings']['context_tokens'], 'reference_only': True})
    atomic_json(output / 'schemes.json', {'unit': 'exact unique value per document; not all occurrences',
        'runs': legacy, 'outside_gold_is_not_proven_false_positive': True})
    scheme_table = []
    for run in legacy:
        for method in run['rows']:
            quality = method['quality']
            scheme_table.append([run['model'], run['profile'], METHOD_NAMES[method['method']],
                quality['proposal_matches'], quality['expected'], quality['confirmed_matches'],
                quality['correct_values_filtered'], method['known_public_mask_without_protection'],
                quality['visible_outside'], quality['needs_review_values'], method['protected_values']])
    scheme_lines = ['# Поиск и прежние шесть схем', '',
        'Семь архивных моделей и новые прогоны имеют один фиксированный исходный пул и корпус. '
        'Common использует 4096/1600, native имеет отдельные параметры. Новое клиентское ожидание 3600 с '
        'не меняет тело запроса. Политика/контракт A/B/C/D сюда не подмешаны.', '',
        '«Вне эталона» не означает доказанное ложное срабатывание. Ручной разбор не засчитывается как '
        'правильная автоматическая маска. Защищённый обход указан отдельно и не доказывает работу LLM.', '']
    scheme_lines += table(['Модель', 'Профиль', 'Схема', 'Найдено эталона', 'Всего эталона',
        'Подтверждено mask', 'Ошибочно отсеяно', 'Публичных mask без обхода', 'Вне эталона', 'На разборе', 'Защищённых'], scheme_table)
    (output / 'schemes.md').write_text('\n'.join(scheme_lines) + '\n', encoding='utf-8')
    ledger = []
    reference_runs = {run['model']: run for run in calibration['runs']}
    for model in all_models:
        selected = [job for job in jobs if job['model'] == model]
        def status_of(predicate):
            matches = [job for job in selected if predicate(job)]
            return rows.get(matches[0]['id'], {}).get('status', 'pending') if matches else 'not_planned'
        reference_status = 'pending'
        if model in reference_runs:
            run = reference_runs[model]
            reference_status = 'running' if run['run_status'] != 'completed' else (
                'completed_technical_incomplete' if any(row['quality']['technical_review'] for row in run['rows']) else 'completed')
        ledger.append({'model': model,
            'calibration': (reference_status if model in MODEL_ORDER else status_of(lambda job: job['dataset'] == 'calibration')),
            'schemes_common': 'archived' if model in MODEL_ORDER else status_of(lambda job: job['kind'] == 'schemes' and job['profile'] == 'common'),
            'schemes_native': status_of(lambda job: job['kind'] == 'schemes' and job['profile'] == 'native'),
            'fresh_native': status_of(lambda job: job['dataset'] == 'holdout' and job['profile'] == 'native'),
            'fresh_common': status_of(lambda job: job['dataset'] == 'holdout' and job['profile'] == 'common')})
    progress = {'version': 1, 'generated_at_utc': utc(), 'status': state['status'], 'planned_jobs': len(jobs),
        'terminal_jobs': sum(row['status'] in ('completed', 'completed_technical_incomplete', 'unavailable_context', 'failed') for row in rows.values()),
        'models': ledger, 'rejected_inputs': rejected, 'new_inference_calls': 0,
        'implementation_in_main_complete': False, 'quality_approval': False}
    atomic_json(output / 'progress.json', progress)
    lines = ['# Сравнение 13 моделей: ход измерений', '',
        'Снимок: ' + progress['generated_at_utc'] + '. Закончено заданий: '
        + str(progress['terminal_jobs']) + '/' + str(len(jobs)) + '. '
        'Качество определяет выбор; время, CPU и память приведены для справки.', '',
        'Завершение измерений ещё требует итогового разбора, mask→restore и проверенного внедрения в main. '
        'Пустые результаты не превращаются в нулевые ошибки. Совпавший результат или компромисс '
        'не разрешаются скоростью модели.', '']
    lines += table(['Модель', 'Калибровка A–D', '6 схем common', '6 схем native', 'Свежий native A–D', 'Свежий common A–D'],
        [[row['model'], STATUS.get(row['calibration'], row['calibration']),
          *(STATUS.get(row[key], row[key]) for key in ('schemes_common', 'schemes_native', 'fresh_native', 'fresh_common'))] for row in ledger])
    lines += ['', '## Таблицы качества', '',
        '- [Свежие документы, нативные профили](fresh-native.md).',
        '- [Свежие документы, общий запрошенный профиль](fresh-common.md).',
        '- [Калибровка A/B/C/D: отдельная серия](calibration.md).',
        '- [Поиск и шесть прежних схем](schemes.md).',
        '- [RU/EN по отдельности, CSV](languages.csv).',
        '- [Время и ресурсы, CSV](resources.csv).', '',
        'Нативный режим может отличаться от запрошенного: шаблон, фактически возвращённые reasoning-токены '
        'и причины N/A сохранены. Наблюдаемый RSS не является универсальной минимальной RAM. '
        'Размер GGUF и активные MoE-параметры не заменяют замер памяти.', '']
    if rejected:
        lines += ['Есть входы, не прошедшие проверку происхождения/целостности. Они не участвуют в выборе.', '']
    (output / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    language_fields = ['model', 'profile', 'variant', 'language', 'cases', 'attempted_cases', 'unattempted_cases', 'applicable_decisions', 'private_cases',
        'public_cases', 'reference_review_cases', 'private_keep', 'public_mask', 'unnecessary_explicit_review',
        'overconfident_on_reference_review', 'technical_review', 'correct_automatic_actions']
    _csv(output / 'languages.csv', languages, language_fields)
    _csv(output / 'resources.csv', resources, ['model', 'series', 'variant', 'http_seconds', 'model_peak_rss_bytes',
        'model_cpu_seconds', 'weight_bytes', 'context_tokens', 'reference_only'])
    return {'output': str(output), 'rejected_inputs': rejected, 'terminal_jobs': progress['terminal_jobs']}
