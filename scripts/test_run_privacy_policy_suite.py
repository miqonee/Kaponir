import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

import run_privacy_policy_suite as suite


class SuiteControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / 'runner'
        self.frozen = self.home / 'gemma-producer'
        for root in (self.root, self.frozen):
            (root / 'scripts').mkdir(parents=True)
            (root / 'scripts/evaluate_privacy_policy.py').write_text('# Test producer only\n')
        (self.root / 'docs/experiments').mkdir(parents=True)
        source_path = Path(__file__).resolve().parents[1] / 'docs/experiments/phase2-model-profiles.json'
        source = json.loads(source_path.read_text())
        self.runtime = self.home / 'llama-server'
        self.runtime.write_bytes(b'never executed')
        source['common']['runtime_sha256'] = suite.file_sha(self.runtime)
        (self.root / 'docs/experiments/phase2-model-profiles.json').write_text(json.dumps(source))
        (self.root / 'scripts/summarize_privacy_policy.py').write_text('# Mock reporter only\n')
        self.args = argparse.Namespace(runner_root=self.root, model_dir=self.home / 'models',
            runtime=self.runtime, suite_output=self.home / 'suite',
            gemma_result=self.home / 'gemma/result.json', gemma_producer_root=self.frozen,
            poll_seconds=10, gemma_pid=None, gemma_start_ticks=None)
        self.profiles = suite.load_profiles(self.root)[0]
        self.active = {}
        self.launched, self.reported, self.waits = [], [], []
        self.fail_model = self.technical_model = None
        self.orphan_group = None
        self.write_result(self.args.gemma_result.parent, self.profiles[0], self.frozen)

    def cases(self):
        return {'reference_private': 16, 'reference_public': 16,
                'candidates': [{'id': f'{index:024x}'} for index in range(32)]}

    def expected(self, args, profile):
        return {'settings': {'model': profile['model'],
            'expected_runtime_sha256': profile['runtime_sha256'],
            **{key: profile[key] for key in suite.CLI_FIELDS}},
            'code_sha256': {'evaluate_privacy_policy.py': suite.file_sha(
                args.runner_root / 'scripts/evaluate_privacy_policy.py')},
            'case_data_sha256': suite.digest(self.cases()),
            'model_artifact': ['test-only.gguf', 1, 'a' * 64]}

    def write_result(self, output, profile, producer, *, status='completed', cleanup=True, technical=False):
        output.mkdir(parents=True, exist_ok=True)
        args = copy.copy(self.args)
        args.runner_root = producer
        signature = self.expected(args, profile)
        ids = [case['id'] for case in self.cases()['candidates']]
        batches = [ids[:8], ids[8:13], ids[13:21], ids[21:26], ids[26:]]
        requests = []
        for variant in suite.VARIANTS:
            for index, batch in enumerate(batches):
                request = {'model': profile['model'], 'messages': [], 'max_tokens': profile['output_tokens']}
                requests.append({'variant': variant, 'batch_index': index, 'case_ids': batch,
                    'request': request, 'request_sha256': suite.digest(request),
                    'prompt_sha256': 'test', 'input_sha256': 'test', 'schema_sha256': 'test'})
        plan = {'signature': signature, 'batches': batches, 'requests': requests,
                'control_cases': self.cases(), 'variant_order': list(suite.VARIANTS)}
        plan['plan_sha256'] = suite.digest({'signature': signature, 'batches': batches, 'requests': requests})
        result = {'version': 1, 'status': status,
            'signature': {'plan_sha256': plan['plan_sha256'], 'runtime_sha256': profile['runtime_sha256'],
                          'model_sha256': 'a' * 64},
            'stages': {variant: {'decisions': {ident: {'verdict': 'review', 'valid': True} for ident in ids}}
                       for variant in suite.VARIANTS},
            'technical_incomplete': technical, 'execution_sessions': []}
        if cleanup:
            result['execution_sessions'].append({'finished_at_utc': '2026-11-15T00:00:00Z',
                'phases_seconds': {'runtime_cleanup': 0.01}})
        suite.atomic_json(output / 'plan.json', plan)
        suite.atomic_json(output / 'result.json', result)
        return result

    def identity(self, pid):
        if pid in self.active:
            return {'start_ticks': pid + 100, 'pgrp': pid, 'state': 'R'}
        if pid >= 1900000 and pid - 1000000 in self.active:
            return {'start_ticks': pid + 100, 'pgrp': pid - 1000000, 'state': 'R'}
        return None

    def group_alive(self, pgid):
        return pgid in self.active or pgid == self.orphan_group

    def find_runtimes(self, runtime):
        return [pid + 1000000 for pid in self.active]

    def wait(self, seconds):
        self.waits.append(seconds)

    def launch(self, command, **kwargs):
        self.assertTrue(kwargs['start_new_session'])
        model = command[command.index('--model') + 1]
        output = Path(command[command.index('--output') + 1])
        self.assertFalse(self.active, 'A second runtime was launched concurrently')
        checkpoint = suite.read_json(self.args.suite_output / 'status.json')['models'][model]
        self.assertEqual(checkpoint['status'], 'launching')
        self.assertNotIn('pid', checkpoint)
        self.launched.append(model)
        pid = 900000 + len(self.launched)
        self.active[pid] = model
        owner = self
        class Child:
            def __init__(self):
                self.pid, self.polls = pid, 0
            def poll(self):
                self.polls += 1
                if self.polls == 1:
                    return None
                profile = next(row for row in owner.profiles if row['model'] == model)
                owner.write_result(output, profile, owner.root,
                    status='failed' if model == owner.fail_model else 'completed',
                    technical=model == owner.technical_model)
                owner.active.pop(pid, None)
                return 1 if model == owner.fail_model else 0
        return Child()

    def report(self, command, **kwargs):
        self.reported.append(command)
        return argparse.Namespace(returncode=0)

    def controller(self, **changes):
        deps = {'launch': self.launch, 'prepare': self.expected, 'wait': self.wait,
                'identity': self.identity, 'group_alive': self.group_alive,
                'find_runtimes': self.find_runtimes, 'report_run': self.report}
        deps.update(changes)
        return suite.SuiteController(self.args, **deps)

    def test_all_six_are_sequential_with_explicit_profiles_and_reports(self):
        controller = self.controller()
        self.assertEqual(controller.run(), 'completed')
        self.assertEqual(self.launched, list(suite.MODEL_ORDER[1:]))
        self.assertTrue(all(interval == 10 for interval in self.waits))
        for row in controller.state['models'].values():
            self.assertTrue(row['cleanup_durable'])
            self.assertEqual(row['status'], 'completed')
            command = row['command']
            self.assertEqual(command[command.index('--reasoning-budget') + 1], '2048')
            self.assertEqual(command[command.index('--context-tokens') + 1], '16384')
        nanbeige = controller.state['models']['nanbeige42-3b']['command']
        self.assertEqual(nanbeige[nanbeige.index('--response-format') + 1], 'prompt')
        self.assertGreaterEqual(len(self.reported), 7)
        final = self.reported[-1]
        self.assertIn(str(self.args.gemma_result), final)
        self.assertEqual(final.count('--results'), 1)

    def test_gemma_wait_requires_last_cleanup_and_refreshes_partial_report(self):
        self.write_result(self.args.gemma_result.parent, self.profiles[0], self.frozen, cleanup=False)
        def waiting(seconds):
            self.assertFalse(self.launched)
            self.assertTrue(self.reported, 'Partial Gemma report was not refreshed')
            self.write_result(self.args.gemma_result.parent, self.profiles[0], self.frozen)
        controller = self.controller(wait=waiting)
        controller.wait_gemma()
        self.assertEqual(controller.state['gemma']['status'], 'completed')
        self.assertFalse(self.launched)
        before = len(self.reported)
        controller.refresh_reports_if_changed()
        controller.refresh_reports_if_changed()
        self.assertEqual(len(self.reported), before)

    def test_gemma_dead_before_durable_cleanup_blocks_without_launch(self):
        self.args.gemma_pid, self.args.gemma_start_ticks = 3076, 501230
        self.write_result(self.args.gemma_result.parent, self.profiles[0], self.frozen,
                          status='running', cleanup=False)
        controller = self.controller()
        with self.assertRaisesRegex(suite.SuiteBlocked, 'gemma_process_disappeared'):
            controller.run()
        self.assertFalse(self.launched)
        self.assertEqual(suite.read_json(controller.path)['status'], 'blocked')

    def test_completed_results_are_reused_without_any_new_inference(self):
        self.controller().run()
        before = list(self.launched)
        self.assertEqual(self.controller().run(), 'completed')
        self.assertEqual(self.launched, before)

    def test_lost_completed_result_blocks_instead_of_claiming_completion(self):
        self.controller().run()
        result = self.args.suite_output / ('01-' + self.profiles[1]['model']) / 'result.json'
        result.unlink()
        controller = self.controller()
        with self.assertRaisesRegex(suite.SuiteBlocked, 'completed_result_missing'):
            controller.run()
        self.assertEqual(len(self.launched), 6)

    def test_snapshot_changes_during_run_block_the_next_profile(self):
        controller = self.controller()
        with (self.root / 'scripts/evaluate_privacy_policy.py').open('a') as source:
            source.write('# changed producer\n')
        with self.assertRaisesRegex(suite.SuiteBlocked, 'immutable_runner_snapshot_changed'):
            controller.run()
        self.assertFalse(self.launched)

    def test_failures_and_technical_incomplete_results_are_preserved(self):
        self.fail_model = 'qwen3.5-0.8b'
        self.technical_model = 'granite42-3b'
        controller = self.controller()
        self.assertEqual(controller.run(), 'completed_with_failures')
        self.assertEqual(len(self.launched), 6)
        self.assertEqual(controller.state['models'][self.fail_model]['status'], 'failed')
        self.assertEqual(controller.state['models'][self.technical_model]['status'], 'completed_technical_incomplete')
        self.controller().run()
        self.assertEqual(len(self.launched), 6)

    def test_uncertain_launch_slot_is_never_replayed(self):
        controller = self.controller()
        profile = self.profiles[1]
        controller.state['models'][profile['model']] = {'status': 'launching',
            'expected_signature_sha256': suite.digest(self.expected(self.args, profile))}
        controller.save()
        with self.assertRaisesRegex(suite.SuiteBlocked, 'launch_outcome_unknown'):
            controller.run()
        self.assertFalse(self.launched)

    def test_surviving_orphan_process_group_blocks_the_next_model(self):
        def orphan_launch(command, **kwargs):
            child = self.launch(command, **kwargs)
            self.orphan_group = child.pid
            return child
        controller = self.controller(launch=orphan_launch)
        with self.assertRaisesRegex(suite.SuiteBlocked, 'orphan_process_group'):
            controller.run()
        self.assertEqual(len(self.launched), 1)

    def test_resumed_known_child_is_monitored_without_relaunch(self):
        controller = self.controller()
        profile = self.profiles[1]
        pid = 900009
        output = self.args.suite_output / ('01-' + profile['model'])
        self.write_result(output, profile, self.root, status='running', cleanup=False)
        self.active[pid] = profile['model']
        controller.state['models'][profile['model']] = {'status': 'running', 'pid': pid,
            'start_ticks': pid + 100, 'process_group': pid, 'result': str(output / 'result.json'),
            'expected_signature_sha256': suite.digest(self.expected(self.args, profile))}
        controller.save()
        def waiting(seconds):
            if pid in self.active:
                self.write_result(output, profile, self.root)
                self.active.pop(pid)
        resumed = self.controller(wait=waiting)
        self.assertEqual(resumed.run(), 'completed')
        self.assertNotIn(profile['model'], self.launched)
        self.assertEqual(len(self.launched), 5)

    def test_changed_profile_result_blocks_and_does_not_overwrite(self):
        self.controller().run()
        path = self.args.suite_output / ('01-' + self.profiles[1]['model']) / 'plan.json'
        changed = suite.read_json(path)
        changed['signature']['settings']['temperature'] = 0.75
        suite.atomic_json(path, changed)
        controller = self.controller()
        with self.assertRaisesRegex(suite.SuiteBlocked, 'existing_result_profile_mismatch'):
            controller.run()
        self.assertEqual(len(self.launched), 6)
        self.assertEqual(suite.read_json(path)['signature']['settings']['temperature'], 0.75)
        self.assertEqual(suite.read_json(controller.path)['status'], 'blocked')

    def test_report_errors_are_separate_and_never_launch_before_gemma_cleanup(self):
        self.write_result(self.args.gemma_result.parent, self.profiles[0], self.frozen, cleanup=False)
        def broken_report(*args, **kwargs):
            raise OSError('mock report failure')
        def waiting(seconds):
            self.assertFalse(self.launched)
            self.write_result(self.args.gemma_result.parent, self.profiles[0], self.frozen)
        controller = self.controller(report_run=broken_report, wait=waiting)
        controller.wait_gemma()
        self.assertEqual(controller.state['reports']['status'], 'failed')
        self.assertEqual(controller.state['reports']['error'], 'OSError')
        self.assertFalse(self.launched)

    def test_missing_reporter_is_honestly_pending(self):
        (self.root / 'scripts/summarize_privacy_policy.py').unlink()
        controller = self.controller()
        controller.wait_gemma()
        self.assertEqual(controller.state['reports']['status'], 'pending')
        self.assertFalse(self.reported)

    def test_singleton_lock_excludes_second_controller(self):
        path = self.home / 'singleton.lock'
        with suite.SuiteLock(path):
            with self.assertRaisesRegex(suite.SuiteBlocked, 'already_running'):
                with suite.SuiteLock(path):
                    self.fail('Second suite lock was acquired')


if __name__ == '__main__':
    unittest.main()
