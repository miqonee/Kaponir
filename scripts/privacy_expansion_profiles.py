"""Isolated research profiles with separately pinned, explicit extensions.

Registration changes only an in-process catalog during an explicit experiment.
It never updates the product catalog, user settings, trained rules or a Vault.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re

from compare_local_models import ROOT
from evaluate_context_controls import ControlledClient
from evaluate_privacy_policy import RUNTIME_SHA256
from kaponir_worker import FILES
from privacy_policy_profiles import iter_profiles, profile as old_profile


NEW_CATALOG = ROOT / 'docs/experiments/phase2-new-model-artifacts.json'
NEW_MODELS = ('lfm2.5-1.2b-instruct', 'lfm2.5-2.6b', 'tiny-aya-global',
              'qwen3.5-4b', 'spark-x2.6-4b', 'lfm2.5-8b-a1b')
GEMMA_IT_CATALOG = ROOT / 'docs/experiments/gemma-it-artifacts-v1.json'
GEMMA_IT_MODELS = ('gemma4-e2b-it-q4km', 'gemma4-e4b-it-q4km')


def pinned_models(path=NEW_CATALOG):
    path = Path(path)
    raw = path.read_bytes()
    data = json.loads(raw)
    rows = data.get('models', [])
    expected = GEMMA_IT_MODELS if data.get('catalog_kind') == 'gemma-it-extension-v1' else NEW_MODELS
    if data.get('version') != 1 or tuple(row.get('id') for row in rows) != expected:
        raise ValueError('invalid_expansion_catalog')
    for row in rows:
        item = row['artifact']
        if (Path(item['filename']).name != item['filename'] or not item['filename'].endswith('.gguf')
                or type(item['bytes']) is not int or item['bytes'] <= 0
                or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])
                or not re.fullmatch(r'[a-f0-9]{40}', item['revision'])
                or item['url'] != 'https://huggingface.co/' + item['repo'] + '/resolve/'
                    + item['revision'] + '/' + item['filename']):
            raise ValueError('unpinned_expansion_artifact')
    return data, {'path': path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
                  'sha256': hashlib.sha256(raw).hexdigest()}


def all_pinned_model_rows():
    """Readers admit the append-only extension; the original default stays13."""
    result = list(pinned_models()[0]['models'])
    if GEMMA_IT_CATALOG.is_file():
        result.extend(pinned_models(GEMMA_IT_CATALOG)[0]['models'])
    return result


def model_ids(path=NEW_CATALOG):
    data, _ = pinned_models(path)
    return [row['model'] for row in iter_profiles()] + [row['id'] for row in data['models']]


def expansion_profile(model, mode='native', catalog_path=NEW_CATALOG):
    if mode not in ('native', 'common'):
        raise ValueError('unknown_expansion_profile_mode')
    data, source = pinned_models(catalog_path)
    row = next((item for item in data['models'] if item['id'] == model), None)
    if row is None:
        result = old_profile(model)
        result.update(template_kwargs={'enable_thinking': True}, extra_sampling={}, thinking_mode='bounded')
    else:
        result = {'model': model, 'context_tokens': 8192 if model == 'tiny-aya-global' else 16384,
            'output_tokens': 4096, 'retry_output_tokens': 8192, 'reserve_tokens': 64,
            'reasoning_budget': 2048, 'batch_size': 8, 'timeout': 1800,
            'response_format': 'grammar', 'temperature': 0.0, 'top_p': 0.95,
            'top_k': 40, 'min_p': 0.0, 'seed': 42, 'threads': 2, 'slots': 1, 'gpu_layers': 0,
            'runtime_sha256': RUNTIME_SHA256, 'quantization': row['artifact']['quantization'],
            'historical_reference': None, 'fresh_A_required': True,
            'template_kwargs': {'enable_thinking': True}, 'extra_sampling': {},
            'thinking_mode': 'bounded_requested_native_support_unverified',
            'native_capability': {'status': 'not_yet_probed',
                'card_note': row['native_reasoning_card_note'],
                'gguf_metadata': row.get('gguf_metadata_inspection')},
            'artifact': dict(row['artifact'])}
        if mode == 'native':
            sampling = row.get('author_sampling_card') or {}
            if 'thinking_general' in sampling:
                sampling = sampling['thinking_general']
            for name in ('temperature', 'top_p', 'top_k', 'min_p'):
                if name in sampling:
                    result[name] = sampling[name]
            if 'repetition_penalty' in sampling:
                result['extra_sampling']['repeat_penalty'] = sampling['repetition_penalty']
            if 'presence_penalty' in sampling:
                result['extra_sampling']['presence_penalty'] = sampling['presence_penalty']
            if model in ('lfm2.5-1.2b-instruct', 'tiny-aya-global'):
                result.update(reasoning_budget=-1, thinking_mode='native_no_thinking')
                result['template_kwargs'] = {'enable_thinking': False}
            elif model == 'spark-x2.6-4b':
                result['template_kwargs']['reasoning_effort'] = 'high'
    if mode == 'common':
        result.update(context_tokens=8192, temperature=0.0, top_p=0.95, top_k=40,
                      min_p=0.0, seed=42, reasoning_budget=2048, timeout=3600,
                      template_kwargs={'enable_thinking': True}, extra_sampling={},
                      thinking_mode='bounded_requested_native_capabilities_recorded_separately')
    legacy_source = result.get('profile_source')
    result['profile_source'] = {'path': 'scripts/privacy_expansion_profiles.py',
        'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'mode': mode, 'pinned_catalog': source, 'legacy_profile_source': legacy_source}
    result['profile_mode'] = mode
    return result


@contextmanager
def research_registration(model, catalog_path=NEW_CATALOG):
    """Admit only a pinned model tuple for this process; restore on every exit."""
    data, _ = pinned_models(catalog_path)
    row = next((item for item in data['models'] if item['id'] == model), None)
    if row is None:
        if model not in FILES:
            raise ValueError('unknown_research_model')
        yield
        return
    artifact = row['artifact']
    value = tuple(artifact[key] for key in ('filename', 'bytes', 'sha256', 'url'))
    previous = FILES.get(model)
    if previous is not None and previous != value:
        raise ValueError('research_model_catalog_conflict')
    FILES[model] = value
    try:
        yield
    finally:
        if previous is None:
            FILES.pop(model, None)
        else:
            FILES[model] = previous


class ProfiledClient(ControlledClient):
    def tokens(self, prompt, value, schema, thinking):
        return self.worker.prompt_token_count(self.prompt(prompt, schema), value,
            dict(self.args.template_kwargs))
