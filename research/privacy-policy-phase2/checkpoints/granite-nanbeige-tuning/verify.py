"""Check the stored measurements and applications without rerunning inference."""

import base64
from copy import deepcopy
import gzip
import hashlib
import io
import math
from pathlib import Path
import sys
import tarfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / 'scripts'))

from apply_measured_privacy import coverage, span_coverage
from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS
from evaluate_context_controls import prepare_cases
from privacy_policy_experiment import metrics
from summarize_privacy_policy import digest, strict_json


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    receipt = strict_json((HERE / 'manifest.json').read_text())
    evidence = (HERE / receipt['evidence_archive']).read_bytes()
    assert sha(evidence) == receipt['evidence_sha256']
    with tarfile.open(fileobj=io.BytesIO(evidence), mode='r:gz') as archive:
        files = {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}
    assert {name: sha(raw) for name, raw in files.items()} == receipt['members']

    def load(name):
        return strict_json(files[name].decode('utf-8'))

    datasets = load('datasets.json')
    _, gold, public, full_source, _ = load_corpus()
    full_source = {name: full_source[name] for name in DOCUMENTS}
    assert datasets['full']['sources'] == full_source
    assert datasets['full']['private_values'] == {
        name: [val for doc, val in gold if doc == name] for name in DOCUMENTS
    }
    assert datasets['full']['public_values'] == {name: public[name] for name in DOCUMENTS}
    fixture = REPO / 'tests/fixtures/context-control-candidates.json'
    cases, metadata = prepare_cases(fixture, full_source, gold, public, 32)
    assert metadata['reference_private'] == metadata['reference_public'] == 16

    restores = 0
    for trial in receipt['trials']:
        folder = f"{trial['model']}/{trial['phase']}/{trial['label']}"
        dataset = datasets[trial['phase']]
        journal = load(folder + '/transports.json')
        plan = load(folder + '/plan.json')
        assert journal['profile'] == plan['profile'] == trial['profile']
        assert trial['model_sha256'] == plan['artifact'][2]
        cap = plan['profile'].get('log_cap')
        reused = plan['profile'].get('reused_nonlog_from')
        if cap or reused:
            label = reused or 'direct-greedy-schema'
            baseline = f"{trial['model']}/full/{label}"
            donor = load(baseline + '/plan.json')
            for k in ('artifact', 'runtime_sha256', 'binary_sha256', 'threads'):
                assert donor[k] == plan[k]
            previous = load(baseline + '/transports.json')['requests']
            saved = {r['record']['measurement_stage']: r for r in previous
                     if r['record']['measurement_stage'].startswith('discovery:')}
            assert len(saved) == 3
        for call in journal['requests']:
            assert digest(call['request']) == call['request_sha256']
            if cap or reused:
                stage = call['record']['measurement_stage']
                source = saved[stage]
                if not stage.endswith('.log'):
                    assert call.get('reused') == label
                    assert call['request'] == source['request']
                    assert call['raw_transport'] == source['raw_transport']
                    assert call['record']['http_wall_seconds'] == source['record']['http_wall_seconds']
                elif cap:
                    assert 'reused' not in call
                    request = deepcopy(source['request'])
                    request['json_schema']['properties']['candidates']['maxItems'] = cap
                    assert call['request'] == request
                else:
                    assert 'reused' not in call
                    assert call['record']['input_tokens'] <= plan['log_target_input_tokens']
            if call.get('endpoint') == '/completion':
                assert sha(call['request']['prompt'].encode()) == call['rendered_prompt_sha256']
                assert call['template_request']['chat_template_kwargs'] == trial['profile']['template_kwargs']
            if 'raw_reply' in call:
                raw = base64.b64decode(call['raw_transport']['body_base64'], validate=True)
                assert call['raw_reply'] == strict_json(raw.decode())
        raw_application = files[folder + '/apply/result.json']
        assert sha(raw_application) == trial['application_sha256']
        if 'application' in trial:
            path = REPO / trial['application']
            compressed = path.read_bytes()
            assert sha(compressed) == receipt['applications'][path.name]
            assert gzip.decompress(compressed) == raw_application
        application = strict_json(raw_application.decode())
        assert application['binary_sha256'] == plan['binary_sha256']
        private_full, public_lost, guide = 0, 0, 0
        source = dataset['sources']
        for i, (name, text) in enumerate(source.items(), 1):
            doc = f'{i:02d}-{Path(name).stem}'
            private, public_controls = (dataset[k][name] for k in ('private_values', 'public_values'))
            for mode in ('baseline', 'model_masks_added'):
                mask = load(f'{folder}/apply/{mode}/{doc}/mask.json')
                restore = load(f'{folder}/apply/{mode}/{doc}/restore.json')
                row, = [r for r in application['documents'] if (r['file'], r['mode']) == (name, mode)]
                assert restore['text'] == text and row['restore_exact'] is True
                covered = span_coverage(text, private, mask['replacements'], mask['text'])
                lost = coverage(text, mask['text'], private, public_controls)['public_occurrences_lost']
                assert row['private_spans_fully_covered'] == covered['private_spans_fully_covered']
                assert row['public_occurrences_lost'] == lost
                if mode == 'model_masks_added':
                    private_full += covered['private_spans_fully_covered']
                    public_lost += lost
                    if not private:
                        guide += len(mask['replacements'])
                restores += 1
        search = trial['search']
        assert (private_full, public_lost, guide) == (
            search['private_full'], search['public_lost'], search['reference_replacements'])
        seconds = sum(e['record']['http_wall_seconds'] for e in journal['requests']
                      if e['record']['measurement_stage'].startswith('discovery:'))
        assert math.isclose(seconds, search['http_seconds'], abs_tol=1e-7)
        quality = 50 * private_full/search['private_total'] + 15 * (search['public_total']-public_lost)/search['public_total'] + 5/(1+guide)
        assert math.isclose(quality, search['quality_70'], abs_tol=1e-8)
        assert math.isclose(quality + 30*693/(693+seconds), search['score_100'], abs_tol=1e-8)
        if 'D' in trial:
            assert sha(fixture.read_bytes()) == trial['D']['case_file_sha256']
            decisions = load(folder + '/review-decisions.json')
            score = metrics(cases, decisions, gold)
            assert score == trial['D']['metrics']
            review = sum(e['record']['http_wall_seconds'] for e in journal['requests']
                         if e['record']['measurement_stage'].startswith('D:'))
            assert math.isclose(review, trial['D']['http_seconds'], abs_tol=1e-7)
            median = 51.77481835 * 60
            recomputed = 50 * score['private_mask']/16 + 20 * score['public_keep']/16 + 30 * median/(median+review)
            assert math.isclose(recomputed, trial['D']['score_100'], abs_tol=1e-8)
    assert restores == receipt['verification']['exact_roundtrips']
    print(f"Verified {len(receipt['trials'])} settings and {restores} exact restores.")


if __name__ == '__main__':
    main()
