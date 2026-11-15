#!/usr/bin/env python3
"""Verify the frozen fresh disclosure controls without models or network."""
from collections import Counter
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def opaque_id(row):
    text = '\0'.join(str(row[key]) for key in ['file', 'value', 'start', 'end'])
    return digest(text.encode('utf-8'))[:24]


def verify():
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['dataset_id'] == 'privacy-policy-holdout-v1'
    assert manifest['version'] == 1
    expected_files = set(manifest['files']) | {'manifest.json'}
    actual_files = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*')
                    if p.is_file() and '__pycache__' not in p.parts}
    assert actual_files == expected_files, 'file_set_changed'
    for name, row in manifest['files'].items():
        path = ROOT / name
        assert path.resolve().is_relative_to(ROOT), 'path_outside_fixture'
        raw = path.read_bytes()
        assert len(raw) == row['bytes'] and digest(raw) == row['sha256'], name
    policy = ROOT / manifest['policy']['path']
    assert digest(policy.read_bytes()) == manifest['policy']['sha256'], 'policy_changed'
    data = json.loads((ROOT / 'cases.json').read_text(encoding='utf-8'))
    assert data['policy_sha256'] == manifest['policy']['sha256']
    cases = data['cases']
    assert len(cases) == 32 and len({row['id'] for row in cases}) == 32
    assert [row['id'] for row in cases] == sorted(row['id'] for row in cases)
    assert Counter(row['expected_verdict'] for row in cases) == {'mask': 14, 'keep': 14, 'review': 4}
    sources = {name: (ROOT / name).read_text(encoding='utf-8') for name in data['source_sha256']}
    for name, text in sources.items():
        assert digest(text.encode('utf-8')) == data['source_sha256'][name]
        assert '\r' not in text and text.endswith('\n')
    counts = Counter()
    for row in cases:
        assert set(row) == {'id', 'file', 'value', 'start', 'end', 'expected_verdict', 'rationale'}
        source = sources[row['file']]
        assert type(row['start']) is int and type(row['end']) is int
        assert 0 <= row['start'] < row['end'] <= len(source)
        assert source[row['start']:row['end']] == row['value']
        assert source.count(row['value']) == 1, 'ambiguous_occurrence'
        assert row['id'] == opaque_id(row), 'id_contains_or_depends_on_label'
        assert row['expected_verdict'] in {'mask', 'keep', 'review'}
        assert isinstance(row['rationale'], str) and row['rationale'].strip()
        counts[row['file']] += 1
    assert len(sources) == 4 and set(counts.values()) == {8}
    return {'dataset_id': manifest['dataset_id'], 'documents': len(sources),
            'cases': len(cases), 'verdict_counts': dict(Counter(row['expected_verdict'] for row in cases)),
            'status': 'verified', 'model_calls': 0}


if __name__ == '__main__':
    print(json.dumps(verify(), ensure_ascii=False))
