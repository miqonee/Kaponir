import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_privacy_fresh_suite as suite


class FreshSuiteTests(unittest.TestCase):
    def test_commands_keep_all_tasks_native_profiles_and_separate_model_directories(self):
        config = {'runner_root': '/producer', 'old_model_dir': '/old', 'new_model_dir': '/new', 'runtime': '/runtime'}
        old = suite.command_for(config, 'gemma4-e4b-qat', '/output')
        new = suite.command_for(config, 'tiny-aya-global', '/output-new')
        self.assertEqual(old[old.index('--model-dir') + 1], '/old')
        self.assertEqual(new[new.index('--model-dir') + 1], '/new')
        self.assertEqual(old[old.index('--variants') + 1:old.index('--variants') + 5], ['A', 'B', 'C', 'D'])
        self.assertEqual(new[new.index('--timeout') + 1], '3600')
        self.assertEqual(new[new.index('--profile') + 1], 'native')

    def test_upstream_must_finish_exit_and_release_runtime(self):
        identity = {'pid': 9, 'start_ticks': 100}
        self.assertFalse(suite.dependency_ready({'status': 'running'}, identity, '/runtime'))
        with patch.object(suite, 'process_identity', return_value={'start_ticks': 100, 'state': 'S'}):
            self.assertFalse(suite.dependency_ready({'status': 'completed'}, identity, '/runtime'))
        with patch.object(suite, 'process_identity', return_value=None), \
                patch.object(suite, 'runtime_processes', return_value=[12]):
            self.assertFalse(suite.dependency_ready({'status': 'completed'}, identity, '/runtime'))
        with patch.object(suite, 'process_identity', return_value=None), \
                patch.object(suite, 'runtime_processes', return_value=[]):
            self.assertTrue(suite.dependency_ready({'status': 'completed_with_failures'}, identity, '/runtime'))
        with self.assertRaisesRegex(ValueError, 'recovery_queue_blocked'):
            suite.dependency_ready({'status': 'blocked'}, identity, '/runtime')

    def test_snapshot_binds_scripts_new_catalog_and_frozen_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('scripts/example.py', 'docs/experiments/phase2-new-model-artifacts.json',
                         'tests/fixtures/privacy-policy-holdout-v1/doc.md'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original')
            before = suite.snapshot(root)
            self.assertEqual(len(before), 3)
            (root / 'scripts/example.py').write_text('changed')
            self.assertNotEqual(before, suite.snapshot(root))

    def test_serial_order_analysis_checkpoint_and_no_automatic_relaunch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wait, artifacts = root / 'recovery/status.json', root / 'artifacts.json'
            wait.parent.mkdir()
            wait.write_text(json.dumps({'status': 'completed'}))
            artifacts.write_text(json.dumps({'status': 'completed'}))
            config = {'runner_root': str(root), 'output': str(root / 'output'),
                'producer_inputs_sha256': {'input': 'hash'}, 'wait_recovery': str(wait),
                'recovery_controller': {'pid': 9, 'start_ticks': 100},
                'original_suite_output': str(root / 'original'), 'runtime': '/runtime',
                'old_model_dir': '/old', 'new_model_dir': '/new', 'new_artifacts_status': str(artifacts)}
            launched, checkpoints = [], []
            def launch(command, **_):
                model = command[command.index('--model') + 1]
                launched.append(model)
                destination = Path(command[command.index('--output') + 1])
                result = {'status': 'completed', 'technical_incomplete': False,
                    'execution_sessions': [{'finished_at_utc': 'fixture', 'phases_seconds': {'runtime_cleanup': .1}}]}
                (destination / 'result.json').write_text(json.dumps(result))
                return SimpleNamespace(pid=987654, wait=lambda **_: 0)
            def report(state, output, expected, name='reports'):
                checkpoints.append((name, list(expected)))
            with patch.object(suite, 'MODEL_ORDER', ('old',)), patch.object(suite, 'NEW_MODELS', ('new',)), \
                    patch.object(suite, 'snapshot', return_value={'input': 'hash'}), \
                    patch.object(suite, 'dependency_ready', return_value=True), \
                    patch.object(suite, 'runtime_processes', return_value=[]), \
                    patch.object(suite, 'process_group_alive', return_value=False), \
                    patch.object(suite.subprocess, 'Popen', side_effect=launch) as spawned, \
                    patch.object(suite, 'reports', side_effect=report):
                suite.run(config)
                self.assertEqual(launched, ['old', 'new'])
                self.assertIn(('before-expansion', ['old']), checkpoints)
                self.assertEqual(json.loads((root / 'output/status.json').read_text())['status'], 'completed')
                with self.assertRaisesRegex(ValueError, 'fresh_suite_attempt_exists_inspect_before_restart'):
                    suite.run(config)
                self.assertEqual(spawned.call_count, 2)


if __name__ == '__main__':
    unittest.main()
