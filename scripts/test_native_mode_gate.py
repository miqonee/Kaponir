import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from evaluate_model_schemes import execute, verify_native_mode
from run_complete_research import Controller, terminal_result
from run_privacy_policy_suite import file_sha


class NativeModeGateTests(unittest.TestCase):
    def profile(self, reasoning='off'):
        return {'model': 'lfm2.5-2.6b', 'mode': 'common', 'reasoning': reasoning,
                'template_kwargs': {'enable_thinking': reasoning != 'off'}}

    def probe(self, forced):
        return {mode: {'sha256': 'a' * 64, 'tokens': 16, 'ends_with_think_prefix': forced}
                for mode in ('requested', 'off', 'on')}

    def stop_before_measurement(self, folder):
        (folder / 'scheme-plan.json').write_text('{"test_only_plan_binding":true}\n')
        with self.assertRaisesRegex(ValueError, 'native_thinking_off_not_supported'):
            verify_native_mode(self.profile(), self.probe(True), folder, [])

    def test_forced_think_prefix_records_unavailable_mode_before_corpus_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.stop_before_measurement(folder)
            value = json.loads((folder / 'native-mode-observations.json').read_text())
            self.assertEqual(value['measurement_request_slots_recorded'], 0)
            self.assertIs(value['control_data_used'], False)
            self.assertEqual(value['status'], 'unavailable_requested_profile')
            self.assertEqual(value['scheme_plan_file_sha256'], file_sha(folder / 'scheme-plan.json'))
            self.assertFalse((folder / 'transports.json').exists())
            self.assertFalse((folder / 'result.json').exists())

    def test_equal_on_off_hashes_do_not_reject_a_non_thinking_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            verify_native_mode(self.profile(), self.probe(False), folder, [])
            self.assertFalse((folder / 'native-mode-observations.json').exists())

    def test_thinking_allowed_profile_keeps_its_native_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            verify_native_mode(self.profile('auto'), self.probe(True), folder, [])
            self.assertFalse((folder / 'native-mode-observations.json').exists())

    def test_existing_history_is_not_relabeled_as_zero_call_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'native_mode_preflight_has_measurement_history'):
                verify_native_mode(self.profile(), self.probe(True), folder, [{'status': 'received'}])
            self.assertFalse((folder / 'native-mode-observations.json').exists())

    def test_execution_refuses_to_overwrite_measurements_or_diagnostics(self):
        for name in ('result.json', 'transports.json', 'native-mode-observations.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                path = folder / name
                path.write_bytes(b'previous evidence')
                with patch('evaluate_model_schemes.runtime_processes', return_value=[]), \
                        self.assertRaisesRegex(ValueError, 'scheme_measured_output_exists_no_replay'):
                    execute(SimpleNamespace(output=folder, runtime=Path('/unused-runtime')), {})
                self.assertEqual(path.read_bytes(), b'previous evidence')

    def test_controller_keeps_mode_failure_distinct_and_requires_clean_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.stop_before_measurement(folder)
            job = {'id': 'mode-preflight', 'command': ['future-runner'], 'output': str(folder),
                   'model': 'lfm2.5-2.6b', 'profile': 'common', 'kind': 'schemes'}
            (folder / 'execution.json').write_text('{"runtime_cleanup_verified":false}')
            with self.assertRaisesRegex(ValueError, 'native_mode_cleanup_not_durable'):
                terminal_result(job)
            (folder / 'execution.json').write_text('{"runtime_cleanup_verified":true}')
            result = terminal_result(job)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['reason'], 'native_thinking_off_not_supported')
            self.assertEqual(result['measurement_requests'], 0)
            self.assertIn('native_mode_observations_sha256', result)
            controller = Controller.__new__(Controller)
            controller.check_source = lambda: None
            controller.state = {'jobs': {job['id']: {**result, 'command': job['command']}}}
            controller.model(job)
            mode = folder / 'native-mode-observations.json'
            mode.write_bytes(mode.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'completed_native_mode_evidence_changed'):
                controller.model(job)
            (folder / 'result.json').write_text('{"calls":["real-attempt"],"responses":{}}')
            with self.assertRaisesRegex(ValueError, 'native_mode_diagnostic_has_measurements'):
                terminal_result(job)


if __name__ == '__main__':
    unittest.main()
