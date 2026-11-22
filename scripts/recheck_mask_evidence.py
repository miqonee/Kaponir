#!/usr/bin/env python3
"""Recheck saved A–D replies with source-matched formatting tolerance for MASK."""

import argparse
import hashlib
import json
from pathlib import Path
import re

from check_learning_corpus import load_corpus
from compare_local_models import ROOT
from evaluate_privacy_policy import parse_response
from experiment_inference import prompts_and_kinds
from privacy_policy_experiment import payload, technical_review, validate
from summarize_privacy_policy import load_run, require, strict_json
from validation_pipeline import Candidate


QUOTE_END = {'"': '"', "'": "'", '«': '»', '“': '”', '`': '`'}
EVIDENCE_LIMIT = 160


def _source_excerpt(evidence, context):
    if not isinstance(evidence, str) or len(evidence) > EVIDENCE_LIMIT:
        return None
    phrase = evidence.strip()
    alternatives = [phrase]
    if len(phrase) > 2 and QUOTE_END.get(phrase[0]) == phrase[-1]:
        alternatives.append(phrase[1:-1].strip())
    for alternative in alternatives:
        if not alternative:
            continue
        pattern = re.compile(r'\s+'.join(re.escape(word) for word in alternative.split()))
        for text in (context['record'], context['context']):
            for match in pattern.finditer(text):
                if match.end() - match.start() <= EVIDENCE_LIMIT:
                    return match.group()
    return None


def validate_mask_evidence(reply, cases, contexts, kinds, variant):
    """Accept formatting differences only where a rejected MASK has an exact source excerpt."""
    decisions = validate(reply, cases, contexts, kinds, variant)
    if variant == 'D' or not any(row.get('error') == 'invalid_review_evidence_or_shape'
                                 for row in decisions.values()):
        return decisions

    rows = []
    for row in reply['decisions']:
        ident = row['id']
        if not decisions[ident]['valid'] and row.get('verdict') == 'mask':
            excerpt = _source_excerpt(row.get('evidence'), contexts[ident])
            if excerpt is not None:
                row = {**row, 'evidence': excerpt}
        rows.append(row)
    return validate({'decisions': rows}, cases, contexts, kinds, variant)


def _read_verified(path, expected_sha):
    raw = Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha, 'quoted_evidence_input_changed')
    return strict_json(raw.decode('utf-8'))


def recheck(path):
    measured = load_run(path)
    require(measured['run_status'] == 'completed' and all(
        row['outcome_evidence']['all_generation_outcomes_observed'] for row in measured['rows']),
        'quoted_evidence_requires_completed_run')
    state = _read_verified(measured['result_path'], measured['result_sha256'])
    plan = _read_verified(measured['plan_path'], measured['plan_file_sha256'])
    _, expected, _, sources, _ = load_corpus()
    signature = plan['signature']
    require(all(hashlib.sha256(sources[name].encode()).hexdigest() == sha
                for name, sha in signature['source_sha256'].items()), 'quoted_evidence_corpus_changed')
    annotations = (ROOT / 'tests/docs/expected.json').read_bytes()
    require(hashlib.sha256(annotations).hexdigest() == signature['annotations_sha256'],
            'quoted_evidence_annotations_changed')
    _, _, kinds = prompts_and_kinds()
    cases = {row['id']: Candidate(**row) for row in plan['control_cases']['candidates']}
    responses = {}
    for call in state['calls']:
        entry = state['responses'][call]
        responses[entry['variant'], entry['batch_index']] = entry

    additions = {variant: [] for variant in plan['variant_order']}
    for request in plan['requests']:
        variant = request['variant']
        batch = [cases[ident] for ident in request['case_ids']]
        _, contexts = payload(variant, batch, sources)
        reply, error = parse_response(responses[variant, request['batch_index']])
        if error:
            strict = checked = {case.id: technical_review(case, error) for case in batch}
        else:
            strict = validate(reply, batch, contexts, kinds, variant)
            checked = validate_mask_evidence(reply, batch, contexts, kinds, variant)
        for case in batch:
            before = strict[case.id]
            require(before == state['stages'][variant]['decisions'][case.id], 'quoted_evidence_reference_mismatch')
            after = checked[case.id]
            if before != after:
                require(not before['valid'] and after['valid'] and after['verdict'] == 'mask',
                        'quoted_evidence_unexpected_decision_change')
                additions[variant].append({'id': case.id, 'private': (case.file, case.value) in expected})

    return {'model': measured['model'], 'source_result_sha256': measured['result_sha256'],
            'source_plan_file_sha256': measured['plan_file_sha256'], 'additional_mask_decisions': additions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', nargs='+', type=Path, help='Completed A–D run directories or result.json files')
    args = parser.parse_args()
    print(json.dumps([recheck(path) for path in args.results], indent=2))


if __name__ == '__main__':
    main()
