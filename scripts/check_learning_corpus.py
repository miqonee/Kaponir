#!/usr/bin/env python3
"""Validate synthetic source documents and compare exact discovery/categorization results."""
import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent / 'tests' / 'docs'
KINDS = {'PROJ', 'SVC', 'HOST', 'SECRET', 'DOM', 'ORG', 'CLIENT', 'PERSON',
         'USER', 'MODULE', 'CUSTOM', 'EMAIL', 'PHONE', 'IP', 'PATH', 'AMOUNT'}
SCOPES = {'personal', 'team', 'global'}


def occurrences(text, value):
    """Do not count s1 in s10, or 7 500,00 ₽ inside 17 500,00 ₽."""
    before = r'(?<!\w)' if value[0].isalnum() or value[0] == '_' else ''
    after = r'(?!\w)' if value[-1].isalnum() or value[-1] == '_' else ''
    return list(re.finditer(before + re.escape(value) + after, text))



def validate_estimate(text, check):
    def amount(value):
        if not re.fullmatch(r'[0-9]+(?: [0-9]{3})*,[0-9]{2} ₽', value):
            raise ValueError(f'Invalid money cell: {value}')
        return Decimal(value.removesuffix(' ₽').replace(' ', '').replace(',', '.'))
    rows, totals = [], {}
    for line in text.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) == 6 and cells[0].isdigit():
            quantity, price, line_total = Decimal(cells[3]), amount(cells[4]), amount(cells[5])
            if price * quantity != line_total:
                raise ValueError(f'Estimate row {cells[0]}: price × quantity != total')
            rows.append(line_total)
        elif len(cells) == 2 and cells[1].endswith(' ₽'):
            totals[cells[0]] = amount(cells[1])
    if len(rows) != check['line_items']:
        raise ValueError('Incorrect estimate line count')
    cents = lambda value: value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    subtotal = sum(rows, Decimal(0))
    discount = cents(subtotal * Decimal(check['discount_percent']) / 100)
    base = subtotal - discount
    vat = cents(base * Decimal(check['vat_percent']) / 100)
    total = base + vat
    advance = cents(total * Decimal(check['advance_percent']) / 100)
    wanted = {'Итого до скидки': subtotal, 'Скидка по переговорам, 3%': discount,
              'База после скидки': base, 'НДС, 20%': vat, 'Всего к оплате с НДС': total,
              'Аванс, 40%': advance, 'Окончательный платёж': total - advance}
    if totals != wanted:
        raise ValueError('Estimate subtotal, discount, VAT or payment schedule does not reconcile')
    return {'line_items': len(rows), 'total_rub': str(total), 'arithmetic_valid': True}


def load_corpus(root=ROOT):
    corpus = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    if corpus.get('version') not in (1, 2) or not isinstance(corpus.get('documents'), list):
        raise ValueError('Unsupported corpus schema')
    input_dir = root / corpus.get('input_dir', '.')
    if input_dir.resolve() != root.resolve() and input_dir.resolve().parent != root.resolve():
        raise ValueError('input_dir must be inside the corpus directory')
    expected, public, sources, stats = {}, {}, {}, []
    for doc in corpus['documents']:
        name = doc['file']
        if not isinstance(name, str) or Path(name).name != name or name in sources:
            raise ValueError(f'Invalid or duplicate document name: {name}')
        raw = (input_dir / name).read_bytes()
        text = raw.decode('utf-8')
        if 'sha256' in doc and hashlib.sha256(raw).hexdigest() != doc['sha256']:
            raise ValueError(f'{name}: source digest changed; review annotations')
        if 'bytes' in doc and len(raw) != doc['bytes']:
            raise ValueError(f'{name}: incorrect byte count')
        sources[name] = text
        seen = set()
        for row in doc['sensitive']:
            value = row['value']
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f'{name}: empty or padded sensitive value')
            if value in seen or type(row['occurrences']) is not int or row['occurrences'] <= 0:
                raise ValueError(f'{name}: duplicate value or invalid occurrence count: {value}')
            if len(occurrences(text, value)) != row['occurrences']:
                raise ValueError(f'{name}: incorrect occurrence count: {value}')
            if row['scope'] not in SCOPES or row['kind'] not in KINDS:
                raise ValueError(f'{name}: invalid kind/scope for {value}')
            seen.add(value)
            expected[(name, value)] = (row['kind'], row['scope'])
        if len(set(doc['public'])) != len(doc['public']):
            raise ValueError(f'{name}: duplicate public control')
        for value in doc['public']:
            if not isinstance(value, str) or not value or not occurrences(text, value) or value in seen:
                raise ValueError(f'{name}: missing or conflicting public value: {value}')
        public[name] = doc['public']
        financial = validate_estimate(text, doc['financial_check']) if 'financial_check' in doc else None
        stats.append({'file': name, 'bytes': len(raw), 'words': len(text.split()),
                      'sensitive_values': len(seen),
                      'sensitive_occurrences': sum(r['occurrences'] for r in doc['sensitive']),
                      'public_controls': len(doc['public']), **({'financial_check': financial} if financial else {})})
    enrolled = set(sources)
    files = {p.name for p in input_dir.iterdir() if p.is_file()}
    if corpus.get('version') == 2 and enrolled != files:
        raise ValueError(f'Unenrolled/missing inputs: {sorted(enrolled ^ files)}')
    return corpus, expected, public, sources, stats


def read_predictions(path, sources):
    predictions = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(predictions, dict) and isinstance(predictions.get('entries'), list):
        predictions = [{**row, 'file': row.get('source')} for row in predictions['entries']]
    if not isinstance(predictions, list):
        raise ValueError('Predictions must be a JSON array or a learning API response')
    actual = {}
    for row in predictions:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k] for k in ('file', 'value')):
            raise ValueError('Each prediction needs nonempty file and value strings')
        key = (row['file'], row['value'])
        if key[0] not in sources:
            raise ValueError(f'Unknown document {key[0]!r}; export only corpus results from a clean workspace')
        for field in ('kind', 'scope'):
            if row.get(field) is not None and (not isinstance(row[field], str) or not row[field]):
                raise ValueError(f'Invalid prediction {field} for {key}')
        category = (row.get('kind'), row.get('scope'))
        if key in actual and actual[key] != category:
            raise ValueError(f'Conflicting categories for {key}')
        actual[key] = category
    return actual


def ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def compare(expected, actual, public, sources):
    matched = expected.keys() & actual.keys()
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    categorized = {key for key in matched if all(actual[key])}
    wrong_kind = sorted(key for key in categorized if expected[key][0] != actual[key][0])
    wrong_scope = sorted(key for key in categorized if expected[key][1] != actual[key][1])
    wrong = sorted(set(wrong_kind) | set(wrong_scope))
    public_hits = [key for key in extra if key[1] in public[key[0]]]
    unsupported = [key for key in actual if not occurrences(sources[key[0]], key[1])]
    boundaries = [{'file': f, 'value': v, 'related_expected': sorted(
        gold for name, gold in expected if name == f and (v in gold or gold in v))}
        for f, v in extra if any(name == f and (v in gold or gold in v) for name, gold in expected)]
    by_document = []
    for name in sources:
        gold = {key for key in expected if key[0] == name}
        found = {key for key in actual if key[0] == name}
        by_document.append({'file': name, 'expected': len(gold), 'predicted': len(found),
                            'matched': len(gold & found), 'missing': len(gold - found),
                            'extra': len(found - gold),
                            'precision': ratio(len(gold & found), len(found)),
                            'recall': ratio(len(gold & found), len(gold))})
    pairs = lambda keys: [{'file': f, 'value': v} for f, v in sorted(keys)]
    return {
        'expected': len(expected), 'predicted': len(actual), 'detected': len(matched),
        'discovery_recall': ratio(len(matched), len(expected)),
        'prediction_precision': ratio(len(matched), len(actual)),
        'correct_categorizations': len(categorized) - len(wrong),
        'categorization': {'scored': len(categorized), 'missing': len(matched - categorized),
                           'kind_accuracy': ratio(len(categorized) - len(wrong_kind), len(categorized)),
                           'scope_accuracy': ratio(len(categorized) - len(wrong_scope), len(categorized)),
                           'joint_accuracy': ratio(len(categorized) - len(wrong), len(categorized))},
        'missing': pairs(missing), 'extra': pairs(extra),
        'public_false_positives': pairs(public_hits), 'not_in_source': pairs(unsupported),
        'boundary_mismatches': boundaries,
        'wrong_category': [{'file': f, 'value': v, 'expected': expected[(f, v)], 'actual': actual[(f, v)]} for f, v in wrong],
        'by_document': by_document,
        'by_kind': {kind: {'expected': count,
                          'detected': sum(key in matched for key in expected if expected[key][0] == kind)}
                    for kind, count in sorted(Counter(kind for kind, _ in expected.values()).items())},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--output', type=Path, help='Save a machine-readable JSON report')
    args = parser.parse_args()
    corpus, expected, public, sources, stats = load_corpus()
    report = {'corpus_version': corpus['version'],
              'corpus_sha256': hashlib.sha256((ROOT / 'expected.json').read_bytes()).hexdigest(),
              'documents': stats}
    print(f"Разметка: {len(sources)} документов, {len(expected)} чувствительных значений, "
          f"{sum(s['sensitive_occurrences'] for s in stats)} вхождений.")
    if args.predictions:
        report['evaluation'] = compare(expected, read_predictions(args.predictions, sources), public, sources)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
