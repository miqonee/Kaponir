"""Verify the two LFM grounding policies on archived responses and API masks."""

import hashlib
import json
import math
from pathlib import Path
import sys
import tarfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / 'scripts'))
from apply_measured_privacy import coverage, span_coverage
from summarize_privacy_policy import require, strict_json


def read_archive(path, expected_hash):
    require(hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash, 'archive_hash_mismatch')
    with tarfile.open(path, 'r:gz') as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}


def read(files, name):
    return strict_json(files[name].decode('utf-8'))


def valid_candidate(row, kinds):
    if not isinstance(row, dict) or set(row) != {'kind', 'value', 'confidence'}:
        return False
    confidence = row['confidence']
    return (isinstance(row['value'], str) and row['kind'] in kinds
            and type(confidence) in (float, int) and math.isfinite(confidence) and 0 <= confidence <= 1)


def main():
    summary = strict_json((HERE / 'comparison.json').read_text())
    prior = read_archive(HERE.parent / 'lfm-cpu-latency/evidence.tar.gz', summary['source_evidence_sha256'])
    files = read_archive(HERE / 'evidence.tar.gz', summary['archive_sha256'])
    receipt = read(files, 'comparison.json')
    require(len(files) == summary['members'] and all(summary[k] == v for k, v in receipt.items()),
            'receipt_mismatch')
    require(hashlib.sha256(files['plan.json']).hexdigest() == receipt['plan_sha256'], 'plan_checksum_mismatch')
    labels = read(prior, 'measurement/annotations.json')
    rows_checked = 0
    shared = None
    for model, measured in receipt['results'].items():
        name = measured['source_profile']
        prefix = f'runs/{name}/'
        own, context, requests = {}, {}, {}
        for path in sorted(p for p in prior if p.startswith(prefix + 'journal-') and p.endswith('.json')):
            for entry in read(prior, path)['requests']:
                request = entry['request']
                stage = entry['record']['measurement_stage']
                doc = stage.split(':')[1]
                chunk = json.loads(request['messages'][-1]['content'])
                requests[stage] = (request['messages'], request['response_format'], request['max_tokens'], request['seed'])
                schema = request['response_format']['json_schema']['schema']['properties']['candidates']['items']
                kinds = schema['properties']['kind']['enum']
                candidates = strict_json(entry['raw_reply']['choices'][0]['message']['content'])['candidates']
                for candidate in candidates:
                    value = candidate.get('value')
                    if not isinstance(value, str) or not 3 <= len(value.encode()) <= 512 or value.strip() != value:
                        continue
                    if value in chunk['source']:
                        own.setdefault(doc, set()).add(value)
                    elif (valid_candidate(candidate, kinds) and value in chunk['context']
                          and value in labels['sources'][doc]):
                        context.setdefault(doc, set()).add(value)
        if shared is None:
            shared = requests
        require(len(requests) == 3 and requests == shared, 'paired_input_mismatch')
        for doc, extra in measured['added_literals'].items():
            require(context.get(doc, set()) - own.get(doc, set()) == set(extra), 'extra_candidates_mismatch')
        for policy, by_document in measured['applications'].items():
            folder = f'{name}/{policy}/apply/'
            result = read(files, folder + 'result.json')
            require(result['status'] == 'completed' and result['exact_roundtrips'] == 4,
                    'incomplete_application')
            for row in result['documents']:
                doc, mode = row['file'], row['mode']
                source = labels['sources'][doc]
                identifier = ('01' if doc == 'nightly-export.log' else '02') + '-' + Path(doc).stem
                mask = read(files, f'{folder}{mode}/{identifier}/mask.json')
                restore = read(files, f'{folder}{mode}/{identifier}/restore.json')
                require(row['source_sha256'] == hashlib.sha256(source.encode()).hexdigest()
                        and restore['text'] == source and row['restore_exact'], 'roundtrip_mismatch')
                spans = span_coverage(source, labels['private'][doc], mask['replacements'], mask['text'])
                checked = coverage(source, mask['text'], labels['private'][doc], labels['public'][doc])
                metrics = {**spans, **checked, 'replacements': len(mask['replacements'])}
                require(all(row[k] == v for k, v in metrics.items()), 'applied_metrics_mismatch')
                if mode == 'model_masks_added':
                    require(all(metrics[k] == v for k, v in by_document[doc].items()), 'published_metrics_mismatch')
                rows_checked += 1
    require(rows_checked == receipt['exact_restores'] == 24 and receipt['new_inference_calls'] == 0,
            'wrong_restore_count')
    print('Verified three single-model comparisons and 24 exact restores.')


if __name__ == '__main__':
    main()
