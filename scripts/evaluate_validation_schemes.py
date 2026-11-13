#!/usr/bin/env python3
"""Compare detector-guided and double validation on the fixed synthetic corpus.

Runs locally in shadow mode. Every answered batch is checkpointed atomically.
No rules, user vaults, training labels, or external providers are touched.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

from check_learning_corpus import load_corpus
from compare_local_models import ROOT, DOCUMENTS
from experiment_inference import LocalClient, discover, prompts_and_kinds, sampling_settings
from kaponir_inference import plan_text, split_span
from kaponir_worker import Worker, configuration
from validation_pipeline import (Candidate, PRIMARY_PROMPT, AUDIT_PROMPT, REGION_PROMPT,
    byte_to_char, candidate_context, check_decisions, combine, opaque_id,
    record_span, review_schema, score, uncertain, validation_hint)


def atomic_json(path, value):
    partial = path.with_suffix('.tmp')
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    partial.replace(path)


def resources():
    result = {}
    for name in ['cpu.max', 'memory.max', 'memory.events']:
        path = Path('/sys/fs/cgroup')/name
        try:
            result[name] = path.read_text().strip()
        except OSError:
            pass
    return result


def detector_pool(binary, sources):
    payload = [{'name': name, 'text': text} for name, text in sources.items()]
    reply = subprocess.run([str(binary.resolve())], input=json.dumps(payload),
                           text=True, capture_output=True, check=True)
    data = json.loads(reply.stdout)
    if data.get('s1') is not False or data.get('rules') is not False:
        raise ValueError('detector_export_must_be_local_and_rule_free')
    candidates = []
    for doc in data['documents']:
        text = sources[doc['file']]
        if doc['suggestions_truncated']:
            raise ValueError('detector_proposals_truncated')
        # Evaluate unique values per document. Select an actual source occurrence,
        # preferring protected/applied evidence. Repeated contexts are not averaged.
        unique = {}
        for row in doc['candidates']:
            a, b = byte_to_char(text, row['start']), byte_to_char(text, row['end'])
            if text[a:b] != row['value']:
                raise ValueError('detector_offset_mismatch')
            c = Candidate(opaque_id(doc['file'], row['value']), doc['file'], row['value'],
                          a, b, row['kind'], protected=row['protected'],
                          applied=row['applied'], origin=row['origin'])
            old = unique.get(c.value)
            if old is None or (c.protected, c.applied) > (old.protected, old.applied):
                unique[c.value] = c
        candidates.extend(unique.values())
    return candidates, data


class Experiment:
    def __init__(self, client, args, sources, kinds, state, save):
        self.client, self.args, self.sources, self.kinds = client, args, sources, kinds
        self.state, self.save = state, save

    def fits(self, prompt, value, schema=None):
        return (self.client.input_tokens(prompt, value, schema)+self.args.output_tokens
                + self.args.reserve_tokens <= self.args.context_tokens)

    def infer(self, stage, prompt, value, schema):
        cache_input = [stage, prompt, value, schema]
        if self.client.output_mode != 'grammar':cache_input.append(self.client.output_mode)
        if self.client.sampling != sampling_settings():cache_input.append(self.client.sampling)
        key = hashlib.sha256(json.dumps(cache_input,
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cached = self.state['responses'].get(key)
        if cached is not None:
            return cached
        started = time.monotonic()
        try:
            if not self.fits(prompt, value, schema):
                raise ValueError('model_context_budget_exceeded')
            result = {'reply': self.client.infer(prompt, value, schema, self.args.output_tokens)}
        except Exception as exc:
            # Never expose provider bodies or accidentally treat errors as KEEP.
            code = str(exc)
            result = {'error': code if re.fullmatch(r'[a-z_]{1,80}', code) else type(exc).__name__}
        result['seconds'] = round(time.monotonic()-started, 3)
        self.state['responses'][key] = result
        self.state['calls'] = self.client.records
        self.save()
        return result

    def validate(self, stage, candidates, prompt):
        decisions = {}
        contexts = {}
        for c in candidates:
            if c.protected:
                decisions[c.id] = combine(c, {}, {})
            else:
                contexts[c.id] = candidate_context(c, self.sources[c.file],
                                                self.client.token_count, self.args.hint_tokens, self.args.context_policy)
        pending = [c for c in candidates if not c.protected]

        def consume(batch):
            value = {'items': [contexts[c.id] for c in batch]}
            schema = review_schema([c.id for c in batch], self.kinds)
            if not self.fits(prompt, value, schema) and len(batch) > 1:
                middle = len(batch)//2
                consume(batch[:middle]); consume(batch[middle:]); return
            reply = self.infer(stage, prompt, value, schema)
            if reply.get('error') == 'model_response_truncated' and len(batch) > 1:
                middle = len(batch)//2
                consume(batch[:middle]); consume(batch[middle:]); return
            if 'error' in reply:
                decisions.update({c.id: uncertain(c, reply['error']) for c in batch})
            else:
                decisions.update(check_decisions(reply['reply'], batch, contexts, self.kinds))

        # Opaque stable order, identical policy and batch size for every model.
        for start in range(0, len(pending), self.args.batch_size):
            consume(pending[start:start+self.args.batch_size])
        return decisions

    def extract_regions(self, candidates):
        properties = {'value': {'type': 'string'}, 'kind': {'type': 'string', 'enum': self.kinds},
                      'scope': {'type': 'string', 'enum': ['personal', 'team', 'global']}}
        schema = {'type': 'object', 'properties': {'candidates': {'type': 'array',
                  'maxItems': self.args.max_candidates, 'items': {'type': 'object',
                  'properties': properties, 'required': list(properties), 'additionalProperties': False}}},
                  'required': ['candidates'], 'additionalProperties': False}
        found, regions = {}, []
        for name, text in self.sources.items():
            spans = sorted({record_span(text, c.start, c.end) for c in candidates if c.file == name})
            merged = []
            for left, right in spans:
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                else:
                    merged.append((left, right))
            for left, right in merged:
                region = text[left:right]
                hint = validation_hint(text, left, self.args.hint_tokens, self.client.token_count, self.args.context_policy)
                def fits(source, context):
                    return self.fits(REGION_PROMPT, {'source': source, 'context': context}, schema)
                try:
                    pieces = plan_text(region, fits, lambda _: hint, max_fragments=self.args.max_fragments)
                except ValueError as exc:
                    regions.append({'file': name, 'start': left, 'end': right, 'error': str(exc)})
                    for c in candidates:
                        if c.file == name and left <= c.start < right:
                            found.setdefault((name, c.value), Candidate(**{**c.json(), 'origin': 'region_pending'}))
                    continue
                def consume(a, b):
                    source = region[a:b]
                    response = self.infer('regions', REGION_PROMPT, {'source': source, 'context': hint}, schema)
                    reply = response.get('reply')
                    rows = reply.get('candidates') if isinstance(reply, dict) else None
                    error = response.get('error')
                    if not error and not isinstance(rows, list): error = 'invalid_region_response'
                    regions.append({'file': name, 'start': left+a, 'end': left+b, 'error': error})
                    grounded = set()
                    for row in rows if isinstance(rows, list) else []:
                        v = row.get('value') if isinstance(row, dict) else None
                        if (not isinstance(v, str) or not 3 <= len(v.encode()) <= 512
                                or v.strip() != v or v not in source
                                or row.get('kind') not in self.kinds
                                or row.get('scope') not in ['personal', 'team', 'global']):
                            self.state['region_invalid_proposals'] = self.state.get('region_invalid_proposals', 0)+1
                            continue
                        grounded.add(v)
                        start = left+a+source.index(v)
                        c = Candidate(opaque_id(name, v), name, v, start, start+len(v),
                                      row['kind'], row['scope'], origin='region')
                        found[(name, v)] = c
                    retry = error == 'model_response_truncated' or len(grounded) >= self.args.max_candidates
                    if retry and b-a > 1 and len(regions) < self.args.max_fragments:
                        first, second = split_span(region, a, b)
                        consume(*first); consume(*second)
                    elif error or retry:
                        if retry and not error:
                            regions[-1]['error'] = 'region_candidate_limit'
                        # A failed region is pending, not a negative sensitivity decision.
                        for c in candidates:
                            if c.file == name and left+a <= c.start < left+b:
                                found.setdefault((name, c.value), Candidate(**{**c.json(), 'origin': 'region_pending'}))
                for piece in pieces:
                    consume(piece['start'], piece['end'])
        # Protected credentials cannot disappear because region extraction omitted them.
        for c in candidates:
            if c.protected: found[(c.file, c.value)] = c
        return list(found.values()), regions

    def probes(self, path):
        cases = json.loads(path.read_text())['cases']
        sources, values = {}, []
        for index, case in enumerate(cases):
            name = opaque_id('heldout', index)
            sources[name] = case['source']
            start = case['source'].index(case['value'])
            values.append(Candidate(opaque_id(name, case['value']), name, case['value'],
                                    start, start+len(case['value'])))
        previous_sources = self.sources
        self.sources = sources
        try:
            first = self.validate('heldout_point', values, PRIMARY_PROMPT)
            second = self.validate('heldout_audit', values, AUDIT_PROMPT) if getattr(self.args,'probe_passes',2)==2 else None
        finally:
            self.sources = previous_sources
        rows = [{'case': case, 'id': c.id, 'first': first[c.id]} for c, case in zip(values, cases)]
        if second is not None:
            for c,row in zip(values,rows):
                row.update(second=second[c.id],combined=combine(c,first[c.id],second[c.id]))
        summary = {}
        stages=[('point','first')]+([('audit','second'),('double','combined')] if second is not None else [])
        for stage, key in stages:
            summary[stage] = {'cases': len(rows),
                'valid': sum(r[key].get('valid', False) for r in rows),
                'action_correct': sum(r[key].get('valid', False) and r[key]['verdict'] == r['case']['expected_verdict'] for r in rows),
                'private_cases': sum('expected_kind' in r['case'] for r in rows),
                'private_kept': sum(r[key].get('valid', False) and 'expected_kind' in r['case'] and r[key]['verdict'] == 'keep' for r in rows),
                'public_masked': sum(r[key].get('valid', False) and r['case']['expected_verdict'] == 'keep' and r[key]['verdict'] == 'mask' for r in rows),
                'needs_review': sum(r[key]['verdict'] == 'review' for r in rows),
                'kind_correct': sum(r[key].get('valid', False) and r[key]['verdict'] == 'mask' and 'expected_kind' in r['case'] and r[key]['kind'] == r['case']['expected_kind'] for r in rows),
                'scope_correct': sum(r[key].get('valid', False) and r[key]['verdict'] == 'mask' and 'expected_scope' in r['case'] and r[key]['scope'] == r['case']['expected_scope'] for r in rows),
                'kind_and_scope_correct': sum(r[key].get('valid', False) and r[key]['verdict'] == 'mask' and 'expected_kind' in r['case'] and r[key]['kind'] == r['case']['expected_kind'] and r[key]['scope'] == r['case']['expected_scope'] for r in rows)}
        return {'rows': rows, 'summary': summary}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', required=True)
    p.add_argument('--hf-url', help='Pinned public GGUF URL; registers a custom artifact before evaluation')
    p.add_argument('--model-dir', type=Path, required=True)
    p.add_argument('--runtime', type=Path, required=True)
    p.add_argument('--detectors', type=Path, default=ROOT/'target/debug/examples/detector_candidates')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--documents', nargs='+', default=DOCUMENTS)
    p.add_argument('--schemes', nargs='+', choices=['free', 'point', 'regions', 'double'],
                   default=['free', 'point', 'regions', 'double'])
    p.add_argument('--batch-size', type=int, default=8)
    p.add_argument('--context-tokens', type=int, default=4096)
    p.add_argument('--output-tokens', type=int, default=1600)
    p.add_argument('--reserve-tokens', type=int, default=64)
    p.add_argument('--hint-tokens', type=int, default=256)
    p.add_argument('--context-policy', choices=['prefix', 'balanced'], default='prefix')
    p.add_argument('--response-format', choices=['grammar','prompt','auto'], default='grammar')
    p.add_argument('--max-candidates', type=int, default=24)
    p.add_argument('--max-fragments', type=int, default=256)
    p.add_argument('--timeout', type=int, default=300)
    p.add_argument('--probes-only', action='store_true')
    p.add_argument('--probe-passes',type=int,choices=[1,2],default=2)
    p.add_argument('--temperature',type=float,default=0.0)
    p.add_argument('--top-p',type=float,default=0.95)
    p.add_argument('--top-k',type=int,default=40)
    p.add_argument('--min-p',type=float,default=0.0)
    p.add_argument('--seed',type=int)
    p.add_argument('--probe-file', type=Path)
    p.add_argument('--review-input', type=Path, help='Completed primary experiment: independently audit its frozen union with this model')
    args = p.parse_args()
    try:
        generation=sampling_settings({'temperature':args.temperature,'top_p':args.top_p,'top_k':args.top_k,'min_p':args.min_p,'seed':args.seed})
    except ValueError as exc:p.error(str(exc))
    if args.probes_only:
        args.schemes = []
        if args.probe_file is None: args.probe_file = ROOT/'tests/fixtures/validation-review-heldout.json'
    if args.review_input: args.schemes = []
    if args.context_tokens <= args.output_tokens+args.reserve_tokens or not 1 <= args.batch_size <= 64:
        p.error('invalid inference budget/batch size')
    _, expected, public, sources, _ = load_corpus()
    if any(name not in sources for name in args.documents): p.error('unknown synthetic document')
    sources = {name: sources[name] for name in args.documents}
    expected = {key: value for key, value in expected.items() if key[0] in sources}
    public = {name: public[name] for name in sources}
    pool, detector_data = detector_pool(args.detectors, sources)
    args.output.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output/'detectors.json', detector_data)
    runtime_hash = hashlib.file_digest(args.runtime.open('rb'), 'sha256').hexdigest()
    state_path = args.output/'result.json'
    with tempfile.TemporaryDirectory(prefix='kaponir-validation-') as temporary:
        home = Path(temporary)
        worker = Worker(home, configuration(home, {'model_dir': str(args.model_dir.resolve()),
                        'runtime': str(args.runtime.resolve()), 'threads': 2,
                        'ocr_timeout_seconds': args.timeout}), 'unused')
        try:
            model = args.model
            if args.hf_url:
                record = worker.register_model(args.hf_url)
                model = record['model']
                job = worker.submit('download', model=model)
                worker.install(job['id'], model)
            artifact = worker.model_files()[worker.canonical_model(model)]
            signature = {'model': args.model, 'artifact': list(artifact), 'runtime_sha256': runtime_hash,
                         'source_sha256': {n: hashlib.sha256(t.encode()).hexdigest() for n, t in sources.items()},
                         'settings': {k: v for k, v in vars(args).items() if k not in ['output', 'model_dir', 'runtime', 'detectors', 'probe_file', 'probes_only', 'review_input']},
                         'prompts': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in
                                     {'primary': PRIMARY_PROMPT, 'audit': AUDIT_PROMPT, 'region': REGION_PROMPT}.items()}}
            for key,default in {**sampling_settings(),'seed':None,'probe_passes':2}.items():
                if signature['settings'].get(key)==default:signature['settings'].pop(key,None)
            if args.response_format == 'grammar':
                signature['settings'].pop('response_format')
            if args.context_policy == 'prefix':
                signature['settings'].pop('context_policy')
            if args.probe_file:
                signature['probe_sha256'] = hashlib.sha256(args.probe_file.read_bytes()).hexdigest()
            primary = None
            if args.review_input:
                primary = json.loads(args.review_input.read_text())
                if primary['status'] != 'completed' or primary['signature']['source_sha256'] != signature['source_sha256']:
                    raise ValueError('primary_run_or_sources_incomplete')
                signature['primary_sha256'] = hashlib.sha256(args.review_input.read_bytes()).hexdigest()
                signature['primary_model'] = primary['signature']['model']
            state = {'signature': signature, 'status': 'running', 'responses': {}, 'stages': {}, 'calls': [],
                     'shadow': True, 'rules_changed': 0, 'candidate_pool': [c.json() for c in pool]}
            if state_path.exists():
                old = json.loads(state_path.read_text())
                if old['signature'] != signature: raise ValueError('resume_profile_changed')
                state = old
                if state['status'] != 'completed':
                    state['status'] = 'running'
                    state.pop('error', None)
            frozen_extract, _, frozen_kinds = prompts_and_kinds()
            provenance = {
                'extraction_prompt_sha256': hashlib.sha256(frozen_extract.encode()).hexdigest(),
                'kind_schema_sha256': hashlib.sha256(json.dumps(frozen_kinds).encode()).hexdigest(),
                'detector_binary_sha256': hashlib.file_digest(args.detectors.open('rb'), 'sha256').hexdigest(),
                'detector_export_sha256': hashlib.sha256(json.dumps(detector_data, sort_keys=True).encode()).hexdigest(),
                'annotations_sha256': hashlib.sha256((ROOT/'tests/docs/expected.json').read_bytes()).hexdigest(),
            }
            if state['candidate_pool'] != [c.json() for c in pool]:
                raise ValueError('resume_detector_pool_changed')
            for key, value in provenance.items():
                if key in state and state[key] != value:
                    raise ValueError('resume_provenance_changed')
                state[key] = value
            def save(): atomic_json(state_path, state)
            state.setdefault('resources_before', resources())
            save()
            if state['status'] == 'completed':
                print(json.dumps(state['metrics'])); return
            client = LocalClient(worker, model, args.context_tokens, args.output_tokens, args.response_format, generation)
            state['active_runtime_memory_policy'] = {
                'cache_ram_mib': 0, 'cache_idle_slots': False, 'request_cache_prompt': False}
            state['generation'] = client.sampling
            state['structured_output'] = client.output_mode
            state['runtime_supports_json_schema'] = worker.process_supports_json_schema
            client.records = state['calls']
            extract_prompt, _, kinds = prompts_and_kinds()
            experiment = Experiment(client, args, sources, kinds, state, save)
            def stage(name, values, decisions):
                # The union pass later extends `first`; keep this stage's pool frozen.
                state['stages'][name] = {'candidates': [c.json() for c in values],
                                         'decisions': {c.id: decisions[c.id] for c in values}}
                state.setdefault('metrics', {})[name] = score(values, decisions, expected, public)
                save()
                print(json.dumps({'stage': name, **state['metrics'][name]}), flush=True)
            baseline = {c.id: {'id': c.id, 'verdict': 'mask', 'kind': c.kind,
                              'scope': c.scope, 'valid': True} for c in pool}
            stage('detector_proposals', pool, baseline)
            stage('detector_applied', [c for c in pool if c.applied], baseline)
            if primary:
                values = [Candidate(**c) for c in primary['stages']['current_union']['candidates']]
                protected = {c.id for c in pool if c.protected}
                for c in values:
                    if (c.file not in sources or sources[c.file][c.start:c.end] != c.value
                            or c.id != opaque_id(c.file, c.value) or c.protected != (c.id in protected)):
                        raise ValueError('primary_candidate_not_grounded')
                audited = experiment.validate('audit', values, AUDIT_PROMPT)
                stage('audit', values, audited)
                stage('audit_detectors', pool, audited)
                stage('cross_double', values, {c.id: combine(c, primary['first_pass'][c.id], audited[c.id]) for c in values})
                state['second_pass'] = audited
            union = {(c.file, c.value): c for c in pool}
            if 'free' in args.schemes or 'double' in args.schemes:
                for name, text in sources.items():
                    key = 'discovery:'+name
                    if key not in state['stages']:
                        settings = argparse.Namespace(**{**vars(args), 'candidate_mode': 'typed', 'strategy': 'auto', 'target_input_tokens': 0})
                        state['stages'][key] = discover(client, text, extract_prompt, kinds, settings, save)
                        save()
                    for row in state['stages'][key]['values']:
                        value = row['value']; a = text.index(value)
                        union.setdefault((name, value), Candidate(opaque_id(name, value), name, value,
                                         a, a+len(value), row['kind'], origin='discovery_llm'))
                all_values = list(union.values())
                stage('current_union', all_values, {c.id: {**baseline.get(c.id, {}), 'verdict': 'mask',
                      'kind': c.kind, 'scope': c.scope, 'valid': True} for c in all_values})
            first = {}
            if 'point' in args.schemes or 'double' in args.schemes:
                first = experiment.validate('point', pool, PRIMARY_PROMPT)
                stage('point', pool, first)
            if 'regions' in args.schemes:
                values, regions = experiment.extract_regions(pool)
                state['regions'] = regions
                stage('regions', values, {c.id: ({'verdict': 'mask', 'kind': c.kind, 'scope': c.scope, 'valid': True} if c.origin != 'region_pending' or c.protected else uncertain(c, 'region_unavailable')) for c in values})
            if 'double' in args.schemes:
                values = list(union.values())
                extras = [c for c in values if c.id not in first]
                first.update(experiment.validate('point_extra', extras, PRIMARY_PROMPT))
                stage('union_point', values, first)
                second = experiment.validate('audit', values, AUDIT_PROMPT)
                stage('double', values, {c.id: combine(c, first[c.id], second[c.id]) for c in values})
                state['first_pass'] = first
                state['second_pass'] = second
            if args.probe_file:
                state['probes'] = experiment.probes(args.probe_file)
                save()
                print(json.dumps({'probes': state['probes']['summary']}), flush=True)
            state['status'] = 'completed'
            state['resources_after'] = resources()
            save()
        except Exception as exc:
            if 'state' in locals():
                state['status'] = 'failed'
                code = str(exc)
                state['error'] = code if re.fullmatch(r'[a-z_]{1,80}', code) else type(exc).__name__
                state['resources_after'] = resources()
                state['runtime_exit_code'] = worker.process.poll() if worker.process else None
                atomic_json(state_path, state)
            raise
        finally:
            worker.stop.set(); worker.stop_runtime()


if __name__ == '__main__': main()
