#!/usr/bin/env python3
"""Archive completed synthetic benchmark checkpoints for independent rescoring.

No model weights, user documents, vault, credentials or reasoning text are read.
Only the explicit synthetic experiment directories below are eligible.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

from compare_local_models import ROOT
from evaluate_scheme_matrix import COHORT
from evaluate_validation_schemes import atomic_json
from report_validation_results import ADDITIONAL, DIAGNOSTICS, CONTEXT_RUNS


def export(directory, output):
    output.mkdir(parents=True,exist_ok=True)
    paths=[*(directory/name/'result.json' for name in COHORT.values()),
        *(directory/name/'result.json' for name in ADDITIONAL.values()),
        *(directory/name/'result.json' for name in DIAGNOSTICS),
        *(directory/'context-controls'/name/'result.json' for name in CONTEXT_RUNS),
        directory/'selected-holdout/result.json',directory/'classifier-audit/result.json',
        directory/'runtime-cache-diagnostic.json',directory/'runtime-cache-replay.json']
    manifest={'version':1,'source':'synthetic tests/docs/input and validation-review-heldout.json',
        'encoding':'gzip; UTF-8 JSON; mtime=0','files':{}}
    for path in paths:
        if not path.is_file():continue
        raw=path.read_bytes();state=json.loads(raw)
        if state.get('status','completed')!='completed':continue
        # Inference runs must be in shadow mode, with no rule or user-vault writes.
        if 'signature' in state and (state.get('shadow') is not True or state.get('rules_changed')!=0):
            raise ValueError('only_shadow_experiments_can_be_exported')
        relative=path.relative_to(directory).as_posix()
        name=relative.replace('/','--')+'.gz'
        compressed=gzip.compress(raw,mtime=0)
        target=output/name
        if not target.exists() or target.read_bytes()!=compressed:target.write_bytes(compressed)
        manifest['files'][relative]={'archive':name,'json_sha256':hashlib.sha256(raw).hexdigest(),
            'archive_sha256':hashlib.sha256(compressed).hexdigest(),'json_bytes':len(raw)}
    atomic_json(output/'manifest.json',manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=ROOT/'artifacts/validation-schemes')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/benchmarks')
    args=parser.parse_args()
    manifest=export(args.directory,args.output)
    print(json.dumps({'archived_checkpoints':len(manifest['files'])}))


if __name__=='__main__':main()
