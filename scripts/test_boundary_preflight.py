import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import preflight_new_models_at_boundary as boundary


class BoundaryPreflightTests(unittest.TestCase):
    def test_no_preflight_before_gemma_cleanup_process_exit_and_idle_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / 'result.json'
            config = {'gemma_result': str(result), 'gemma_process': {'pid': 10, 'start_ticks': 20}, 'runtime': '/runtime'}
            result.write_text(json.dumps({'status': 'running'}))
            with patch.object(boundary, 'alive', return_value=True):
                self.assertFalse(boundary.gemma_ready(config))
            result.write_text(json.dumps({'status': 'completed', 'execution_sessions': [
                {'finished_at_utc': 'fixture', 'phases_seconds': {'runtime_cleanup': .1}}]}))
            with patch.object(boundary, 'alive', return_value=True):
                self.assertFalse(boundary.gemma_ready(config))
            with patch.object(boundary, 'alive', return_value=False), patch.object(boundary, 'runtime_processes', return_value=[11]):
                self.assertFalse(boundary.gemma_ready(config))
            with patch.object(boundary, 'alive', return_value=False), patch.object(boundary, 'runtime_processes', return_value=[]):
                self.assertTrue(boundary.gemma_ready(config))

    def test_missing_cleanup_with_gone_worker_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'result.json'
            path.write_text(json.dumps({'status': 'running'}))
            with patch.object(boundary, 'alive', return_value=False):
                with self.assertRaisesRegex(ValueError, 'gemma_disappeared_before_durable_cleanup'):
                    boundary.gemma_ready({'gemma_result': str(path), 'gemma_process': {'pid': 10, 'start_ticks': 20}})

    def test_boundary_commands_probe_native_limits_without_scored_generations(self):
        config = {'runner_root': '/producer', 'new_model_dir': '/models', 'runtime': '/runtime'}
        command = boundary.command_for(config, 'tiny-aya-global', '/output')
        self.assertIn('--preflight-only', command)
        self.assertEqual(command[command.index('--dataset-kind') + 1], 'holdout')
        self.assertEqual(command[command.index('--profile') + 1], 'native')
        self.assertEqual(command[command.index('--variants') + 1:command.index('--variants') + 5], ['A', 'B', 'C', 'D'])


if __name__ == '__main__':
    unittest.main()
