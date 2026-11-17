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
from report_complete_research import METHOD_NAMES, STATUS, _csv, legacy_summary, table
from run_legacy_new_models import MODELS
from run_privacy_policy_suite import atomic_json, file_sha, utc
from scheme_reference import (ARCHIVES, EFFECTIVE_REFERENCE, REQUESTED_REFERENCE,
                              matching_requested_references, verify_reference_inputs)
from summarize_privacy_policy import digest, require, strict_json
from validation_pipeline import Candidate, score


def corpus_summary():
    _, expected, public, sources, _ = load_corpus()
    documents = []
    for name in DOCUMENTS:
        private = [value for file, value in expected if file == name]
        counts = coverage(sources[name], sources[name], private, public.get(name, []))
        documents.append({'file': name, 'private_values': len(private),
            'private_occurrences': counts['private_occurrences_before'],
            'public_occurrences': counts['public_occurrences_before']})
    return {'documents': documents, 'document_count': len(documents),
            **{key: sum(doc[key] for doc in documents)
               for key in ('private_values', 'private_occurrences', 'public_occurrences')}}


def measurement_details(state):
    """Add denominators and source-pool attribution; never call a model."""
    _, expected, public, sources, _ = load_corpus()
    expected = {key: value for key, value in expected.items() if key[0] in DOCUMENTS}
    pool = {(row['file'], row['value']) for row in state['candidate_pool']}
    methods = {}
    for method in METHODS:
        stage = state.get('stages', {}).get(method)
        if stage is None:
            continue
        candidates = [Candidate(**row) for row in stage['candidates']]
        proposed = {(row.file, row.value) for row in candidates}
        quality = score(candidates, stage['decisions'], expected, public)
        documents = []
        for name in DOCUMENTS:
            selected = [row for row in candidates if row.file == name]
            gold = {key: value for key, value in expected.items() if key[0] == name}
            q = score(selected, stage['decisions'], gold, public)
            documents.append({'file': name, 'quality': q,
                'private_not_proposed': q['expected'] - q['proposal_matches'],
                'private_not_confirmed': q['expected'] - q['confirmed_matches']})
        methods[method] = {'documents_processed': len(DOCUMENTS),
            'private_not_proposed': quality['expected'] - quality['proposal_matches'],
            'private_not_confirmed': quality['expected'] - quality['confirmed_matches'],
            'new_private_over_source_pool': len((proposed - pool) & set(expected)),
            'documents': documents}
    calls = state.get('calls', [])
    return {'documents_processed': len(DOCUMENTS), 'methods': methods,
        'requests_with_recorded_error': sum(bool(row.get('error')) for row in calls),
        'request_errors_by_code': {code: sum(row.get('error') == code for row in calls)
            for code in sorted({row['error'] for row in calls if row.get('error')})},
        'requested_json': state['signature']['settings'].get('response_format'),
        'effective_json': state.get('structured_output')}


def saved_run_details(run):
    raw = Path(run['source_path']).read_bytes()
    raw = gzip.decompress(raw) if run['archived'] else raw
    require(hashlib.sha256(raw).hexdigest() == run['result_sha256'], 'comparison_details_source_changed')
    return measurement_details(strict_json(raw.decode('utf-8')))


def progress_counts(study, model, receipt):
    """Describe partial work without assigning an incomplete quality score."""
    output = study / 'runs' / model
    result, journal = output / 'result.json', output / 'transports.json'
    data = strict_json(result.read_text()) if result.is_file() else {}
    finished = {name for name in DOCUMENTS if 'discovery:' + name in data.get('stages', {})}
    attempted = finished | (set(data.get('discovery_progress', {})) & set(DOCUMENTS))
    requests = strict_json(journal.read_text())['requests'] if journal.is_file() else []
    return {'documents_attempted': len(attempted), 'documents_discovery_finished': len(finished),
            'responses_received': sum('raw_transport' in row for row in requests),
            'status': receipt.get('status', 'pending'), 'reason': receipt.get('reason')}


def application_summary(job, receipt, measured, binary_sha256, *, phase='six_scheme_calibration'):
    """Check receipts and recompute coverage from saved API responses only."""
    path = Path(receipt['output']) / 'result.json'
    require(file_sha(path) == receipt['result_sha256'], 'comparison_application_result_changed')
    data = strict_json(path.read_text())
    require(data['status'] == 'completed' and data['source_phase'] == phase,
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
    source_sha256 = file_sha(source)
    require(source_sha256 == receipt['source_file_sha256'], 'comparison_application_source_file_changed')
    if phase == 'calibration_controls':
        require(data['source_plan_sha256'] == measured['plan_sha256']
                and data.get('decision_view', 'original') == 'original'
                and 'transport_supplement_result_sha256' not in data,
                'comparison_application_wrong_calibration_plan_or_view')
        identifier = 'variant'
    else:
        require(source_sha256 == data['source_file_sha256'], 'comparison_application_source_file_changed')
        identifier = 'method'
    raw = gzip.decompress(source.read_bytes()) if job['archived'] else source.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == measured['result_sha256'],
            'comparison_application_source_bytes_changed')
    row = next(row for row in measured['rows'] if row[identifier] == job['method'])
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
              'binary_sha256': binary_sha256, 'exact_roundtrips': 6, 'path': str(path), 'baseline': {},
              'documents': [{key: doc[key] for key in ('file', 'private_spans_fully_covered',
                  'private_spans_partly_covered', 'private_spans_untouched', 'public_occurrences_lost', 'replacements')}
                  for (_, mode), doc in docs.items() if mode == 'model_masks_added']}
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
    reference_mode = config.get('reference_mode', EFFECTIVE_REFERENCE)
    require(reference_mode in (REQUESTED_REFERENCE, EFFECTIVE_REFERENCE), 'comparison_unknown_reference_mode')
    shared = {}
    if reference_mode == REQUESTED_REFERENCE:
        anchor = strict_json((study / 'prepared' / MODELS[0] / 'scheme-plan.json').read_text())
        require(anchor['legacy_reference'] == config['reference_binding']
                == verify_reference_inputs(anchor, config['reference_model']), 'comparison_prepared_reference_changed')
        shared = matching_requested_references(anchor)
    new_config = strict_json((new_applications / 'config.json').read_text())
    binary_sha = new_config['binary_sha256']
    runs, pending, rejected = {}, [], []
    for model, archive in ARCHIVES.items():
        runs[model] = legacy_summary(ROOT / 'benchmark/data/validation' / archive, archived=True)
    for model in MODELS:
        receipt = state['jobs'].get(model, {})
        if not receipt.get('status', '').startswith('completed'):
            progress = progress_counts(study, model, receipt)
            if reference_mode == EFFECTIVE_REFERENCE:
                progress.update(recorded_status=progress['status'], status='superseded')
            pending.append({'model': model, **progress})
            continue
        path = study / 'runs' / model / 'result.json'
        try:
            require(file_sha(path) == receipt['result_sha256'], 'comparison_model_result_changed')
            plan = strict_json(path.with_name('scheme-plan.json').read_text())
            require(plan.get('legacy_reference_mode', EFFECTIVE_REFERENCE) == reference_mode,
                    'comparison_requested_and_effective_profiles_mixed')
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
    for run in runs.values():
        run['details'] = saved_run_details(run)
    rows = []
    for model in (*ARCHIVES, *MODELS):
        group = ('archived_shared_requested' if model in shared else
                 'archived_other_profile' if model in ARCHIVES else
                 'new_matched_reference' if reference_mode == REQUESTED_REFERENCE else 'superseded_effective')
        measured = runs.get(model)
        for method in METHODS:
            source = next((row for row in measured['rows'] if row['method'] == method), None) if measured else None
            rows.append({'model': model, 'group': group, 'method': method,
                         'measurement': source, 'application': applications.get((model, method)),
                         'details': measured['details']['methods'][method] if source else None})
    baselines = list({tuple(sorted(app['baseline'].items())) for app in applications.values()})
    require(len(baselines) <= 1, 'comparison_product_baseline_changed')
    return {'kind': 'new-models-first-applied-comparison-v2', 'created_at_utc': utc(),
            'report_producer_sha256': file_sha(Path(__file__)),
            'source_config_sha256': {str(folder / 'config.json'): file_sha(folder / 'config.json')
                                     for folder in (study, old_applications, new_applications)},
            'reference': config['reference_binding'], 'reference_mode': reference_mode,
            'shared_requested_references': shared, 'corpus': corpus_summary(), 'binary_sha256': binary_sha,
            'study_status': state['status'],
            'application_status': status['status'],
            'product_baseline': dict(baselines[0]) if baselines else None,
            'new_models_complete': sum(model in runs for model in MODELS)
                if reference_mode == REQUESTED_REFERENCE else 0,
            'completed_new_experiments': sum(model in runs for model in MODELS), 'new_models_total': len(MODELS),
            'application_jobs_complete': len(applications),
            'exact_roundtrips': sum(app['exact_roundtrips'] for app in applications.values()),
            'new_inference_calls': 0, 'old_model_benchmarks_repeated': 0, 'performance_used_for_quality': False,
            'all_fifteen_in_one_identical_profile_claimed': False, 'rows': rows, 'runs': list(runs.values()),
            'pending_models': pending, 'pending_applications': application_pending, 'rejected_inputs': rejected}


def render(report):
    corpus = report.get('corpus') or corpus_summary()
    runs = {run['model']: run for run in report['runs']}
    pending = {row['model']: row for row in report.get('pending_models', [])}
    protocol = ('Одинаковый записанный исходный профиль auto — Gemma, Granite, Nanbeige и новые восемь; '
        'runtime выбирает grammar при поддержке схемы, иначе prompt, как в исходном опыте. '
        'Другие исторические настройки показаны отдельно.'
        if report.get('reference_mode', REQUESTED_REFERENCE) == REQUESTED_REFERENCE else
        'В сохранённой очереди v1 новые модели ошибочно получили принудительный prompt; '
        'эти попытки не входят в исправленное сравнение auto.')
    lines = ['# Новые модели: прежний корпус и фактическое применение', '',
        'Снимок: ' + report.get('created_at_utc', 'время не записано') + '. '
        'Архивные строки ниже — ранее полученные результаты; новая таблица не означает нового прогона.', '',
        f"Завершены новые модели: {report['new_models_complete']}/{report['new_models_total']}. "
        f"Проверены применения: {report['application_jobs_complete']}; точных mask→restore: {report['exact_roundtrips']}. "
        f"Отклонённых входов: {len(report['rejected_inputs'])}. Генераций при построении отчёта: 0.", '',
        'Все шесть схем используют прежние документы и prompts. Архивные ответы семи моделей сохранены. '
        + protocol + ' '
        'A–D и малый срез — отдельные серии, здесь они не смешиваются.', '',
        '## Что именно проверяется', '',
        'Каждая из шести схем обрабатывает те же три документа. Это три исходных документа, '
        'а не восемнадцать независимых примеров. Поиск использует прежнее деление на окна и записи.', '']
    lines += table(['Документ', 'Приватных значений', 'Приватных вхождений', 'Публичных контрольных вхождений'],
        [[doc['file'], doc['private_values'], doc['private_occurrences'], doc['public_occurrences']]
         for doc in corpus['documents']] + [['Всего', corpus['private_values'], corpus['private_occurrences'],
                                            corpus['public_occurrences']]])
    lines += ['', 'Кандидаты включают исходный пул из 94 значений, среди которых 68 приватных. '
        '«Найдено» означает точное приватное значение среди кандидатов. '
        '«MASK» — подтверждено к скрытию, включая защищённый обход. Поэтому MASK не равно '
        'числу правильных решений самой модели. «Новых приватных сверх пула» исключает уже имевшиеся значения. '
        '«Не найдено» и «Без MASK» считаются из 92; KEEP приватного значения — ошибка. '
        'Частичное совпадение с меткой может не попасть в точный счёт значений, поэтому отдельно '
        'показано фактическое покрытие 193 вхождений. Пустой замер — «—».', '',
        'Ошибки запроса и непригодные решения имеют разные единицы. Один невалидный JSON '
        'может затронуть несколько значений; складывать эти колонки нельзя. '
        'Непригодное решение остаётся REVIEW, не превращается в KEEP. '
        'Ложное скрытие оценивается по 87 публичным вхождениям, а также по всем заменам '
        'в полностью публичном документе.', '',
        'Применение использует действующие детекторы и явно подтверждённые MASK, CUSTOM/personal. '
        'KEEP не создаёт исключений, тип/scope модели не применяются. Защищённые кандидаты обходят модель. '
        'Полное геометрическое покрытие не доказывает отсутствие остаточной информации; '
        'частичные и пересекающиеся диапазоны не являются независимыми событиями.', '']
    if report.get('reference_mode') == EFFECTIVE_REFERENCE:
        lines += ['Этот файл читает остановленную очередь v1 с ошибочно принудительным prompt. '
            'Её новые попытки сохранены отдельно и не засчитываются как новые модели '
            'в исправленном сравнении auto.', '']
    lines += ['## Объём и ошибки запусков', '']
    volume = []
    for model in dict.fromkeys(row['model'] for row in report['rows']):
        run, progress = runs.get(model), pending.get(model, {})
        if run:
            detail = run.get('details', {})
            source = ('архив' if run.get('archived') else 'остановленная v1: prompt'
                      if report.get('reference_mode') == EFFECTIVE_REFERENCE else 'новый полный результат')
            volume.append([model, source,
                str(detail.get('documents_processed', corpus['document_count'])) + '/3',
                str(len(run['rows'])) + '/6', run['calls'], detail.get('requests_with_recorded_error'),
                str(detail.get('requested_json') or '—') + ' → ' + str(detail.get('effective_json') or '—')])
        else:
            status = ('N/A: thinking off не поддерживается' if progress.get('reason') ==
                      'native_thinking_off_not_supported' else 'v1 остановлена'
                      if progress.get('status') == 'superseded' else STATUS.get(progress.get('status', 'pending'), 'ожидает'))
            volume.append([model, status, '—', '—', progress.get('responses_received'), None, '—'])
    lines += table(['Модель', 'Источник / статус', 'Документов полного результата', 'Схем',
                    'HTTP-ответов / попыток в архиве', 'Запросов с записанной ошибкой', 'JSON: запрошен → выбран'], volume)
    lines += ['', 'У незавершённых моделей качество не оценивается. «HTTP» у архивов — записанные '
        'попытки, включая ошибки; у текущих моделей — фактически полученные транспорты. '
        'N/A до корпуса означает 0 документов и 0 измерительных запросов, а не нулевое качество.', '']
    for model, progress in pending.items():
        if progress.get('documents_attempted'):
            lines += [f"Текущий {model}: начато документов {progress['documents_attempted']}/3; "
                f"этап поиска завершён в {progress['documents_discovery_finished']}/3. "
                'Полный результат шести схем ещё не готов.', '']
    baseline = report.get('product_baseline')
    if baseline:
        lines += ['Обычные действующие детекторы во всех сохранённых применениях: '
            f"{baseline['private_spans_fully_covered']} полных, {baseline['private_spans_partly_covered']} частичных, "
            f"{baseline['private_spans_untouched']} нетронутых приватных диапазонов; "
            f"потеряно публичных контролей: {baseline['public_occurrences_lost']}. "
            'Строка «Детекторы» ниже означает отдельное явное подтверждение всех предложений, '
            'включая спорные, и отличается от обычного применения.', '']
    for title, groups in [('Одинаковый исходный профиль auto', {'archived_shared_requested', 'new_matched_reference'}),
                          ('Остальные исторические профили', {'archived_other_profile'}),
                          ('Сохранённые попытки с принудительным prompt', {'superseded_effective'})]:
        if not any(row['group'] in groups for row in report['rows']):
            continue
        lines += ['## ' + title, '']
        models = list(dict.fromkeys(row['model'] for row in report['rows'] if row['group'] in groups))
        for model in models:
            lines += ['### ' + model, '']
            if not any(row['model'] == model and row['measurement'] for row in report['rows']):
                lines += ['Полный результат ещё не получен; оценок качества нет.', '']
                continue
            values, decisions, applied = [], [], []
            for row in report['rows']:
                if row['model'] != model:
                    continue
                measured, app = row['measurement'], row['application']
                quality = measured['quality'] if measured else {}
                detail = row.get('details') or {}
                values.append([METHOD_NAMES[row['method']], detail.get('documents_processed'),
                    quality.get('proposals'), quality.get('proposal_matches'),
                    detail.get('new_private_over_source_pool'), detail.get('private_not_proposed'),
                    quality.get('confirmed_matches'), detail.get('private_not_confirmed')])
                decisions.append([METHOD_NAMES[row['method']], quality.get('correct_values_filtered'),
                    measured['technical_review_values'] if measured else None,
                    measured['protected_values'] if measured else None,
                    measured.get('known_public_mask_without_protection') if measured else None])
                applied.append([METHOD_NAMES[row['method']],
                    *[app.get(key) if app else None for key in ('private_spans_fully_covered',
                        'private_spans_partly_covered', 'private_spans_untouched',
                        'public_occurrences_lost', 'public_reference_replacements')]])
            lines += table(['Схема', 'Документов', 'Всего кандидатов', 'Найдено /92',
                'Новых приватных сверх пула', 'Не найдено /92', 'MASK /92', 'Без MASK /92'], values) + ['']
            lines += table(['Схема', 'Приватных KEEP', 'Тех. непригодных значений', 'Защищённых',
                            'Публичных MASK по точной метке'], decisions) + ['']
            lines += table(['Схема', 'Полностью /193', 'Частично /193', 'Не затронуто /193',
                            'Публичных потерь /87', 'Замен в публичном документе'], applied) + ['']
    lines += ['## Время и память, справочно', '',
        'HTTP — сумма всех шести схем, включая ошибки; это не время одного ответа. '
        'Полный пик включает загрузку, запросный RSS — отдельный неполный замер. '
        'Наблюдённая память не задаёт минимальные требования к железу. CPU: два потока; GPU: 0. '
        'Архивный лимит контейнера — 2 CPU / 8 GiB, первый новый запуск — 4 CPU / 16 GiB; '
        'времена между этими средами не дают контролируемого выигрыша в скорости. '
        'Цикл runtime включает загрузку, запросы и cleanup, но не внешнюю проверку файлов. '
        'Ресурсы не входят в оценку качества.', '']
    values = []
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
    parser.add_argument('--study', type=Path, default=base / 'new-models-reference-v2')
    parser.add_argument('--old-applications', type=Path, default=base / 'application-v2')
    parser.add_argument('--new-applications', type=Path, default=base / 'new-models-reference-v2-application')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'comparison_output_exists_no_overwrite')
    report = build(args.study, args.old_applications, args.new_applications)
    write_report(report, args.output)
    print({key: report[key] for key in ('new_models_complete', 'application_jobs_complete', 'exact_roundtrips', 'rejected_inputs')})


def write_report(report, output):
    require(not output.exists(), 'comparison_output_exists_no_overwrite')
    output.mkdir(parents=True)
    atomic_json(output / 'report.json', report)
    (output / 'REPORT.md').write_text(render(report), encoding='utf-8')
    columns = ['model', 'group', 'method', 'documents_processed', 'candidates', 'private_proposed',
               'new_private_over_source_pool', 'private_not_proposed', 'private_mask', 'private_not_confirmed',
               'private_keep', 'technical_review',
               'protected_values', 'private_full', 'private_partial', 'private_untouched',
               'public_control_losses', 'public_reference_replacements']
    rows, documents = [], []
    for row in report['rows']:
        measured, app = row['measurement'], row['application']
        q = measured['quality'] if measured else {}
        detail = row.get('details') or {}
        rows.append(dict(zip(columns, [row['model'], row['group'], row['method'],
            detail.get('documents_processed'), q.get('proposals'), q.get('proposal_matches'),
            detail.get('new_private_over_source_pool'), detail.get('private_not_proposed'),
            q.get('confirmed_matches'), detail.get('private_not_confirmed'),
            q.get('correct_values_filtered'), measured['technical_review_values'] if measured else None,
            measured['protected_values'] if measured else None,
            *[app.get(key) if app else None for key in ('private_spans_fully_covered',
                'private_spans_partly_covered', 'private_spans_untouched',
                'public_occurrences_lost', 'public_reference_replacements')]])))
        applied_docs = {doc['file']: doc for doc in app['documents']} if app else {}
        for doc in detail.get('documents', []):
            quality = doc['quality'];application = applied_docs.get(doc['file'], {})
            documents.append({'model': row['model'], 'method': row['method'], 'file': doc['file'],
                'private_expected': quality['expected'], 'candidates': quality['proposals'],
                'private_proposed': quality['proposal_matches'], 'private_mask': quality['confirmed_matches'],
                'private_not_proposed': doc['private_not_proposed'],
                'private_not_confirmed': doc['private_not_confirmed'],
                **{key: application.get(key) for key in ('private_spans_fully_covered',
                    'private_spans_partly_covered', 'private_spans_untouched', 'public_occurrences_lost')}})
    _csv(output / 'comparison.csv', rows, columns)
    document_columns = ['model', 'method', 'file', 'private_expected', 'candidates', 'private_proposed',
        'private_mask', 'private_not_proposed', 'private_not_confirmed', 'private_spans_fully_covered',
        'private_spans_partly_covered', 'private_spans_untouched', 'public_occurrences_lost']
    _csv(output / 'documents.csv', documents, document_columns)


if __name__ == '__main__':
    main()
