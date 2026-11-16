"""Read the registered paired policies on frozen fresh controls, with no inference."""
from pathlib import Path

from privacy_expansion_profiles import NEW_MODELS
from privacy_policy_clarification import load_clarification_run, verify_pair_plans
from report_complete_research import privacy_resources, table, _csv
from run_privacy_policy_suite import MODEL_ORDER, atomic_json, file_sha, utc
from summarize_privacy_holdout import AXES, load_fresh_run
from summarize_privacy_policy import compare_quality, require, strict_json


def read_pair(reference_path, treatment_path):
    reference_path, treatment_path = Path(reference_path), Path(treatment_path)
    baseline = load_fresh_run(reference_path)
    treatment = load_clarification_run(treatment_path)
    require(baseline['model'] == treatment['model'], 'policy_pair_model_mismatch')
    a = strict_json(reference_path.with_name('plan.json').read_text())
    b = strict_json(treatment_path.with_name('plan.json').read_text())
    verified = verify_pair_plans(a, b)
    binding = strict_json(treatment_path.with_name('policy-pair.json').read_text())
    require(binding['reference_result_sha256'] == baseline['result_sha256']
            and binding['reference_plan_sha256'] == baseline['plan_sha256'],
            'policy_pair_reference_binding_mismatch')
    if treatment['run_status'] == 'completed':
        require(binding.get('treatment_result_sha256') == treatment['result_sha256']
                and binding.get('postcondition') == verified and verified['native_pair_verified'],
                'policy_pair_completion_binding_not_durable')
    old = next(row for row in baseline['rows'] if row['variant'] == 'D')
    new = treatment['rows'][0]
    require(baseline['run_status'] == 'completed' and old['protocol_complete'],
            'policy_pair_reference_not_complete')
    old['policy'], new['policy'] = 'v1', 'v2'
    before = strict_json(reference_path.read_text())['stages'].get('D', {}).get('decisions', {})
    after = strict_json(treatment_path.read_text())['stages'].get('D', {}).get('decisions', {})
    transitions = []
    for ident in verified['case_ids']:
        a, b = before.get(ident, {}), after.get(ident, {})
        fields = ('verdict', 'valid', 'review_origin', 'error')
        a, b = ({key: a.get(key) for key in fields}, {key: b.get(key) for key in fields})
        transitions.append({'id': ident, 'v1': a, 'v2': b, 'changed': a != b})
    return {'model': baseline['model'], 'baseline': baseline, 'treatment': treatment,
        'rows': [old, new], 'verification': verified, 'transitions': transitions,
        'comparison': compare_quality([old, new], AXES), 'new_inference_calls': 0}


def write_reports(config, state, jobs):
    destination = Path(config['output']) / 'reports'
    destination.mkdir(parents=True, exist_ok=True)
    pairs, pending, rejected, resource_rows = [], [], [], []
    for job in jobs:
        target = Path(job['output']) / 'result.json'
        if not target.is_file():
            pending.append(job['model'])
            continue
        try:
            pair = read_pair(job['reference_result'], target)
            pairs.append(pair)
            for run, rows, series in [(pair['baseline'], [pair['rows'][0]], 'fresh-native-D-policy-v1'),
                                      (pair['treatment'], [pair['rows'][1]], 'fresh-native-D-policy-v2')]:
                resource_rows.extend(privacy_resources(run, rows, series))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            rejected.append({'path': str(target), 'error': str(exc)})
    all_rows = [row for pair in pairs for row in pair['rows']]
    comparison = compare_quality(all_rows, AXES)
    expected = list(config.get('expected_models', (*MODEL_ORDER, *NEW_MODELS)))
    if pending or rejected or len(pairs) != len(expected):
        comparison.update(winner=None, reason='policy_pairs_incomplete', comparison_complete=False)
    report = {'version': 1, 'generated_at_utc': utc(), 'status': state['status'], 'pairs': pairs,
        'expected_models': expected, 'pending_models': pending, 'rejected_inputs': rejected,
        'comparison': comparison, 'new_inference_calls': 0, 'production_approval': False,
        'time_or_memory_used_for_quality': False}
    atomic_json(destination / 'report.json', report)
    rows = []
    for pair in pairs:
        for row in pair['rows']:
            q = row['quality']
            rows.append([pair['model'], row['policy'], q['attempted_cases'], q['private_mask'],
                q['private_keep'], q['public_mask'], q['unnecessary_explicit_review'],
                q['reference_review_correct'], q['overconfident_on_reference_review'],
                q['technical_review'], row['outcome_evidence']['unobserved_scored_cases']])
    lines = ['# Парная проверка политики v1/v2', '',
        'Свежие фиксированные RU/EN-контроли: 14 mask / 14 keep / 4 review. Только D, нативные профили. '
        'В каждой паре проверяется, что тело запроса различается лишь текстом доверенной политики. '
        'Ресурсы справочные; нулевая утечка при ручном разборе не является автоматизацией.', '']
    lines += table(['Модель', 'Политика', 'Случаи / 32', 'Private mask / 14', 'Private keep / 14',
        'Public mask / 14', 'Лишний review / 28', 'Правильный review / 4',
        'Самоуверенные / 4', 'Тех. review', 'Без ответа'], rows)
    lines += ['', 'Ожидают: ' + (', '.join(pending) or 'нет') + '.',
        'Отклонено входов: ' + str(len(rejected)) + '.',
        'Основание сравнения: `' + comparison['reason'] + '`.',
        'Кандидат: ' + str(comparison['winner']) + '.', '',
        'Переходы ID, хеши и параметры — в `report.json`; время/CPU/RSS — в `resources.csv`.', '']
    (destination / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    _csv(destination / 'resources.csv', resource_rows, ['model', 'series', 'variant', 'http_seconds',
        'model_peak_rss_bytes', 'model_cpu_seconds', 'weight_bytes', 'context_tokens', 'reference_only'])
    return {'output': str(destination), 'pairs': len(pairs), 'pending': pending, 'rejected_inputs': rejected,
            'report_sha256': file_sha(destination / 'report.json')}
