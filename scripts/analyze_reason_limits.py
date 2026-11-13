"""Post-hoc reason-length diagnostic; no inference, rule or original-score changes.

Only full-document oracle controls can be replayed without a running tokenizer.
Every strict decision is reconstructed first. The counterfactual then clips only
the display explanation; all batch, evidence, kind and scope checks stay active.
"""
from collections import defaultdict
import copy
import hashlib

from check_learning_corpus import load_corpus
from compare_local_models import ROOT
from evaluate_scheme_matrix import validation_quality
from experiment_inference import prompts_and_kinds
from validation_pipeline import Candidate,check_decisions,record_span,score,uncertain


def decision_rows(reply):
    rows=reply.get('decisions') if isinstance(reply,dict) else None
    return rows if isinstance(rows,list) else []


def clip_reasons(reply,limit=120):
    result=copy.deepcopy(reply)
    for row in decision_rows(result):
        if isinstance(row,dict) and isinstance(row.get('reason'),str):
            row['reason']=row['reason'][:limit]
    return result


def analyze(state):
    if (state.get('control_cases',{}).get('candidate_source')!='reference_oracle'
            or not state.get('review_batches')):return {}
    trace=defaultdict(list)
    if len(state['calls'])!=len(state['responses']):raise ValueError('reason_diagnostic_trace_mismatch')
    for call,response in zip(state['calls'],state['responses'].values()):
        trace[call['stage']].append(response)
    methods={name:stage for name,stage in state.get('stages',{}).items()
        if name.startswith('review-document-') and
        not state['metrics'][name].get('quality_is_lower_bound_if_incomplete') and
        any(isinstance(row,dict) and isinstance(row.get('reason'),str) and len(row['reason'])>120
            for response in trace[name] for row in decision_rows(response.get('reply')))}
    if not methods:return {}
    expected_hash=state['signature']['code_sha256']['validation_pipeline.py']
    if hashlib.sha256((ROOT/'scripts/validation_pipeline.py').read_bytes()).hexdigest()!=expected_hash:
        raise ValueError('reason_diagnostic_pipeline_changed')
    _,gold,public,sources,_=load_corpus();_,_,kinds=prompts_and_kinds()
    if any(hashlib.sha256(sources[name].encode()).hexdigest()!=sha
           for name,sha in state['signature']['source_sha256'].items()):
        raise ValueError('reason_diagnostic_sources_changed')
    result={}
    for name,stage in methods.items():
        candidates=[Candidate(**row) for row in stage['candidates']]
        if any(c.protected or sources[c.file][c.start:c.end]!=c.value for c in candidates):
            raise ValueError('reason_diagnostic_candidates_changed')
        lookup={c.id:c for c in candidates};keys={(c.file,c.value) for c in candidates}
        expected={key:value for key,value in gold.items() if key in keys}
        contexts={c.id:{'record':sources[c.file][slice(*record_span(sources[c.file],c.start,c.end))],
                       'context':sources[c.file]} for c in candidates}
        strict={};normalized={};index=0;long_reasons=0
        for identifiers in state['review_batches']:
            response=trace[name][index];index+=1
            if (response.get('error')=='model_response_truncated' and index<len(trace[name])
                    and trace[name][index]['record']['max_output_tokens']>
                        response['record']['max_output_tokens']):
                response=trace[name][index];index+=1
            batch=[lookup[ident] for ident in identifiers]
            if 'error' in response:
                decisions={c.id:uncertain(c,response['error']) for c in batch}
                strict.update(decisions);normalized.update(decisions);continue
            reply=response['reply']
            rows=reply.get('decisions',[])
            if isinstance(rows,list):
                long_reasons+=sum(isinstance(row,dict) and isinstance(row.get('reason'),str)
                                  and len(row['reason'])>120 for row in rows)
            strict.update(check_decisions(reply,batch,contexts,kinds))
            normalized.update(check_decisions(clip_reasons(reply),batch,contexts,kinds))
        if index!=len(trace[name]) or strict!=stage['decisions']:
            raise ValueError('reason_diagnostic_strict_replay_mismatch')
        before=score(candidates,strict,expected,public)
        if any(value!=state['metrics'][name][key] for key,value in before.items()):
            raise ValueError('reason_diagnostic_score_mismatch')
        after=score(candidates,normalized,expected,public)
        normalized_stage={'candidates':stage['candidates'],'decisions':normalized}
        result[name]={'posthoc_diagnostic':True,'new_inference_calls':0,'original_results_changed':False,
            'change':'Clip explanation reason to 120 characters; preserve every other check and field.',
            'overlong_reasons':long_reasons,
            'rows_rejected_only_for_reason_length':sum(not strict[c.id]['valid'] and normalized[c.id]['valid'] for c in candidates),
            'strict':before,'reason_clipped':after,
            'metadata_after_clip':validation_quality({'stages':{'point':normalized_stage}},expected)['point']}
    return result
