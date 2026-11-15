#!/usr/bin/env python3
"""Verify the offline session package and independently rescore its 42 main cells.

Standard library only. Does not run models, contact services or modify the package.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

MODELS = {
    'minicpm5-2b': 'minicpm-calibration',
    'qwen3.5-2b': 'qwen-calibration',
    'huihui-qwen3.5-2b-abliterated': 'huihui-calibration',
    'qwen3.5-0.8b': 'qwen08-calibration',
    'nanbeige42-3b': 'nanbeige-calibration',
    'granite42-3b': 'granite-calibration',
    'gemma4-e4b-qat': 'gemma-calibration',
}
METHODS = ['detector_proposals', 'current_union', 'point', 'regions', 'union_point', 'double']


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('path_outside_package:' + relative)
    if path.is_symlink():
        raise ValueError('symlink_not_supported:' + relative)
    return path


def content(root: Path, row: dict) -> bytes:
    raw = safe_path(root, row['path']).read_bytes()
    return gzip.decompress(raw) if row.get('encoding') == 'gzip' else raw


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def score(stage: dict, gold: dict, public: dict) -> dict:
    """Recompute the published exact-value metrics from saved candidate decisions."""
    proposed = {(c['file'], c['value']) for c in stage['candidates']}
    visible, confirmed, kept, kinds, scopes = set(), set(), set(), set(), set()
    for candidate in stage['candidates']:
        key = candidate['file'], candidate['value']
        row = stage['decisions'].get(candidate['id'], {})
        verdict = row.get('verdict') if row.get('valid') else 'review'
        protected = candidate.get('protected', False)
        (visible if protected or verdict != 'keep' else kept).add(key)
        if protected or verdict == 'mask':
            confirmed.add(key)
        if key in gold and row.get('valid') and verdict == 'mask':
            if row.get('kind') == gold[key][0]:
                kinds.add(key)
            if row.get('scope') == gold[key][1]:
                scopes.add(key)
    kept -= visible
    expected = set(gold)
    private_files = {file for file, _ in expected}
    return {
        'expected': len(expected), 'proposals': len(proposed),
        'proposal_matches': len(proposed & expected),
        'visible_matches': len(visible & expected), 'visible_outside': len(visible - expected),
        'confirmed_matches': len(confirmed & expected), 'confirmed_outside': len(confirmed - expected),
        'correct_values_filtered': len(kept & expected),
        'outside_values_filtered': len((proposed - visible) - expected),
        'needs_review_values': len(visible - confirmed),
        'correct_kind': len(kinds), 'correct_scope': len(scopes),
        'public_control_visible': sum(value in public.get(file, []) for file, value in visible),
        'negative_document_visible': sum(file not in private_files for file, _ in visible),
    }


def verify(root: Path, manifest: dict | None = None) -> dict:
    root = root.resolve()
    manifest = manifest or json.loads((root / 'manifest.json').read_text())
    require(manifest['version'] == 1, 'unsupported_manifest_version')
    for relative, row in manifest['files'].items():
        path = safe_path(root, relative)
        require(path.is_file(), 'missing_file:' + relative)
        raw = path.read_bytes()
        require(len(raw) == row['bytes'], 'size_mismatch:' + relative)
        require(digest(raw) == row['sha256'], 'sha256_mismatch:' + relative)

    collection = json.loads((root / 'data/COLLECTION.json').read_text())
    catalog = {row['path']: row for row in collection['files']}
    for row in collection['files']:
        raw = safe_path(root, row['path']).read_bytes()
        require(digest(raw) == row['sha256'], 'collection_sha_mismatch:' + row['path'])
        if 'content_sha256' in row:
            decoded = content(root, row)
            require(digest(decoded) == row['content_sha256'], 'decoded_sha_mismatch:' + row['path'])
            require(len(decoded) == row['content_bytes'], 'decoded_size_mismatch:' + row['path'])
        if row.get('redactions', 0) == 0 and row.get('source_sha256') and row.get('encoding') != 'gzip':
            require(digest(raw) == row['source_sha256'], 'original_bytes_changed:' + row['path'])
    for alias in collection['aliases']:
        require(alias['path'] in catalog, 'missing_alias_target:' + alias['source'])
        require(digest(content(root, catalog[alias['path']])) == alias['source_sha256'],
                'alias_content_mismatch:' + alias['source'])

    evidence = json.loads((root / 'data/validation/manifest.json').read_text())
    decoded_runs = {}
    for source, row in evidence['files'].items():
        packed = (root / 'data/validation' / row['archive']).read_bytes()
        raw = gzip.decompress(packed)
        require(digest(packed) == row['archive_sha256'], 'archive_sha_mismatch:' + source)
        require(digest(raw) == row['json_sha256'] and len(raw) == row['json_bytes'], 'json_sha_mismatch:' + source)
        state = json.loads(raw)
        require(state.get('status', 'completed') == row['status'], 'status_changed:' + source)
        if 'signature' in state:
            require(state.get('shadow') is True and state.get('rules_changed') == 0, 'non_shadow_run:' + source)
        decoded_runs[source] = state

    annotations_raw = (root / 'data/corpus/expected.json').read_bytes()
    annotations = json.loads(annotations_raw)
    gold = {(d['file'], item['value']): (item['kind'], item['scope'])
            for d in annotations['documents'] for item in d['sensitive']}
    public = {d['file']: [item if isinstance(item, str) else item['value'] for item in d['public']]
              for d in annotations['documents']}
    report = json.loads((root / 'data/validation-results.json').read_text())
    require(digest(annotations_raw) == report['annotations_sha256'], 'annotations_changed')
    require(len(gold) == 219, 'full_corpus_size_changed')
    for name, expected_sha in report['source_sha256'].items():
        require(digest((root / 'data/corpus/input' / name).read_bytes()) == expected_sha, 'corpus_changed:' + name)
    reference_pool = None
    for model, label in MODELS.items():
        source = label + '/result.json'
        state = decoded_runs[source]
        require(state['status'] == 'completed', 'main_run_incomplete:' + model)
        row = report['primary_matrix']['models'].get(model) or report['additional_document_runs'][model]
        require(row['result_sha256'] == evidence['files'][source]['json_sha256'], 'report_source_sha:' + model)
        selected_gold = {key: value for key, value in gold.items() if key[0] in state['signature']['source_sha256']}
        require(len(selected_gold) == 92, 'calibration_size_changed:' + model)
        pool = [(c['file'], c['value'], c['start'], c['end'], c['protected']) for c in state['candidate_pool']]
        if reference_pool is None:
            reference_pool = pool
        require(pool == reference_pool, 'main_detector_pool_changed:' + model)
        for method in METHODS:
            actual = score(state['stages'][method], selected_gold, public)
            require(actual == state['metrics'][method], 'score_mismatch:' + model + ':' + method)
            require(actual == (row.get('methods') or row['metrics'])[method], 'report_metric_mismatch:' + model + ':' + method)
    session = json.loads((root / 'STATE.json').read_text())
    require(session['status'] == 'completed', 'session_not_completed')
    require(session['new_inference_calls_during_packaging'] == 0, 'unexpected_new_experiments')
    return {'files': len(manifest['files']), 'published_archives': len(evidence['files']),
            'source_aliases': len(collection['aliases']), 'rescored_cells': len(MODELS) * len(METHODS),
            'full_corpus_values': len(gold), 'status': 'verified'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.root), ensure_ascii=False))
    except (ValueError, OSError, KeyError, EOFError) as error:
        print('Verification failed: ' + str(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
