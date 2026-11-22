"""Replay the recorded search metrics and check the linked CSV without running an LLM."""
import base64
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
import tarfile

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / 'scripts'))
from apply_measured_privacy import coverage, span_coverage
from check_learning_corpus import load_corpus
from experiment_inference import extraction_schema
from summarize_privacy_policy import digest, require, strict_json


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def verify_run(trial, blobs, corpora, ranking):
    prefix = 'runs/' + trial['trial_directory'] + '/'

    def load(name):
        return strict_json(blobs[prefix + name].decode())

    plan = load('plan.json')
    result = load('result.json')
    journal = load('transports.json')
    current = load('apply/result.json')
    sources, private, public = corpora.get(trial['phase'], corpora['full'])
    documents = result['by_document']
    require(all(plan['source_sha256'][n] == checksum(sources[n].encode()) for n in documents),
            'source_hash_mismatch')
    if result['completed']:
        require(set(documents) == set(plan['source_sha256']), 'missing_planned_document')

    calls = journal['requests']
    expected_schema = extraction_schema(plan['kinds'], 'typed', 24)
    seconds = 0.0
    for index, row in enumerate(calls):
        request = row['request']
        record = row['record']
        name = record['measurement_stage'].removeprefix('discovery:')
        require(name in documents and index == record['physical_request_index']
                and not row.get('reused') and request['cache_prompt'] is False
                and digest(request) == row['request_sha256'], 'invalid_physical_receipt')
        seconds += record['http_wall_seconds']
        if row['status'] != 'received' or record.get('error'):
            require(not result['completed'], 'unfinished_request_in_complete_run')
            continue
        transport = row['raw_transport']
        reply = strict_json(base64.b64decode(transport['body_base64'], validate=True).decode())
        require(transport['status'] == 200 and reply == row['raw_reply'], 'transport_mismatch')
        if 'json_schema' in request:
            messages = row['template_request']['messages']
            schema = request['json_schema']
            content = reply['content']
        else:
            messages = request['messages']
            schema = request['response_format']['json_schema']['schema']
            content = reply['choices'][0]['message']['content']
        require(checksum(messages[0]['content'].encode()) == plan['prompt_sha256']
                and strict_json(messages[1]['content'])['source'] in sources[name]
                and schema == expected_schema, 'prompt_or_source_mismatch')
        Draft202012Validator(schema).validate(strict_json(content))
    require(len(calls) == result['requests']
            and math.isclose(seconds, result['http_seconds'], rel_tol=0, abs_tol=1e-7),
            'fresh_time_mismatch')

    hidden = lost = reference = 0
    for row in current['documents']:
        name, mode = row['file'], row['mode']
        paths = [p for p in blobs if p.startswith(prefix + 'apply/' + mode + '/')
                 and p.endswith('-' + Path(name).stem + '/mask.json')]
        require(len(paths) == 1, 'mask_receipt_missing')
        path = paths[0]
        mask = strict_json(blobs[path].decode())
        restore = strict_json(blobs[path.removesuffix('mask.json') + 'restore.json'].decode())
        metrics = coverage(sources[name], mask['text'], private[name], public[name])
        metrics.update(span_coverage(sources[name], private[name], mask['replacements'], mask['text']))
        require(restore['text'] == sources[name] and row['persistent_rules_unchanged']
                and all(row[k] == v for k, v in metrics.items()), 'applied_coverage_mismatch')
        if mode == 'model_masks_added':
            hidden += metrics['private_spans_fully_covered']
            lost += metrics['public_occurrences_lost']
            if not private[name]:
                reference += len(mask['replacements'])
    require((hidden, lost) == (result['H'], result['P'])
            and (result['R'] is None or reference == result['R'])
            and current['exact_roundtrips'] == result['exact_restores'] == 2 * len(documents),
            'trial_totals_mismatch')

    if trial['phase'] == 'full' and result['completed']:
        score = (60*hidden/193 + 25*(87-lost)/87 + 5/(1+reference) + 15*693/(693+seconds)) / 1.05
        entry = ranking[(result['model'], result['option'])]
        application = (REPO / trial['application_file']).read_bytes()
        require(checksum(application) == entry['application_sha256'] == trial['application_sha256']
                and gzip.decompress(application) == blobs[prefix + 'apply/result.json']
                and math.isclose(result['score'], score)
                and entry['score_60_30_15_100'] == f'{score:.2f}'
                and (entry['private_full_193'], entry['public_lost_87'], entry['guide_replacements'])
                == (str(hidden), str(lost), str(reference))
                and math.isclose(float(entry['http_minutes']), seconds/60, rel_tol=0, abs_tol=1e-8),
                'ranking_mismatch')


def main():
    manifest = json.loads((HERE / 'manifest.json').read_text())
    require(checksum(Path(__file__).read_bytes()) == manifest['offline_verifier_sha256'],
            'verifier_hash_mismatch')
    archive = HERE / 'evidence.tar.gz'
    require(checksum(archive.read_bytes()) == manifest['archive_sha256'], 'archive_hash_mismatch')
    with tarfile.open(archive) as bundle:
        members = [m for m in bundle.getmembers() if m.isfile()]
        blobs = {m.name: bundle.extractfile(m).read() for m in members}
    require(len(blobs) == len(members) and {k: checksum(v) for k, v in blobs.items()}
            == manifest['members_sha256'], 'archive_member_mismatch')
    _, expected, public, sources, _ = load_corpus()
    private = {n: [v for d, v in expected if d == n] for n in sources}
    corpora = {'full': (sources, private, public)}
    for phase, keys in [('pilot', ('sources', 'private_values', 'public_values')),
                        ('holdout', ('sources', 'private', 'public'))]:
        fixture = strict_json(blobs[phase + '/fixture.json'].decode())
        corpora[phase] = tuple(fixture[k] for k in keys)
    table = REPO / 'docs/experiments/model-search-summary-60-30-15.csv'
    with table.open(newline='') as stream:
        ranking = {(r['model'], r['profile']): r for r in csv.DictReader(stream) if r['series'] == 'fresh_tuning'}
    for trial in manifest['fresh_trials']:
        verify_run(trial, blobs, corpora, ranking)
    count = sum(t['phase'] == 'full' and t['completed'] for t in manifest['fresh_trials'])
    require(count == len(ranking), 'fresh_row_count_mismatch')
    print(f"Verified {count} full profiles, {manifest['new_requests']} fresh requests and {len(blobs)} evidence files.")


if __name__ == '__main__':
    main()
