"""Frozen fresh disclosure controls and three-way, evaluator-only scoring.

The oracle is mask/keep/review. An expected review is neither a private positive
nor a public negative. Technical failures and unattempted cases are not semantic
decisions. No type/scope ground truth is available for this dataset.
"""
from collections import Counter
from dataclasses import dataclass
import hashlib
from pathlib import Path

from compare_local_models import ROOT
from privacy_policy_experiment import VARIANTS, strict_json
from validation_pipeline import Candidate


DEFAULT_DATASET = ROOT / 'tests/fixtures/privacy-policy-holdout-v1'
DATASET_ID = 'privacy-policy-holdout-v1'
MANIFEST_SHA256 = '4c9e0e0bed53785bc6618e11fbf6e31d5d5519cec6a6cb9a887f3b82bb76fba4'
CASES_SHA256 = 'b1b1cb939c4a362d570c3168a406ecfe6c94b2e42f0a9829a5b4b7b40a728539'
VERDICTS = ('mask', 'keep', 'review')
REFERENCE_COUNTS = {'mask': 14, 'keep': 14, 'review': 4}
_MISSING = object()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _require(value, code):
    if not value:
        raise ValueError(code)


def _safe_file(directory, name):
    _require(isinstance(name, str) and not Path(name).is_absolute()
             and '..' not in Path(name).parts, 'invalid_holdout_file_path')
    path = directory / name
    _require(path.is_file() and not path.is_symlink()
             and path.resolve().is_relative_to(directory.resolve()), 'invalid_holdout_file_path')
    return path


def _case_id(row):
    text = '\0'.join(str(row[key]) for key in ('file', 'value', 'start', 'end'))
    return _sha(text.encode())[:24]


@dataclass
class Holdout:
    cases: list
    sources: dict
    case_data: dict
    expected_by_id: dict
    quality_contract: dict
    annotations_path: Path
    policy_sha256: str

    @property
    def documents(self):
        return list(self.sources)


def load_holdout(directory=DEFAULT_DATASET, policy_path=None):
    """Load only the pinned v1 package; return a label-free candidate pool."""
    directory = Path(directory).resolve()
    raw_manifest = _safe_file(directory, 'manifest.json').read_bytes()
    _require(_sha(raw_manifest) == MANIFEST_SHA256, 'holdout_manifest_checksum_mismatch')
    manifest = strict_json(raw_manifest.decode('utf-8'))
    _require(manifest['dataset_id'] == DATASET_ID and manifest['version'] == 1,
             'invalid_holdout_version')
    files = manifest['files']
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob('*')
              if path.is_file() and '__pycache__' not in path.parts}
    _require(actual == set(files) | {'manifest.json'}, 'holdout_file_set_changed')
    contents = {}
    for name, entry in files.items():
        raw = _safe_file(directory, name).read_bytes()
        _require(len(raw) == entry['bytes'] and _sha(raw) == entry['sha256'],
                 'holdout_file_checksum_mismatch:' + name)
        contents[name] = raw
    _require(_sha(contents['cases.json']) == CASES_SHA256, 'holdout_cases_checksum_mismatch')
    data = strict_json(contents['cases.json'].decode('utf-8'))
    policy_path = (directory / manifest['policy']['path'] if policy_path is None
                   else Path(policy_path))
    _require(_sha(policy_path.read_bytes()) == manifest['policy']['sha256']
             == data['policy_sha256'], 'holdout_policy_checksum_mismatch')
    sources = {name: contents[name].decode('utf-8') for name in sorted(data['source_sha256'])}
    _require(len(sources) == 4, 'holdout_document_count_changed')
    for name, source in sources.items():
        _require(files[name]['role'] == 'model_input_source'
                 and _sha(source.encode()) == data['source_sha256'][name]
                 and '\r' not in source and source.endswith('\n'), 'holdout_source_changed')
    rows = data['cases']
    _require(len(rows) == 32 and len({row['id'] for row in rows}) == 32
             and [row['id'] for row in rows] == sorted(row['id'] for row in rows),
             'holdout_candidate_ids_changed')
    _require(dict(Counter(row['expected_verdict'] for row in rows)) == REFERENCE_COUNTS,
             'holdout_reference_balance_changed')
    candidates, expected = [], {}
    for row in rows:
        _require(set(row) == {'id', 'file', 'value', 'start', 'end', 'expected_verdict', 'rationale'},
                 'holdout_case_shape_changed')
        source = sources.get(row['file'])
        _require(source is not None and type(row['start']) is int and type(row['end']) is int
                 and 0 <= row['start'] < row['end'] <= len(source)
                 and source[row['start']:row['end']] == row['value']
                 and source.count(row['value']) == 1 and row['id'] == _case_id(row),
                 'holdout_candidate_not_grounded')
        candidates.append(Candidate(**{key: row[key] for key in ('id', 'file', 'value', 'start', 'end')}))
        expected[row['id']] = row['expected_verdict']
    _require(set(Counter(case.file for case in candidates).values()) == {8},
             'holdout_document_balance_changed')
    safe_rows = [{key: getattr(case, key) for key in ('id', 'file', 'value', 'start', 'end')}
                 for case in candidates]
    case_data = {'version': 1, 'dataset_id': DATASET_ID,
        'candidate_source': 'fresh_holdout_reference_spans',
        'source_sha256': dict(data['source_sha256']), 'candidates': safe_rows,
        'manifest_sha256': MANIFEST_SHA256, 'case_annotations_sha256': CASES_SHA256,
        'reference_class_counts': dict(REFERENCE_COUNTS),
        'selection': data['selection'], 'offset_convention': data['offset_convention']}
    quality_contract = {'phase': 'fresh_holdout', 'dataset_id': DATASET_ID,
        'reference_classes': list(VERDICTS), 'reference_class_counts': dict(REFERENCE_COUNTS),
        'scoring': 'three_way_disclosure', 'manifest_sha256': MANIFEST_SHA256,
        'annotations_sha256': CASES_SHA256,
        'typing': 'unavailable_no_type_or_scope_oracle',
        'reference_review_is_public': False,
        'resources_role': 'reference_only_low_priority_separate_from_quality',
        'provenance': manifest['provenance']}
    return Holdout(candidates, sources, case_data, expected, quality_contract,
                   directory / 'cases.json', data['policy_sha256'])


def _reference(cases, expected_by_id):
    ids = [case.id for case in cases]
    _require(len(ids) == len(set(ids)) and set(ids) == set(expected_by_id)
             and all(value in VERDICTS for value in expected_by_id.values()),
             'holdout_reference_does_not_match_cases')


def _state(row, expected_id=None):
    if row is _MISSING:
        return 'unattempted'
    if (not isinstance(row, dict) or row.get('valid') is not True or row.get('verdict') not in VERDICTS
            or row.get('review_origin') == 'technical' or row.get('error')
            or expected_id is not None and row.get('id') != expected_id):
        return 'technical_review'
    return 'explicit_review' if row['verdict'] == 'review' else 'automated'


def score_holdout(cases, decisions, expected_by_id):
    """Three-class oracle, applicable confusion matrix, separate missing/invalid."""
    _reference(cases, expected_by_id)
    _require(isinstance(decisions, dict), 'invalid_holdout_decisions')
    _require(not set(decisions) - set(expected_by_id), 'unknown_holdout_decision_ids')
    matrix = {label: {verdict: 0 for verdict in VERDICTS} for label in VERDICTS}
    per_class = {label: {'reference_cases': 0, 'attempted_cases': 0, 'applicable_decisions': 0,
                        'technical_review': 0, 'unattempted_cases': 0} for label in VERDICTS}
    errors = Counter()
    result = {'cases': len(cases), 'attempted_cases': 0, 'unattempted_cases': 0,
        'applicable_decisions': 0, 'technical_review': 0, 'explicit_review': 0,
        'automated_decisions': 0, 'correct_actions': 0, 'correct_automatic_actions': 0,
        'typing': None}
    for case in cases:
        label = expected_by_id[case.id]
        per_class[label]['reference_cases'] += 1
        row = decisions.get(case.id, _MISSING)
        state = _state(row, case.id)
        if state == 'unattempted':
            result['unattempted_cases'] += 1
            per_class[label]['unattempted_cases'] += 1
            continue
        result['attempted_cases'] += 1
        per_class[label]['attempted_cases'] += 1
        if state == 'technical_review':
            result['technical_review'] += 1
            per_class[label]['technical_review'] += 1
            code = row.get('error') if isinstance(row, dict) else None
            if isinstance(row, dict) and row.get('id') != case.id:
                code = 'invalid_decision_id'
            errors[code if isinstance(code, str) and code else 'invalid_decision'] += 1
            continue
        verdict = row['verdict']
        matrix[label][verdict] += 1
        per_class[label]['applicable_decisions'] += 1
        result['applicable_decisions'] += 1
        result['explicit_review' if verdict == 'review' else 'automated_decisions'] += 1
        if label == verdict:
            result['correct_actions'] += 1
            result['correct_automatic_actions'] += verdict != 'review'
    result.update(confusion_matrix=matrix, per_reference_class=per_class,
        private_cases=per_class['mask']['reference_cases'],
        public_cases=per_class['keep']['reference_cases'],
        reference_review_cases=per_class['review']['reference_cases'],
        private_mask=matrix['mask']['mask'], private_keep=matrix['mask']['keep'],
        public_mask=matrix['keep']['mask'], public_keep=matrix['keep']['keep'],
        private_explicit_review=matrix['mask']['review'], public_explicit_review=matrix['keep']['review'],
        private_technical_review=per_class['mask']['technical_review'],
        public_technical_review=per_class['keep']['technical_review'],
        reference_review_technical_review=per_class['review']['technical_review'],
        reference_review_mask=matrix['review']['mask'], reference_review_keep=matrix['review']['keep'],
        reference_review_correct=matrix['review']['review'],
        overconfident_on_reference_review=matrix['review']['mask'] + matrix['review']['keep'],
        unnecessary_explicit_review=matrix['mask']['review'] + matrix['keep']['review'],
        technical_error_counts=dict(errors),
        confusion_scope='applicable valid decisions only; fixed oracle denominators shown separately')
    return result


def holdout_transitions(cases, stages, expected_by_id, variants=VARIANTS):
    """Keep expected review and technical/explicit/missing response states visible."""
    _reference(cases, expected_by_id)
    present = [variant for variant in variants if variant in stages]
    result = []
    for case in cases:
        answers = {}
        for variant in present:
            decision = stages[variant].get('decisions', {}).get(case.id, _MISSING)
            answers[variant] = {'state': _state(decision, case.id)}
            if isinstance(decision, dict):
                answers[variant].update({key: decision.get(key)
                    for key in ('verdict', 'valid', 'review_origin', 'error')})
        changes = {left + '_to_' + right: (
            answers[left] != answers[right] if all(answers[v]['state'] != 'unattempted'
                                                 for v in (left, right)) else None)
                   for left, right in zip(present, present[1:])}
        result.append({'id': case.id, 'file': case.file, 'value': case.value,
                       'expected_verdict': expected_by_id[case.id],
                       'variants': answers, 'changes': changes})
    return result


def scoring_callbacks(expected_by_id, variants=VARIANTS):
    """Close the three-way oracle over generic runner callbacks, never requests."""
    oracle = dict(expected_by_id)
    order = tuple(variants)

    def metric_function(cases, decisions, _binary_expected, _joint=False):
        return score_holdout(cases, decisions, oracle)

    def transition_function(cases, stages, _binary_expected):
        return holdout_transitions(cases, stages, oracle, order)

    return metric_function, transition_function
