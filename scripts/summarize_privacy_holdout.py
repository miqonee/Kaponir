#!/usr/bin/env python3
"""Verify and report measured fresh three-way controls for thirteen pinned models."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from privacy_expansion_profiles import pinned_models
from privacy_policy_holdout import load_holdout, score_holdout
from summarize_privacy_policy import (VARIANTS, _attempts, _confirmed_verdicts,
    _delivered_generation, _performance, _validate_source, _write, catalog_models,
    compare_quality, digest, require, strict_json)


AXES = ('private_keep', 'public_mask', 'unnecessary_explicit_review',
        'overconfident_on_reference_review', 'technical_review')


def load_fresh_run(path):
    path = Path(path).resolve()
    if path.is_dir():
        path = path / 'result.json'
    require(path.name == 'result.json', 'explicit_phase2_result_required')
    raw = path.read_bytes()
    state = strict_json(raw.decode('utf-8'))
    plan_raw = path.with_name('plan.json').read_bytes()
    plan = strict_json(plan_raw.decode('utf-8'))
    holdout = load_holdout()
    require(plan['signature']['quality_contract'] == holdout.quality_contract
            and plan['signature']['annotations_sha256'] == holdout.quality_contract['annotations_sha256'],
            'not_frozen_three_way_holdout')
    catalog = catalog_models()
    pinned, _ = pinned_models()
    for model in pinned['models']:
        catalog[model['id']] = model
    model, ids, indexed = _validate_source(state, plan, catalog,
        control_validator=lambda cases: cases == holdout.case_data)
    entries = _attempts(state, plan, indexed)
    run_id = digest(str(path))[:12]
    rows = []
    for variant in VARIANTS:
        decisions = state['stages'].get(variant, {}).get('decisions', {})
        quality = score_holdout(holdout.cases, decisions, holdout.expected_by_id)
        attempted = {ident for entry in entries[variant] for ident in entry['case_ids']}
        completed = {ident for entry in entries[variant] if entry['status'] != 'pending' for ident in entry['case_ids']}
        delivered = {ident for entry in entries[variant] if _delivered_generation(entry) for ident in entry['case_ids']}
        confirmed = _confirmed_verdicts(entries[variant])
        require(set(decisions) <= attempted, 'decision_without_registered_attempt')
        for ident, decision in decisions.items():
            require(isinstance(decision, dict) and type(decision.get('valid')) is bool
                    and decision.get('id') == ident, 'invalid_scored_decision')
            if decision['valid']:
                require((ident, decision.get('verdict')) in confirmed, 'decision_not_supported_by_response')
            else:
                require(ident in completed, 'pending_request_has_scored_answer')
        stored = state.get('metrics', {}).get(variant)
        require(stored is None or stored == quality, 'stored_holdout_metrics_mismatch')
        pending = sum(entry['status'] == 'pending' for entry in entries[variant])
        complete = len(decisions) == 32 and pending == 0
        rows.append({'row_id': run_id + ':' + variant, 'model': model, 'variant': variant,
            'registered_request_slots': len(entries[variant]),
            'protocol_complete': complete, 'run_finalized': state.get('status') == 'completed',
            'status': 'complete' if complete else ('partial' if decisions or entries[variant] else 'not_started'),
            'quality': quality, 'all_review': complete and quality['automated_decisions'] == 0,
            'outcome_evidence': {'all_generation_outcomes_observed': complete and set(decisions) <= delivered,
                'unobserved_scored_cases': len(set(decisions) - delivered),
                'delivered_inapplicable_cases': sum(not row['valid'] and ident in delivered
                                                   for ident, row in decisions.items())},
            'performance': _performance(entries[variant], state, plan, variant)})
    cohort = {key: plan['signature'][key] for key in ('case_data_sha256', 'source_sha256', 'annotations_sha256', 'policy_sha256')}
    return {'run_id': run_id, 'model': model, 'run_status': state['status'],
        'result_path': str(path), 'result_sha256': hashlib.sha256(raw).hexdigest(),
        'plan_sha256': plan['plan_sha256'], 'plan_file_sha256': hashlib.sha256(plan_raw).hexdigest(),
        'cohort_sha256': digest(cohort), 'profile': plan['signature']['settings'], 'rows': rows}


def build_fresh_report(paths, expected_models=None):
    runs, rejected, seen = [], [], set()
    for path in paths:
        try:
            run = load_fresh_run(path)
            require(run['plan_sha256'] not in seen, 'duplicate_plan_snapshot')
            seen.add(run['plan_sha256'])
            runs.append(run)
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
            rejected.append({'path': str(path), 'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    cohorts = defaultdict(list)
    for run in runs:
        cohorts[run['cohort_sha256']].extend(run['rows'])
    comparisons = {key: compare_quality(rows, AXES) for key, rows in cohorts.items()}
    within = {run['run_id']: compare_quality(run['rows'], AXES) for run in runs}
    for value in list(comparisons.values()) + list(within.values()):
        value['scope'] = '32 frozen fresh controls: 14 mask, 14 keep, 4 review; no calibration answers included'
    provided = sorted({run['model'] for run in runs})
    missing = sorted(set(expected_models or []) - set(provided))
    if missing:
        for value in comparisons.values():
            value.update(winner=None, reason='expected_models_missing', comparison_complete=False)
    return {'report_version': 1, 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'new_inference_calls': 0, 'runs': runs, 'rejected_inputs': rejected,
        'within_run_comparisons': within, 'cohort_comparisons': comparisons,
        'model_coverage': {'expected_models': expected_models, 'provided_models': provided, 'missing_models': missing},
        'performance_used_for_quality': False, 'production_approval': False,
        'provenance_scope': 'consistency of trusted local records; hashes do not independently attest execution'}


def render_fresh(report):
    coverage = report['model_coverage']
    lines = ['# Свежая проверка приватности', '',
        'Эталон: 14 mask, 14 keep, 4 review. Известные калибровочные ответы не включены. '
        'Правильный review неоднозначного случая не штрафуется как лишний ручной разбор. '
        'Качество сравнивается без численных весов; время и ресурсы справочные.', '',
        'Модели с переданными результатами: ' + ', '.join(coverage['provided_models']) + '. '
        'Ожидаемые модели без результатов: ' + (', '.join(coverage['missing_models']) or 'нет') + '.', '',
        '| Модель | Вариант | Случаи / 32 | Private mask / 14 | Private keep / 14 | Public mask / 14 | Лишний review / 28 | Самоуверенные ответы / 4 | Технический review | Правильные автоответы / 28 |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for run in report['runs']:
        for row in run['rows']:
            q = row['quality']
            values = [run['model'], row['variant'], q['attempted_cases'], q['private_mask'],
                      *(q[key] for key in AXES), q['correct_automatic_actions']]
            lines.append('| ' + ' | '.join(map(str, values)) + ' |')
    lines.extend(['', '## Наблюдаемая граница качества', ''])
    for key, value in report['cohort_comparisons'].items():
        lines.append('- `' + key + '`: кандидат ' + str(value['winner']) + '; основание `' + value['reason']
                     + '`; фронт: ' + ', '.join(value['frontier']) + '.')
    lines.extend(['', '## Время и ресурсы', '',
        'Профили нативные и могут различаться. Параметры полностью сохранены в JSON; '
        'объявленный контекст модели не считается измеренной минимальной RAM. '
        'Значения RSS получены периодическими замерами и могут пропускать короткие пики.', '',
        '| Модель | Вариант | HTTP, с | Пик RSS модели, байт | CPU модели, с | Контекст |',
        '|---|---|---:|---:|---:|---:|'])
    for run in report['runs']:
        for row in run['rows']:
            p = row['performance']
            values = [run['model'], row['variant'], p['http_wall_seconds_including_retries'],
                p['model_observed_peak_rss_bytes'], p['known_model_cpu_seconds'], run['profile']['context_tokens']]
            lines.append('| ' + ' | '.join('—' if value is None else str(value) for value in values) + ' |')
    if report['rejected_inputs']:
        lines.extend(['', '## Отклонённые входы', ''])
        lines.extend('- `' + row['path'] + '`: `' + row['error'] + '`.' for row in report['rejected_inputs'])
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, nargs='+', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    require(args.output_dir.resolve() not in {(path if path.is_dir() else path.parent).resolve() for path in args.results},
            'report_must_be_separate_from_sources')
    report = build_fresh_report(args.results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write(args.output_dir / 'report.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    _write(args.output_dir / 'report.md', render_fresh(report))
    print(json.dumps({'accepted_runs': len(report['runs']), 'rejected_inputs': report['rejected_inputs'], 'new_inference_calls': 0}))
    if not report['runs']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
