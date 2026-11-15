#!/usr/bin/env python3
"""Probe six new native families between Gemma cleanup and the old-model queue.

The launcher hands off only an idle coordinator, never the active Gemma process.
This supervisor waits for durable cleanup, runs unscored native preflights one at
a time, then restarts the exact frozen reference controller command.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from privacy_expansion_profiles import NEW_MODELS
from run_complete_research import producer_snapshot
from run_privacy_policy_suite import (SuiteLock, atomic_json, cleanup_durable, process_group_alive,
    process_identity, runtime_processes, utc)
from summarize_privacy_policy import digest, require, strict_json


def alive(owner):
    state = process_identity(owner['pid'])
    return bool(state and state['start_ticks'] == owner['start_ticks'] and state['state'] != 'Z')


def gemma_ready(config):
    state = strict_json(Path(config['gemma_result']).read_text())
    require(state.get('status') in ('running', 'completed'), 'gemma_failed_before_boundary')
    if state.get('status') != 'completed' or not cleanup_durable(state):
        require(alive(config['gemma_process']), 'gemma_disappeared_before_durable_cleanup')
        return False
    return not alive(config['gemma_process']) and not runtime_processes(config['runtime'])


def command_for(config, model, output):
    return [sys.executable, '-u', str(Path(config['runner_root']) / 'scripts/evaluate_privacy_expansion.py'),
        '--preflight-only', '--dataset-kind', 'holdout', '--model', model, '--profile', 'native',
        '--timeout', '3600', '--variants', 'A', 'B', 'C', 'D', '--model-dir', config['new_model_dir'],
        '--runtime', config['runtime'], '--output', str(output)]


def run(config):
    output = Path(config['output'])
    output.mkdir(parents=True, exist_ok=True)
    status = output / 'status.json'
    with SuiteLock(output / '.boundary.lock'):
        require(not status.exists(), 'boundary_attempt_exists_inspect_before_restart')
        require(producer_snapshot(config['runner_root']) == config['producer_inputs_sha256'], 'boundary_producer_changed')
        state = {'version': 1, 'status': 'waiting_reference_controller_exit', 'models': {},
            'controller': {'pid': os.getpid(), 'identity': process_identity(os.getpid())},
            'started_at_utc': utc(), 'config_sha256': digest(config), 'measurement_inference_calls': 0}
        def save(phase=None):
            if phase:
                state['status'] = phase
            state['updated_at_utc'] = utc()
            atomic_json(status, state)
        save()
        try:
            while alive(config['reference_controller']):
                time.sleep(5)
            reference = strict_json((Path(config['reference_suite_output']) / 'status.json').read_text())
            require(not reference['models'], 'reference_models_already_launched_boundary_missed')
            require(digest(reference['signature']) == config['reference_signature_sha256'], 'reference_signature_changed')
            with SuiteLock(Path(config['reference_suite_output']) / '.suite.lock'):
                save('waiting_gemma_cleanup')
                while not gemma_ready(config):
                    time.sleep(15)
                for model in NEW_MODELS:
                    require(producer_snapshot(config['runner_root']) == config['producer_inputs_sha256'], 'boundary_producer_changed')
                    require(not runtime_processes(config['runtime']), 'runtime_busy_no_parallel_preflight')
                    destination = output / model
                    require(not destination.exists(), 'preflight_output_exists_no_automatic_replay')
                    destination.mkdir()
                    command = command_for(config, model, destination)
                    row = {'status': 'launching', 'command': command, 'output': str(destination)}
                    state['models'][model] = row
                    state['current_model'] = model
                    save('native_preflight')
                    with (destination / 'runner.log').open('a') as log:
                        child = subprocess.Popen(command, cwd=config['runner_root'], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                        row.update(status='running', pid=child.pid, identity=process_identity(child.pid))
                        save()
                        code = child.wait()
                    require(not process_group_alive(child.pid) and not runtime_processes(config['runtime']),
                            'preflight_left_live_runtime_no_automatic_kill')
                    path = destination / 'preflight.json'
                    preflight = strict_json(path.read_text()) if path.is_file() else None
                    completed = code == 0 and preflight and preflight.get('status') == 'preflight_completed'
                    require(not completed or preflight.get('measurement_inference_calls') == 0, 'unexpected_scored_preflight')
                    row.update(status='completed' if completed else 'failed', exit_code=code, finished_at_utc=utc())
                    save()
                state.pop('current_model', None)
                save('native_preflights_finished')
            require(not runtime_processes(config['runtime']), 'runtime_busy_before_reference_restart')
            log_path = Path(config['reference_suite_output']) / 'controller.log'
            with log_path.open('a') as log:
                child = subprocess.Popen(config['reference_command'], cwd=config['reference_runner_root'],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            state['restarted_reference_controller'] = {'pid': child.pid, 'identity': process_identity(child.pid),
                                                       'command': config['reference_command']}
            state['finished_at_utc'] = utc()
            save('reference_restarted')
        except BaseException as exc:
            state.update(error=type(exc).__name__, blocked_reason=str(exc)[:240])
            save('blocked')
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    run(strict_json(args.config.read_text()))


if __name__ == '__main__':
    main()
