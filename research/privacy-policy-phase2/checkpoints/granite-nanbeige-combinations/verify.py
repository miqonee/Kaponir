"""Recompute the combined search metrics and check the saved inference records."""

import base64
import gzip
import hashlib
import io
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
from compare_local_models import DOCUMENTS
from summarize_privacy_policy import digest, strict_json


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unpack(root):
    receipt = strict_json((root / 'manifest.json').read_text())
    filename = receipt.get('evidence_file', receipt.get('evidence_archive'))
    compressed = (root / filename).read_bytes()
    assert sha(compressed) == receipt['evidence_sha256']
    with tarfile.open(fileobj=io.BytesIO(compressed), mode='r:gz') as tar:
        contents = {member.name: tar.extractfile(member).read() for member in tar if member.isfile()}
    assert {name: sha(raw) for name, raw in contents.items()} == receipt['members']
    return receipt, contents


def records(data, folder):
    def load(name):
        return strict_json(data[f'{folder}/{name}'].decode())
    return load


def verify_donor(prior, model, doc, result, text):
    source = result['source']
    load = records(prior, f"{model}/full/{source['profile']}")
    previous = load('discovery.json')[doc]
    assert {k:v for k,v in result.items() if k != 'source'} == previous
    journal = load('transports.json')
    entry, = [e for e in journal['requests'] if e['record']['measurement_stage'] == f'discovery:{doc}']
    assert entry['request_sha256'] == digest(entry['request']) == source['request_sha256']
    template = entry.get('template_request', entry['request'])
    assert strict_json(template['messages'][1]['content'])['source'] == text
    assert entry['record']['http_wall_seconds'] == result['http_seconds']


def verify_legacy(result, doc):
    source = result['source']
    raw = (REPO / source['path']).read_bytes()
    assert sha(raw) == source['sha256']
    reference = strict_json(gzip.decompress(raw).decode())
    calls = reference['calls'][source['call_start']:source['call_end']]
    assert reference['stages'][f'discovery:{doc}'] == {
        k:v for k,v in result.items() if k not in ('source', 'http_seconds')
    }
    assert math.isclose(sum(c['seconds'] for c in calls), result['http_seconds'], abs_tol=1e-8)


def validate_reply(call):
    request = call['request']
    raw = base64.b64decode(call['raw_transport']['body_base64'], validate=True)
    reply = strict_json(raw.decode())
    if call.get('endpoint') == '/completion':
        assert sha(request['prompt'].encode()) == call['rendered_prompt_sha256']
        content = reply['content']
        schema = request['json_schema']
    else:
        content = reply['choices'][0]['message']['content']
        schema = request['response_format']['json_schema']['schema']
    answer = strict_json(content)
    assert Draft202012Validator(schema).is_valid(answer)
    return answer['candidates']


def collect(found, candidates, source):
    for row in candidates:
        quoted = row.get('value', '')
        if isinstance(quoted, str) and 3 <= len(quoted.encode()) <= 512 and quoted == quoted.strip() and quoted in source:
            found.setdefault(quoted, {'value': quoted, 'kind': row.get('kind', 'CUSTOM'),
                                      'confidence': row.get('confidence')})


def verify_fresh(load, doc, result, text, profile):
    journal = load(result['source']['journal'])
    assert journal['profile'] == profile
    calls = [c for c in journal['requests'] if c['record']['measurement_stage'] == f'discovery:{doc}']
    assert len(calls) == result['requests']
    seconds = sum(x['record']['http_wall_seconds'] for x in calls)
    assert math.isclose(seconds, result['http_seconds'], abs_tol=1e-8)
    found = {}
    for call in calls:
        request = call['request']
        assert digest(request) == call['request_sha256']
        source = strict_json(request['messages'][1]['content'])['source']
        assert source in text and request['temperature'] == profile['temperature']
        if 'error' not in call['record']:
            collect(found, validate_reply(call), source)
    assert list(found.values()) == result['values']


def verify_log(load, result, text, profile):
    layout = load('layout.json')
    assert len(layout) == result['fragments'] == 4
    ends = [part['end'] for part in layout]
    assert [part['start'] for part in layout] == [0] + ends[:-1]
    assert ends[-1] == len(text) and sum(p['legacy_call_index'] is not None for p in layout) >= 3
    found = {}
    times = []
    succeeded = set()
    for call in load('transports.json')['requests']:
        request = call['request']
        assert digest(request) == call['request_sha256']
        assert request['seed'] == profile['seed'] and request['temperature'] == profile['temperature']
        record = call['record']
        times.append(record['http_wall_seconds'])
        if 'fragment_index' not in record:
            continue
        index = record['fragment_index']
        assert index not in succeeded
        succeeded.add(index)
        part = layout[index-1]
        message = call.get('template_request', request)['messages'][1]['content']
        value = strict_json(message)
        assert value == {'context': part['context'], 'source': text[part['start']:part['end']]}
        collect(found, validate_reply(call), value['source'])
    assert list(found.values()) == result['values']
    assert len(succeeded) + len(result['errors']) == len(layout)
    assert len(times) == result['requests']
    assert math.isclose(sum(times), result['http_seconds'], abs_tol=1e-8)


def verify_first(files, model, doc, result, text, profile):
    source = result['source']
    load = records(files, f"{model}/{source['first_profile']}")
    layout = load('layout.json')
    journal = load('transports.json')
    assert journal['profile'] == profile['first_log_fragment']
    first, = [e for e in journal['requests'] if e['record'].get('fragment_index') == 1]
    body = (REPO / source['legacy']['path']).read_bytes()
    assert sha(body) == source['legacy']['sha256']
    archive = strict_json(gzip.decompress(body).decode())
    assert archive['generation'] == profile['remaining_log_fragments']
    found = {}
    collect(found, validate_reply(first), text[:layout[0]['end']])
    times = [first['record']['http_wall_seconds']]
    for part in layout[1:]:
        old = archive['discovery_progress'][doc]['responses'][part['legacy_key']]
        assert old['record_index'] == part['legacy_call_index']
        collect(found, old['reply']['candidates'], text[part['start']:part['end']])
        times.append(archive['calls'][old['record_index']]['seconds'])
    assert not result['errors'] and list(found.values()) == result['values']
    assert math.isclose(sum(times), result['http_seconds'], abs_tol=1e-8)


def main():
    manifest, files = unpack(HERE)
    _, prior = unpack(HERE.with_name('granite-nanbeige-tuning'))
    _, gold, controls, sources, _ = load_corpus()
    restores = 0
    for trial in manifest['trials']:
        model = trial['model']
        folder = f"{model}/{trial['label']}"
        load = records(files, folder)
        discovery = load('discovery.json')
        application = load('apply/result.json')
        if 'application' in trial:
            raw = (REPO / trial['application']).read_bytes()
            assert sha(raw) == trial['application_sha256']
            assert gzip.decompress(raw) == files[f'{folder}/apply/result.json']
        hidden = lost = guide = 0
        for index, doc in enumerate(DOCUMENTS, 1):
            text = sources[doc]
            private = [value for name, value in gold if name == doc]
            public = controls[doc]
            stage = discovery[doc]
            kind = stage['source']['kind']
            if kind == 'measured':
                verify_log(load, stage, text, trial['profile']['log'])
            elif kind == 'first_schema':
                verify_first(files, model, doc, stage, text, trial['profile'])
            elif kind == 'fresh':
                verify_fresh(load, doc, stage, text, trial['profile']['non_log_profile'])
            elif kind == 'recorded':
                verify_donor(prior, model, doc, stage, text)
            else:
                assert kind == 'legacy'
                verify_legacy(stage, doc)
            for mode in ('baseline', 'model_masks_added'):
                origin = f'apply/{mode}/{index:02d}-{Path(doc).stem}'
                mask = load(f'{origin}/mask.json')
                restore = load(f'{origin}/restore.json')
                app, = [x for x in application['documents'] if x['file'] == doc and x['mode'] == mode]
                assert app['source_sha256'] == sha(text.encode()) and restore['text'] == text
                assert app['restore_exact'] and app['replacements'] == len(mask['replacements'])
                covered = span_coverage(text, private, mask['replacements'], mask['text'])
                check = coverage(text, mask['text'], private, public)
                assert app['private_spans_fully_covered'] == covered['private_spans_fully_covered']
                assert app['public_occurrences_lost'] == check['public_occurrences_lost']
                if mode == 'model_masks_added':
                    hidden += covered['private_spans_fully_covered']
                    lost += check['public_occurrences_lost']
                    if not private:
                        guide += len(mask['replacements'])
                restores += 1
        s = trial['search']
        assert (hidden,lost,guide) == (s['private_full'],s['public_lost'],s['reference_replacements'])
        assert (s['private_total'],s['public_total']) == (193,87)
        assert s['completed'] == all(not stage['errors'] for stage in discovery.values())
        http = sum(stage['http_seconds'] for stage in discovery.values())
        assert math.isclose(http,s['http_seconds'],abs_tol=1e-8)
        quality = 50*hidden/193 + 15*(87-lost)/87 + 5/(1+guide)
        assert math.isclose(quality,s['quality_70'],abs_tol=1e-8)
        assert math.isclose(quality+30*693/(693+http),s['score_100'],abs_tol=1e-8)
    print(f"Verified {len(manifest['trials'])} combined presets and {restores} exact restores.")


if __name__ == '__main__':
    main()
