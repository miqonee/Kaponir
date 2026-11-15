#!/usr/bin/env python3
"""Run the six remaining phase2 models sequentially after durable Gemma cleanup.

Only explicit immutable producer roots are used. Failed attempts are preserved;
the controller never kills a process or automatically replays an uncertain launch.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


MODEL_ORDER = ('gemma4-e4b-qat', 'qwen3.5-2b', 'qwen3.5-0.8b',
               'huihui-qwen3.5-2b-abliterated', 'minicpm5-2b', 'granite42-3b', 'nanbeige42-3b')
VARIANTS = ('A', 'B', 'C', 'D')
REQUEST_FIELDS = ('variant', 'batch_index', 'case_ids', 'request', 'request_sha256',
                  'prompt_sha256', 'input_sha256', 'schema_sha256')
CLI_FIELDS = ('context_tokens', 'output_tokens', 'retry_output_tokens', 'reserve_tokens',
              'reasoning_budget', 'batch_size', 'top_p', 'min_p', 'seed', 'temperature',
              'top_k', 'timeout', 'response_format')
DONE = {'completed', 'completed_technical_incomplete'}


class SuiteBlocked(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError):
        raise SuiteBlocked('invalid_json_file:' + str(path)) from None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def process_identity(pid):
    try:
        raw = Path('/proc', str(pid), 'stat').read_text()
        fields = raw[raw.rindex(')') + 2:].split()
        return {'start_ticks': int(fields[19]), 'pgrp': int(fields[2]), 'state': fields[0]}
    except (OSError, ValueError, IndexError):
        return None


def process_group_alive(pgid):
    if type(pgid) is not int or pgid <= 0:
        return False
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            identity = process_identity(int(path.name))
            if identity and identity['pgrp'] == pgid and identity['state'] != 'Z':
                return True
    return False


def runtime_processes(runtime):
    found = []
    target = str(Path(runtime).resolve())
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            command = (path / 'cmdline').read_bytes().split(b'\0')
            if command and command[0] and str(Path(os.fsdecode(command[0])).resolve()) == target:
                found.append(int(path.name))
        except (OSError, ValueError):
            continue
    return found


class SuiteLock:
    def __init__(self, path):
        self.path, self.handle = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise SuiteBlocked('suite_controller_already_running') from exc
        return self

    def __exit__(self, *_):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def load_profiles(root):
    path = Path(root) / 'docs/experiments/phase2-model-profiles.json'
    source = read_json(path)
    if (not isinstance(source, dict) or source.get('version') != 1
            or [row.get('model') for row in source.get('models', [])] != list(MODEL_ORDER)):
        raise SuiteBlocked('invalid_suite_profiles')
    common = source['common']
    if any(common.get(key) != wanted for key, wanted in
           {'thinking': 'bounded', 'threads': 2, 'slots': 1, 'gpu_layers': 0}.items()):
        raise SuiteBlocked('runner_fixed_execution_profile_mismatch')
    return [{**common, **row} for row in source['models']], file_sha(path)


def snapshot_inputs(root):
    paths = [root / name for name in [
        'docs/experiments/phase2-model-profiles.json', 'docs/experiments/privacy-policy-v1.txt',
        'tests/fixtures/context-control-candidates.json', 'tests/docs/expected.json',
        'crates/kaponir-core/src/kinds.rs', 'benchmark/data/models.json',
        'benchmark/data/planned-models.json', 'benchmark/data/validation/manifest.json']]
    paths.extend((root / 'tests/docs/input').glob('*'))
    paths.extend((root / 'benchmark/data/validation').glob('context-controls--*--result.json.gz'))
    return {path.relative_to(root).as_posix(): file_sha(path) for path in sorted(set(paths)) if path.is_file()}


def command_for(args, profile, output, prepare=False):
    command = [sys.executable, '-u', str(args.runner_root / 'scripts/evaluate_privacy_policy.py'),
        '--model', profile['model'], '--model-dir', str(args.model_dir), '--runtime', str(args.runtime),
        '--policy', str(args.runner_root / 'docs/experiments/privacy-policy-v1.txt'),
        '--cases', str(args.runner_root / 'tests/fixtures/context-control-candidates.json'),
        '--expected-runtime-sha256', profile['runtime_sha256'], '--output', str(output)]
    for field in CLI_FIELDS:
        command.extend(['--' + field.replace('_', '-'), str(profile[field])])
    if prepare:
        command.append('--prepare-only')
    return command


def prepare_signature(args, profile):
    with tempfile.TemporaryDirectory(prefix='.planning-', dir=args.suite_output) as temporary:
        output = Path(temporary)
        prepared = subprocess.run(command_for(args, profile, output, prepare=True),
            cwd=args.runner_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if prepared.returncode:
            raise SuiteBlocked('offline_profile_preparation_failed:' + profile['model'])
        plan = read_json(output / 'plan.json')
        if not isinstance(plan, dict) or not isinstance(plan.get('signature'), dict):
            raise SuiteBlocked('offline_profile_plan_missing:' + profile['model'])
        return plan['signature']


def cleanup_durable(state):
    sessions = state.get('execution_sessions') if isinstance(state, dict) else None
    return bool(isinstance(sessions, list) and sessions
        and sessions[-1].get('finished_at_utc')
        and isinstance(sessions[-1].get('phases_seconds', {}).get('runtime_cleanup'), (int, float)))


def validate_result(result_path, profile, producer_root, expected_signature=None):
    state = read_json(result_path)
    if state is None:
        return None
    plan = read_json(Path(result_path).parent / 'plan.json')
    try:
        signature = plan['signature']
        if signature['settings']['model'] != profile['model']:
            raise ValueError()
        for field in CLI_FIELDS:
            if signature['settings'][field] != profile[field]:
                raise ValueError()
        if signature['settings']['expected_runtime_sha256'] != profile['runtime_sha256']:
            raise ValueError()
        if expected_signature is not None and signature != expected_signature:
            raise ValueError()
        producers = signature['code_sha256']
        if not producers or any(file_sha(Path(producer_root) / 'scripts' / name) != wanted
                                for name, wanted in producers.items()):
            raise ValueError()
        canonical = [{key: row[key] for key in REQUEST_FIELDS} for row in plan['requests']]
        actual_hash = digest({'signature': signature, 'batches': plan['batches'], 'requests': canonical})
        if actual_hash != plan['plan_sha256'] or actual_hash != state['signature']['plan_sha256']:
            raise ValueError()
        if state['signature']['runtime_sha256'] != profile['runtime_sha256']:
            raise ValueError()
        if state['signature']['model_sha256'] != signature['model_artifact'][2]:
            raise ValueError()
        cases = plan['control_cases']
        ids = {row['id'] for row in cases['candidates']}
        if len(ids) != 32 or signature['case_data_sha256'] != digest(cases):
            raise ValueError()
        if state.get('status') == 'completed':
            if plan['variant_order'] != list(VARIANTS) or not all(
                    set(state['stages'][variant]['decisions']) == ids for variant in VARIANTS):
                raise ValueError()
    except (KeyError, TypeError, ValueError, OSError):
        raise SuiteBlocked('existing_result_profile_mismatch:' + profile['model']) from None
    return state


class SuiteController:
    def __init__(self, args, *, launch=subprocess.Popen, prepare=prepare_signature,
                 wait=time.sleep, identity=process_identity, group_alive=process_group_alive,
                 find_runtimes=runtime_processes, report_run=subprocess.run):
        self.args, self.launch, self.prepare, self.wait = args, launch, prepare, wait
        self.identity, self.group_alive = identity, group_alive
        self.find_runtimes, self.report_run = find_runtimes, report_run
        args.suite_output.mkdir(parents=True, exist_ok=True)
        self.profiles, profile_hash = load_profiles(args.runner_root)
        source_hashes = {path.name: file_sha(path) for path in sorted((args.runner_root / 'scripts').glob('*.py'))}
        signature = {'runner_root': str(args.runner_root), 'producer_code_sha256': source_hashes,
            'producer_inputs_sha256': snapshot_inputs(args.runner_root),
            'profile_source_sha256': profile_hash, 'model_dir': str(args.model_dir),
            'runtime': str(args.runtime), 'runtime_sha256': file_sha(args.runtime),
            'gemma_result': str(args.gemma_result), 'gemma_producer_root': str(args.gemma_producer_root)}
        if signature['runtime_sha256'] != self.profiles[0]['runtime_sha256']:
            raise SuiteBlocked('suite_runtime_checksum_mismatch')
        self.path = args.suite_output / 'status.json'
        previous = read_json(self.path)
        if previous is not None and previous.get('signature') != signature:
            raise SuiteBlocked('suite_signature_changed_use_new_output')
        self.state = previous or {'version': 1, 'signature': signature, 'status': 'planned', 'models': {}}
        owner = self.identity(os.getpid())
        self.state['controller'] = {'pid': os.getpid(), 'start_ticks': owner.get('start_ticks') if owner else None,
                                    'started_at_utc': utc()}
        if args.gemma_pid is not None:
            found = self.identity(args.gemma_pid)
            ticks = args.gemma_start_ticks if args.gemma_start_ticks is not None else (
                found.get('start_ticks') if found else None)
            self.state['gemma_monitor'] = {'pid': args.gemma_pid, 'start_ticks': ticks}
        self.report_versions = self.state.get('report_source_versions')

    def check_snapshot(self):
        code = {path.name: file_sha(path) for path in sorted((self.args.runner_root / 'scripts').glob('*.py'))}
        if (code != self.state['signature']['producer_code_sha256']
                or snapshot_inputs(self.args.runner_root) != self.state['signature']['producer_inputs_sha256']
                or file_sha(self.args.runtime) != self.state['signature']['runtime_sha256']):
            self.block('immutable_runner_snapshot_changed')

    def save(self, status=None):
        if status is not None:
            self.state['status'] = status
        self.state['updated_at_utc'] = utc()
        atomic_json(self.path, self.state)

    def event(self, name, **fields):
        with (self.args.suite_output / 'suite.log').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'utc': utc(), 'event': name, **fields}, ensure_ascii=False) + '\n')

    def block(self, reason):
        self.state['blocked_reason'] = reason
        self.save('blocked')
        self.event('blocked', reason=reason)
        raise SuiteBlocked(reason)

    def no_runtime_left(self, allow_recorded_child=False):
        pids = self.find_runtimes(self.args.runtime)
        if pids and allow_recorded_child:
            allowed = set()
            for row in self.state['models'].values():
                if row.get('status') != 'running' or not row.get('pid') or row.get('start_ticks') is None:
                    continue
                current = self.identity(row['pid'])
                if current and current.get('start_ticks') == row['start_ticks'] and current.get('state') != 'Z':
                    allowed.add(row['process_group'])
            if len(allowed) > 1:
                self.block('multiple_recorded_model_process_groups')
            pids = [pid for pid in pids if (self.identity(pid) or {}).get('pgrp') not in allowed]
        if pids:
            self.state['unmanaged_runtime_pids'] = pids
            self.block('runtime_still_alive_no_automatic_kill')

    def wait_gemma(self):
        self.save('waiting_gemma')
        while True:
            self.refresh_reports_if_changed()
            result = validate_result(self.args.gemma_result, self.profiles[0], self.args.gemma_producer_root)
            if result is not None and cleanup_durable(result):
                if result.get('status') != 'completed':
                    self.block('gemma_failed_before_suite_start')
                self.no_runtime_left(allow_recorded_child=True)
                self.state['gemma'] = {'status': 'completed', 'cleanup_durable': True,
                    'result': str(self.args.gemma_result), 'plan_sha256': result['signature']['plan_sha256'],
                    'technical_incomplete': bool(result.get('technical_incomplete'))}
                self.save()
                self.event('gemma_cleanup_confirmed')
                return
            monitor = self.state.get('gemma_monitor')
            if monitor:
                identity = self.identity(monitor['pid'])
                if (identity is None or identity.get('state') == 'Z'
                        or monitor['start_ticks'] is None
                        or identity.get('start_ticks') != monitor['start_ticks']):
                    self.block('gemma_process_disappeared_before_durable_cleanup')
            self.wait(self.args.poll_seconds)

    def report_results(self):
        results = [str(self.args.gemma_result)] if self.args.gemma_result.is_file() else []
        results.extend(row['result'] for row in self.state['models'].values()
                       if row.get('status') in DONE | {'running'} and Path(row['result']).is_file())
        return results

    def refresh_reports_if_changed(self):
        results = self.report_results()
        versions = {}
        for result in results:
            try:
                stat = Path(result).stat()
                versions[result] = [stat.st_mtime_ns, stat.st_size]
            except FileNotFoundError:
                return  # Atomic replacement raced with inspection; next poll retries.
        if not versions or versions == self.report_versions:
            return
        self.report_versions = versions
        self.state['report_source_versions'] = versions
        self.update_reports(results)

    def update_reports(self, results):
        script = self.args.runner_root / 'scripts/summarize_privacy_policy.py'
        if not script.is_file():
            self.state['reports'] = {'status': 'pending', 'reason': 'summarizer_not_present'}
            self.event('report_pending', reason='summarizer_not_present')
            self.save()
            return
        output = self.args.suite_output / 'reports'
        command = [sys.executable, '-u', str(script), '--results', *results, '--output-dir', str(output)]
        try:
            with (self.args.suite_output / 'reports.log').open('a') as log:
                reply = self.report_run(command, cwd=self.args.runner_root, stdout=log, stderr=subprocess.STDOUT)
        except Exception as exc:
            self.state['reports'] = {'status': 'failed', 'error': type(exc).__name__, 'results': results}
            self.event('report_error', error=type(exc).__name__)
            self.save()
            return
        self.state['reports'] = {'status': 'updated' if reply.returncode == 0 else 'failed',
                                 'returncode': reply.returncode, 'output': str(output), 'results': results}
        self.event('report_finished', returncode=reply.returncode)
        self.save()

    def finish_child(self, row, profile, expected, child=None):
        while True:
            self.refresh_reports_if_changed()
            if child is not None:
                code = child.poll()
                exited = code is not None
            else:
                current = self.identity(row['pid'])
                exited = current is None or current.get('state') == 'Z'
                if current and row.get('start_ticks') is not None and current['start_ticks'] != row['start_ticks']:
                    self.block('recorded_child_pid_reused')
                if current and row.get('start_ticks') is None:
                    self.block('recorded_child_identity_unknown')
                code = None
            if not exited:
                self.wait(self.args.poll_seconds)
                continue
            if self.group_alive(row['process_group']):
                self.block('child_exited_with_orphan_process_group')
            self.no_runtime_left()
            result = validate_result(Path(row['result']), profile, self.args.runner_root, expected)
            row.update(exit_code=code, finished_at_utc=utc())
            if result is not None and result.get('status') == 'completed' and cleanup_durable(result):
                incomplete = bool(result.get('technical_incomplete')) or code not in (None, 0)
                row.update(status='completed_technical_incomplete' if incomplete else 'completed',
                           cleanup_durable=True, plan_sha256=result['signature']['plan_sha256'],
                           technical_incomplete=incomplete)
            else:
                row.update(status='failed', reason='runner_failed_or_cleanup_not_durable',
                           result_status=result.get('status') if result else 'missing')
            self.save('running')
            self.event('model_finished', model=profile['model'], status=row['status'], exit_code=code)
            if row['status'] in DONE:
                self.refresh_reports_if_changed()
            return

    def run_model(self, profile, index):
        self.check_snapshot()
        model = profile['model']
        output = self.args.suite_output / f'{index:02d}-{model}'
        expected = self.prepare(self.args, profile)
        expected_hash = digest(expected)
        row = self.state['models'].get(model)
        if row and row.get('expected_signature_sha256') != expected_hash:
            self.block('recorded_model_profile_changed:' + model)
        existing = validate_result(output / 'result.json', profile, self.args.runner_root, expected)
        if row and row.get('status') == 'running':
            if not row.get('pid'):
                self.block('running_child_identity_missing:' + model)
            self.finish_child(row, profile, expected)
            return
        if existing is not None and existing.get('status') == 'completed' and cleanup_durable(existing):
            self.no_runtime_left()
            if row and row.get('process_group') and self.group_alive(row['process_group']):
                self.block('completed_result_has_live_process_group')
            incomplete = bool(existing.get('technical_incomplete'))
            self.state['models'][model] = {**(row or {}), 'status': 'completed_technical_incomplete' if incomplete else 'completed',
                'output': str(output), 'result': str(output / 'result.json'), 'cleanup_durable': True,
                'expected_signature_sha256': expected_hash, 'technical_incomplete': incomplete,
                'plan_sha256': existing['signature']['plan_sha256']}
            self.save('running')
            self.event('model_result_reused_without_inference', model=model)
            self.refresh_reports_if_changed()
            return
        if row and row.get('status') == 'launching':
            self.block('launch_outcome_unknown_no_automatic_replay:' + model)
        if row and row.get('status') in DONE:
            self.block('recorded_completed_result_missing_or_incomplete:' + model)
        if row and row.get('status') == 'failed':
            self.event('recorded_terminal_attempt_preserved', model=model, status=row['status'])
            return
        if existing is not None:
            self.block('unmanaged_incomplete_result_no_automatic_resume:' + model)
        self.no_runtime_left()
        output.mkdir(parents=True, exist_ok=True)
        command = command_for(self.args, profile, output)
        row = {'status': 'launching', 'output': str(output), 'result': str(output / 'result.json'),
            'log': str(output / 'runner.log'), 'command': command, 'started_at_utc': utc(),
            'expected_signature_sha256': expected_hash}
        self.state['models'][model] = row
        self.save('running')
        with Path(row['log']).open('a') as log:
            try:
                child = self.launch(command, cwd=self.args.runner_root, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except Exception as exc:
                row.update(status='failed', reason='child_launch_failed', error=type(exc).__name__)
                self.save()
                self.event('child_launch_failed', model=model, error=type(exc).__name__)
                return
            identity = self.identity(child.pid)
            row.update(status='running', pid=child.pid, process_group=child.pid,
                       start_ticks=identity.get('start_ticks') if identity else None)
            self.save()
            self.event('model_started', model=model, pid=child.pid)
            self.finish_child(row, profile, expected, child)

    def run(self):
        try:
            self.state.pop('blocked_reason', None)
            self.check_snapshot()
            self.wait_gemma()
            self.refresh_reports_if_changed()
            for index, profile in enumerate(self.profiles[1:], 1):
                self.run_model(profile, index)
            self.state['finished_at_utc'] = utc()
            failed = any(row.get('status') != 'completed' for row in self.state['models'].values())
            failed = failed or self.state['gemma']['technical_incomplete']
            failed = failed or self.state.get('reports', {}).get('status') == 'failed'
            self.save('completed_with_failures' if failed else 'completed')
            self.event('suite_finished', status=self.state['status'])
            return self.state['status']
        except SuiteBlocked as exc:
            if self.state.get('status') != 'blocked':
                self.block(str(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('runner-root', 'model-dir', 'runtime', 'suite-output', 'gemma-result', 'gemma-producer-root'):
        parser.add_argument('--' + option, type=Path, required=True)
    parser.add_argument('--poll-seconds', type=float, default=10)
    parser.add_argument('--gemma-pid', type=int)
    parser.add_argument('--gemma-start-ticks', type=int)
    args = parser.parse_args()
    if not 10 <= args.poll_seconds <= 60 or args.gemma_pid is not None and args.gemma_pid <= 0:
        parser.error('poll interval must be in [10,60]; PID must be positive')
    for key in ('runner_root', 'model_dir', 'runtime', 'suite_output', 'gemma_result', 'gemma_producer_root'):
        setattr(args, key, getattr(args, key).resolve())
    try:
        with SuiteLock(args.suite_output / '.suite.lock'):
            controller = SuiteController(args)
            status = controller.run()
    except SuiteBlocked as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}), flush=True)
        return 2
    except KeyboardInterrupt:
        if 'controller' in locals():
            controller.state['interrupted_reason'] = 'controller_interrupted_child_processes_not_killed'
            controller.save('interrupted')
        print(json.dumps({'status': 'interrupted', 'child_processes_not_killed': True}), flush=True)
        return 130
    print(json.dumps({'status': status, 'status_file': str(args.suite_output / 'status.json')}), flush=True)
    return 0 if status == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
