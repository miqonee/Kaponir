import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_complete_research as complete
from report_complete_research import write_reports, language_rows
from run_privacy_policy_suite import MODEL_ORDER
from privacy_expansion_profiles import NEW_MODELS


class CompleteResearchTests(unittest.TestCase):
    def config(self, root):
        return {'runner_root': str(root), 'output': str(root / 'complete'),
            'old_model_dir': '/old', 'new_model_dir': '/new', 'runtime': '/runtime',
            'producer_inputs_sha256': {}}

    def test_all_models_profiles_and_stage_order_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = complete.build_jobs(self.config(Path(temporary)))
        self.assertEqual(len(jobs), 44)
        self.assertEqual([job['model'] for job in jobs[:7]], list(MODEL_ORDER))
        self.assertTrue(all(job['dataset'] == 'holdout' for job in jobs[:7]))
        calibration = [job for job in jobs if job['stage'] == 'calibration-new']
        self.assertEqual([job['model'] for job in calibration], list(NEW_MODELS))
        for profile in ('common', 'native'):
            scheme = [job for job in jobs if job['kind'] == 'schemes' and job['profile'] == profile]
            self.assertEqual([job['model'] for job in scheme], list(NEW_MODELS))
            fresh = [job for job in jobs if job['kind'] == 'privacy' and job['dataset'] == 'holdout' and job['profile'] == profile]
            self.assertEqual(len({job['model'] for job in fresh}), 13)
        self.assertEqual(len({job['output'] for job in jobs}), len(jobs))

    def test_context_unavailability_requires_native_evidence_and_zero_sent_measurements(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {'output': str(root)}
            observations = {'error': 'single_case_context_budget_exceeded', 'measurement_request_slots_recorded': 0}
            path = root / 'native-budget-observations.json'
            path.write_text(json.dumps(observations))
            self.assertEqual(complete.terminal_result(job)['status'], 'unavailable_context')
            observations['measurement_request_slots_recorded'] = 1
            path.write_text(json.dumps(observations))
            self.assertEqual(complete.terminal_result(job)['status'], 'failed')

    def test_uncertain_launch_is_never_automatically_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = complete.Controller(self.config(Path(temporary)))
            job = controller.jobs[0]
            controller.state['jobs'][job['id']] = {**job, 'status': 'launching'}
            with patch.object(controller, 'check_source'), patch.object(complete.subprocess, 'Popen') as spawned:
                with self.assertRaisesRegex(ValueError, 'launch_outcome_unknown_no_automatic_replay'):
                    controller.model(job)
            spawned.assert_not_called()

    def test_existing_queue_requires_explicit_resume_and_same_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            complete.Controller(config)
            with self.assertRaisesRegex(ValueError, 'complete_queue_exists_use_explicit_resume'):
                complete.Controller(config)
            resumed = complete.Controller(config, True)
            self.assertEqual(resumed.state['planned_jobs'], 44)
            with self.assertRaisesRegex(ValueError, 'complete_queue_config_changed'):
                complete.Controller({**config, 'runtime': '/different'}, True)

    def test_empty_fresh_matrix_stays_missing_while_historical_rows_are_real_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            jobs = complete.build_jobs(config)
            state = {'status': 'waiting_reference_research', 'jobs': {}}
            result = write_reports(config, state, jobs)
            output = Path(result['output'])
            progress = json.loads((output / 'progress.json').read_text())
            self.assertEqual((len(progress['models']), progress['terminal_jobs']), (13, 0))
            self.assertTrue(all(row['fresh_native'] == 'pending' for row in progress['models']))
            fresh = json.loads((output / 'fresh-native.json').read_text())
            self.assertFalse(fresh['runs'])
            self.assertEqual(len(fresh['model_coverage']['missing_models']), 13)
            historical = json.loads((output / 'schemes.json').read_text())
            self.assertEqual(len(historical['runs']), 7)
            self.assertTrue(all(run['archived'] for run in historical['runs']))
            self.assertEqual(sum(len(run['rows']) for run in historical['runs']), 42)
            self.assertFalse(result['rejected_inputs'])
            with (output / 'languages.csv').open() as handle:
                self.assertFalse(list(csv.DictReader(handle)))

    def test_language_slices_keep_correct_uncertainty_and_missing_cases(self):
        from test_summarize_privacy_holdout import FreshPrivacySummaryTests
        with tempfile.TemporaryDirectory() as temporary:
            helper = FreshPrivacySummaryTests()
            path, _, _ = helper.fixture(Path(temporary))
            rows = language_rows(path, 'native')
        self.assertEqual(len(rows), 8)
        for row in rows:
            if row['variant'] == 'D':
                self.assertEqual((row['cases'], row['correct_automatic_actions']), (16, 14))
                self.assertEqual(row['overconfident_on_reference_review'], 0)
                self.assertEqual(row['unnecessary_explicit_review'], 0)
            else:
                self.assertEqual((row['attempted_cases'], row['unattempted_cases']), (0, 16))

    def test_resources_include_attempted_calibration_without_inventing_unsent_costs(self):
        from test_summarize_privacy_policy import PrivacySummaryTests
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = PrivacySummaryTests()
            path, result, plan = helper.fixture(root / 'calibration')
            helper.add_batch(result, plan, 'A', 0)
            helper.save(path, result, plan)
            config = self.config(root)
            config['reference_results'] = {'gemma4-e4b-qat': str(path)}
            report = write_reports(config, {'status': 'running', 'jobs': {}}, complete.build_jobs(config))
            with (Path(report['output']) / 'resources.csv').open() as handle:
                rows = [row for row in csv.DictReader(handle) if row['series'] == 'calibration-original']
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['variant'], rows[0]['http_seconds']), ('A', '10.0'))
            self.assertTrue((Path(report['output']) / 'transport-recovery.md').exists())

    def test_serial_execution_records_context_na_and_does_not_replay_terminal_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            wait, artifacts = root / 'recovery/status.json', root / 'artifacts.json'
            wait.parent.mkdir()
            wait.write_text(json.dumps({'status': 'completed'}))
            artifacts.write_text(json.dumps({'status': 'completed'}))
            config.update(wait_recovery=str(wait), original_suite_output=str(root / 'reference'),
                          new_artifacts_status=str(artifacts), recovery_controller={'pid': 9, 'start_ticks': 9})
            all_jobs = complete.build_jobs(config)
            jobs = [all_jobs[0], all_jobs[9], all_jobs[13]]
            launched = []
            def launch(command, **_):
                job = next(job for job in jobs if job['command'] == command)
                launched.append(job['id'])
                output = Path(job['output'])
                if job['model'] == 'tiny-aya-global':
                    (output / 'native-budget-observations.json').write_text(json.dumps({
                        'error': 'single_case_context_budget_exceeded', 'measurement_request_slots_recorded': 0}))
                    code = 1
                elif job['kind'] == 'privacy':
                    result = {'status': 'completed', 'technical_incomplete': False,
                        'execution_sessions': [{'finished_at_utc': 'fixture', 'phases_seconds': {'runtime_cleanup': .1}}],
                        'stages': {v: {'decisions': {str(n): {} for n in range(32)}} for v in ('A', 'B', 'C', 'D')}}
                    plan = {'signature': {'settings': {'model': job['model']},
                        'model_profile_source': {'mode': job['profile']}, 'quality_contract': {'phase': 'fresh_holdout'}}}
                    (output / 'result.json').write_text(json.dumps(result))
                    (output / 'plan.json').write_text(json.dumps(plan))
                    code = 0
                else:
                    methods = ('detector_proposals', 'current_union', 'point', 'regions', 'union_point', 'double')
                    result = {'status': 'completed', 'signature': {'model': job['model']},
                        'research_execution': {'runtime_cleanup_verified': True}, 'metrics': {m: {} for m in methods}, 'stages': {}}
                    (output / 'result.json').write_text(json.dumps(result))
                    (output / 'scheme-plan.json').write_text(json.dumps({'profile': {'mode': job['profile']}}))
                    code = 0
                class Child:
                    pid = 900000 + len(launched)
                    def wait(self, **_):
                        return code
                return Child()
            with patch.object(complete, 'build_jobs', return_value=jobs), \
                    patch.object(complete, 'dependency_ready', return_value=True), \
                    patch.object(complete, 'runtime_processes', return_value=[]), \
                    patch.object(complete, 'process_group_alive', return_value=False), \
                    patch.object(complete, 'process_identity', side_effect=lambda pid: {'start_ticks': pid, 'state': 'S'}), \
                    patch.object(complete.subprocess, 'Popen', side_effect=launch) as spawned:
                controller = complete.Controller(config)
                with patch.object(controller, 'check_source'), patch.object(controller, 'refresh_reports'):
                    controller.run()
                self.assertEqual(launched, [job['id'] for job in jobs])
                self.assertEqual(controller.state['jobs'][jobs[1]['id']]['status'], 'unavailable_context')
                self.assertEqual(controller.state['status'], 'measurements_finished_with_limitations')
                resumed = complete.Controller(config, True)
                with patch.object(resumed, 'check_source'), patch.object(resumed, 'refresh_reports'):
                    resumed.run()
                self.assertEqual(spawned.call_count, 3)

    def test_report_failure_is_visible_without_aborting_a_running_measurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = complete.Controller(self.config(Path(temporary)))
            with patch('report_complete_research.write_reports', side_effect=RuntimeError('parser failure')):
                controller.refresh_reports(force=True)
            self.assertEqual(controller.state['reports']['status'], 'failed')
            self.assertEqual(controller.state['status'], 'waiting_reference_research')


if __name__ == '__main__':
    unittest.main()
