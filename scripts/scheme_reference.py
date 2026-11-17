"""Replay an existing seven-model protocol on new models, without rerunning old ones."""
import gzip
import hashlib

from compare_local_models import ROOT
from summarize_privacy_policy import require, strict_json

ARCHIVES = {
    'gemma4-e4b-qat': 'gemma-calibration--result.json.gz',
    'qwen3.5-2b': 'qwen-calibration--result.json.gz',
    'qwen3.5-0.8b': 'qwen08-calibration--result.json.gz',
    'huihui-qwen3.5-2b-abliterated': 'huihui-calibration--result.json.gz',
    'minicpm5-2b': 'minicpm-calibration--result.json.gz',
    'granite42-3b': 'granite-calibration--result.json.gz',
    'nanbeige42-3b': 'nanbeige-calibration--result.json.gz',
}
REQUESTED_REFERENCE = 'archived-requested-v2'
EFFECTIVE_REFERENCE = 'archived-effective-v1'


def reference(model):
    require(model in ARCHIVES, 'unknown_legacy_scheme_reference')
    path = ROOT / 'benchmark/data/validation' / ARCHIVES[model]
    compressed = path.read_bytes();raw = gzip.decompress(compressed)
    manifest = strict_json((path.parent / 'manifest.json').read_text())
    entry = next(row for row in manifest['files'].values() if row['archive'] == path.name)
    require(hashlib.sha256(compressed).hexdigest() == entry['archive_sha256']
            and hashlib.sha256(raw).hexdigest() == entry['json_sha256'], 'legacy_reference_bytes_changed')
    state = strict_json(raw.decode())
    require(state['signature']['model'] == model and state['status'] == 'completed'
            and state['shadow'] is True and state['rules_changed'] == 0, 'invalid_legacy_scheme_reference')
    require(isinstance(state.get('generation'), dict) and state.get('structured_output') in ('grammar', 'prompt'),
            'legacy_reference_missing_explicit_sampling_or_format')
    return state, {'model': model, 'path': path.relative_to(ROOT).as_posix(),
        'archive_sha256': entry['archive_sha256'], 'json_sha256': entry['json_sha256'],
        'old_model_inference_repeated': False}


def profile_from_reference(profile, model, *, reference_mode=REQUESTED_REFERENCE):
    state, binding = reference(model)
    settings = state['signature']['settings']
    for name in ('context_tokens', 'output_tokens', 'reserve_tokens', 'batch_size',
                 'hint_tokens', 'max_candidates', 'max_fragments'):
        require(profile[name] == settings[name], 'legacy_reference_budget_differs:' + name)
    for name in ('temperature', 'top_p', 'top_k', 'min_p'):
        require(profile[name] == state['generation'][name], 'legacy_reference_sampling_differs:' + name)
    require(reference_mode in (REQUESTED_REFERENCE, EFFECTIVE_REFERENCE), 'unknown_legacy_reference_mode')
    response_format = state['structured_output']
    if reference_mode == REQUESTED_REFERENCE:
        response_format = settings.get('response_format')
        require(response_format in ('grammar', 'prompt', 'auto'),
                'legacy_reference_missing_explicit_requested_format')
    profile.update(response_format=response_format,
        transport_timeout_seconds=settings['timeout'], worker_configuration_timeout_seconds=settings['timeout'])
    require(profile['retry_output_tokens'] == profile['output_tokens'] == 1600
            and profile['seed'] is None and profile['reasoning'] == 'off'
            and profile['reasoning_budget'] == -1
            and profile['template_kwargs'] == {'enable_thinking': False}
            and profile['extra_sampling'] == {}, 'legacy_reference_client_profile_differs')
    return binding


def verify_reference_inputs(plan, model):
    state, binding = reference(model)
    for field, value in [('source_sha256', state['signature']['source_sha256']),
        ('prompt_sha256', state['signature']['prompts']),
        ('extraction_prompt_sha256', state['extraction_prompt_sha256']),
        ('kind_schema_sha256', state['kind_schema_sha256']),
        ('annotations_sha256', state['annotations_sha256']),
        ('frozen_detector_export_sha256', state['detector_export_sha256'])]:
        require(plan[field] == value, 'legacy_reference_input_differs:' + field)
    require(plan['expected_runtime_sha256'] == state['signature']['runtime_sha256'],
            'legacy_reference_runtime_differs')
    checked = dict(plan['profile'])
    profile_from_reference(checked, model,
        reference_mode=plan.get('legacy_reference_mode', EFFECTIVE_REFERENCE))
    require(checked == plan['profile'], 'legacy_reference_transport_or_format_differs')
    return binding


def matching_requested_references(plan):
    """Find old archives with the same recorded starting protocol, without inference."""
    require(plan.get('legacy_reference_mode') == REQUESTED_REFERENCE,
            'requested_reference_plan_required')
    matches = {}
    for model in ARCHIVES:
        try:
            matches[model] = verify_reference_inputs(plan, model)
        except ValueError:
            continue
    return matches
