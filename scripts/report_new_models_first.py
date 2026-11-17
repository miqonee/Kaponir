#!/usr/bin/env python3
"""Join saved model decisions and actual application evidence, without inference.

The old seven are rescored from archives. The new eight retain their registered
reference profile. Missing measurements stay missing; resources never rank quality.
"""
import argparse
import gzip
import hashlib
from pathlib import Path

from apply_measured_privacy import coverage, span_coverage
from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS, ROOT
from evaluate_model_schemes import METHODS
from report_complete_research import METHOD_NAMES, _csv, legacy_summary, table
from run_legacy_new_models import MODELS
from run_privacy_policy_suite import atomic_json, file_sha, utc
from scheme_reference import ARCHIVES, verify_reference_inputs
from summarize_privacy_policy import digest, require, strict_json


def application_summary(job, receipt, measured, binary_sha256):
    """Check receipts and recompute coverage from saved API responses only."""
    path = Path(receipt['output']) / 'result.json'
    require(file_sha(path) == receipt['result_sha256'], 'comparison_application_result_changed')
    data = strict_json(path.read_text())
    require(data['status'] == 'completed' and data['source_phase'] == 'six_scheme_calibration',
            'comparison_application_wrong_phase')
    require(data['model'] == measured['model'] == job['model']
            and data['variant'] == job['method'], 'comparison_application_wrong_identity')
    require(data['source_result_sha256'] == measured['result_sha256'],
            'comparison_application_wrong_source')
    require(data['binary_sha256'] == binary_sha256, 'comparison_application_different_binary')
    require(data['new_inference_calls'] == data['user_rules_changed'] == 0
            and data['model_predicted_type_or_scope_applied'] is False,
            'comparison_application_contract_changed')
    source = Path(job['source'])
    require(file_sha(source) == receipt['source_file_sha256'] == data['source_file_sha256'],
            'comparison_application_source_file_changed')
    raw = gzip.decompress(source.read_bytes()) if job['archived'] else source.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == measured['result_sha256'],
            'comparison_application_source_bytes_changed')
    row = next(row for row in measured['rows'] if row['method'] == job['method'])
    require(data['measured_decision_quality'] == row['quality'],
            'comparison_application_quality_mismatch')
    evidence = receipt['evidence_files_sha256']
    require(len(evidence) == 18, 'comparison_application_evidence_missing')
    for relative, expected_sha in evidence.items():
        artifact = path.parent / relative
        require(artifact.resolve().is_relative_to(path.parent.resolve())
                and file_sha(artifact) == expected_sha, 'comparison_application_evidence_changed')
    _, expected, public, sources, _ = load_corpus()
    private = {name: [value for file, value in expected if file == name] for name in DOCUMENTS}
    docs = {(doc['file'], doc['mode']): doc for doc in data['documents']}
    require(len(data['documents']) == len(docs) == data['exact_roundtrips'] == receipt['exact_roundtrips'] == 6
            and set(docs) == {(name, mode) for name in DOCUMENTS for mode in ('baseline', 'model_masks_added')},
            'comparison_application_documents_incomplete')
    for (name, mode), doc in docs.items():
        prefix = mode + '/' + str(DOCUMENTS.index(name) + 1).zfill(2) + '-' + Path(name).stem
        paths = [prefix + '/' + suffix for suffix in ('mask.json', 'restore.json', 'masked.txt')]
        require(all(relative in evidence for relative in paths), 'comparison_application_document_evidence_missing')
        mask, restored = [strict_json((path.parent / relative).read_text()) for relative in paths[:2]]
        original, masked = sources[name], mask['text']
        require((path.parent / paths[2]).read_text() == masked and restored['text'] == original
                and doc['restore_exact'] is True and doc['persistent_rules_unchanged'] is True,
                'comparison_application_restore_mismatch')
        rescored = {**coverage(original, masked, private[name], public.get(name, [])),
                    **span_coverage(original, private[name], mask['replacements'], masked)}
        require(all(doc[key] == value for key, value in rescored.items())
                and doc['replacements'] == len(mask['replacements']), 'comparison_application_coverage_mismatch')
    result = {'result_sha256': receipt['result_sha256'], 'source_result_sha256': measured['result_sha256'],
              'binary_sha256': binary_sha256, 'exact_roundtrips': 6, 'path': str(path), 'baseline': {}}
    fields = ('private_spans_fully_covered', 'private_spans_partly_covered', 'private_spans_untouched',
              'public_occurrences_lost')
    for mode in ('baseline', 'model_masks_added'):
        selected = [doc for (_, current), doc in docs.items() if current == mode]
        values = {key: sum(doc[key] for doc in selected) for key in fields}
        values['public_reference_replacements'] = sum(
            doc['replacements'] for doc in selected if doc['file'] == '08-public-reference.md')
        if mode == 'baseline':
            result['baseline'] = values
        else:
            result.update(values)
    return result


def build(study, old_applications, new_applications):
    study, old_applications, new_applications = map(Path, (study, old_applications, new_applications))
    config, state = [strict_json((study / name).read_text()) for name in ('config.json', 'status.json')]
    require(state['config_sha256'] == digest(config), 'comparison_model_configuration_changed')
    new_config = strict_json((new_applications / 'config.json').read_text())
    binary_sha = new_config['binary_sha256']
    runs, pending, rejected = {}, [], []
    for model, archive in ARCHIVES.items():
        runs[model] = legacy_summary(ROOT / 'benchmark/data/validation' / archive, archived=True)
    for model in MODELS:
        receipt = state['jobs'].get(model, {})
        if not receipt.get('status', '').startswith('completed'):
            pending.append({'model': model, 'status': receipt.get('status', 'pending')})
            continue
        path = study / 'runs' / model / 'result.json'
        try:
            require(file_sha(path) == receipt['result_sha256'], 'comparison_model_result_changed')
            plan = strict_json(path.with_name('scheme-plan.json').read_text())
            require(plan['legacy_reference'] == config['reference_binding']
                    == verify_reference_inputs(plan, config['reference_model']), 'comparison_reference_changed')
            measured = legacy_summary(path)
            require(measured['model'] == model and measured['status'] == 'completed'
                    and {row['method'] for row in measured['rows']} == set(METHODS),
                    'comparison_model_identity_or_completeness_changed')
            execution = strict_json(path.read_text())['research_execution']
            require(execution['runtime_cleanup_verified'] is True, 'comparison_runtime_cleanup_missing')
            peaks = [sample.get('roles', {}).get('model', {}).get('observed_peak_rss_bytes')
                     for sample in execution['sampled_resources']]
            peaks.append(measured['model_sampled_peak_rss_bytes'])
            measured = dict(measured, execution_wall_seconds=execution['execution_wall_seconds'],
                full_session_peak_rss_bytes=max((value for value in peaks if type(value) is int), default=None))
            runs[model] = measured
        except (ValueError, KeyError, TypeError, OSError) as error:
            rejected.append({'model': model, 'phase': 'measurement', 'reason': str(error)[:200]})
    applications, application_pending, seen = {}, [], set()
    for folder in (old_applications, new_applications):
        cfg, status = [strict_json((folder / name).read_text()) for name in ('config.json', 'status.json')]
        require(status['config_sha256'] == file_sha(folder / 'config.json'), 'comparison_application_configuration_changed')
        require(cfg['binary_sha256'] == binary_sha and file_sha(Path(cfg['binary'])) == binary_sha,
                'comparison_application_binary_mismatch')
        for job in cfg['jobs']:
            if job['kind'] != 'schemes':
                continue
            # The historical application owner also contains deferred common,
            # native and fresh jobs. Only its seven archived runs belong here.
            if folder == old_applications and (not job['archived'] or job['model'] not in ARCHIVES):
                continue
            if folder == new_applications and (job['archived'] or job['model'] not in MODELS):
                continue
            key = (job['model'], job['method'])
            require(key not in seen, 'comparison_duplicate_application')
            seen.add(key)
            receipt = status['jobs'].get(job['id'], {})
            if receipt.get('status') != 'completed' or job['model'] not in runs:
                application_pending.append({'model': key[0], 'method': key[1], 'status': receipt.get('status', 'pending')})
                continue
            try:
                applications[key] = application_summary(job, receipt, runs[key[0]], binary_sha)
            except (ValueError, KeyError, TypeError, OSError) as error:
                rejected.append({'model': key[0], 'method': key[1], 'phase': 'application', 'reason': str(error)[:200]})
    require(seen == {(model, method) for model in (*ARCHIVES, *MODELS) for method in METHODS},
            'comparison_application_plan_incomplete')
    rows = []
    for model in (*ARCHIVES, *MODELS):
        group = ('archived_reference' if model == config['reference_model'] else
                 'archived_other_profile' if model in ARCHIVES else 'new_matched_reference')
        measured = runs.get(model)
        for method in METHODS:
            source = next((row for row in measured['rows'] if row['method'] == method), None) if measured else None
            rows.append({'model': model, 'group': group, 'method': method,
                         'measurement': source, 'application': applications.get((model, method))})
    baselines = list({tuple(sorted(app['baseline'].items())) for app in applications.values()})
    require(len(baselines) <= 1, 'comparison_product_baseline_changed')
    return {'kind': 'new-models-first-applied-comparison-v1', 'created_at_utc': utc(),
            'report_producer_sha256': file_sha(Path(__file__)),
            'source_config_sha256': {str(folder / 'config.json'): file_sha(folder / 'config.json')
                                     for folder in (study, old_applications, new_applications)},
            'reference': config['reference_binding'], 'binary_sha256': binary_sha,
            'study_status': state['status'],
            'product_baseline': dict(baselines[0]) if baselines else None,
            'new_models_complete': sum(model in runs for model in MODELS), 'new_models_total': len(MODELS),
            'application_jobs_complete': len(applications),
            'exact_roundtrips': sum(app['exact_roundtrips'] for app in applications.values()),
            'new_inference_calls': 0, 'old_model_benchmarks_repeated': 0, 'performance_used_for_quality': False,
            'all_fifteen_in_one_identical_profile_claimed': False, 'rows': rows, 'runs': list(runs.values()),
            'pending_models': pending, 'pending_applications': application_pending, 'rejected_inputs': rejected}


def render(report):
    lines = ['# Новые модели: прежний корпус и фактическое применение', '',
        f"Завершены новые модели: {report['new_models_complete']}/{report['new_models_total']}. "
        f"Проверены применения: {report['application_jobs_complete']}; точных mask→restore: {report['exact_roundtrips']}. "
        f"Отклонённых входов: {len(report['rejected_inputs'])}. Генераций при построении отчёта: 0.", '',
        'Все шесть схем используют прежние документы и prompts. Архивные ответы семи моделей сохранены. '
        'Прямое совпадение профиля — Nanbeige и новые восемь; другие исторические настройки показаны отдельно. '
        'A–D и малый срез — отдельные серии, здесь они не смешиваются.', '',
        'MASK/KEEP и технический REVIEW считаются по уникальным значениям. Private MASK включает '
        'защищённый обход, поэтому не является числом правильных решений самой модели. Полное/частичное покрытие — '
        'по 193 приватным вхождениям; публичные потери — по 87 контрольным вхождениям. '
        'Последняя колонка учитывает все замены в полностью публичном документе. Пустой замер обозначен «—».', '',
        'Применение использует действующие детекторы и явно подтверждённые MASK, CUSTOM/personal. '
        'KEEP не создаёт исключений, тип/scope модели не применяются. Защищённые кандидаты обходят модель. '
        'Полное геометрическое покрытие не доказывает отсутствие остаточной информации; '
        'частичные и пересекающиеся диапазоны не являются независимыми событиями.', '']
    baseline = report.get('product_baseline')
    if baseline:
        lines += ['Обычные действующие детекторы во всех сохранённых применениях: '
            f"{baseline['private_spans_fully_covered']} полных, {baseline['private_spans_partly_covered']} частичных, "
            f"{baseline['private_spans_untouched']} нетронутых приватных диапазонов; "
            f"потеряно публичных контролей: {baseline['public_occurrences_lost']}. "
            'Строка «Детекторы» ниже означает отдельное явное подтверждение всех предложений, '
            'включая спорные, и отличается от обычного применения.', '']
    for title, groups in [('Совпадающий архивный профиль', {'archived_reference', 'new_matched_reference'}),
                          ('Остальные исторические профили', {'archived_other_profile'})]:
        lines += ['## ' + title, '']
        models = list(dict.fromkeys(row['model'] for row in report['rows'] if row['group'] in groups))
        for model in models:
            lines += ['### ' + model, '']
            if not any(row['model'] == model and row['measurement'] for row in report['rows']):
                lines += ['Полный результат ещё не получен; оценок качества нет.', '']
                continue
            values = []
            for row in report['rows']:
                if row['model'] != model:
                    continue
                measured, app = row['measurement'], row['application']
                quality = measured['quality'] if measured else {}
                values.append([METHOD_NAMES[row['method']], quality.get('confirmed_matches'),
                    quality.get('correct_values_filtered'), measured['technical_review_values'] if measured else None,
                    measured['protected_values'] if measured else None,
                    *[app.get(key) if app else None for key in ('private_spans_fully_covered',
                        'private_spans_partly_covered', 'private_spans_untouched',
                        'public_occurrences_lost', 'public_reference_replacements')]])
            lines += table(['Схема', 'Private MASK', 'Private KEEP', 'Тех. REVIEW', 'Защищённых',
                            'Полностью', 'Частично', 'Не затронуто', 'Потери контролей', 'Замен в публичном'], values) + ['']
    lines += ['## Время и память, справочно', '',
        'HTTP — сумма всех шести схем, включая ошибки; это не время одного ответа. '
        'Полный пик включает загрузку, запросный RSS — отдельный неполный замер. '
        'Наблюдённая память не задаёт минимальные требования к железу. CPU: два потока; GPU: 0. '
        'Архивный лимит контейнера — 2 CPU / 8 GiB, первый новый запуск — 4 CPU / 16 GiB; '
        'времена между этими средами не дают контролируемого выигрыша в скорости. '
        'Цикл runtime включает загрузку, запросы и cleanup, но не внешнюю проверку файлов. '
        'Ресурсы не входят в оценку качества.', '']
    values = []
    runs = {run['model']: run for run in report['runs']}
    for model in dict.fromkeys(row['model'] for row in report['rows']):
        run = runs.get(model)
        values.append([model, run['calls'] if run else None,
            None if run is None else round(run['http_seconds_known']/60, 2),
            None if run is None else run['http_seconds_complete'],
            None if not run or run.get('execution_wall_seconds') is None else round(run['execution_wall_seconds']/60, 2),
            None if not run or run.get('full_session_peak_rss_bytes') is None else round(run['full_session_peak_rss_bytes']/2**30, 2),
            None if not run or (run['model_sampled_peak_rss_bytes'] or run['model_snapshot_peak_rss_bytes']) is None else
                round((run['model_sampled_peak_rss_bytes'] or run['model_snapshot_peak_rss_bytes'])/2**30, 2),
            None if not run else round(run['weight_bytes']/2**30, 2),
            None if not run or run.get('common_off_supported_by_observed_template') is None else
                not run['common_off_supported_by_observed_template']])
    lines += table(['Модель', 'HTTP-запросов', 'HTTP, мин', 'Полный замер HTTP', 'Цикл runtime, мин',
                    'Пик всей сессии, GiB', 'Запросный RSS, GiB', 'GGUF, GiB', 'Think-префикс при off'], values)
    lines += ['', 'Последняя колонка описывает только наблюдённый шаблон при запросе off: '
        'True — префикс рассуждения остался, False — отсутствует, «—» — отдельного наблюдения нет. '
        'Переданный флаг не гарантирует фактическое отключение reasoning.', '']
    if report['rejected_inputs']:
        lines += ['', '## Входы, требующие проверки', '']
        lines += ['- ' + str(row) for row in report['rejected_inputs']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = ROOT / 'artifacts/privacy-policy-phase2'
    parser.add_argument('--study', type=Path, default=base / 'new-models-reference-v1')
    parser.add_argument('--old-applications', type=Path, default=base / 'application-v2')
    parser.add_argument('--new-applications', type=Path, default=base / 'new-models-reference-v1-application')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'comparison_output_exists_no_overwrite')
    report = build(args.study, args.old_applications, args.new_applications)
    args.output.mkdir(parents=True)
    atomic_json(args.output / 'report.json', report)
    (args.output / 'REPORT.md').write_text(render(report), encoding='utf-8')
    columns = ['model', 'group', 'method', 'private_mask', 'private_keep', 'technical_review',
               'protected_values', 'private_full', 'private_partial', 'private_untouched',
               'public_control_losses', 'public_reference_replacements']
    rows = []
    for row in report['rows']:
        measured, app = row['measurement'], row['application']
        q = measured['quality'] if measured else {}
        rows.append(dict(zip(columns, [row['model'], row['group'], row['method'], q.get('confirmed_matches'),
            q.get('correct_values_filtered'), measured['technical_review_values'] if measured else None,
            measured['protected_values'] if measured else None,
            *[app.get(key) if app else None for key in ('private_spans_fully_covered',
                'private_spans_partly_covered', 'private_spans_untouched',
                'public_occurrences_lost', 'public_reference_replacements')]])))
    _csv(args.output / 'comparison.csv', rows, columns)
    print({key: report[key] for key in ('new_models_complete', 'application_jobs_complete', 'exact_roundtrips', 'rejected_inputs')})


if __name__ == '__main__':
    main()
