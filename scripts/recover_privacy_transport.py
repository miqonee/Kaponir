#!/usr/bin/env python3
"""Supplement completed phase2 runs with exact-request timeout recovery.

Only the last, durably failed TimeoutError of a batch is eligible. Received
answers, invalid types, truncated finals and uncertain interrupted sends are never
retried here. Original files remain unchanged; supplements have their own kind,
lineage and client timeout. An optional queue waits for the original suite to end.
"""
import argparse
from collections import defaultdict
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from run_privacy_policy_suite import (SuiteLock, atomic_json, file_sha,
    process_identity, runtime_processes, utc)
from summarize_privacy_policy import digest, load_run, strict_json


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def recovery_producer_hashes():
    root = Path(__file__).resolve().parents[1]
    names = ['scripts/recover_privacy_transport.py', 'scripts/summarize_privacy_policy.py',
             'scripts/run_privacy_policy_suite.py', 'benchmark/data/models.json',
             'benchmark/data/planned-models.json']
    return {name: file_sha(root / name) for name in names}


def selected_timeouts(state, plan):
    """Select by transport outcome, never by correctness or model identity."""
    grouped = defaultdict(list)
    for key in state['calls']:
        entry = state['responses'][key]
        grouped[(entry['variant'], entry['batch_index'])].append((key, entry))
    indexed = {(row['variant'], row['batch_index']): row for row in plan['requests']}
    selected = []
    for batch, attempts in grouped.items():
        key, entry = attempts[-1]
        if (entry.get('status') != 'failed' or entry.get('error') != 'TimeoutError'
                or 'raw_transport' in entry):
            continue
        planned = indexed[batch]
        require(entry['request_sha256'] == digest(entry['request']), 'original_request_hash_mismatch')
        require(entry['request'] == {**planned['request'], 'max_tokens': entry['request']['max_tokens']},
                'original_request_changed')
        selected.append({'original_response_key': key, 'original_error': entry['error'],
            'original_http_wall_seconds': entry.get('record', {}).get('http_wall_seconds'),
            'variant': entry['variant'], 'batch_index': entry['batch_index'],
            'case_ids': list(entry['case_ids']), 'request': entry['request'],
            'request_sha256': entry['request_sha256'],
            'input_tokens': planned['input_tokens'],
            'rendered_prompt_sha256': planned['rendered_prompt_sha256']})
    return selected


def make_recovery_plan(result, producer_root, timeout):
    result, producer_root = Path(result).resolve(), Path(producer_root).resolve()
    # Verify the original provenance, batch registration and scored decisions.
    verified = load_run(result)
    state = strict_json(result.read_text())
    plan_path = result.with_name('plan.json')
    original = strict_json(plan_path.read_text())
    require(state['status'] == 'completed' and all(row['protocol_complete'] for row in verified['rows']),
            'original_run_not_complete')
    sessions = state.get('execution_sessions', [])
    require(bool(sessions) and all('runtime_cleanup' in session.get('phases_seconds', {})
                                  for session in sessions), 'original_cleanup_not_durable')
    signature = original['signature']
    require(timeout > signature['settings']['timeout'], 'recovery_wait_must_be_larger')
    for name, expected in signature['code_sha256'].items():
        require(Path(name).name == name and file_sha(producer_root / 'scripts' / name) == expected,
                'original_producer_changed:' + name)
    require(file_sha(producer_root / 'docs/experiments/privacy-policy-v1.txt') == signature['policy_sha256'],
            'original_policy_changed')
    require(file_sha(producer_root / 'tests/docs/expected.json') == signature['annotations_sha256'],
            'original_annotations_changed')
    require(file_sha(producer_root / 'crates/kaponir-core/src/kinds.rs') == signature['kind_source_sha256'],
            'original_kinds_changed')
    output = {'version': 1, 'experiment_kind': 'transport-recovery-v1',
        'original_result_path': str(result), 'original_result_sha256': file_sha(result),
        'original_plan_path': str(plan_path), 'original_plan_file_sha256': file_sha(plan_path),
        'original_plan_sha256': original['plan_sha256'],
        'producer_root': str(producer_root), 'original_signature': signature,
        'original_runtime_libraries': original['adjacent_runtime_libraries_sha256'],
        'original_template_probe': original['native_template_probe'],
        'original_control_cases': original['control_cases'],
        'client_timeout_seconds': timeout,
        'worker_configuration_timeout_seconds': signature['settings']['timeout'],
        'inference_request_parameters_changed': False,
        'selection_rule': 'last attempt is failed TimeoutError with no raw_transport',
        'requests': selected_timeouts(state, original),
        'recovery_code_sha256': file_sha(Path(__file__)),
        'recovery_producer_sha256': recovery_producer_hashes(),
        'shadow': True, 'rules_changed': 0, 'automatic_production_approval': False}
    output['recovery_plan_sha256'] = digest(output)
    return output


def load_producer(root):
    root = Path(root).resolve()
    sys.path.insert(0, str(root / 'scripts'))
    core = importlib.import_module('evaluate_privacy_policy')
    require(Path(core.__file__).resolve() == root / 'scripts/evaluate_privacy_policy.py'
            and core.ROOT.resolve() == root, 'producer_module_collision')
    return core


def execute_recovery(args, plan):
    require(not runtime_processes(args.runtime), 'runtime_busy_no_parallel_inference')
    core = load_producer(plan['producer_root'])
    signature = plan['original_signature']
    settings = signature['settings']
    artifact = signature['model_artifact']
    model_path = args.model_dir / artifact[0]
    require(model_path.stat().st_size == artifact[1] and file_sha(model_path) == artifact[2],
            'model_checksum_mismatch')
    require(file_sha(args.runtime) == settings['expected_runtime_sha256'], 'runtime_checksum_mismatch')
    libraries = {path.relative_to(args.runtime.parent).as_posix(): file_sha(path)
                 for path in sorted(args.runtime.parent.rglob('*.so*')) if path.is_file()}
    require(libraries == plan['original_runtime_libraries'], 'runtime_libraries_changed')
    _, expected, public, all_sources, _ = core.load_corpus()
    sources = {name: all_sources[name] for name in core.DOCUMENTS}
    cases, data = core.prepare_cases(core.ROOT / 'tests/fixtures/context-control-candidates.json',
                                    sources, expected, public, 32)
    require(data == plan['original_control_cases'], 'original_control_data_changed')
    _, _, kinds = core.prompts_and_kinds()
    indexed = {case.id: case for case in cases}
    local_args = SimpleNamespace(**settings)
    local_args.model_dir, local_args.runtime = args.model_dir, args.runtime
    local_args.timeout = plan['client_timeout_seconds']
    result_path = args.output / 'recovery-result.json'
    require(not result_path.exists(), 'recovery_attempt_exists_no_automatic_replay')
    state = {'version': 1, 'experiment_kind': 'transport-recovery-v1',
        'recovery_plan_sha256': plan['recovery_plan_sha256'], 'started_at_utc': utc(),
        'status': 'initializing', 'responses': {}, 'calls': [], 'batches': [],
        'shadow': True, 'rules_changed': 0, 'original_files_changed': False,
        'hardware': core.hardware_snapshot(), 'runtime_starts': [], 'capability_probes': []}
    def save():
        atomic_json(result_path, state)
    save()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='kaponir-privacy-transport-recovery-') as temporary:
        home = Path(temporary)
        worker = core.Worker(home, core.configuration(home, {
            'model_dir': str(args.model_dir), 'runtime': str(args.runtime), 'threads': 2,
            # The direct HTTP call below owns the longer client wait. Keep the
            # original Worker's OCR configuration, including its validated cap.
            'ocr_timeout_seconds': settings['timeout']}), 'unused')
        core.observe_runtime_starts(worker, state['runtime_starts'])
        sampler = core.ProcessSampler(lambda: core.worker_pids(worker)).start()
        original_probe = worker.detect_json_schema
        def probe():
            before = time.monotonic()
            try:
                return original_probe()
            finally:
                state['capability_probes'].append({'measurement_request': False,
                    'contains_control_data': False, 'physical_generation_requests': 1,
                    'supported': worker.process_supports_json_schema,
                    'seconds': time.monotonic() - before})
        worker.detect_json_schema = probe
        try:
            client = core.ControlledClient(worker, local_args)
            client.configure_budget('bounded')
            require(settings['response_format'] != 'grammar'
                    or worker.process_supports_json_schema is not False, 'native_grammar_unavailable')
            probes = {}
            for mode in ('off', 'on'):
                rendered = core.http_json(worker.ocr_base + '/apply-template', {
                    'messages': [{'role': 'user', 'content': 'Public reference text. Return JSON.'}],
                    'chat_template_kwargs': {'enable_thinking': mode == 'on'},
                    'add_generation_prompt': True})['prompt']
                probes[mode] = {'formatted_sha256': hashlib.sha256(rendered.encode()).hexdigest(),
                                'tokens': worker.count_text_tokens(rendered)}
            require(probes == plan['original_template_probe'], 'native_template_changed')
            state.update(status='running', native_template_probe=probes)
            save()
            for selected in plan['requests']:
                request = selected['request']
                require(digest(request) == selected['request_sha256'], 'recovery_request_changed')
                rendered = core.http_json(worker.ocr_base + '/apply-template', {
                    'messages': request['messages'], 'chat_template_kwargs': request['chat_template_kwargs'],
                    'add_generation_prompt': True})['prompt']
                require(hashlib.sha256(rendered.encode()).hexdigest() == selected['rendered_prompt_sha256']
                        and worker.count_text_tokens(rendered) == selected['input_tokens'],
                        'native_request_rendering_changed')
                entry = core.perform_request(state, save, worker, local_args, selected,
                                             request['max_tokens'], selected['input_tokens'])
                require(entry['request_sha256'] == selected['request_sha256'], 'actual_recovery_request_changed')
                reply, error = core.parse_response(entry)
                batch = [indexed[ident] for ident in selected['case_ids']]
                _, contexts = core.payload(selected['variant'], batch, sources)
                decisions = ({case.id: core.technical_review(case, error) for case in batch} if error
                    else core.validate(reply, batch, contexts, kinds, selected['variant']))
                state['batches'].append({'variant': selected['variant'], 'batch_index': selected['batch_index'],
                    'original_response_key': selected['original_response_key'],
                    'request_sha256': selected['request_sha256'], 'decisions': decisions,
                    'metrics_selected_cases_only': core.metrics(batch, decisions, expected, selected['variant'] != 'D')})
                save()
            state.update(status='completed', technical_incomplete=any(
                row['metrics_selected_cases_only']['technical_review'] for row in state['batches']))
        except BaseException as exc:
            state.update(status='failed', error=type(exc).__name__, reason=str(exc)[:200])
            raise
        finally:
            cleanup = time.monotonic()
            try:
                worker.stop.set()
                worker.stop_runtime()
            finally:
                state.update(runtime_cleanup_seconds=time.monotonic() - cleanup,
                    sampled_resources=sampler.stop(), execution_wall_seconds=time.monotonic() - started,
                    finished_at_utc=utc(),
                    original_files_unchanged=(file_sha(Path(plan['original_result_path'])) == plan['original_result_sha256']
                        and file_sha(Path(plan['original_plan_path'])) == plan['original_plan_file_sha256']))
                save()


def run_one(args):
    plan = make_recovery_plan(args.result, args.producer_root, args.timeout)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / 'recovery-plan.json'
    if target.exists():
        require(strict_json(target.read_text()) == plan, 'recovery_plan_changed_use_new_output')
    else:
        atomic_json(target, plan)
    if args.prepare_only or not plan['requests']:
        print(json.dumps({'status': 'prepared' if args.prepare_only else 'no_timeouts_to_recover',
            'new_inference_calls': 0, 'eligible_batches': len(plan['requests']),
            'recovery_plan_sha256': plan['recovery_plan_sha256']}), flush=True)
        return
    require(args.model_dir is not None and args.runtime is not None, 'runtime_and_models_required')
    execute_recovery(args, plan)


def suite_ready(state, runtime):
    require(state.get('status') != 'blocked', 'original_suite_blocked')
    if state.get('status') not in ('completed', 'completed_with_failures'):
        return False
    controller = state.get('controller', {})
    identity = process_identity(controller.get('pid', -1))
    if identity and identity['start_ticks'] == controller.get('start_ticks') and identity['state'] != 'Z':
        return False
    return not runtime_processes(runtime)


def run_queue(args):
    config = strict_json(args.queue.read_text())
    require(config['recovery_producer_sha256'] == recovery_producer_hashes(), 'recovery_queue_producer_changed')
    output = Path(config['output']).resolve()
    output.mkdir(parents=True, exist_ok=True)
    status_path = output / 'status.json'
    with SuiteLock(output / '.queue.lock'):
        require(not status_path.exists(), 'queue_attempt_exists_inspect_before_restart')
        state = {'status': 'waiting_original_suite', 'started_at_utc': utc(),
                 'config_sha256': digest(config), 'runs': [], 'controller_pid': os.getpid()}
        atomic_json(status_path, state)
        try:
            while True:
                suite = strict_json(Path(config['wait_suite']).read_text())
                if suite_ready(suite, config['runtime']):
                    break
                time.sleep(15)
            with SuiteLock(Path(config['wait_suite']).parent / '.suite.lock'):
                require(suite_ready(strict_json(Path(config['wait_suite']).read_text()), config['runtime']),
                        'original_suite_became_active')
                for item in config['runs']:
                    require(config['recovery_producer_sha256'] == recovery_producer_hashes(), 'recovery_queue_producer_changed')
                    require(not runtime_processes(config['runtime']), 'runtime_busy_no_parallel_inference')
                    command = [sys.executable, str(Path(__file__).resolve()), '--result', item['result'],
                        '--producer-root', item['producer_root'], '--output', item['output'],
                        '--model-dir', config['model_dir'], '--runtime', config['runtime'],
                        '--timeout', str(config['timeout'])]
                    row = {'result': item['result'], 'output': item['output'], 'status': 'launching', 'command': command}
                    state.update(status='running')
                    state['runs'].append(row)
                    atomic_json(status_path, state)
                    log_path = Path(item['output']).parent / (Path(item['output']).name + '-launch.log')
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open('a') as log:
                        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
                        row.update(status='running', pid=child.pid,
                                   identity=process_identity(child.pid), log=str(log_path))
                        atomic_json(status_path, state)
                        code = child.wait()
                    row.update(status='completed' if code == 0 else 'failed', exit_code=code, finished_at_utc=utc())
                    atomic_json(status_path, state)
                state['status'] = 'completed_with_failures' if any(row['exit_code'] for row in state['runs']) else 'completed'
                state['finished_at_utc'] = utc()
        except BaseException as exc:
            state.update(status='blocked', error=type(exc).__name__, reason=str(exc)[:200])
            raise
        finally:
            atomic_json(status_path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--result', type=Path)
    source.add_argument('--queue', type=Path)
    parser.add_argument('--producer-root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.queue:
        require(not args.prepare_only, 'queue_is_execution_not_preparation')
        run_queue(args)
    else:
        require(args.producer_root is not None and args.output is not None, 'producer_and_output_required')
        require(args.output.resolve() != args.result.resolve().parent, 'supplement_must_have_separate_output')
        if args.model_dir:
            args.model_dir = args.model_dir.resolve()
        if args.runtime:
            args.runtime = args.runtime.resolve()
        args.output.mkdir(parents=True, exist_ok=True)
        with SuiteLock(args.output / '.recovery.lock'):
            run_one(args)


if __name__ == '__main__':
    main()
