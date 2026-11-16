#!/usr/bin/env python3
"""Report explicit phase2 result snapshots without running or changing experiments.

The sibling plan establishes model/protocol provenance. Historical references,
preflight records, unknown test models and pending requests never become answers.
Quality uses unweighted Pareto comparisons; performance is reference data only.
"""
import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ('A', 'B', 'C', 'D')
REQUEST_FIELDS = ('variant', 'batch_index', 'case_ids', 'request', 'request_sha256',
                  'prompt_sha256', 'input_sha256', 'schema_sha256')
QUALITY_AXES = ('private_keep', 'public_mask', 'explicit_review', 'technical_review')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('invalid_json_constant')
    def decimal(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('invalid_json_number')
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=decimal)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def catalog_models():
    old = strict_json((ROOT / 'benchmark/data/models.json').read_text())['models']
    planned = strict_json((ROOT / 'benchmark/data/planned-models.json').read_text())['models']
    # Admit the separately registered Gemma variants without rewriting the
    # original thirteen-model input or changing any frozen producer.
    from privacy_expansion_profiles import GEMMA_IT_CATALOG, pinned_models
    extra = pinned_models(GEMMA_IT_CATALOG)[0]['models'] if GEMMA_IT_CATALOG.is_file() else []
    return {row['id']: row for row in old + planned + extra}


def _validate_source(state, plan, catalog, control_validator=None, variants=VARIANTS):
    require(isinstance(state, dict) and state.get('version') == 1
            and isinstance(state.get('stages'), dict)
            and isinstance(state.get('responses'), dict)
            and isinstance(state.get('calls'), list), 'not_phase2_result')
    require(bool(variants) and len(set(variants)) == len(variants) and set(variants) <= set(VARIANTS),
            'invalid_expected_variants')
    require(isinstance(plan, dict) and plan.get('version') == 1 and plan.get('variant_order') == list(variants),
            'not_phase2_plan')
    require(not any(source.get(key) for source in (state, plan)
                    for key in ('mock', 'test_only', 'mocked', 'fixture')),
            'mock_result_not_measurement')
    signature = plan['signature']
    model = signature['settings']['model']
    require(model in catalog, 'unknown_or_test_model')
    require(state.get('shadow') is True and state.get('rules_changed') == 0
            and plan.get('protected_bypass') is False, 'wrong_measurement_contract')
    require(plan.get('model_sha256_verified') is True
            and plan.get('token_budget_validation') == 'verified_runtime', 'preflight_not_verified')
    canonical = [{key: row[key] for key in REQUEST_FIELDS} for row in plan['requests']]
    actual_plan_hash = digest({'signature': signature, 'batches': plan['batches'], 'requests': canonical})
    require(actual_plan_hash == plan.get('plan_sha256')
            == state['signature'].get('plan_sha256'), 'plan_hash_mismatch')
    artifact = signature['model_artifact']
    require(isinstance(artifact, list) and len(artifact) >= 3
            and state['signature'].get('model_sha256') == artifact[2], 'model_hash_mismatch')
    expected_artifact = catalog[model].get('artifact', {})
    expected_hash = expected_artifact.get('sha256') or expected_artifact.get('hf_reported_sha256')
    require(expected_hash is None or artifact[2] == expected_hash, 'catalog_model_hash_mismatch')
    if 'bytes' in expected_artifact:
        require(artifact[1] == expected_artifact['bytes'], 'catalog_model_size_mismatch')
    require(state['signature'].get('runtime_sha256') == plan.get('runtime_sha256')
            == signature['settings'].get('expected_runtime_sha256'), 'runtime_hash_mismatch')
    libraries = plan.get('adjacent_runtime_libraries_sha256')
    require(isinstance(libraries, dict)
            and state['signature'].get('adjacent_runtime_libraries_sha256') == libraries,
            'runtime_library_hash_mismatch')
    require(state['signature'].get('hardware_sha256') == digest(plan['hardware']),
            'hardware_hash_mismatch')
    require(state['signature'].get('template_probe_sha256') == digest(plan['native_template_probe']),
            'template_hash_mismatch')
    rendered = [[row['variant'], row['batch_index'], row['rendered_prompt_sha256'], row['input_tokens']]
                for row in plan['requests']]
    require(state['signature'].get('rendered_requests_sha256') == digest(rendered),
            'rendered_request_hash_mismatch')
    cases = plan['control_cases']
    require(signature['case_data_sha256'] == digest(cases), 'case_hash_mismatch')
    ids = [row['id'] for row in cases['candidates']]
    require(len(ids) == len(set(ids)) == 32, 'not_32_unique_controls')
    if control_validator is None:
        require(cases.get('reference_private') == 16 and cases.get('reference_public') == 16,
                'not_frozen_binary_32_controls')
    else:
        require(control_validator(cases), 'not_expected_frozen_controls')
    indexed = {}
    for row in plan['requests']:
        variant, index = row['variant'], row['batch_index']
        require(variant in variants and type(index) is int and 0 <= index < len(plan['batches']),
                'invalid_request_batch')
        require(row['case_ids'] == plan['batches'][index], 'unmatched_batch_plan')
        require(row['request'].get('model') == model
                and row['request_sha256'] == digest(row['request']), 'request_hash_or_model_mismatch')
        key = (variant, index)
        require(key not in indexed, 'duplicate_planned_batch')
        indexed[key] = row
    for variant in variants:
        flattened = [ident for row in plan['requests'] if row['variant'] == variant for ident in row['case_ids']]
        require(len(flattened) == 32 and set(flattened) == set(ids), 'incomplete_variant_plan')
    return model, ids, indexed


def _attempts(state, plan, indexed):
    require(len(state['calls']) == len(set(state['calls']))
            and set(state['calls']) == set(state['responses']), 'request_slot_mismatch')
    result = defaultdict(list)
    for key in state['calls']:
        entry = state['responses'][key]
        require(entry.get('status') in ('pending', 'received', 'failed', 'interrupted'),
                'unknown_request_status')
        planned = indexed.get((entry.get('variant'), entry.get('batch_index')))
        require(planned is not None and entry.get('case_ids') == planned['case_ids'],
                'unplanned_request')
        request = entry['request']
        require(entry.get('request_sha256') == digest(request)
                and key == digest([entry['variant'], entry['batch_index'], request]), 'response_request_hash_mismatch')
        expected = {**planned['request'], 'max_tokens': request.get('max_tokens')}
        settings = plan['signature']['settings']
        maximum = min(settings['retry_output_tokens'], settings['context_tokens']
                      - settings['reserve_tokens'] - planned['input_tokens'])
        require(request == expected and type(request.get('max_tokens')) is int
                and planned['request']['max_tokens'] <= request['max_tokens'] <= maximum,
                'unplanned_request_parameters')
        if 'raw_transport' in entry:
            require(entry['status'] == 'received', 'transport_status_mismatch')
            try:
                base64.b64decode(entry['raw_transport']['body_base64'], validate=True)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError('invalid_transport_evidence') from exc
        require(entry['status'] != 'received' or 'raw_transport' in entry,
                'missing_transport_evidence')
        result[entry['variant']].append(entry)
    return result


def _labels(state, ids):
    source = state.get('case_transitions')
    if source is None:
        require(not any(stage.get('decisions') for stage in state['stages'].values()),
                'missing_evaluator_case_labels')
        return {}
    require(isinstance(source, list) and len(source) == 32, 'invalid_evaluator_case_labels')
    labels = {}
    for row in source:
        require(row.get('id') in ids and row['id'] not in labels
                and type(row.get('reference_private')) is bool, 'invalid_evaluator_case_labels')
        labels[row['id']] = row['reference_private']
    require(sum(labels.values()) == 16, 'invalid_binary_label_balance')
    return labels


def _confirmed_verdicts(entries):
    """Check persisted decisions against replies; never create decisions from replies."""
    confirmed = set()
    for entry in entries:
        if entry.get('status') != 'received' or entry.get('error'):
            continue
        try:
            transport = entry['raw_transport']
            if transport.get('status') != 200:
                continue
            reply = strict_json(base64.b64decode(transport['body_base64'], validate=True).decode('utf-8'))
            choice = reply['choices'][0]
            if choice.get('finish_reason') != 'stop':
                continue
            final = strict_json(choice['message']['content'])
            if not isinstance(final, dict) or set(final) != {'decisions'}:
                continue
            decisions = final['decisions']
            if not isinstance(decisions, list) or any(not isinstance(row, dict) for row in decisions):
                continue
            counts = Counter(row.get('id') for row in decisions if isinstance(row.get('id'), str))
            if len(decisions) != len(entry['case_ids']) or counts != Counter(entry['case_ids']):
                continue
            confirmed.update((row['id'], row['verdict']) for row in decisions
                if isinstance(row.get('id'), str) and counts[row['id']] == 1
                and row.get('verdict') in ('mask', 'keep', 'review'))
        except (ValueError, UnicodeError, KeyError, TypeError, IndexError):
            continue
    return confirmed


def _delivered_generation(entry):
    """Distinguish an observed model outcome from a missing transport response.

    A delivered malformed/truncated final is an observed protocol failure. HTTP
    errors, missing transport bytes and invalid native envelopes do not establish
    what the model generated. This never parses or rehabilitates a final answer.
    """
    if entry.get('status') != 'received':
        return False
    try:
        transport = entry['raw_transport']
        if transport.get('status') != 200:
            return False
        reply = strict_json(base64.b64decode(transport['body_base64'], validate=True).decode('utf-8'))
        choice = reply['choices'][0]
        return (choice.get('finish_reason') in ('stop', 'length')
                and isinstance(choice['message'].get('content'), str))
    except (ValueError, UnicodeError, KeyError, TypeError, IndexError, AttributeError):
        return False


def _quality_row(variant, ids, labels, decisions, entries):
    require(isinstance(decisions, dict) and not (set(decisions) - set(ids)), 'unknown_decision_ids')
    attempted = {ident for entry in entries for ident in entry['case_ids']}
    require(set(decisions) <= attempted, 'decision_without_registered_attempt')
    answered = {ident for entry in entries if entry['status'] == 'received' for ident in entry['case_ids']}
    confirmed = _confirmed_verdicts(entries)
    delivered = {ident for entry in entries if _delivered_generation(entry) for ident in entry['case_ids']}
    completed_attempt = {ident for entry in entries if entry['status'] != 'pending' for ident in entry['case_ids']}
    quality = {'cases': 32, 'private_cases': 16, 'public_cases': 16,
        'attempted_cases': len(decisions), 'unattempted_cases': 32 - len(decisions),
        'private_attempted_cases': 0, 'public_attempted_cases': 0,
        'applicable_decisions': 0, 'automated_decisions': 0, 'correct_actions': 0,
        'private_keep': 0, 'private_mask': 0, 'public_mask': 0, 'public_keep': 0,
        'explicit_review': 0, 'technical_review': 0,
        'private_explicit_review': 0, 'public_explicit_review': 0,
        'private_technical_review': 0, 'public_technical_review': 0}
    for ident, decision in decisions.items():
        require(ident in labels and isinstance(decision, dict) and decision.get('id') == ident
                and type(decision.get('valid')) is bool, 'invalid_scored_decision')
        label = 'private' if labels[ident] else 'public'
        quality[label + '_attempted_cases'] += 1
        if not decision['valid']:
            require(ident in completed_attempt, 'pending_request_has_scored_answer')
            quality['technical_review'] += 1
            quality[label + '_technical_review'] += 1
            continue
        require(ident in answered, 'applicable_decision_without_response')
        verdict = decision.get('verdict')
        require(verdict in ('mask', 'keep', 'review'), 'invalid_scored_verdict')
        require((ident, verdict) in confirmed, 'applicable_decision_not_supported_by_response')
        quality['applicable_decisions'] += 1
        if verdict == 'review':
            quality['explicit_review'] += 1
            quality[label + '_explicit_review'] += 1
        else:
            quality['automated_decisions'] += 1
            quality[label + '_' + verdict] += 1
            quality['correct_actions'] += verdict == ('mask' if labels[ident] else 'keep')
    pending = sum(entry['status'] == 'pending' for entry in entries)
    complete = len(decisions) == 32 and pending == 0
    status = ('complete_with_technical_review' if quality['technical_review'] else 'complete') if complete else (
        'partial' if decisions or entries else 'not_started')
    return {'variant': variant, 'status': status, 'protocol_complete': complete, 'quality': quality,
        'outcome_evidence': {
            'delivered_scored_cases': len(set(decisions) & delivered),
            'delivered_inapplicable_cases': sum(not row['valid'] and ident in delivered
                                               for ident, row in decisions.items()),
            'unobserved_scored_cases': len(set(decisions) - delivered),
            'unobserved_case_ids': sorted(set(decisions) - delivered),
            'all_generation_outcomes_observed': complete and set(decisions) <= delivered},
        'registered_request_slots': len(entries), 'received_responses': sum('raw_transport' in entry for entry in entries),
        'pending_request_slots': pending, 'interrupted_request_slots': sum(entry['status'] == 'interrupted' for entry in entries),
        'registered_case_slots': len(attempted), 'received_case_slots': len(answered),
        'all_review': complete and quality['automated_decisions'] == 0}


def _performance(entries, state, plan, variant):
    records = [entry.get('record', {}) for entry in entries]
    elapsed = [row['http_wall_seconds'] for row in records if _number(row.get('http_wall_seconds'))]
    model = [row.get('sampled_resources', {}).get('roles', {}).get('model', {}) for row in records]
    peaks = [row['observed_peak_rss_bytes'] for row in model if _number(row.get('observed_peak_rss_bytes'))]
    cpu = [row['cpu_seconds'] for row in model if _number(row.get('cpu_seconds'))]
    sessions = state.get('execution_sessions', [])
    starts = [event['seconds'] for session in sessions for event in session.get('runtime_starts', [])
              if event.get('process_started') and _number(event.get('seconds'))]
    walls = [session['execution_wall_seconds'] for session in sessions if _number(session.get('execution_wall_seconds'))]
    return {'reference_only': True, 'affects_quality_frontier': False,
        'known_http_wall_seconds_including_retries': sum(elapsed),
        'http_wall_seconds_including_retries': sum(elapsed) if len(elapsed) == len(entries) else None,
        'timed_request_slots': len(elapsed), 'untimed_request_slots': len(entries) - len(elapsed),
        'model_observed_peak_rss_bytes': max(peaks) if peaks else None,
        'known_model_cpu_seconds': sum(cpu) if cpu else None, 'request_slots_with_cpu': len(cpu),
        'shared_runtime_readiness_seconds': sum(starts) if starts else None,
        'shared_recorded_session_wall_seconds': sum(walls) if walls else None,
        'model_disk_bytes': plan['signature']['model_artifact'][1],
        'hardware': plan['hardware'], 'settings': plan['signature']['settings'],
        'stored_variant_summary': state.get('performance', {}).get(variant)}


def load_run(result_path, catalog=None):
    path = Path(result_path).resolve()
    if path.is_dir():
        path = path / 'result.json'
    require(path.name == 'result.json', 'explicit_phase2_result_required')
    raw = path.read_bytes()
    state = strict_json(raw.decode('utf-8'))
    plan_path = path.with_name('plan.json')
    plan_raw = plan_path.read_bytes()
    plan = strict_json(plan_raw.decode('utf-8'))
    model, ids, indexed = _validate_source(state, plan, catalog_models() if catalog is None else catalog)
    entries = _attempts(state, plan, indexed)
    labels = _labels(state, ids)
    run_id = digest(str(path))[:12]
    rows = []
    for variant in VARIANTS:
        decisions = state['stages'].get(variant, {}).get('decisions', {})
        row = _quality_row(variant, ids, labels, decisions, entries[variant])
        stored = state.get('metrics', {}).get(variant, {})
        for key, value in row['quality'].items():
            require(key not in stored or stored[key] == value, 'stored_quality_metrics_mismatch')
        row.update(row_id=run_id + ':' + variant, run_id=run_id, model=model,
                   run_finalized=state.get('status') == 'completed',
                   typing=stored.get('typing'), performance=_performance(entries[variant], state, plan, variant))
        rows.append(row)
    cases = []
    for ident in ids:
        values = {}
        for variant in VARIANTS:
            decision = state['stages'].get(variant, {}).get('decisions', {}).get(ident)
            if decision is not None:
                values[variant] = {key: decision.get(key) for key in ('verdict', 'valid', 'review_origin', 'error')}
        cases.append({'id': ident, 'reference_private': labels.get(ident), 'variants': values})
    cohort = {key: plan['signature'][key] for key in
              ('case_data_sha256', 'source_sha256', 'annotations_sha256', 'policy_sha256')}
    return {'run_id': run_id, 'model': model, 'run_status': state.get('status'),
        'result_path': str(path), 'result_sha256': hashlib.sha256(raw).hexdigest(),
        'plan_path': str(plan_path), 'plan_file_sha256': hashlib.sha256(plan_raw).hexdigest(),
        'plan_sha256': plan['plan_sha256'], 'cohort_sha256': digest(cohort), 'cohort': cohort,
        'historical_reference_excluded': True, 'rows': rows, 'case_transitions': cases}


def compare_quality(rows, axes=QUALITY_AXES):
    complete = [row for row in rows if row['protocol_complete']]
    frontier, dominated = [], {}
    def dominates(left, right):
        a, b = left['quality'], right['quality']
        return all(a[key] <= b[key] for key in axes) and any(a[key] < b[key] for key in axes)
    for row in complete:
        by = [other['row_id'] for other in complete if dominates(other, row)]
        if by:
            dominated[row['row_id']] = by
        else:
            frontier.append(row['row_id'])
    all_complete = (bool(rows) and len(complete) == len(rows)
                    and all(row.get('run_finalized', True) for row in rows))
    # Rejected type/shape/truncated outputs are measured applicability outcomes.
    # Missing transport data may hide either good or bad decisions and cannot
    # establish a winner. Legacy in-memory callers without evidence stay strict.
    all_observed = all(row.get('outcome_evidence', {}).get(
        'all_generation_outcomes_observed', row['quality']['technical_review'] == 0) for row in complete)
    candidates = [row for row in complete if row['row_id'] in frontier]
    winner, reason = None, 'no_completed_variants'
    if not all_complete:
        reason = 'comparison_incomplete'
    elif not all_observed:
        reason = 'technical_incompleteness'
    elif candidates and all(row['all_review'] for row in candidates):
        reason = 'all_review_is_not_automation'
    elif len(candidates) == 1 and not candidates[0]['all_review']:
        winner, reason = candidates[0]['row_id'], 'unique_observed_quality_frontier_member'
    elif candidates:
        vectors = {tuple(row['quality'][key] for key in axes) for row in candidates}
        reason = 'equivalent_quality' if len(vectors) == 1 else 'unresolved_quality_tradeoffs'
    return {'quality_axes_minimized': list(axes), 'numeric_weights': None,
        'performance_used': False, 'comparison_complete': all_complete,
        'all_generation_outcomes_observed': all_complete and all_observed,
        'frontier': frontier, 'dominated_by': dominated, 'winner': winner, 'reason': reason,
        'production_approval': False,
        'priority_note': 'private disclosure errors are the primary concern; unresolved quality tradeoffs are not decided automatically',
        'scope': 'supplied snapshots of the same 32 known controls; fresh-document validation remains required'}


def build_report(result_paths):
    runs, rejected, seen_paths, seen_plans = [], [], set(), set()
    for supplied in result_paths:
        path = Path(supplied).resolve()
        if path.is_dir():
            path = path / 'result.json'
        if path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            run = load_run(path)
            require(run['plan_sha256'] not in seen_plans, 'duplicate_plan_snapshot')
            seen_plans.add(run['plan_sha256'])
            runs.append(run)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            rejected.append({'path': str(path), 'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    cohorts = defaultdict(list)
    for run in runs:
        cohorts[run['cohort_sha256']].extend(run['rows'])
    return {'report_version': 2, 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'new_inference_calls': 0, 'runs': runs, 'rejected_inputs': rejected,
        'provenance_scope': 'consistency of trusted local experiment records; hashes alone do not independently attest model execution',
        'within_run_comparisons': {run['run_id']: compare_quality(run['rows']) for run in runs},
        'cohort_comparisons': {key: compare_quality(rows) for key, rows in cohorts.items()},
        'unprovided_models_included': False}


def _cell(value):
    return '—' if value is None else str(value).replace('|', '\\|').replace('\n', ' ')


def _decision(value):
    if value is None:
        return '—'
    return value['verdict'] if value.get('valid') else 'technical review'


def render_markdown(report):
    lines = ['# Сравнение вариантов проверки приватности', '',
        'Отчёт читает только явно переданные снимки новых прогонов. Новые LLM-запросы не выполнялись; '
        'архивная опора A и предварительные проверки не включаются в измерения.', '',
        'Главный критерий — качество приватности, прежде всего ошибочное раскрытие приватного. Время и ресурсы приведены отдельно для справки '
        'и не влияют на границу Парето. Численные веса не назначены. Все 32 контроля уже известны; '
        'вывод не является допуском к автоматизации.', '',
        '## Качество', '',
        '| Модель / запуск | Вариант | Статус | Разобрано / 32 | Private разобрано / 16 | Public разобрано / 16 | Private keep / 16 | Public mask / 16 | Явный review | Технический review | Применимо / 32 |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for run in report['runs']:
        for row in run['rows']:
            q = row['quality']
            values = [run['model'] + ' / ' + run['run_id'], row['variant'], row['status'], q['attempted_cases'],
                      q['private_attempted_cases'], q['public_attempted_cases'],
                      q['private_keep'], q['public_mask'], q['explicit_review'], q['technical_review'], q['applicable_decisions']]
            lines.append('| ' + ' | '.join(map(_cell, values)) + ' |')
    lines.extend(['', 'Числа частичного варианта относятся только к сохранённым разобранным случаям. '
        'Остальные случаи остаются незапрошенными/неразобранными; нулевой счётчик ошибок частичного варианта не доказывает качество.', ''])
    lines.extend(['Технический review не превращается в правильное решение. Отдельно показаны '
        'полученные, но неприменимые ответы и случаи без наблюдаемого ответа модели. '
        'Ошибочный тип, форма или исчерпание бюджета — измеренный исход контракта; '
        'тайм-аут и ошибка транспорта оставляют содержание ответа неизвестным.', '',
        '| Модель / запуск | Вариант | Получено и оценено | Получено, неприменимо | Оценено без наблюдаемой генерации |',
        '|---|---|---:|---:|---:|'])
    for run in report['runs']:
        for row in run['rows']:
            evidence = row['outcome_evidence']
            values = [run['model'] + ' / ' + run['run_id'], row['variant'],
                      evidence['delivered_scored_cases'], evidence['delivered_inapplicable_cases'],
                      evidence['unobserved_scored_cases']]
            lines.append('| ' + ' | '.join(map(_cell, values)) + ' |')
    lines.append('')
    lines.extend(['## Сопоставимые группы', '',
        'Сравниваются только одинаковые случаи, исходные документы, разметка и версия политики. '
        'Происхождение проверяется как связность доверенных локальных записей; хеши сами по себе не доказывают выполнение модели.', ''])
    for cohort, comparison in report['cohort_comparisons'].items():
        lines.extend(['- Группа `' + cohort + '`; фронт: '
                      + (', '.join(comparison['frontier']) or 'нет') + '; кандидат: '
                      + (comparison['winner'] or 'нет') + '; основание: `' + comparison['reason'] + '`.'])
    lines.append('')
    for run in report['runs']:
        comparison = report['within_run_comparisons'][run['run_id']]
        lines.extend(['### ' + run['model'] + ' / ' + run['run_id'], '',
            '- Статус всего прогона: `' + str(run['run_status']) + '`.',
            '- Фронт завершённых вариантов: ' + (', '.join(comparison['frontier']) or 'нет') + '.',
            '- Автоматически выделенный кандидат: ' + (comparison['winner'] or 'нет') + '.',
            '- Основание: `' + comparison['reason'] + '`.',
            '- Исходный снимок: [' + run['run_id'] + '](' + run['result_path'] + ').', '',
            '| ID случая | Приватный эталон | A | B | C | D |', '|---|---|---|---|---|---|'])
        for case in run['case_transitions']:
            values = [case['id'], case['reference_private']] + [_decision(case['variants'].get(v)) for v in VARIANTS]
            lines.append('| ' + ' | '.join(map(_cell, values)) + ' |')
        lines.append('')
    lines.extend(['## Время и ресурсы: справочно', '',
        'Запуск runtime и время сессий являются общей стоимостью прогона, а не отдельной стоимостью каждого варианта. '
        'RSS — наблюдавшийся пик с периодическими замерами; он не определяет универсальную минимальную RAM.', '',
        '| Модель / запуск | Вариант | HTTP с повторами, с | Замерено / попыток | Пик RSS модели, байт | CPU модели: известные секунды | Общая готовность runtime, с | Контекст | Потоки |',
        '|---|---|---:|---|---:|---:|---:|---:|---:|'])
    for run in report['runs']:
        for row in run['rows']:
            p = row['performance']
            values = [run['model'] + ' / ' + run['run_id'], row['variant'], p['http_wall_seconds_including_retries'],
                      str(p['timed_request_slots']) + '/' + str(row['registered_request_slots']),
                      p['model_observed_peak_rss_bytes'], p['known_model_cpu_seconds'], p['shared_runtime_readiness_seconds'],
                      p['settings'].get('context_tokens'), p['settings'].get('threads')]
            lines.append('| ' + ' | '.join(map(_cell, values)) + ' |')
    if report['rejected_inputs']:
        lines.extend(['', '## Отклонённые входы', ''])
        for error in report['rejected_inputs']:
            lines.append('- `' + _cell(error['path']) + '`: `' + _cell(error['error']) + '`.')
    return '\n'.join(lines) + '\n'


def _write(path, text):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, nargs='+', required=True, help='Explicit result.json paths or run directories')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    sources = {(path if path.is_dir() else path.parent).resolve() for path in args.results}
    if output in sources or (output / 'result.json').exists() or (output / 'plan.json').exists():
        parser.error('output directory must be separate from experiment inputs')
    report = build_report(args.results)
    output.mkdir(parents=True, exist_ok=True)
    _write(output / 'report.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    _write(output / 'report.md', render_markdown(report))
    print(json.dumps({'accepted_runs': len(report['runs']), 'rejected_inputs': len(report['rejected_inputs']),
                      'new_inference_calls': 0, 'report': str(output / 'report.md')}))
    if not report['runs']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
