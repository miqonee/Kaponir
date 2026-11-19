from copy import deepcopy
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compare_local_models import ROOT
from report_full_model_study import build, write_report
from run_legacy_new_models import MODELS
from run_privacy_policy_suite import MODEL_ORDER, VARIANTS
from summarize_privacy_policy import strict_json


class FullModelStudyTests(unittest.TestCase):
    def fixture(self):
        reports = ROOT / 'research/privacy-policy-phase2/checkpoints/2026-11-17/reports'
        schemes = strict_json((reports / 'schemes.json').read_text())
        old = strict_json((reports / 'old-abcd.json').read_text())
        schemes.update(study_status='measurements_finished_with_limitations',
                       application_status='applications_finished_with_source_limitations')
        template = next(row for row in schemes['runs'] if row['model'] == MODELS[0])
        template_rows = {row['method']: row for row in schemes['rows'] if row['model'] == MODELS[0]}
        completed = [model for model in MODELS if model != 'lfm2.5-2.6b']
        for model in completed[1:]:
            schemes['runs'].append(dict(template, model=model))
            for row in schemes['rows']:
                if row['model'] == model:
                    row.update(deepcopy(template_rows[row['method']]), model=model)
        schemes['pending_models'] = [row for row in schemes['pending_models'] if row['model'] not in completed]
        schemes['application_jobs_complete'] = 42 + 6 * len(completed)

        # Verify the report with a synthetic finished matrix. These rows are
        # solely a fixture for the renderer and completion checks.
        exemplar = next(run for run in old['runs'] if run['model'] == MODEL_ORDER[0])
        abcd = {'status': 'measurements_finished', 'application_status': 'applications_finished',
                'unique_cases': 32, 'application_jobs_completed': 32,
                'rows': [{'model': model, 'variant': variant, 'status': 'completed', 'exact_roundtrips': 6}
                         for model in MODELS for variant in VARIANTS],
                'runs': [dict(exemplar, model=model) for model in MODELS]}
        return schemes, old, abcd

    def test_summary_has_all_models_and_missing_results_are_blank(self):
        schemes, old, abcd = self.fixture()
        measured = abcd['runs'][0]
        measured['rows'] = deepcopy(measured['rows'])
        measured['rows'][-1]['quality'].update(private_mask=16, private_keep=0, explicit_review=0)
        report = build(schemes, old, abcd)
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report'
            write_report(report, output)
            with (output / 'all-models.csv').open(newline='', encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual([row['model'] for row in rows], [*MODEL_ORDER, *MODELS])
        rows = {row['model']: row for row in rows}
        self.assertEqual(rows['lfm2.5-2.6b']['private_found'], '')
        self.assertEqual(rows['lfm2.5-2.6b']['abcd_responses'], '128')
        self.assertEqual(rows['nanbeige42-3b']['d_private_mask'], '')
        self.assertEqual(rows['nanbeige42-3b']['d_missing'], '32')
        self.assertEqual(rows['nanbeige42-3b']['abcd_preferred'], '')
        self.assertTrue(rows['gemma4-e4b-qat']['scheme_choices'])
        self.assertEqual(rows['gemma4-e4b-qat']['abcd_preferred'], '')
        self.assertEqual(rows['gemma4-e4b-qat']['abcd_selection_reason'], 'unresolved_quality_tradeoffs')
        self.assertEqual(rows[MODELS[0]]['abcd_preferred'], 'D')
        self.assertEqual(rows[MODELS[0]]['private_found'], '71')

    def test_terminal_inference_still_requires_finished_application_checks(self):
        schemes, old, abcd = self.fixture()
        abcd['application_status'] = 'waiting_sources'
        with self.assertRaisesRegex(ValueError, 'queues_have_not_finished'):
            build(schemes, old, abcd)
        abcd['application_status'] = 'applications_finished'
        abcd['rows'][0].pop('exact_roundtrips')
        with self.assertRaisesRegex(ValueError, 'ABCD_application_incomplete'):
            build(schemes, old, abcd)

    def test_failed_scheme_attempt_retains_resources_with_empty_quality(self):
        schemes, old, abcd = self.fixture()
        missing = next(row for row in schemes['pending_models'] if row['model'] == 'lfm2.5-2.6b')
        missing.update(reason='document_fragment_limit', failed_attempt={
            'http_seconds_known': 120, 'http_seconds_complete': True,
            'full_session_peak_rss_bytes': 2**30, 'details': {'requests_with_recorded_error': 4}})
        models = build(schemes, old, abcd)['models']
        row = next(model for model in models if model['model'] == missing['model'])
        self.assertEqual(row['schemes_measured'], 0)
        self.assertNotIn('private_found', row)
        self.assertEqual((row['scheme_resource_scope'], row['scheme_request_errors'], row['scheme_http_minutes']),
                         ('failed_attempt', 4, 2.0))


if __name__ == '__main__':
    unittest.main()
