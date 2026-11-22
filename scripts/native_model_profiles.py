"""Frozen manufacturer settings for the eight-model native retest."""

import re

from privacy_expansion_profiles import GEMMA_IT_CATALOG, NEW_CATALOG, expansion_profile
from summarize_privacy_policy import digest, strict_json


MODEL_ORDER = (
    'lfm2.5-1.2b-instruct', 'tiny-aya-global', 'qwen3.5-4b', 'spark-x2.6-4b',
    'lfm2.5-8b-a1b', 'gemma4-e4b-qat', 'gemma4-e4b-it-q4km', 'lfm2.5-2.6b',
)

SOURCES = dict(zip(MODEL_ORDER, (
    'https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF',
    'https://huggingface.co/CohereLabs/tiny-aya-global',
    'https://huggingface.co/Qwen/Qwen3.5-4B#best-practices',
    'https://huggingface.co/XHToken/Spark-X2.6-4B',
    'https://huggingface.co/LiquidAI/LFM2.5-8B-A1B-GGUF',
    'https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf',
    'https://huggingface.co/google/gemma-4-E4B-it',
    'https://huggingface.co/LiquidAI/LFM2.5-2.6B',
)))

SETTINGS = (
    'context_tokens', 'output_tokens', 'retry_output_tokens', 'reserve_tokens', 'batch_size',
    'reasoning_budget', 'response_format', 'temperature', 'top_p', 'top_k', 'min_p', 'seed',
    'timeout', 'template_kwargs', 'extra_sampling', 'thinking_mode',
)


def catalog_for(model):
    return GEMMA_IT_CATALOG if model == 'gemma4-e4b-it-q4km' else NEW_CATALOG


def profiles_for(model):
    if model not in SOURCES:
        raise ValueError('not_a_native_retest_model')
    native = expansion_profile(model, 'native', catalog_for(model))
    base = {key: native[key] for key in SETTINGS}
    base.update(timeout=1200, response_format='grammar')

    def option(name, budget, **overrides):
        thinking = budget > 0
        settings = {
            **base,
            'template_kwargs': {'enable_thinking': thinking},
            'reasoning_budget': budget,
            'thinking_mode': 'bounded' if thinking else 'native_no_thinking',
            **overrides,
        }
        match = {'version': 1, 'model': model, 'option': name, 'vendor': SOURCES[model],
                 'settings': settings, 'declared_profile': native['profile_source']}
        return {**match, 'profile_sha256': digest(match)}

    if model in ('lfm2.5-1.2b-instruct', 'tiny-aya-global'):
        if model == 'tiny-aya-global':
            base.update(temperature=0.1, top_p=0.95)
        return option('grammar', -1), option('prompt', -1, response_format='prompt')
    if model in ('lfm2.5-2.6b', 'lfm2.5-8b-a1b'):
        return option('thinking-2048', 2048), option('thinking-1024', 1024)
    if model == 'spark-x2.6-4b':
        return tuple(option(effort, 2048, template_kwargs={
            'enable_thinking': True, 'reasoning_effort': effort}) for effort in ('high', 'low'))
    if model == 'qwen3.5-4b':
        return option('off', -1, temperature=0.7, top_p=0.8), option('thinking-2048', 2048)
    return option('off', -1), option('thinking-2048', 2048)


def load_profile(path, model, mode, catalog):
    """Only accept an exactly registered profile for the requested research artifact."""
    value = strict_json(path.read_text())
    if (mode != 'native' or catalog.resolve() != catalog_for(model).resolve()
            or value not in profiles_for(model)):
        raise ValueError('native_retest_profile_changed_or_mismatched')
    return value


def thought_text(message):
    """Extract observed reasoning even when it was left in the final field."""
    separate = message.get('reasoning_content')
    if isinstance(separate, str) and separate.strip():
        return separate
    content = message.get('content')
    if isinstance(content, str):
        match = re.match(r'\s*<think>(.*?)(?:</think>|\Z)', content, re.DOTALL)
        if match is not None:
            return match.group(1)
    return ''


def check_thinking(reply, settings, count_tokens):
    choice = reply['choices'][0]
    thought = thought_text(choice['message'])
    if not thought.strip():
        return 0
    if not settings['template_kwargs']['enable_thinking']:
        raise ValueError('native_retest_thinking_ignored_off')
    usage = (reply.get('usage') or {}).get('completion_tokens_details') or {}
    explicit = usage.get('reasoning_tokens')
    counted = explicit if type(explicit) is int and explicit > 0 else count_tokens(thought)
    # Tokenizing extracted text can change the tokens at the reasoning delimiters.
    slack = 0 if type(explicit) is int and explicit > 0 else 4
    if counted > settings['reasoning_budget'] + slack:
        raise ValueError('native_retest_reasoning_budget_exceeded')
    return counted
