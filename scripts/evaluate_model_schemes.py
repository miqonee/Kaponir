#!/usr/bin/env python3
"""Extend the historical six-method experiment with pinned research models.

The original detector export is an explicit frozen input, never a new detector
execution. Common 4096/1600 and native profiles remain separate. Raw HTTP bytes,
requests and CPU/RSS are durable before parsing. No rules or S1 are modified.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import time

from benchmark_resources import ProcessSampler, hardware_snapshot, throughput_metrics
from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS, ROOT
from evaluate_privacy_policy import PRODUCERS, RUNTIME_SHA256, observe_runtime_starts, request_raw, worker_pids
import evaluate_validation_schemes as legacy
from experiment_inference import sampling_settings
from experiment_inference import prompts_and_kinds
from kaponir_worker import FILES, http_json, schema_prompt
from privacy_expansion_profiles import NEW_CATALOG, expansion_profile, model_ids, research_registration
from run_privacy_policy_suite import atomic_json, file_sha, runtime_processes, utc
from summarize_privacy_policy import digest, require


EXPORT = ROOT / 'benchmark/evidence/artifacts/validation-schemes/gemma-calibration/detectors.json.gz'
EXPORT_SHA = 'f4fd3152bb4ddaefb661a37a1492e8631b9daa9823f324634ebdd3ba73d207a0'
EXPORT_CANONICAL_SHA = '04a3f06ee818443560762b375ccbf5b8642ae38c7892fb80345391ff0017e146'
METHODS = ('detector_proposals', 'current_union', 'point', 'regions', 'union_point', 'double')


def scheme_profile(model, mode, catalog_path=NEW_CATALOG):
    native = expansion_profile(model, 'native', catalog_path)
    common = {'context_tokens': 4096, 'output_tokens': 1600, 'retry_output_tokens': 1600,
        'reserve_tokens': 64, 'batch_size': 8, 'hint_tokens': 256, 'max_candidates': 24,
        'max_fragments': 256, 'temperature': 0.0, 'top_p': .95, 'top_k': 40,
        'min_p': 0.0, 'seed': None, 'response_format': 'grammar',
        'reasoning': 'off', 'reasoning_format': 'auto', 'reasoning_budget': -1,
        'template_kwargs': {'enable_thinking': False}, 'extra_sampling': {}}
    if mode == 'native':
        for name in ('context_tokens', 'output_tokens', 'retry_output_tokens', 'reserve_tokens',
                     'batch_size', 'temperature', 'top_p', 'top_k', 'min_p', 'seed',
                     'response_format', 'reasoning_budget', 'template_kwargs', 'extra_sampling'):
            common[name] = native[name]
        common.update(reasoning='auto', reasoning_format='deepseek', max_candidates=128)
    common.update(model=model, mode=mode, transport_timeout_seconds=3600,
                  worker_configuration_timeout_seconds=300, source_profile=native['profile_source'],
                  native_mode_constraints=native.get('native_capability'),
                  thinking_claim='requested mode; actual template and returned reasoning recorded separately')
    return common


def prepare(args):
    require(file_sha(EXPORT) == EXPORT_SHA, 'frozen_detector_archive_changed')
    _, _, _, all_sources, _ = load_corpus()
    sources = {name: all_sources[name] for name in DOCUMENTS}
    pool, export = legacy.frozen_detector_pool(EXPORT, sources)
    require(hashlib.sha256(json.dumps(export, sort_keys=True).encode()).hexdigest() == EXPORT_CANONICAL_SHA,
            'frozen_detector_export_changed')
    profile = scheme_profile(args.model, args.profile, getattr(args, 'new_catalog', NEW_CATALOG))
    if args.response_format is not None:
        profile['response_format'] = args.response_format
    producers = list(dict.fromkeys(PRODUCERS + ['evaluate_model_schemes.py', 'privacy_expansion_profiles.py']))
    extraction, _, kinds = prompts_and_kinds()
    plan = {'version': 1, 'experiment_kind': 'six-schemes-expansion-v1', 'model': args.model,
        'profile': profile, 'methods': list(METHODS), 'shadow': True, 'rules_changed': 0,
        'source_sha256': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()},
        'annotations_sha256': file_sha(ROOT / 'tests/docs/expected.json'),
        'frozen_detector_export_sha256': EXPORT_CANONICAL_SHA,
        'frozen_detector_archive_sha256': EXPORT_SHA,
        'frozen_candidate_pool_sha256': digest([case.json() for case in pool]),
        'candidate_count': len(pool), 'protected_bypass': 'historical behavior; scored separately',
        'model_artifact': list(FILES[args.model]), 'expected_runtime_sha256': RUNTIME_SHA256,
        'producer_sha256': {name: file_sha(ROOT / 'scripts' / name) for name in producers},
        'legacy_prompts_pool_and_validators_preserved': True, 'context_policy': 'prefix',
        'extraction_prompt_sha256': hashlib.sha256(extraction.encode()).hexdigest(),
        'kind_schema_sha256': hashlib.sha256(json.dumps(kinds).encode()).hexdigest(),
        'prompt_sha256': {name: hashlib.sha256(prompt.encode()).hexdigest() for name, prompt in
            [('primary', legacy.PRIMARY_PROMPT), ('audit', legacy.AUDIT_PROMPT), ('region', legacy.REGION_PROMPT)]},
        'failure_handling': 'durable raw transport; retries only for returned length truncation; invalid finals never rehabilitated',
        'transport_note': 'client wait extended to 3600; request bodies retain the declared token/sampling profile',
        'capabilities_verified_during_preparation': False}
    plan['plan_sha256'] = digest(plan)
    return plan


class MeasuredClient:
    def __init__(self, worker, model, context_tokens, output_tokens, response_format, sampling,
                 profile, output, history):
        self.worker, self.model, self.context_tokens = worker, model, context_tokens
        self.output_tokens, self.profile, self.output, self.history = output_tokens, profile, output, history
        self.sampling, self.records, self.planning_schema = sampling_settings(sampling), [], {}
        deadline = time.monotonic() + 30
        while not worker.status()['models'][model]['downloaded']:
            require(time.monotonic() < deadline, 'model_not_ready')
            time.sleep(.1)
        job = worker.submit('evaluate', model=model)
        worker.start_runtime(job['id'], model, 1, context_tokens=context_tokens,
            reasoning=profile['reasoning'], reasoning_format=profile['reasoning_format'],
            reasoning_budget=profile['reasoning_budget'])
        require(response_format != 'grammar' or worker.process_supports_json_schema is not False,
                'runtime_json_schema_not_supported')
        self.output_mode = response_format
        self.template_probe = {}
        for mode, kwargs in [('requested', profile['template_kwargs']),
                             ('off', {'enable_thinking': False}), ('on', {'enable_thinking': True})]:
            rendered = http_json(worker.ocr_base + '/apply-template', {
                'messages': [{'role': 'user', 'content': 'Public reference text. Return JSON.'}],
                'chat_template_kwargs': kwargs, 'add_generation_prompt': True})['prompt']
            self.template_probe[mode] = {'sha256': hashlib.sha256(rendered.encode()).hexdigest(),
                'tokens': worker.count_text_tokens(rendered), 'kwargs': dict(kwargs),
                'ends_with_think_prefix': rendered.rstrip().endswith('<think>')}

    def token_count(self, text):
        return self.worker.count_text_tokens(text)

    def prepared_prompt(self, prompt, schema=None):
        return schema_prompt(prompt, self.planning_schema if schema is None else schema) if self.output_mode == 'prompt' else prompt

    def input_tokens(self, prompt, value, schema=None):
        return self.worker.prompt_token_count(self.prepared_prompt(prompt, schema), value,
                                              self.profile['template_kwargs'])

    def save_history(self):
        atomic_json(self.output / 'transports.json', {'version': 1,
            'experiment_kind': 'six-schemes-expansion-v1', 'model': self.model, 'profile': self.profile,
            'native_template_probe': self.template_probe, 'requests': self.history})

    def one(self, prompt, value, schema, output_tokens):
        messages = [{'role': 'system', 'content': self.prepared_prompt(prompt, schema)},
                    {'role': 'user', 'content': json.dumps(value, ensure_ascii=False)}]
        request = {'model': 'qwen-cpu', 'messages': messages, **self.sampling,
            **self.profile['extra_sampling'], 'max_tokens': output_tokens,
            'stream': False, 'cache_prompt': False,
            'chat_template_kwargs': self.profile['template_kwargs']}
        if self.output_mode == 'grammar':
            request['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'learning', 'strict': True, 'schema': schema}}
        rendered = http_json(self.worker.ocr_base + '/apply-template', {
            'messages': messages, 'chat_template_kwargs': self.profile['template_kwargs'],
            'add_generation_prompt': True})['prompt']
        input_tokens = self.worker.count_text_tokens(rendered)
        require(input_tokens + output_tokens + self.profile['reserve_tokens'] <= self.context_tokens,
                'model_context_budget_exceeded')
        record = {'input_tokens': input_tokens, 'max_output_tokens': output_tokens,
                  'structured_output': self.output_mode, 'physical_request_index': len(self.history),
                  'measurement_stage': getattr(self, 'measurement_stage', 'unassigned')}
        entry = {'status': 'pending', 'request': request, 'request_sha256': digest(request),
            'rendered_prompt_sha256': hashlib.sha256(rendered.encode()).hexdigest(), 'record': record}
        self.history.append(entry)
        self.records.append(record)
        self.save_history()  # Durable exact request before sending; never replayed automatically.
        sampler = ProcessSampler(lambda: worker_pids(self.worker)).start()
        started = time.monotonic()
        try:
            transport = request_raw(self.worker.ocr_base + '/v1/chat/completions', request,
                                    self.profile['transport_timeout_seconds'])
            record['http_wall_seconds'] = time.monotonic() - started
            entry.update(status='received', raw_transport=transport)
            self.save_history()  # Preserve bytes before parsing, including HTTP/malformed replies.
            reply = json.loads(base64.b64decode(transport['body_base64']).decode('utf-8'))
            entry['raw_reply'] = reply
            require(transport['status'] == 200, 'runtime_http_' + str(transport['status']))
        except BaseException as exc:
            if 'raw_transport' not in entry:
                entry['status'] = 'failed'
            entry['error'] = type(exc).__name__
            record['error'] = type(exc).__name__
            raise
        finally:
            record.setdefault('http_wall_seconds', time.monotonic() - started)
            record['seconds'] = record['http_wall_seconds']
            record['sampled_resources'] = sampler.stop()
            record['memory'] = self.worker.resources()
            self.save_history()
        try:
            record['throughput'] = throughput_metrics(reply, record['http_wall_seconds'])
            choice = reply['choices'][0]
            record.update(finish_reason=choice.get('finish_reason'), usage=reply.get('usage'))
            thought = choice.get('message', {}).get('reasoning_content')
            record['reasoning_text_chars'] = len(thought) if isinstance(thought, str) else 0
            record['reasoning_text_tokens'] = self.token_count(thought) if isinstance(thought, str) and thought else 0
            require(choice.get('finish_reason') in ('stop', 'length'), 'model_nonstop_response')
            require(choice.get('finish_reason') != 'length', 'model_response_truncated')
            # Preserve the historical final parser. The unchanged validator still
            # decides applicability; short privacy-contract runs use strict_json.
            return json.loads(choice['message']['content'])
        except BaseException as exc:
            entry['parse_error'] = str(exc)[:160]
            record['error'] = type(exc).__name__
            raise
        finally:
            self.save_history()

    def infer(self, prompt, value, schema, output_tokens):
        try:
            return self.one(prompt, value, schema, output_tokens)
        except ValueError as exc:
            if str(exc) != 'model_response_truncated':
                raise
            available = self.context_tokens - self.profile['reserve_tokens'] - self.input_tokens(prompt, value, schema)
            retry = min(self.profile['retry_output_tokens'], available)
            if retry <= output_tokens:
                raise
            return self.one(prompt, value, schema, retry)


def execute(args, plan):
    require(not runtime_processes(args.runtime), 'runtime_busy_no_parallel_inference')
    artifact = plan['model_artifact']
    model = args.model_dir / artifact[0]
    require(model.stat().st_size == artifact[1] and file_sha(model) == artifact[2], 'model_checksum_mismatch')
    require(file_sha(args.runtime) == RUNTIME_SHA256, 'runtime_checksum_mismatch')
    verification = {'model_sha256_verified': True, 'model_disk_bytes': model.stat().st_size,
        'runtime_sha256': RUNTIME_SHA256, 'libraries_sha256': {
            path.relative_to(args.runtime.parent).as_posix(): file_sha(path)
            for path in sorted(args.runtime.parent.rglob('*.so*')) if path.is_file()},
        'hardware': hardware_snapshot()}
    atomic_json(args.output / 'verification.json', verification)
    history, clients, samplers = [], [], []
    execution = {'started_at_utc': utc(), 'runtime_starts': [], 'capability_probes': []}
    def worker_hook(worker):
        observe_runtime_starts(worker, execution['runtime_starts'])
        measured_start = worker.start_runtime
        def traced_start(*parameters, **keywords):
            try:
                return measured_start(*parameters, **keywords)
            finally:
                process = getattr(worker, 'process', None)
                execution['last_runtime_argv'] = getattr(process, 'args', None)
                execution['last_runtime_pid'] = getattr(process, 'pid', None)
        worker.start_runtime = traced_start
        original = worker.detect_json_schema
        def probe():
            started = time.monotonic()
            try:
                return original()
            finally:
                execution['capability_probes'].append({'measurement_request': False,
                    'physical_generation_requests': 1, 'contains_control_data': False,
                    'supported': worker.process_supports_json_schema, 'seconds': time.monotonic() - started})
        worker.detect_json_schema = probe
        samplers.append(ProcessSampler(lambda: worker_pids(worker)).start())
    def factory(*parameters):
        client = MeasuredClient(*parameters, plan['profile'], args.output, history)
        clients.append(client)
        return client
    def checkpoint(state):
        state['research_execution'] = execution
        state['measurement_request_slots_recorded'] = len(history)
        state['transport_responses_received'] = sum('raw_transport' in row for row in history)
        journal = args.output / 'transports.json'
        if journal.is_file():
            state['transport_journal_sha256'] = file_sha(journal)
        if clients:
            state['native_template_probe'] = clients[0].template_probe
    profile = plan['profile']
    command = ['--model', args.model, '--model-dir', str(args.model_dir), '--runtime', str(args.runtime),
        '--frozen-detectors', str(EXPORT), '--output', str(args.output), '--response-format', profile['response_format']]
    for name in ('context_tokens', 'output_tokens', 'reserve_tokens', 'batch_size', 'hint_tokens',
                 'max_candidates', 'max_fragments', 'temperature', 'top_p', 'top_k', 'min_p'):
        command.extend(['--' + name.replace('_', '-'), str(profile[name])])
    if profile['seed'] is not None:
        command.extend(['--seed', str(profile['seed'])])
    started = time.monotonic()
    try:
        legacy.main(command, client_factory=factory, worker_hook=worker_hook, checkpoint_hook=checkpoint,
            experiment_lineage={'kind': plan['experiment_kind'], 'plan_sha256': plan['plan_sha256'],
                                'verification_sha256': digest(verification), 'profile': profile})
    finally:
        execution.update(finished_at_utc=utc(), execution_wall_seconds=time.monotonic() - started,
                         sampled_resources=[sampler.stop() for sampler in samplers],
                         runtime_cleanup_verified=not runtime_processes(args.runtime))
        result_path = args.output / 'result.json'
        if result_path.exists():
            result = json.loads(result_path.read_text())
            checkpoint(result)
            result['native_template_probe'] = clients[0].template_probe if clients else None
            atomic_json(result_path, result)
        atomic_json(args.output / 'execution.json', execution)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--new-catalog', type=Path, default=NEW_CATALOG)
    parser.add_argument('--profile', choices=['common', 'native'], default='common')
    parser.add_argument('--response-format', choices=['grammar', 'prompt'])
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    if args.model not in model_ids(args.new_catalog):
        parser.error('model is outside the explicitly selected pinned matrix')
    require(not (args.output / 'result.json').exists() and not (args.output / 'transports.json').exists(),
            'recorded_scheme_attempt_exists_no_automatic_replay')
    with research_registration(args.model, args.new_catalog):
        plan = prepare(args)
        args.output.mkdir(parents=True, exist_ok=True)
        destination = args.output / 'scheme-plan.json'
        if destination.exists():
            require(json.loads(destination.read_text()) == plan, 'prepared_scheme_profile_changed')
        else:
            atomic_json(destination, plan)
        if args.prepare_only:
            print(json.dumps({'status': 'prepared', 'model': args.model, 'profile': args.profile,
                'methods': list(METHODS), 'candidate_count': plan['candidate_count'],
                'plan_sha256': plan['plan_sha256'], 'new_inference_calls': 0}))
            return
        require(args.model_dir is not None and args.runtime is not None, 'runtime_and_models_required')
        args.model_dir, args.runtime = args.model_dir.resolve(), args.runtime.resolve()
        execute(args, plan)


if __name__ == '__main__':
    main()
