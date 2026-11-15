#!/usr/bin/env python3
"""Prepare or run a fresh paired A/B/C/D privacy experiment on 32 frozen cases.

Preparation needs no model, runtime, detector exporter, network or inference.
Generation verifies pinned artifacts and shares one runtime-checked batch plan.
All results are shadow-only and use an isolated temporary worker home.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from check_learning_corpus import load_corpus
from benchmark_resources import ProcessSampler, hardware_snapshot, throughput_metrics
from compare_local_models import DOCUMENTS, ROOT
from evaluate_context_controls import ControlledClient, digest, prepare_cases
from evaluate_validation_schemes import atomic_json, resources
from experiment_inference import prompts_and_kinds, sampling_settings
from kaponir_worker import (FILES, RUNTIME_NAME, RUNTIME_SHA, RUNTIME_URL,
                            Worker, configuration, http_json, schema_prompt)
from privacy_policy_experiment import (VARIANTS, common_batches, metrics, payload,
    prompts, schema, strict_json, technical_review, transitions, validate)
from privacy_policy_profiles import profile as declared_profile


RUNTIME_SHA256 = '3e042c4518a74fc3bdb3edb80e07922ee01d6a3455618952af23f3e546508934'
PRODUCERS = ['evaluate_privacy_policy.py', 'privacy_policy_experiment.py',
             'benchmark_resources.py', 'privacy_policy_profiles.py',
             'evaluate_context_controls.py', 'validation_pipeline.py', 'kaponir_worker.py',
             'experiment_inference.py', 'kaponir_inference.py', 'evaluate_validation_schemes.py',
             'check_learning_corpus.py', 'compare_local_models.py']


def worker_pids(worker):
    def pid(attribute):
        value = getattr(getattr(worker, attribute, None), 'pid', None)
        return value if type(value) is int and value > 0 else None
    return {'worker': os.getpid(), 'model': pid('process'), 'ocr': pid('ocr_process')}


def observe_runtime_starts(worker, events):
    """Measure readiness including capability probes; retain every restart."""
    original = getattr(worker, 'start_runtime', None)
    if not callable(original):
        return

    def measured(*args, **kwargs):
        before = worker_pids(worker)['model']
        started = time.monotonic()
        event = {'reasoning_budget': kwargs.get('reasoning_budget', -1),
                 'context_tokens': kwargs.get('context_tokens', 4096),
                 'includes_capability_probe': True, 'filesystem_cache_state': 'uncontrolled'}
        try:
            result = original(*args, **kwargs)
            event['status'] = 'ready'
            return result
        except Exception as exc:
            event.update(status='failed', error=type(exc).__name__)
            raise
        finally:
            after = worker_pids(worker)['model']
            event.update(seconds=round(time.monotonic() - started, 6),
                         process_started=after is not None and before != after,
                         process_reused=before is not None and before == after)
            events.append(event)

    worker.start_runtime = measured


def performance_summary(state):
    """Include every recorded retry; keep unknown interrupted timings unknown."""
    result = {}
    for variant in VARIANTS:
        entries = [entry for entry in state['responses'].values()
                   if entry.get('variant') == variant]
        records = [entry.get('record', {}) for entry in entries]
        durations = [record['http_wall_seconds'] for record in records
                     if isinstance(record.get('http_wall_seconds'), (int, float))]
        native = {}
        for name in ('native_prompt', 'native_prediction'):
            values = [record.get('throughput', {}).get(name) for record in records]
            values = [value for value in values if isinstance(value, dict)
                      and isinstance(value.get('tokens'), (int, float))
                      and isinstance(value.get('seconds'), (int, float)) and value['seconds'] > 0]
            seconds = sum(value['seconds'] for value in values)
            tokens = sum(value['tokens'] for value in values)
            native[name] = {'measured_requests': len(values), 'tokens': tokens,
                            'seconds': seconds,
                            'tokens_per_second': tokens / seconds if seconds else None}
        model_resources = [record.get('sampled_resources', {}).get('roles', {}).get('model', {})
                           for record in records]
        peaks = [row['observed_peak_rss_bytes'] for row in model_resources
                 if isinstance(row.get('observed_peak_rss_bytes'), (int, float))]
        cpu = [row['cpu_seconds'] for row in model_resources
               if isinstance(row.get('cpu_seconds'), (int, float))]
        case_ids = {ident for entry in entries for ident in entry.get('case_ids', [])}
        complete_duration = len(durations) == len(entries)
        batches = {}
        for entry in entries:
            batches[entry.get('batch_index')] = batches.get(entry.get('batch_index'), 0) + 1
        intervals = [interval for interval in state.get('variant_execution_intervals', [])
                     if interval['variant'] == variant]
        result[variant] = {'recorded_request_attempts': len(entries),
            'received_transport_responses': sum('raw_transport' in entry for entry in entries),
            'timed_attempts': len(durations),
            'unknown_attempt_timings': len(entries) - len(durations),
            'retried_batches': sum(count > 1 for count in batches.values()),
            'http_wall_seconds_observed_including_retries': round(sum(durations), 6),
            'http_wall_seconds_including_retries': round(sum(durations), 6) if complete_duration else None,
            'amortized_http_seconds_per_case_including_retries': (
                sum(durations) / len(case_ids) if case_ids and complete_duration else None),
            'mean_attempt_seconds': sum(durations) / len(durations) if durations else None,
            'maximum_attempt_seconds': max(durations) if durations else None,
            'model_observed_peak_rss_bytes': max(peaks) if peaks else None,
            'model_cpu_seconds_including_retries': sum(cpu) if cpu else None,
            'attempts_with_model_cpu_measurement': len(cpu),
            'model_cpu_measurement_complete': len(cpu) == len(entries),
            'active_wall_seconds_including_scoring': (
                sum(interval['wall_seconds'] for interval in intervals) if intervals else None),
            'active_execution_intervals': len(intervals),
            'native_throughput': native,
            'ttft_available': False,
            'scope': 'review requests, including retries; startup and scoring reported per session'}
    return result


def file_sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def request_for(args, variant, batch, sources, instructions, kinds, output=None):
    value, _ = payload(variant, batch, sources)
    contract = schema(variant, batch, kinds)
    prompt = instructions[variant]
    if args.response_format == 'prompt':
        prompt = schema_prompt(prompt, contract)
    extra_sampling = dict(getattr(args, 'extra_sampling', {}))
    for key, sampling_value in extra_sampling.items():
        if (key not in ('repeat_penalty', 'presence_penalty') or isinstance(sampling_value, bool)
                or not isinstance(sampling_value, (int, float)) or not math.isfinite(sampling_value)
                or not (0 < sampling_value <= 2 if key == 'repeat_penalty' else -2 <= sampling_value <= 2)):
            raise ValueError('invalid_extra_sampling')
    template_kwargs = dict(getattr(args, 'template_kwargs', {'enable_thinking': True}))
    result = {'model': args.model, 'messages': [{'role': 'system', 'content': prompt},
              {'role': 'user', 'content': json.dumps(value, ensure_ascii=False)}],
              **sampling_settings({key: getattr(args, key)
                  for key in ['temperature', 'top_p', 'top_k', 'min_p', 'seed']}),
              **extra_sampling,
              'max_tokens': args.output_tokens if output is None else output,
              'stream': False, 'cache_prompt': False,
              'chat_template_kwargs': template_kwargs}
    if args.response_format == 'grammar':
        result['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'control', 'strict': True, 'schema': contract}}
    return result


def make_plan(args, cases, case_data, sources, instructions, kinds, batches):
    variants = tuple(getattr(args, 'variants', VARIANTS))
    if not variants or len(set(variants)) != len(variants) or any(v not in VARIANTS for v in variants):
        raise ValueError('invalid_experiment_variants')
    producer_files = list(dict.fromkeys(PRODUCERS + list(getattr(args, 'extra_producer_files', []))))
    if any(Path(name).name != name or not name.endswith('.py') for name in producer_files):
        raise ValueError('invalid_experiment_producer_file')
    settings = {key: getattr(args, key) for key in [
        'model', 'batch_size', 'context_tokens', 'output_tokens', 'retry_output_tokens',
        'reserve_tokens', 'reasoning_budget', 'response_format', 'temperature',
        'top_p', 'top_k', 'min_p', 'seed', 'timeout', 'expected_runtime_sha256']}
    settings.update(thinking=getattr(args, 'thinking_mode', 'bounded'), threads=2, slots=1, gpu_layers=0,
                    resource_sample_interval_seconds=0.25, telemetry_version=1,
                    request_mode='nonstreaming', variants=list(variants),
                    template_kwargs=dict(getattr(args, 'template_kwargs', {'enable_thinking': True})),
                    extra_sampling=dict(getattr(args, 'extra_sampling', {})),
                    worker_configuration_timeout_seconds=min(args.timeout, 1800))
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, check=True,
                              text=True, capture_output=True).stdout.strip()
    catalog = json.loads((ROOT / 'benchmark/data/models.json').read_text())
    quantization = next((row.get('quantization') for row in catalog['models']
                         if row['id'] == args.model), None)
    try:
        model_profile = getattr(args, 'model_profile', None) or declared_profile(args.model)
    except ValueError as exc:
        if str(exc) != 'unknown_phase2_model':
            raise
        model_profile = None
    if model_profile:
        quantization = model_profile.get('quantization', quantization)
    signatures = {'settings': settings, 'git_head': revision,
        'case_data_sha256': digest(case_data),
        'source_sha256': case_data['source_sha256'],
        'annotations_sha256': file_sha(getattr(args, 'annotations_path', ROOT / 'tests/docs/expected.json')),
        'policy_sha256': file_sha(args.policy),
        'kinds_sha256': digest(kinds),
        'code_sha256': {name: file_sha(ROOT / 'scripts' / name) for name in producer_files},
        'kind_source_sha256': file_sha(ROOT / 'crates/kaponir-core/src/kinds.rs'),
        'model_artifact': list(FILES[args.model]), 'quantization': quantization,
        'quality_contract': getattr(args, 'quality_contract', {
            'dataset': 'calibration-controls-v1', 'reference_classes': ['mask', 'keep']}),
        'model_profile_source': model_profile['profile_source'] if model_profile else None,
        'declared_profile_deviations': {key: {'requested': value, 'declared': model_profile[key]}
            for key, value in settings.items() if model_profile and key in model_profile
            and model_profile[key] != value}}
    requests = []
    for variant in variants:
        for index, batch in enumerate(batches):
            request = request_for(args, variant, batch, sources, instructions, kinds)
            value = strict_json(request['messages'][1]['content'])
            contract = schema(variant, batch, kinds)
            requests.append({'variant': variant, 'batch_index': index,
                'case_ids': [case.id for case in batch], 'request': request,
                'request_sha256': digest(request),
                'prompt_sha256': hashlib.sha256(request['messages'][0]['content'].encode()).hexdigest(),
                'input_sha256': digest(value), 'schema_sha256': digest(contract)})
    plan = {'version': 1, 'signature': signatures, 'control_cases': case_data,
            'batches': [[case.id for case in batch] for batch in batches],
            'variant_order': list(variants), 'requests': requests,
            'historical_reference': getattr(args, 'historical_reference',
                model_profile['historical_reference'] if model_profile else None),
            'offset_convention': 'Unicode code point offsets; selected occurrence only',
            'protected_bypass': False, 'shadow': True, 'rules_changed': 0,
            'inference_calls': 0, 'model_sha256_verified': False,
            'token_budget_validation': 'pending_runtime'}
    plan['plan_sha256'] = digest({'signature': signatures, 'batches': plan['batches'],
                                  'requests': requests})
    return plan


def save_plan(path, plan):
    if path.exists():
        old = strict_json(path.read_text())
        if old['signature'] != plan['signature']:
            raise ValueError('prepared_profile_changed_use_new_output')
    atomic_json(path, plan)


def parse_response(entry):
    if entry.get('error'):
        return None, entry['error']
    try:
        choice = entry['raw_reply']['choices'][0]
        if choice.get('finish_reason') != 'stop':
            return None, 'model_response_truncated'
        final = choice.get('message', {}).get('content')
        if not isinstance(final, str):
            return None, 'invalid_final_json'
        reply = strict_json(final)
        if not isinstance(reply, dict):
            return None, 'invalid_final_json'
        return reply, None
    except ValueError as exc:
        return None, str(exc) if str(exc) in (
            'duplicate_json_key', 'invalid_json_constant') else 'invalid_final_json'
    except (KeyError, IndexError, TypeError):
        return None, 'invalid_runtime_response'


def request_raw(url, request, timeout):
    """Retain transport bytes, including malformed JSON and HTTP error bodies."""
    message = urllib.request.Request(url, data=json.dumps(request).encode(),
                                     headers={'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(message, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        return {'status': response.status,
                'body_base64': base64.b64encode(body).decode('ascii'),
                'content_type': response.headers.get('Content-Type')}


def decode_transport(entry):
    try:
        transport = entry['raw_transport']
        raw = base64.b64decode(transport['body_base64'], validate=True).decode('utf-8')
        entry['raw_reply'] = strict_json(raw)
        if transport['status'] != 200:
            entry['error'] = 'runtime_http_' + str(transport['status'])
    except (ValueError, TypeError, UnicodeError):
        entry['error'] = 'invalid_runtime_json'


def perform_request(state, save, worker, args, planned, output, input_tokens):
    request = {**planned['request'], 'max_tokens': output}
    key = digest([planned['variant'], planned['batch_index'], request])
    if key in state['responses']:
        entry = state['responses'][key]
        if entry['status'] == 'pending':
            entry.update(status='interrupted', error='interrupted_before_response_checkpoint')
            save()
        elif 'raw_transport' in entry and 'raw_reply' not in entry and not entry.get('error'):
            decode_transport(entry)
            save()
        return entry
    entry = {'request': request, 'request_sha256': digest(request),
             'variant': planned['variant'], 'batch_index': planned['batch_index'],
             'case_ids': planned['case_ids'], 'status': 'pending',
             'record': {'input_tokens': input_tokens, 'max_output_tokens': output,
                        'reasoning_budget': args.reasoning_budget}}
    state['responses'][key] = entry
    state['calls'].append(key)
    save()  # Exact request is durable before sending it.
    sampler = ProcessSampler(lambda: worker_pids(worker)).start()
    started = time.monotonic()
    try:
        try:
            transport = request_raw(worker.ocr_base + '/v1/chat/completions',
                                    request, timeout=args.timeout)
        except Exception as exc:
            code = str(exc)
            entry.update(status='failed', error=code if re.fullmatch(r'[a-z_]{1,80}', code)
                         else type(exc).__name__)
        else:
            entry['record']['http_wall_seconds'] = round(time.monotonic() - started, 6)
            entry['raw_transport'] = transport
            entry['status'] = 'received'
            save()  # Persist transport bytes before parsing or collecting more metrics.
            decode_transport(entry)
    finally:
        entry['record'].setdefault('http_wall_seconds', round(time.monotonic() - started, 6))
        entry['record']['sampled_resources'] = sampler.stop()
        entry['record']['resource_window'] = 'HTTP request and immediate raw response checkpoint'
    entry['record'].update(seconds=round(entry['record']['http_wall_seconds'], 3),
        memory=worker.resources(), runtime_exit_code=worker.process.poll() if worker.process else None)
    if 'raw_reply' in entry:
        try:
            choice = entry['raw_reply']['choices'][0]
            message = choice.get('message', {})
            thought = message.get('reasoning_content', '')
            entry['record'].update(finish_reason=choice.get('finish_reason'),
                usage=entry['raw_reply'].get('usage'),
                final_text_chars=len(message.get('content') or ''),
                reasoning_text_chars=len(thought) if isinstance(thought, str) else 0)
            try:
                entry['record']['reasoning_text_tokens'] = (
                    worker.count_text_tokens(thought) if isinstance(thought, str) and thought else 0)
            except Exception:
                entry['record']['reasoning_text_tokens'] = None
                entry['record']['reasoning_token_measurement'] = 'unavailable'
        except (KeyError, TypeError, IndexError):
            pass
        entry['record']['throughput'] = throughput_metrics(
            entry['raw_reply'], entry['record']['http_wall_seconds'])
    save()
    return entry


def execute(args, plan, cases, sources, expected, instructions, kinds, metric_function=metrics,
            documents=None, transition_function=transitions, client_factory=None):
    documents = list(sources) if documents is None else documents
    execution_started = time.monotonic()
    hardware = hardware_snapshot()
    session = {'session_id': str(time.time_ns()),
        'started_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'mode': 'preflight' if args.preflight_only else 'measurement',
        'hardware': hardware, 'hardware_sha256': digest(hardware),
        'phases_seconds': {}, 'runtime_starts': [],
        'filesystem_cache_state': 'uncontrolled',
        'resource_scope': 'worker/runtime initialization through cleanup; verification has separate wall time'}
    verification_started = time.monotonic()
    artifact = FILES[args.model]
    model_path = args.model_dir / artifact[0]
    if model_path.stat().st_size != artifact[1] or file_sha(model_path) != artifact[2]:
        raise ValueError('model_artifact_checksum_mismatch')
    runtime_sha = file_sha(args.runtime)
    if runtime_sha != args.expected_runtime_sha256:
        raise ValueError('runtime_checksum_mismatch')
    library_hashes = {path.relative_to(args.runtime.parent).as_posix(): file_sha(path)
        for path in sorted(args.runtime.parent.rglob('*.so*')) if path.is_file()}
    archive_path = args.model_dir / RUNTIME_NAME
    archive_provenance = {'expected_url': RUNTIME_URL, 'expected_sha256': RUNTIME_SHA,
                          'local_archive_sha256': file_sha(archive_path) if archive_path.is_file() else None}
    session['phases_seconds']['artifact_verification'] = round(time.monotonic() - verification_started, 6)
    session['model_disk_bytes'] = model_path.stat().st_size
    state_path = args.output / 'result.json'
    previous = strict_json(state_path.read_text()) if state_path.exists() else None
    with tempfile.TemporaryDirectory(prefix='kaponir-privacy-policy-') as temporary:
        home = Path(temporary)
        worker = Worker(home, configuration(home, {'model_dir': str(args.model_dir.resolve()),
            'runtime': str(args.runtime.resolve()), 'threads': 2,
            'ocr_timeout_seconds': min(args.timeout, 1800)}), 'unused')
        session_sampler = ProcessSampler(lambda: worker_pids(worker)).start()
        observe_runtime_starts(worker, session['runtime_starts'])
        try:
            probes = []
            original_probe = worker.detect_json_schema

            def probe_capability():
                started = time.monotonic()
                original_probe()
                probes.append({'type': 'json_schema_capability', 'contains_control_data': False,
                    'measurement_request': False, 'physical_generation_requests': 1,
                    'supported': worker.process_supports_json_schema,
                    'seconds': round(time.monotonic() - started, 3)})

            worker.detect_json_schema = probe_capability
            initialization_started = time.monotonic()
            client = (client_factory or ControlledClient)(worker, args)
            session['phases_seconds']['client_initialization'] = round(
                time.monotonic() - initialization_started, 6)
            configuration_started = time.monotonic()
            client.configure_budget('bounded')
            session['phases_seconds']['bounded_runtime_configuration'] = round(
                time.monotonic() - configuration_started, 6)
            if args.response_format == 'grammar' and worker.process_supports_json_schema is False:
                raise ValueError('runtime_json_schema_not_supported')
            token_counts = {}
            planning_started = time.monotonic()

            def fits(variant, batch):
                value, _ = payload(variant, batch, sources)
                contract = schema(variant, batch, kinds)
                count = client.tokens(instructions[variant], value, contract, True)
                token_counts[(variant, tuple(case.id for case in batch))] = count
                return count + args.output_tokens + args.reserve_tokens <= args.context_tokens

            batches = common_batches(cases, documents, args.batch_size, fits)
            runtime_plan = make_plan(args, cases, plan['control_cases'], sources,
                                     instructions, kinds, batches)
            runtime_plan.update(token_budget_validation='verified_runtime',
                model_sha256_verified=True, runtime_sha256=runtime_sha,
                adjacent_runtime_libraries_sha256=library_hashes,
                runtime_archive_provenance=archive_provenance,
                hardware=hardware, hardware_sha256=digest(hardware))
            for row in runtime_plan['requests']:
                row['input_tokens'] = token_counts[(row['variant'], tuple(row['case_ids']))]
                rendered = http_json(worker.ocr_base + '/apply-template', {
                    'messages': row['request']['messages'],
                    'chat_template_kwargs': row['request']['chat_template_kwargs'],
                    'add_generation_prompt': True})['prompt']
                row['rendered_prompt_sha256'] = hashlib.sha256(rendered.encode()).hexdigest()
            template_probes = {}
            for mode in ('off', 'on'):
                rendered = http_json(worker.ocr_base + '/apply-template', {
                    'messages': [{'role': 'user', 'content': 'Public reference text. Return JSON.'}],
                    'chat_template_kwargs': {'enable_thinking': mode == 'on'},
                    'add_generation_prompt': True})['prompt']
                template_probes[mode] = {'formatted_sha256': hashlib.sha256(rendered.encode()).hexdigest(),
                                         'tokens': worker.count_text_tokens(rendered)}
            runtime_plan['native_template_probe'] = template_probes
            runtime_plan['runtime_capability_probes'] = probes
            session['phases_seconds']['native_request_planning'] = round(
                time.monotonic() - planning_started, 6)
            if runtime_plan['batches'] != plan['batches']:
                runtime_plan['supersedes_prepared_plan_sha256'] = plan['plan_sha256']
                if previous is not None:
                    raise ValueError('runtime_batch_plan_changed_after_inference')
            signature = {'plan_sha256': runtime_plan['plan_sha256'],
                         'runtime_sha256': runtime_sha, 'model_sha256': artifact[2],
                         'hardware_sha256': digest(hardware),
                         'adjacent_runtime_libraries_sha256': library_hashes,
                         'template_probe_sha256': digest(template_probes),
                         'rendered_requests_sha256': digest([
                             [row['variant'], row['batch_index'], row['rendered_prompt_sha256'],
                              row['input_tokens']] for row in runtime_plan['requests']])}
            if previous is not None and previous['signature'] != signature:
                raise ValueError('privacy_policy_resume_profile_changed')
            # Freeze the common runtime plan before any model generation.
            save_plan(args.output / 'plan.json', runtime_plan)
            if args.preflight_only:
                preflight = {'status': 'preflight_completed', 'signature': signature,
                    'measurement_inference_calls': 0, 'cases': len(cases),
                    'batches_per_variant': len(batches), 'planned_measurement_requests': len(runtime_plan['requests']),
                    'token_budget_validation': 'verified_runtime', 'probes': probes,
                    'native_template_probe': template_probes, 'resources': resources(),
                    'worker_memory': worker.resources(), 'execution_session': session}
                atomic_json(args.output / 'preflight.json', preflight)
                print(json.dumps({key: preflight[key] for key in [
                    'status', 'measurement_inference_calls', 'cases', 'batches_per_variant',
                    'planned_measurement_requests', 'token_budget_validation']}), flush=True)
                return
            state = previous or {'version': 1, 'signature': signature,
                'status': 'running', 'responses': {}, 'calls': [], 'stages': {},
                'metrics': {}, 'shadow': True, 'rules_changed': 0,
                'historical_reference': runtime_plan['historical_reference'],
                'resources_before': resources()}
            session['recorded_request_slots_at_start'] = len(state['calls'])

            def save():
                atomic_json(state_path, state)

            if state['status'] == 'completed':
                print(json.dumps({'status': 'completed', 'metrics': state['metrics']}))
                return
            state.update(status='running')
            state.pop('error', None)
            save()
            indexed = {case.id: case for case in cases}
            measurement_started = time.monotonic()
            for variant in runtime_plan['variant_order']:
                active_variant = variant
                variant_started = time.monotonic()
                variant_slots_before = len(state['calls'])
                decisions = state['stages'].setdefault(variant, {'decisions': {}})['decisions']
                for planned in runtime_plan['requests']:
                    if planned['variant'] != variant:
                        continue
                    batch = [indexed[ident] for ident in planned['case_ids']]
                    entry = perform_request(state, save, worker, args, planned,
                                            args.output_tokens, planned['input_tokens'])
                    reply, error = parse_response(entry)
                    if error == 'model_response_truncated':
                        available = args.context_tokens - args.reserve_tokens - planned['input_tokens']
                        retry = min(args.retry_output_tokens, available)
                        if retry > args.output_tokens:
                            entry = perform_request(state, save, worker, args, planned,
                                                    retry, planned['input_tokens'])
                            reply, error = parse_response(entry)
                    _, contexts = payload(variant, batch, sources)
                    checked = ({case.id: technical_review(case, error) for case in batch}
                               if error else validate(reply, batch, contexts, kinds, variant))
                    decisions.update(checked)
                    state['metrics'][variant] = metric_function(cases, decisions, expected, variant != 'D')
                    state['case_transitions'] = transition_function(cases, state['stages'], expected)
                    state['performance'] = performance_summary(state)
                    save()
                state.setdefault('variant_execution_intervals', []).append({
                    'session_id': session['session_id'], 'variant': variant, 'status': 'completed',
                    'wall_seconds': round(time.monotonic() - variant_started, 6),
                    'new_request_slots_recorded': len(state['calls']) - variant_slots_before})
                active_variant = None
                state['performance'] = performance_summary(state)
                save()
                print(json.dumps({'variant': variant, **state['metrics'][variant]}), flush=True)
            session['phases_seconds']['measurement_and_scoring'] = round(
                time.monotonic() - measurement_started, 6)
            entries = list(state['responses'].values())
            state.update(status='completed', resources_after=resources(),
                measurement_request_slots_recorded=len(state['calls']),
                transport_responses_received=sum('raw_transport' in entry for entry in entries),
                transport_failures=sum(entry['status'] == 'failed' for entry in entries),
                interrupted_request_slots=sum(entry['status'] == 'interrupted' for entry in entries),
                physical_request_count_indeterminate=any(entry['status'] == 'interrupted' for entry in entries),
                technical_incomplete=any(value['technical_review'] or value['unattempted_cases']
                                         for value in state['metrics'].values()))
            save()
        except Exception as exc:
            session.update(status='failed', error=type(exc).__name__)
            if 'token_counts' in locals():
                atomic_json(args.output / 'native-budget-observations.json', {
                    'status': 'failed', 'error': str(exc)[:200],
                    'context_tokens': args.context_tokens, 'output_tokens': args.output_tokens,
                    'reserve_tokens': args.reserve_tokens,
                    'input_token_observations': [{'variant': variant, 'case_ids': list(ids), 'input_tokens': count}
                        for (variant, ids), count in token_counts.items()],
                    'measurement_request_slots_recorded': len((state if 'state' in locals()
                        else previous or {}).get('calls', [])),
                    'whole_document_not_truncated': True})
            if 'state' in locals():
                if locals().get('active_variant') is not None:
                    state.setdefault('variant_execution_intervals', []).append({
                        'session_id': session['session_id'], 'variant': active_variant, 'status': 'failed',
                        'wall_seconds': round(time.monotonic() - variant_started, 6),
                        'new_request_slots_recorded': len(state['calls']) - variant_slots_before})
                state.update(status='failed', error=str(exc)[:200], resources_after=resources())
                atomic_json(state_path, state)
            raise
        finally:
            cleanup_started = time.monotonic()
            try:
                worker.stop.set()
                worker.stop_runtime()
            finally:
                session['phases_seconds']['runtime_cleanup'] = round(
                    time.monotonic() - cleanup_started, 6)
                session['sampled_resources'] = session_sampler.stop()
                session['execution_wall_seconds'] = round(time.monotonic() - execution_started, 6)
                session['finished_at_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                session.setdefault('status', 'completed')
                if 'state' in locals():
                    session['new_request_slots_recorded'] = (
                        len(state['calls']) - session['recorded_request_slots_at_start'])
                    state.setdefault('execution_sessions', []).append(session)
                    state['performance'] = performance_summary(state)
                    atomic_json(state_path, state)
                if 'preflight' in locals():
                    atomic_json(args.output / 'preflight.json', preflight)
                atomic_json(args.output / ('session-' + session['session_id'] + '.json'), session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--preflight-only', action='store_true',
                        help='Verify artifacts and native token budgets; no scored control inference')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--policy', type=Path, default=ROOT / 'docs/experiments/privacy-policy-v1.txt')
    parser.add_argument('--cases', type=Path, default=ROOT / 'tests/fixtures/context-control-candidates.json')
    parser.add_argument('--model', choices=sorted(FILES), default='gemma4-e4b-qat')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--expected-runtime-sha256', default=RUNTIME_SHA256)
    parser.add_argument('--context-tokens', type=int, default=16384)
    parser.add_argument('--output-tokens', type=int, default=4096)
    parser.add_argument('--retry-output-tokens', type=int, default=8192)
    parser.add_argument('--reserve-tokens', type=int, default=64)
    parser.add_argument('--reasoning-budget', type=int, default=2048)
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--response-format', choices=['grammar', 'prompt'], default='grammar')
    parser.add_argument('--temperature', type=float, default=1.0)
    parser.add_argument('--top-p', type=float, default=.95)
    parser.add_argument('--top-k', type=int, default=64)
    parser.add_argument('--min-p', type=float, default=0.0)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    if args.prepare_only and args.preflight_only:
        parser.error('--prepare-only and --preflight-only are mutually exclusive')
    if args.preflight_only and (args.output / 'result.json').exists():
        parser.error('preflight requires an output directory without measured results')
    if (not 1 <= args.batch_size <= 64 or args.context_tokens <= args.output_tokens + args.reserve_tokens
            or args.output_tokens <= 0 or args.retry_output_tokens < args.output_tokens
            or args.reserve_tokens < 0 or not 0 <= args.reasoning_budget <= args.context_tokens
            or args.timeout <= 0 or not re.fullmatch(r'[a-f0-9]{64}', args.expected_runtime_sha256)):
        parser.error('invalid experiment budget or runtime checksum')
    sampling_settings({key: getattr(args, key) for key in
                       ['temperature', 'top_p', 'top_k', 'min_p', 'seed']})
    if not args.prepare_only and (args.model_dir is None or args.runtime is None):
        parser.error('generation/preflight requires --model-dir and --runtime')
    if not args.cases.is_file():
        parser.error('the frozen control-case fixture must exist')
    _, expected, public, all_sources, _ = load_corpus()
    sources = {name: all_sources[name] for name in DOCUMENTS}
    cases, case_data = prepare_cases(args.cases, sources, expected, public, 32)
    if (len(cases) != 32 or case_data['reference_private'] != 16
            or case_data['reference_public'] != 16
            or sum((case.file, case.value) in expected for case in cases) != 16
            or any(case.protected for case in cases)
            or len({case.id for case in cases}) != 32):
        raise ValueError('frozen_control_cases_changed')
    for row in case_data['candidates']:
        if set(row) != {'id', 'file', 'value', 'start', 'end'}:
            raise ValueError('control_case_contains_scoring_metadata')
    policy = args.policy.read_text(encoding='utf-8')
    if not policy.strip():
        raise ValueError('empty_privacy_policy')
    instructions = prompts(policy)
    _, _, kinds = prompts_and_kinds()
    args.output.mkdir(parents=True, exist_ok=True)
    batches = common_batches(cases, DOCUMENTS, args.batch_size)
    plan = make_plan(args, cases, case_data, sources, instructions, kinds, batches)
    if args.prepare_only:
        if (args.output / 'result.json').exists():
            raise ValueError('cannot_prepare_over_existing_results')
        save_plan(args.output / 'plan.json', plan)
        print(json.dumps({'status': 'prepared', 'plan_sha256': plan['plan_sha256'],
            'cases': len(cases), 'batches_per_variant': len(batches),
            'planned_requests': len(plan['requests']), 'inference_calls': 0,
            'token_budget_validation': 'pending_runtime'}))
    else:
        plan_path = args.output / 'plan.json'
        if plan_path.exists():
            old = strict_json(plan_path.read_text())
            if old['signature'] != plan['signature']:
                raise ValueError('prepared_profile_changed_use_new_output')
        execute(args, plan, cases, sources, expected, instructions, kinds)


if __name__ == '__main__':
    main()
