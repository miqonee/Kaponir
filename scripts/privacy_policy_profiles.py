"""Model-specific phase2 profiles and verified historical references.

References are recalculations of saved validated decisions, never fresh inference.
This module does not alter prompts, start runtimes, or write results. Profile source
hashes are returned so a later runner can bind the complete configuration to its
signature without importing this module into an already active experiment.
"""
import gzip
import hashlib
import json
from pathlib import Path

from check_learning_corpus import load_corpus


ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / 'docs/experiments/phase2-model-profiles.json'
VALIDATION_ROOT = ROOT / 'benchmark/data/validation'
METHOD = 'review-document-bounded'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _source():
    raw = PROFILE_PATH.read_bytes()
    data = json.loads(raw)
    rows = data.get('models')
    if data.get('version') != 1 or not isinstance(data.get('common'), dict) or not isinstance(rows, list):
        raise ValueError('invalid_phase2_profile_source')
    identifiers = [row.get('model') for row in rows if isinstance(row, dict)]
    if (len(identifiers) != len(rows) or any(not isinstance(name, str) for name in identifiers)
            or len(set(identifiers)) != len(rows)):
        raise ValueError('invalid_phase2_model_profiles')
    path = PROFILE_PATH.relative_to(ROOT) if PROFILE_PATH.is_relative_to(ROOT) else PROFILE_PATH
    return data, {'path': path.as_posix(), 'sha256': _sha(raw)}


def _row(data, model):
    row = next((row for row in data['models'] if row['model'] == model), None)
    if row is None:
        raise ValueError('unknown_phase2_model')
    return row


def _counts(state):
    _, expected, _, sources, _ = load_corpus()
    signature = state['signature']
    annotation_sha = _sha((ROOT / 'tests/docs/expected.json').read_bytes())
    if signature.get('annotations_sha256') != annotation_sha:
        raise ValueError('historical_annotations_changed')
    for name, wanted in signature['source_sha256'].items():
        if name not in sources or _sha(sources[name].encode()) != wanted:
            raise ValueError('historical_document_changed')
    cases = state['control_cases']['candidates']
    decisions = state['stages'][METHOD]['decisions']
    ids = [case['id'] for case in cases]
    if len(cases) != 32 or len(set(ids)) != 32 or set(decisions) != set(ids):
        raise ValueError('historical_control_cases_changed')
    result = {name: 0 for name in ['private_cases', 'public_cases', 'private_mask',
        'private_keep', 'public_mask', 'public_keep', 'explicit_review', 'technical_review',
        'private_mask_kind_correct', 'private_mask_scope_correct', 'private_mask_kind_and_scope_correct']}
    for case in cases:
        key = case['file'], case['value']
        private = key in expected
        result['private_cases' if private else 'public_cases'] += 1
        row = decisions[case['id']]
        if not row.get('valid'):
            result['technical_review'] += 1
            continue
        verdict = row.get('verdict')
        if verdict == 'review':
            result['explicit_review'] += 1
        elif verdict in ('mask', 'keep'):
            result[('private_' if private else 'public_') + verdict] += 1
        else:
            raise ValueError('invalid_historical_verdict')
        if private and verdict == 'mask':
            kind, scope = expected[key]
            result['private_mask_kind_correct'] += row.get('kind') == kind
            result['private_mask_scope_correct'] += row.get('scope') == scope
            result['private_mask_kind_and_scope_correct'] += (row.get('kind'), row.get('scope')) == (kind, scope)
    result['review'] = result['explicit_review'] + result['technical_review']
    published = state['metrics'][METHOD]
    checks = {'private_mask': 'confirmed_matches', 'private_keep': 'correct_values_filtered',
              'public_mask': 'confirmed_outside', 'public_keep': 'outside_values_filtered',
              'technical_review': 'invalid_decisions', 'review': 'needs_review_values'}
    if any(result[key] != published[field] for key, field in checks.items()):
        raise ValueError('historical_metrics_mismatch')
    if (result['private_cases'], result['public_cases']) != (16, 16):
        raise ValueError('historical_control_balance_changed')
    result.update(attempted_cases=published['attempted_cases'],
                  failed_batches=published['failed_batches'],
                  technical_incomplete=bool(result['technical_review'] or published['failed_batches']
                                           or published.get('quality_is_lower_bound_if_incomplete')))
    return result


def _reference(row):
    archive = row['baseline_archive']
    if archive is None:
        return None
    if not isinstance(archive, str) or Path(archive).name != archive:
        raise ValueError('invalid_historical_archive_path')
    path = VALIDATION_ROOT / archive
    compressed = path.read_bytes()
    raw = gzip.decompress(compressed)
    state = json.loads(raw)
    model = row['model']
    if (state['signature']['model'] != model
            or state['signature']['settings'].get('model') != model):
        raise ValueError('historical_model_mismatch')
    catalog = json.loads((ROOT / 'benchmark/data/models.json').read_text())
    catalog_row = next(item for item in catalog['models'] if item['id'] == model)
    artifact = catalog_row['artifact']
    wanted = [artifact[field] for field in ('filename', 'bytes', 'sha256', 'url')]
    if state.get('artifact') != wanted:
        raise ValueError('historical_model_artifact_mismatch')
    manifest = json.loads((VALIDATION_ROOT / 'manifest.json').read_text())
    archived = next((item for item in manifest['files'].values() if item['archive'] == archive), None)
    if (archived is None or archived['archive_sha256'] != _sha(compressed)
            or archived['json_sha256'] != _sha(raw)):
        raise ValueError('historical_archive_checksum_mismatch')
    return {'model': model, 'path': path.relative_to(ROOT).as_posix(),
            'archive_sha256': _sha(compressed), 'json_sha256': _sha(raw),
            'method': METHOD, 'protocol_status': state['status'],
            'artifact': wanted, 'quantization': catalog_row['quantization'],
            'runtime_sha256': state['signature']['runtime_sha256'],
            'effective_response_format': state['structured_output'],
            'settings': state['signature']['settings'],
            'action_counts': _counts(state),
            'role': 'Historical reference only; every phase2 A/B/C/D response must be freshly measured.'}


def historical_reference(model):
    """Return the correct verified bounded archive; Huihui has no such archive."""
    data, _ = _source()
    return _reference(_row(data, model))


def _profile(data, row, source):
    return {**data['common'], **row, 'profile_source': dict(source),
            'historical_reference': _reference(row),
            'historical_full_baseline_available': row['baseline_archive'] is not None,
            'fresh_A_required': True}


def profile(model):
    """Return a new JSON-safe profile plus source-file provenance."""
    data, source = _source()
    return _profile(data, _row(data, model), source)


def iter_profiles():
    """Yield Gemma, Qwen2, Qwen0.8, Huihui, MiniCPM, Granite, Nanbeige."""
    data, source = _source()
    for row in data['models']:
        yield _profile(data, row, source)
