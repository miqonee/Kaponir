import json
from pathlib import Path
import tempfile
import unittest

from apply_scheme_masks import complete_discovery_snapshot
from build_scenario_benchmarks import OUTPUT as UI_BENCHMARKS, build as build_ui_benchmarks
from calibrate_native_models import calibration_documents, select_option
from evaluate_privacy_policy import parse_response
from native_model_profiles import MODEL_ORDER, catalog_for, check_thinking, load_profile, profiles_for
from report_native_model_retest import build, conclusion, differences, render_detail, render_main, update_main
from run_privacy_policy_suite import atomic_json, file_sha
from run_tiny_aya_context_supplement import ABCD, RECEIPT, original
from summarize_privacy_policy import digest


class NativeRetestTests(unittest.TestCase):
    def test_panel_measurements_match_the_published_runs(self):
        models = build_ui_benchmarks()
        self.assertEqual(json.loads(UI_BENCHMARKS.read_text()), models)
        self.assertEqual({name for name, study in models.items() if study['series'] == 'native'}, set(MODEL_ORDER))

    def test_manufacturer_settings_and_artifact_variants(self):
        self.assertEqual(len(MODEL_ORDER), 8)
        self.assertEqual(MODEL_ORDER[-1], 'lfm2.5-2.6b')
        self.assertNotEqual(catalog_for('gemma4-e4b-qat'), catalog_for('gemma4-e4b-it-q4km'))
        self.assertEqual(profiles_for('tiny-aya-global')[0]['settings']['temperature'], 0.1)
        self.assertEqual(profiles_for('qwen3.5-4b')[0]['settings']['extra_sampling']['presence_penalty'], 1.5)
        self.assertEqual([p['settings']['reasoning_budget'] for p in profiles_for('lfm2.5-8b-a1b')], [2048, 1024])

    def test_manifest_does_not_allow_runtime_overrides(self):
        model = 'lfm2.5-1.2b-instruct'
        profile = profiles_for(model)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            path.write_text(json.dumps(profile))
            self.assertEqual(load_profile(path, model, 'native', catalog_for(model)), profile)
            profile['settings']['temperature'] = 0
            path.write_text(json.dumps(profile))
            with self.assertRaisesRegex(ValueError, 'profile_changed'):
                load_profile(path, model, 'native', catalog_for(model))

    def test_observed_reasoning_is_checked_even_without_template_prefix(self):
        off = profiles_for('qwen3.5-4b')[0]['settings']
        for message in ({'reasoning_content': 'reason', 'content': '{}'},
                        {'content': '<think>reason</think>\n{}'}):
            reply = {'choices': [{'message': message}]}
            with self.assertRaisesRegex(ValueError, 'thinking_ignored_off'):
                check_thinking(reply, off, len)
        bounded = profiles_for('lfm2.5-8b-a1b')[1]['settings']
        reply = {'choices': [{'message': {'reasoning_content': 'reason'}}],
                 'usage': {'completion_tokens_details': {'reasoning_tokens': 1025}}}
        with self.assertRaisesRegex(ValueError, 'reasoning_budget_exceeded'):
            check_thinking(reply, bounded, len)

    def test_truncated_json_cannot_become_a_calibration_success(self):
        reply = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"decisions": []}'}}]}
        self.assertEqual(parse_response({'raw_reply': reply}), (None, 'model_response_truncated'))
        first, second = profiles_for('lfm2.5-1.2b-instruct')
        result = select_option([
            {'profile': first, 'eligible': False, 'private_missed': 0, 'public_masked': 0},
            {'profile': second, 'eligible': True, 'private_missed': 2, 'public_masked': 0},
        ])
        self.assertEqual(result, second)

    def test_precommitted_calibration_cohort_has_both_languages(self):
        documents = list(calibration_documents())
        self.assertEqual([doc['file'] for doc in documents],
                         ['04-incident-report.md', '07-operations-handover-en.md'])
        for doc in documents:
            self.assertEqual(len(doc['cases']), 3)
            self.assertIn(doc['cases'][-1].value, doc['public'])

    def test_comparison_skips_deltas_without_a_pair(self):
        fields = ('private_full', 'private_untouched', 'public_control_losses')
        base = dict(zip(fields, (70, 80, 2)))
        native = dict(zip(fields, (75, 75, 2)))
        self.assertEqual(differences(base, native, fields, False), [None, None, None])
        self.assertEqual(differences(base, native, fields, True), [5, -5, 0])
        self.assertEqual(conclusion({'base': base, 'native': native, 'paired': True}), 'улучшение')
        native['public_control_losses'] += 1
        self.assertEqual(conclusion({'base': base, 'native': native, 'paired': True}), 'компромисс')

    def test_open_run_can_publish_only_completed_discovery(self):
        model, file = 'lfm2.5-1.2b-instruct', 'synthetic.md'
        stage = f'discovery:{file}'
        profile = {'native_retest': 'profile-sha'}
        plan = {'model': model, 'profile': profile, 'source_sha256': {file: 'source-sha'}}
        state = {'status': 'running', 'stages': {stage: {'errors': [], 'calls': 1}, 'current_union': {}}}
        request = {'model': model}
        entry = {'status': 'received', 'request': request, 'request_sha256': digest(request),
                 'record': {'measurement_stage': stage, 'finish_reason': 'stop'}}
        journal = {'model': model, 'profile': profile, 'requests': [entry]}
        complete_discovery_snapshot(state, plan, journal)
        entry['record']['finish_reason'] = 'length'
        with self.assertRaisesRegex(ValueError, 'native_discovery_stage_incomplete'):
            complete_discovery_snapshot(state, plan, journal)

    def test_main_comparison_preserves_the_reference_document(self):
        with tempfile.TemporaryDirectory() as directory:
            section = render_main(build(Path(directory)))
        self.assertIn('138 / 36 / 19 / 4 | — | —', section)
        self.assertIn('gemma4-e4b-qat', section)
        self.assertIn('gemma4-e4b-it-q4km', section)
        original = '# Отчёт\n\n## Что именно проверяется\n\nТри прежних документа.\n'
        updated = update_main(original, section)
        self.assertTrue(updated.endswith(original.split('\n\n', 1)[1]))
        self.assertEqual(update_main(updated, section), updated)

    def test_tiny_context_preparation_records_its_budget_and_original_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            study = Path(directory)
            attempt = study / ABCD
            settings = {'output_tokens': 4096, 'context_tokens': 8192, 'reserve_tokens': 64}
            atomic_json(attempt / 'plan.json', {'plan_sha256': 'plan', 'signature': {'settings': settings}})
            observations = attempt / 'native-budget-observations.json'
            atomic_json(observations, {**settings, 'error': 'single_case_context_budget_exceeded',
                        'measurement_request_slots_recorded': 0, 'whole_document_not_truncated': True})
            state = {'jobs': {ABCD.as_posix(): {'status': 'unavailable_context', 'measurement_requests': 0,
                              'budget_observations_sha256': file_sha(observations)}}}
            atomic_json(study / 'status.json', state)
            _, evidence = original(study)
            receipt = {'status': 'prepared', 'provenance': evidence, 'applications': {}}
            atomic_json(study / RECEIPT, receipt)
            rows = build(study)
            self.assertIn('подготовлен · выход 2 048', render_detail(rows))
            tiny = next(row for row in rows if row['model'] == ABCD.name)
            self.assertTrue(all(row['native'] is None for row in tiny['abcd'].values()))
            state['jobs'][ABCD.as_posix()]['measurement_requests'] = 1
            atomic_json(study / 'status.json', state)
            with self.assertRaisesRegex(ValueError, 'tiny_aya_supplement_requires_unmeasured_context_limit'):
                build(study)


if __name__ == '__main__':
    unittest.main()
