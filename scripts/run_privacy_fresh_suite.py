#!/usr/bin/env python3
"""Continue authorized research after original A/B/C/D and timeout supplements.

All four frozen tasks are measured on fresh controls for the seven old models,
then all six new models. Reports preserve uncertainty and native profile limits.
The controller never changes policy, switches contracts, kills another process,
or automatically replays a prior launch after restart.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from privacy_expansion_profiles import NEW_MODELS
from run_privacy_policy_suite import (MODEL_ORDER, SuiteLock, atomic_json, cleanup_durable,
    file_sha, process_group_alive, process_identity, runtime_processes, snapshot_inputs, utc)
from summarize_privacy_holdout import build_fresh_report, render_fresh
from summarize_privacy_policy import require, strict_json


def snapshot(root):
    root = Path(root).resolve()
    names = set(snapshot_inputs(root))
    names.update(path.relative_to(root).as_posix() for path in (root / 'scripts').glob('*.py'))
    names.add('docs/experiments/phase2-new-model-artifacts.json')
    names.update(path.relative_to(root).as_posix() for path in
                 (root / 'tests/fixtures/privacy-policy-holdout-v1').rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts)
    return {name: file_sha(root / name) for name in sorted(names)}


def dependency_ready(state, identity, runtime):
    require(state.get('status') != 'blocked', 'recovery_queue_blocked')
    if state.get('status') not in ('completed', 'completed_with_failures'):
        return False
    live = process_identity(identity['pid'])
    if live and live['start_ticks'] == identity['start_ticks'] and live['state'] != 'Z':
        return False
    return not runtime_processes(runtime)


def command_for(config, model, output):
    directory = config['old_model_dir'] if model in MODEL_ORDER else config['new_model_dir']
    return [sys.executable, '-u', str(Path(config['runner_root']) / 'scripts/evaluate_privacy_expansion.py'),
        '--model', model, '--profile', 'native', '--variants', 'A', 'B', 'C', 'D',
        '--timeout', '3600', '--model-dir', directory, '--runtime', config['runtime'], '--output', str(output)]


def reports(state, output, expected, name='reports'):
    paths = [Path(row['output']) / 'result.json' for row in state['models'].values()
             if (Path(row['output']) / 'result.json').is_file()]
    report = build_fresh_report(paths, list(expected))
    directory = output / name
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / 'report.json', report)
    (directory / 'report.md').write_text(render_fresh(report), encoding='utf-8')
    state['reports'] = {'output': str(directory), 'accepted_runs': len(report['runs']),
                        'rejected_inputs': report['rejected_inputs'], 'model_coverage': report['model_coverage']}


def run(config):
    root, output = Path(config['runner_root']).resolve(), Path(config['output']).resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'status.json'
    with SuiteLock(output / '.suite.lock'):
        require(not path.exists(), 'fresh_suite_attempt_exists_inspect_before_restart')
        require(snapshot(root) == config['producer_inputs_sha256'], 'fresh_producer_changed')
        state = {'version': 1, 'status': 'waiting_original_research', 'started_at_utc': utc(),
                 'models': {}, 'policy_changed': False, 'shadow': True, 'rules_changed': 0}
        atomic_json(path, state)
        try:
            while not dependency_ready(strict_json(Path(config['wait_recovery']).read_text()),
                                       config['recovery_controller'], config['runtime']):
                time.sleep(15)
            with SuiteLock(Path(config['original_suite_output']) / '.suite.lock'), \
                    SuiteLock(Path(config['wait_recovery']).parent / '.queue.lock'):
                require(dependency_ready(strict_json(Path(config['wait_recovery']).read_text()),
                    config['recovery_controller'], config['runtime']), 'upstream_became_active')
                for index, model in enumerate((*MODEL_ORDER, *NEW_MODELS)):
                    require(snapshot(root) == config['producer_inputs_sha256'], 'fresh_producer_changed')
                    require(not runtime_processes(config['runtime']), 'runtime_busy_no_parallel_inference')
                    if index == len(MODEL_ORDER):
                        reports(state, output, MODEL_ORDER, 'before-expansion')
                        atomic_json(path, state)
                        while True:
                            artifacts = strict_json(Path(config['new_artifacts_status']).read_text())
                            require(artifacts.get('status') != 'failed', 'new_artifact_download_failed')
                            if artifacts.get('status') == 'completed':
                                break
                            state['status'] = 'waiting_new_artifacts'
                            atomic_json(path, state)
                            time.sleep(15)
                    destination = output / f'{index:02d}-{model}'
                    require(not destination.exists(), 'fresh_model_output_exists_no_automatic_replay')
                    destination.mkdir()
                    command = command_for(config, model, destination)
                    row = {'status': 'launching', 'output': str(destination), 'command': command, 'started_at_utc': utc()}
                    state.update(status='running', current_model=model)
                    state['models'][model] = row
                    atomic_json(path, state)
                    with (destination / 'runner.log').open('a') as log:
                        child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                        row.update(status='running', pid=child.pid, identity=process_identity(child.pid), process_group=child.pid)
                        atomic_json(path, state)
                        version = None
                        while True:
                            try:
                                code = child.wait(timeout=15)
                                break
                            except subprocess.TimeoutExpired:
                                result = destination / 'result.json'
                                if result.exists():
                                    current = (result.stat().st_mtime_ns, result.stat().st_size)
                                    if current != version:
                                        reports(state, output, (*MODEL_ORDER, *NEW_MODELS))
                                        atomic_json(path, state)
                                        version = current
                    require(not process_group_alive(child.pid) and not runtime_processes(config['runtime']),
                            'child_exited_with_live_runtime')
                    result_path = destination / 'result.json'
                    result = strict_json(result_path.read_text()) if result_path.exists() else None
                    completed = code == 0 and result and result.get('status') == 'completed' and cleanup_durable(result)
                    row.update(status=('completed_technical_incomplete' if result.get('technical_incomplete') else 'completed')
                        if completed else 'failed', exit_code=code, finished_at_utc=utc(), cleanup_durable=bool(result and cleanup_durable(result)))
                    reports(state, output, (*MODEL_ORDER, *NEW_MODELS))
                    atomic_json(path, state)
                state.update(status='completed_with_failures' if any(row['status'] != 'completed' for row in state['models'].values())
                             else 'completed', finished_at_utc=utc())
        except BaseException as exc:
            state.update(status='blocked', error=type(exc).__name__, reason=str(exc)[:200])
            raise
        finally:
            atomic_json(path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    run(strict_json(args.config.read_text()))


if __name__ == '__main__':
    main()
