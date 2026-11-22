"""Verify the Gemma QAT comparison using the repository's synthetic corpus and archives."""

import base64
import gzip
import hashlib
import io
import math
from pathlib import Path
import sys
import tarfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
from apply_measured_privacy import coverage, span_coverage
from check_learning_corpus import load_corpus, occurrences
from summarize_privacy_policy import digest, require, strict_json


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def archive(path, expected):
    raw = path.read_bytes()
    require(sha(raw) == expected, 'archive_checksum_mismatch')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as stream:
        return {entry.name: stream.extractfile(entry).read() for entry in stream if entry.isfile()}


def member(files, name):
    return strict_json(files[name].decode())


def covered_occurrences(text, private, replacements):
    offsets = [0]
    for ch in text:
        offsets.append(offsets[-1] + len(ch.encode()))
    covered = set()
    for val in private:
        for match in occurrences(text, val):
            start, end = offsets[match.start()], offsets[match.end()]
            total = sum(max(0, min(end, r['end']) - max(start, r['start'])) for r in replacements)
            if total == end - start:
                covered.add((val, start, end))
    return covered


def main():
    receipt = strict_json((HERE / 'manifest.json').read_text())
    current = archive(HERE / 'evidence.tar.gz', receipt['archives']['evidence.tar.gz']['sha256'])
    require(receipt['members'] == {name: sha(data) for name, data in current.items()}, 'member_checksum_mismatch')
    compact = (HERE / 'application.json.gz').read_bytes()
    require(sha(compact) == receipt['archives']['application.json.gz']['sha256']
            and gzip.decompress(compact) == current['apply/result.json'], 'application_checksum_mismatch')
    prior = HERE.parent / 'native-retest-gemma4-e4b-qat'
    reference = archive(prior / 'evidence.tar.gz', strict_json((prior / 'manifest.json').read_text())['archive_sha256'])
    model = receipt['model']
    plan = member(current, 'plan.json')
    journal = member(current, 'transports.json')
    native_log = f'schemes/{model}/transports.json'
    require(sha(reference[native_log]) == plan['recommended_reference']['journal_sha256'], 'reference_journal_mismatch')
    native_calls = member(reference, native_log)['requests']
    _, annotations, public, sources, _ = load_corpus()
    old_calls = strict_json(gzip.decompress((ROOT / 'benchmark/data/validation/gemma-calibration--result.json.gz').read_bytes()).decode())
    paths = {
        'original': (reference, f'baseline-application/{model}/current_union'),
        'recommended': (reference, f'application/schemes/{model}/current_union'),
        'direct-temperature-0': (current, 'apply'),
    }
    per_occurrence = {}
    for label, (files, folder) in paths.items():
        expected = receipt['results'][label]
        applied = member(files, f'{folder}/result.json')
        spans, times = set(), []
        for i, doc in enumerate(plan['documents'], 1):
            text = sources[doc]
            private = [value for name, value in annotations if name == doc]
            stage = 'discovery:' + doc
            base, = [e for e in native_calls if e['record']['measurement_stage'] == stage]
            if label == 'original':
                indices = [x['record_index'] for x in old_calls['discovery_progress'][doc]['responses'].values()]
                require(len(set(indices)) == len(indices) == old_calls['stages'][stage]['calls'], 'original_requests_missing')
                seconds = sum(old_calls['calls'][j]['seconds'] for j in indices)
            elif label == 'recommended':
                seconds = base['record']['http_wall_seconds']
            else:
                entry, = [e for e in journal['requests'] if e['record']['measurement_stage'] == stage]
                req = entry['request']
                require(req == dict(base['request'], temperature=0.0, chat_template_kwargs={'enable_thinking':False})
                        and digest(req) == entry['request_sha256'], 'unpaired_request')
                transport = entry['raw_transport']
                reply = strict_json(base64.b64decode(transport['body_base64'], validate=True).decode())
                choice = reply['choices'][0]
                require(reply == entry['raw_reply'] and transport['status'] == 200 and choice['finish_reason'] == 'stop'
                        and entry['record']['reasoning_text_tokens'] == 0, 'invalid_completion')
                obj = strict_json(choice['message']['content'])
                items = req['response_format']['json_schema']['schema']['properties']['candidates']
                require(set(obj) == {'candidates'} and isinstance(obj['candidates'], list)
                        and len(obj['candidates']) <= items['maxItems'], 'candidate_list_mismatch')
                for row in obj['candidates']:
                    require(set(row) == {'value','confidence','kind'} and isinstance(row['value'], str)
                            and row['kind'] in items['items']['properties']['kind']['enum']
                            and type(row['confidence']) in (int,float) and 0 <= row['confidence'] <= 1,
                            'invalid_candidate_row')
                seconds = entry['record']['http_wall_seconds']
            times.append(seconds)
            require(math.isclose(seconds, expected['documents'][doc]['http_seconds'], abs_tol=1e-7), 'document_seconds_mismatch')
            for mode in ('baseline','model_masks_added'):
                prefix = f'{folder}/{mode}/{i:02d}-{Path(doc).stem}'
                mask = member(files, prefix+'/mask.json')
                restored = member(files, prefix+'/restore.json')
                row, = [r for r in applied['documents'] if r['mode']==mode and r['file']==doc]
                require(restored['text'] == text and row['restore_exact'] and row['source_sha256'] == sha(text.encode()), 'restore_mismatch')
                geometry = span_coverage(text, private, mask['replacements'], mask['text'])
                visibility = coverage(text, mask['text'], private, public[doc])
                require(all(row[k]==v for k,v in {**geometry,**visibility}.items()), 'coverage_mismatch')
                if mode == 'model_masks_added':
                    spans.update((doc,*value) for value in covered_occurrences(text, private, mask['replacements']))
                    headline = expected['documents'][doc]
                    require(headline['private_full']==geometry['private_spans_fully_covered']
                            and headline['public_lost']==visibility['public_occurrences_lost']
                            and headline['replacements']==len(mask['replacements']), 'document_metrics_mismatch')
        require(len(spans) == expected['private_full'] and math.isclose(sum(times), expected['http_seconds'], abs_tol=1e-7),
                'total_mismatch')
        lost = sum(d['public_lost'] for d in expected['documents'].values())
        guide = expected['documents']['08-public-reference.md']['replacements']
        score = 50*len(spans)/193 + 15*(87-lost)/87 + 5/(1+guide) + 30*11.55/(11.55+sum(times)/60)
        require(lost == expected['public_lost'] and guide == expected['guide_replacements']
                and math.isclose(score, expected['score_100'], abs_tol=1e-9), 'score_mismatch')
        per_occurrence[label] = spans
    direct = per_occurrence['direct-temperature-0']
    for label, gained, missed in [('original',17,0),('recommended',59,1)]:
        previous = per_occurrence[label]
        require((len(direct-previous),len(previous-direct)) == (gained,missed), 'occurrence_transition_mismatch')
    print('Verified three Gemma profiles, their raw timings, coverage and exact document restores.')


if __name__ == '__main__':
    main()
