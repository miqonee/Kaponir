"""Bind all four calibration variants to the existing Gemma experiment."""
import gzip
import hashlib
from pathlib import Path

from compare_local_models import ROOT
from privacy_policy_experiment import strict_json
from summarize_privacy_policy import require

REFERENCE = 'research/privacy-policy-phase2/reference-gemma-abcd-plan.json.gz'
REFERENCE_SHA = 'f4ccb15602d6319a6edca89e9f0c7dc7eebbe7f3581df73c89fb125ca2e154cb'
FIELDS = ('batch_size', 'context_tokens', 'output_tokens', 'retry_output_tokens',
          'reserve_tokens', 'reasoning_budget', 'response_format', 'temperature',
          'top_p', 'top_k', 'min_p', 'seed', 'timeout', 'expected_runtime_sha256',
          'threads', 'slots', 'gpu_layers')


def reference():
    raw = gzip.decompress((ROOT / REFERENCE).read_bytes())
    require(hashlib.sha256(raw).hexdigest() == REFERENCE_SHA, 'paired_reference_source_changed')
    return strict_json(raw.decode())


def contract(plan):
    rows = []
    for row in plan['requests']:
        request = dict(row['request']);request.pop('model')
        rows.append({'variant': row['variant'], 'batch_index': row['batch_index'],
                     'case_ids': row['case_ids'], 'request': request})
    return rows


def bind(plan):
    old = reference()
    require(plan['variant_order'] == old['variant_order'] == ['A', 'B', 'C', 'D'],
            'paired_reference_requires_all_ABCD')
    for key in FIELDS:
        require(plan['signature']['settings'][key] == old['signature']['settings'][key],
                'paired_reference_setting_differs:' + key)
    for key in ('case_data_sha256', 'source_sha256', 'annotations_sha256',
                'policy_sha256', 'kinds_sha256', 'kind_source_sha256'):
        require(plan['signature'][key] == old['signature'][key], 'paired_reference_input_differs:' + key)
    require(plan['batches'] == old['batches'], 'paired_reference_batch_plan_differs')
    require(contract(plan) == contract(old), 'paired_reference_request_contract_differs')
    require(plan['protected_bypass'] is False, 'paired_reference_protected_bypass_changed')
    return {'model': 'gemma4-e4b-qat', 'path': REFERENCE, 'raw_file_sha256': REFERENCE_SHA,
            'plan_sha256': old['plan_sha256'], 'old_model_inference_repeated': False,
            'variants': ['A', 'B', 'C', 'D'], 'cases_per_variant': 32}
