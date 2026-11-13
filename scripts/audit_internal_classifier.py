#!/usr/bin/env python3
"""Audit detector hypotheses, policy decisions and applied spans independently of LLMs.

The gold annotations are used for scoring only. No training, rules or vault writes.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

from check_learning_corpus import load_corpus, occurrences
from compare_local_models import ROOT, DOCUMENTS
from evaluate_validation_schemes import atomic_json, detector_pool
from validation_pipeline import byte_to_char


def source_span(text, row, field='value'):
    left, right = byte_to_char(text, row['start']), byte_to_char(text, row['end'])
    if not left < right or text[left:right] != row[field]:
        raise ValueError('classifier_audit_offset_mismatch')
    return left, right


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def span_coverage(text, gold, public, applied):
    """Literal span and letter/digit coverage; neither is a privacy guarantee."""
    hidden = [False]*len(text)
    for row in applied:
        left, right = source_span(text, row, 'surface')
        hidden[left:right] = [True]*(right-left)
    sensitive = [(match.start(), match.end()) for value in gold for match in occurrences(text, value)]
    rows = []
    for value in gold:
        samples = []
        for match in occurrences(text, value):
            left, right = match.span()
            positions = [i for i in range(left, right) if text[i].isalnum()]
            if not positions: positions = [i for i in range(left, right) if not text[i].isspace()]
            samples.append({'start': left, 'end': right,
                'touched': any(hidden[left:right]),
                'full_span_covered': all(hidden[left:right]),
                'letters_digits_covered': bool(positions) and all(hidden[i] for i in positions)})
        rows.append({'value': value, 'occurrences': samples})
    public_clear, public_damaged, public_confounded = 0, 0, 0
    for value in public:
        for match in occurrences(text, value):
            if any(overlap(match.span(), span) for span in sensitive):
                public_confounded += 1
                continue
            public_clear += 1
            public_damaged += any(hidden[match.start():match.end()])
    samples = [sample for row in rows for sample in row['occurrences']]
    return {'gold_occurrences': len(samples),
            'gold_occurrences_touched': sum(row['touched'] for row in samples),
            'gold_occurrences_full_span_covered': sum(row['full_span_covered'] for row in samples),
            'gold_occurrences_letters_digits_covered': sum(row['letters_digits_covered'] for row in samples),
            'gold_occurrences_untouched': sum(not row['touched'] for row in samples),
            'public_clear_occurrences': public_clear,
            'public_clear_occurrences_masked': public_damaged,
            'public_occurrences_inside_gold_excluded': public_confounded,
            'gold_coverage': rows}


def attribution(span, private_spans, value, public):
    if any(a <= span[0] and span[1] <= b for a, b in private_spans):
        return 'inside_gold_span'
    if any(span[0] <= a and b <= span[1] for a, b in private_spans):
        return 'contains_gold_span'
    if any(overlap(span, other) for other in private_spans):
        return 'overlaps_gold_span'
    return 'public_control' if value in public else 'unannotated_no_overlap'


def summarize(raw, pool, expected, public, sources, documents):
    by_file = {row['file']: row for row in raw['documents']}
    report = {'documents': list(documents), 'expected_values': 0, 'files': {}}
    stages = {key: set() for key in ['raw_engine', 'discovery', 'combined_proposals', 'applied_masks']}
    all_expected = {key for key in expected if key[0] in documents}
    for name in documents:
        text = sources[name]; document = by_file[name]
        gold = {value: kind_scope for (file, value), kind_scope in expected.items() if file == name}
        private_spans = [match.span() for value in gold for match in occurrences(text, value)]
        candidates = [c for c in pool if c.file == name]
        for row in document['raw_candidates']:
            source_span(text, row)
            if row['polarity'] == 'mask': stages['raw_engine'].add((name, row['value']))
        for row in document['policy_decisions']: source_span(text, row, 'surface')
        for row in document['applied_masks']:
            source_span(text, row, 'surface'); stages['applied_masks'].add((name, row['surface']))
        if document['discovery']['truncated']: raise ValueError('discovery_audit_truncated')
        for row in document['discovery']['suggestions']:
            stages['discovery'].add((name, row['value']))
        stages['combined_proposals'].update((name, c.value) for c in candidates)
        outside = []
        wrong_kind = []
        for c in candidates:
            if c.value in gold:
                if c.kind != gold[c.value][0]:
                    wrong_kind.append({'value': c.value, 'predicted_kind': c.kind,
                                       'expected_kind': gold[c.value][0], 'origin': c.origin})
                continue
            outside.append({'value': c.value, 'kind': c.kind, 'origin': c.origin,
                'protected': c.protected, 'applied': c.applied,
                'attribution': attribution((c.start,c.end),private_spans,c.value,public[name])})
        proposed = {c.value for c in candidates}
        missing = [{'value': value, 'kind': kind_scope[0], 'scope': kind_scope[1]}
                   for value, kind_scope in gold.items() if value not in proposed]
        report['files'][name] = {
            'expected_values': len(gold),
            'policy_verdicts_per_occurrence': dict(Counter(row['verdict'] for row in document['policy_decisions'])),
            'unapplied_mask_verdicts_per_occurrence': sum(
                row['verdict']=='mask' and not row['applied'] for row in document['policy_decisions']),
            'missing_from_combined_proposals': missing,
            'wrong_kind_exact': wrong_kind,
            'outside_proposals': outside,
            'outside_attribution': dict(Counter(row['attribution'] for row in outside)),
            **span_coverage(text,gold,public[name],document['applied_masks']),
        }
    report['expected_values'] = len(all_expected)
    report['exact_value_stages'] = {name: {'values': len(values),
        'gold_exact': len(values & all_expected), 'outside_gold': len(values-all_expected),
        'gold_not_proposed': len(all_expected-values)} for name, values in stages.items()}
    for key in ['gold_occurrences','gold_occurrences_touched','gold_occurrences_full_span_covered',
                'gold_occurrences_letters_digits_covered','gold_occurrences_untouched',
                'public_clear_occurrences','public_clear_occurrences_masked',
                'public_occurrences_inside_gold_excluded']:
        report[key] = sum(file[key] for file in report['files'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-binary', type=Path, required=True)
    parser.add_argument('--detectors', type=Path, required=True, help='Frozen matrix exporter')
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/validation-schemes/classifier-audit')
    args = parser.parse_args()
    _, expected, public, sources, _ = load_corpus()
    result = subprocess.run([str(args.audit_binary.resolve())],
        input=json.dumps([{'name':name,'text':text} for name,text in sources.items()]),
        text=True,capture_output=True,check=True)
    raw = json.loads(result.stdout)
    if raw.get('s1') is not False or raw.get('rules') is not False:
        raise ValueError('unexpected_classifier_audit_mode')
    pool, _ = detector_pool(args.detectors, sources)
    args.output.mkdir(parents=True,exist_ok=True)
    atomic_json(args.output/'raw.json',raw)
    report = {'version':1,'s1_status':'disabled_no_trained_user_model_supplied',
        'rules':False,'policy':raw['policy'],
        'source_sha256':{name:hashlib.sha256(text.encode()).hexdigest() for name,text in sources.items()},
        'annotations_sha256':hashlib.sha256((ROOT/'tests/docs/expected.json').read_bytes()).hexdigest(),
        'audit_binary_sha256':hashlib.file_digest(args.audit_binary.open('rb'),'sha256').hexdigest(),
        'detector_binary_sha256':hashlib.file_digest(args.detectors.open('rb'),'sha256').hexdigest(),
        'calibration':summarize(raw,pool,expected,public,sources,DOCUMENTS),
        'full_corpus':summarize(raw,pool,expected,public,sources,list(sources)),
        'limits':['Letter/digit coverage and exact-span coverage are literal overlap measures, not a privacy guarantee.',
                  'Discovery hypotheses are not automatically applied masks.',
                  'Public controls overlapping a gold sensitive span are reported separately.',
                  'No user-specific S1 n-gram model was evaluated and no model was trained on test annotations.']}
    atomic_json(args.output/'result.json',report)
    print(json.dumps({name:report[name]['exact_value_stages'] for name in ['calibration','full_corpus']}))


if __name__=='__main__':main()
