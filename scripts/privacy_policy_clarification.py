"""The pre-registered v1/v2 policy pair; same frozen oracle, explicit treatment."""
import hashlib

from compare_local_models import ROOT
from privacy_policy_experiment import prompts
from privacy_policy_holdout import load_holdout
from summarize_privacy_holdout import _load_pinned_fresh_run
from summarize_privacy_policy import require


POLICY_V1_SHA = 'a6ce52bb481ae186d81c00e1df0d1171ebac91834e16c59b45fce0e2f1d6a144'
POLICY_V2_SHA = '6a0d5d22002fc83600ec5f2911c3daf2f433a84303017a532a533e5f5c061830'
POLICY_V2 = ROOT / 'docs/experiments/privacy-policy-v2.txt'


def quality_contract(holdout):
    return {**holdout.quality_contract, 'phase': 'fresh_holdout_policy_clarification',
        'policy_pair': [POLICY_V1_SHA, POLICY_V2_SHA], 'evaluated_policy': POLICY_V2_SHA,
        'reference_semantics': 'unchanged_owner_disclosure_rule_explicitly_clarified',
        'registered_protocol': 'docs/experiments/POLICY_V2_ABLATION.md', 'measured_variants': ['D']}


def load_clarification_run(path):
    return _load_pinned_fresh_run(path, ('D',), quality_contract(load_holdout()), POLICY_V2_SHA)


def verify_pair_plans(baseline, treatment):
    """Reject a policy-only claim if cases, packets or actual request fields differ."""
    left, right = baseline['signature'], treatment['signature']
    holdout = load_holdout()
    require(left['quality_contract'] == holdout.quality_contract
            and right['quality_contract'] == quality_contract(holdout), 'policy_pair_changed_quality_contract')
    require(left['policy_sha256'] == POLICY_V1_SHA and right['policy_sha256'] == POLICY_V2_SHA,
            'not_registered_policy_pair')
    require(treatment['variant_order'] == ['D'] and 'D' in baseline['variant_order'],
            'policy_pair_wrong_variants')
    for field in ('model_artifact', 'case_data_sha256', 'source_sha256', 'annotations_sha256',
                  'kinds_sha256', 'kind_source_sha256', 'model_profile_source'):
        require(left[field] == right[field], 'policy_pair_changed_' + field)
    require(left['model_profile_source']['mode'] == 'native', 'policy_pair_requires_native_profile')
    require({k: v for k, v in left['settings'].items() if k != 'variants'}
            == {k: v for k, v in right['settings'].items() if k != 'variants'},
            'policy_pair_changed_inference_settings')
    require(baseline['control_cases'] == treatment['control_cases']
            and baseline['batches'] == treatment['batches'], 'policy_pair_changed_candidates_or_batches')
    old = [row for row in baseline['requests'] if row['variant'] == 'D']
    new = treatment['requests']
    require(len(old) == len(new) and bool(new), 'policy_pair_changed_request_count')
    policy1 = (ROOT / 'docs/experiments/privacy-policy-v1.txt').read_text()
    policy2 = POLICY_V2.read_text()
    require(hashlib.sha256(policy1.encode()).hexdigest() == POLICY_V1_SHA
            and hashlib.sha256(policy2.encode()).hexdigest() == POLICY_V2_SHA, 'registered_policy_text_changed')
    old_prompt, new_prompt = prompts(policy1)['D'], prompts(policy2)['D']
    for before, after in zip(old, new):
        require(before['case_ids'] == after['case_ids']
                and before['batch_index'] == after['batch_index'], 'policy_pair_changed_packet_identity')
        a, b = before['request'], after['request']
        require({k: v for k, v in a.items() if k != 'messages'}
                == {k: v for k, v in b.items() if k != 'messages'}, 'policy_pair_changed_request_parameters')
        require(len(a['messages']) == len(b['messages']) == 2
                and a['messages'][0]['role'] == b['messages'][0]['role'] == 'system'
                and a['messages'][0]['content'].startswith(old_prompt)
                and b['messages'][0]['content'].startswith(new_prompt)
                and a['messages'][0]['content'].count(policy1) == 1
                and b['messages'][0]['content'] == a['messages'][0]['content'].replace(policy1, policy2, 1)
                and a['messages'][1] == b['messages'][1], 'policy_pair_changed_nonpolicy_input')
    native_verified = all(plan.get('model_sha256_verified') is True
        and plan.get('token_budget_validation') == 'verified_runtime' for plan in (baseline, treatment))
    if native_verified:
        for field in ('runtime_sha256', 'adjacent_runtime_libraries_sha256', 'native_template_probe'):
            require(baseline[field] == treatment[field], 'policy_pair_changed_native_' + field)
    return {'policy_only_request_difference_verified': True, 'native_pair_verified': native_verified,
            'paired_requests': len(new),
            'case_ids': [ident for row in new for ident in row['case_ids']]}
