#!/usr/bin/env python3
"""Run baseline local models through one fixed scheme comparison.

Each model uses the same corpus, prompts, budgets and schemes. Models run
sequentially; completed requests are resumed by evaluate_validation_schemes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from check_learning_corpus import load_corpus
from compare_local_models import ROOT, DOCUMENTS
from evaluate_validation_schemes import atomic_json
from kaponir_worker import Worker, configuration

COHORT = {
    'minicpm5-2b': 'minicpm-calibration',
    'qwen3.5-2b': 'qwen-calibration',
    'huihui-qwen3.5-2b-abliterated': 'huihui-calibration',
    'qwen3.5-0.8b': 'qwen08-calibration',
}
METHODS = ['detector_proposals', 'current_union', 'point', 'regions', 'union_point', 'double']
PROFILE = {
    'context_tokens': 4096, 'output_tokens': 1600, 'reserve_tokens': 64,
    'hint_tokens': 256, 'batch_size': 8, 'max_candidates': 24,
    'max_fragments': 256, 'timeout': 300,
}
GENERATION = {'temperature': 0, 'top_p': 0.95, 'top_k': 40, 'min_p': 0, 'seed': None}


def validation_quality(state, expected):
    result = {}
    for method in ['point', 'regions', 'union_point', 'double']:
        stage = state.get('stages', {}).get(method)
        if stage is None: continue
        rows = [(stage['decisions'].get(c['id'], {}), expected[(c['file'], c['value'])])
                for c in stage['candidates']
                if not c['protected'] and (c['file'], c['value']) in expected]
        confirmed = [(row, gold) for row, gold in rows if row.get('valid') and row.get('verdict') == 'mask']
        result[method] = {
            'gold_proposals_without_protection': len(rows),
            'gold_confirmed_without_protection': len(confirmed),
            'kind_correct_without_protection': sum(row['kind'] == gold[0] for row, gold in confirmed),
            'scope_correct_without_protection': sum(row['scope'] == gold[1] for row, gold in confirmed),
            'kind_and_scope_correct_without_protection': sum(
                row['kind'] == gold[0] and row['scope'] == gold[1] for row, gold in confirmed),
        }
    return result


def summarize(directory):
    _, expected, _, sources, _ = load_corpus()
    source_hashes = {name: hashlib.sha256(sources[name].encode()).hexdigest() for name in DOCUMENTS}
    annotation_hash = hashlib.sha256((ROOT/'tests/docs/expected.json').read_bytes()).hexdigest()
    count = sum(file in DOCUMENTS for file, _ in expected)
    result = {'version': 1, 'documents': DOCUMENTS, 'expected_values': count,
              'unit': 'exact value per document; not end-to-end masking coverage',
              'profile': {**PROFILE, **GENERATION, 'context_policy': 'prefix',
                          'response_format': 'grammar',
                          'thinking': False, 'threads': 2, 'rules': False, 's1': False},
              'models': {}}
    reference = None
    for model, name in COHORT.items():
        path = directory/name/'result.json'
        if not path.is_file():
            result['models'][model] = {'status': 'not_run', 'methods': {}}
            continue
        snapshot = path.read_bytes()
        state = json.loads(snapshot)
        # Never mix the 20-case probe sweep with the document comparison.
        settings = state['signature']['settings']
        if (state['signature']['model'] != model or settings['documents'] != DOCUMENTS
                or settings.get('schemes') != ['free', 'point', 'regions', 'double']
                or settings.get('response_format', 'grammar') != 'grammar'
                or settings.get('context_policy', 'prefix') != 'prefix'
                or settings.get('thinking', 'off') != 'off'
                or settings.get('validation_context', 'record') != 'record'
                or 'probe_sha256' in state['signature']
                or any(settings.get(key, value) != value for key, value in GENERATION.items())
                or any(settings.get(key) != value for key, value in PROFILE.items())):
            raise ValueError('matrix_profile_mismatch:'+model)
        if (state['signature']['source_sha256'] != source_hashes
                or state.get('annotations_sha256') != annotation_hash):
            raise ValueError('matrix_corpus_mismatch:'+model)
        frozen = {key: state['signature'][key] for key in ['runtime_sha256','source_sha256','prompts']}
        frozen.update({key:state.get(key) for key in ['detector_binary_sha256','detector_export_sha256','annotations_sha256','extraction_prompt_sha256','kind_schema_sha256']})
        if reference is None:reference=frozen
        elif reference != frozen:raise ValueError('matrix_provenance_mismatch:'+model)
        result['models'][model] = {'status': state['status'], 'artifact': state['signature']['artifact'],
            'result_sha256': hashlib.sha256(snapshot).hexdigest(),
            'runtime_sha256': state['signature']['runtime_sha256'],
            'source_sha256': state['signature']['source_sha256'], 'prompts': state['signature']['prompts'],
            'provenance': frozen,
            'methods': {method: state.get('metrics',{}).get(method) for method in METHODS},
            'validation_quality': validation_quality(state, expected),
            'detector_applied': state.get('metrics',{}).get('detector_applied'),
            'resources_before': state.get('resources_before'),
            'resources_after': state.get('resources_after'),
            'runtime_cache_history': state.get('runtime_cache_history'),
            'active_runtime_memory_policy': state.get('active_runtime_memory_policy'),
            'rss_by_request_bytes': [call.get('memory',{}).get('model_rss_bytes') for call in state.get('calls',[])],
            'request_errors': [call['error'] for call in state.get('calls',[]) if 'error' in call],
            'cache_origin': state.get('cache_origin')}
    atomic_json(directory/'matrix.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--detectors', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/validation-schemes')
    parser.add_argument('--models', nargs='+', choices=list(COHORT), default=list(COHORT))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for model in args.models:
        current = args.output/COHORT[model]/'result.json'
        completed = current.is_file() and json.loads(current.read_text())['status'] == 'completed'
        if not completed:
            with tempfile.TemporaryDirectory(prefix='kaponir-matrix-install-') as temporary:
                task_home=Path(temporary)
                worker=Worker(task_home, configuration(task_home, {
                    'model_dir':str(args.model_dir.resolve()),'runtime':str(args.runtime.resolve())}), 'unused')
                try:
                    job=worker.submit('download',model=model)
                    worker.install(job['id'],model)
                finally:
                    worker.stop.set();worker.stop_runtime()
        print(json.dumps({'model': model, 'state': 'running'}, ensure_ascii=False), flush=True)
        try:
            subprocess.run([sys.executable,str(ROOT/'scripts/evaluate_validation_schemes.py'),
                '--model',model,'--model-dir',str(args.model_dir.resolve()),'--runtime',str(args.runtime.resolve()),
                '--detectors',str(args.detectors.resolve()),'--output',str((args.output/COHORT[model]).resolve())],check=True)
        finally:
            summarize(args.output)
        print(json.dumps({'model':model,'state':'completed'}),flush=True)


if __name__ == '__main__':
    main()
