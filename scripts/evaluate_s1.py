#!/usr/bin/env python3
"""Офлайн-подбор калибровки на одном наборе и оценка на отдельном. Не меняет конфиг.
JSONL: id, group, label (true=private), probability, local_probability, category.
Только спорные встроенные кандидаты; жёсткие маски и правила сюда не включаются.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

THRESHOLDS = {'credential': (.5, .02), 'network': (.6, .2), 'infra': (.7, .3),
              'identity': (.7, .2), 'financial': (.7, .2), 'contact': (.7, .2), 'custom': (.5, .1)}


def logit(p):
    p = min(.9999, max(.0001, p))
    return math.log(p / (1 - p))


def sigmoid(x):
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


def load(path):
    rows = []
    ids = set()
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        if not isinstance(r, dict) or set(r) != {'id', 'group', 'label', 'probability', 'local_probability', 'category'}:
            raise ValueError(f'{path}:{number}: unexpected schema')
        if any(not isinstance(r[k], str) or not r[k].strip() for k in ('id', 'group')):
            raise ValueError(f'{path}:{number}: nonempty id/group required')
        if r['id'] in ids:
            raise ValueError(f'{path}:{number}: duplicate id')
        ids.add(r['id'])
        if type(r['label']) is not bool or r['category'] not in THRESHOLDS:
            raise ValueError(f'{path}:{number}: invalid label/category')
        for k in ('probability', 'local_probability'):
            p = r[k]
            if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError(f'{path}:{number}: invalid {k}')
        mask, keep = THRESHOLDS[r['category']]
        if not keep < r['local_probability'] < mask:
            raise ValueError(f'{path}:{number}: candidate is not locally uncertain')
        rows.append(r)
    if {r['label'] for r in rows} != {True, False}:
        raise ValueError(f'{path}: both classes required')
    return rows


def calibrated(row, a, b):
    return sigmoid(a * logit(row['probability']) + b)


def released(row, a, b, keep_below, weight=1.0):
    p = calibrated(row, a, b)
    final = sigmoid(logit(row['local_probability']) + weight * logit(p))
    _, keep = THRESHOLDS[row['category']]
    return final <= keep and p <= keep_below


def loss(rows, a, b):
    values = []
    for row in rows:
        p = min(1 - 1e-12, max(1e-12, calibrated(row, a, b)))
        values.append(-math.log(p if row['label'] else 1 - p))
    return sum(values) / len(values)


def fit(rows):
    # Ограниченный положительный наклон: воспроизводимый поиск без внешних библиотек.
    a, b = min(((a, b / 2) for a in (.25, .5, 1., 2., 4.) for b in range(-8, 9)),
               key=lambda ab: (loss(rows, *ab), abs(ab[0] - 1), abs(ab[1])))
    choices = []
    for threshold in (0., .01, .02, .05, .1):
        leaks = sum(r['label'] and released(r, a, b, threshold) for r in rows)
        utility = sum(not r['label'] and released(r, a, b, threshold) for r in rows)
        if leaks == 0:
            choices.append((utility, -threshold, threshold))
    return a, b, max(choices)[2]


def wilson_upper(errors, total):
    z = 1.959963984540054
    p = errors / total
    return (p + z*z/(2*total) + z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))) / (1+z*z/total)


def evaluate(rows, a, b, threshold):
    private = sum(r['label'] for r in rows)
    leaks = sum(r['label'] and released(r, a, b, threshold) for r in rows)
    public = len(rows) - private
    freed = sum(not r['label'] and released(r, a, b, threshold) for r in rows)
    return {'candidates': len(rows), 'groups': len({r['group'] for r in rows}),
            'private': private, 'public': public, 'private_released': leaks,
            'public_released': freed, 'public_still_masked': public-freed,
            'private_release_rate': leaks/private,
            'private_release_wilson95_upper_candidate_level': wilson_upper(leaks, private),
            'brier': sum((calibrated(r, a, b)-r['label'])**2 for r in rows)/len(rows),
            'log_loss': loss(rows, a, b)}


def run(calibration, evaluation):
    for key in ('id', 'group'):
        if {r[key] for r in calibration} & {r[key] for r in evaluation}:
            raise ValueError(f'calibration/evaluation overlap by {key}')
    a, b, threshold = fit(calibration)
    return {'calibration': {'a': a, 'b': b, 'keep_below': threshold, 'weight': 1.0},
            'calibration_set': evaluate(calibration, a, b, threshold),
            'evaluation_set': evaluate(evaluation, a, b, threshold),
            'baseline_evaluation': evaluate(evaluation, 1., 0., .1),
            'automatically_approved': False,
            'limits': ['Evaluates supplied candidates only, not detector recall.',
                       'Default category thresholds and uncertain=mask only.',
                       'Grouping and independent provenance require human verification.',
                       'Candidate-level confidence bound assumes independence; repeated groups violate it.',
                       'No automatic activation, configuration update, or external request.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calibration', required=True, type=Path)
    parser.add_argument('--evaluation', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = run(load(args.calibration), load(args.evaluation))
    result['input_sha256'] = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                             for name, path in [('calibration', args.calibration), ('evaluation', args.evaluation)]}
    # Существующее доказательство не перезаписываем.
    with args.output.open('x') as out:
        json.dump(result, out, indent=2, allow_nan=False)
        out.write('\n')
    print(json.dumps(result['evaluation_set']))


if __name__ == '__main__':
    main()
