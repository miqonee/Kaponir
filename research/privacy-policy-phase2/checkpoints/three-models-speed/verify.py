"""Verify fresh search latency and actual mask coverage from the packaged receipts."""
import base64
import csv
import gzip
import hashlib
import json
import math
import sys
import tarfile
from pathlib import Path

from jsonschema import Draft202012Validator

def checksum(data):
    return hashlib.sha256(data).hexdigest()

def near(left, right):
    return math.isclose(left, right, rel_tol=0, abs_tol=1e-6)

def main():
    here = Path(__file__).resolve().parent
    repo = here.parents[3]
    sys.path.insert(0, str(repo / 'scripts'))
    from apply_measured_privacy import coverage, span_coverage
    from check_learning_corpus import load_corpus, occurrences
    from experiment_inference import extraction_schema, prompts_and_kinds
    from summarize_privacy_policy import digest, require, strict_json

    manifest = strict_json((here / 'manifest.json').read_text())
    require(checksum(Path(__file__).read_bytes())==manifest['verify_sha256'],'verifier_changed')
    archive = here / 'evidence.tar.gz'
    require(checksum(archive.read_bytes()) == manifest['archive_sha256'], 'archive_changed')
    with tarfile.open(archive) as tar:
        entries = [member for member in tar.getmembers() if member.isfile()]
        blobs = {entry.name: tar.extractfile(entry).read() for entry in entries}
    require(len(entries) == len(blobs) and {k: checksum(v) for k,v in blobs.items()} == manifest['members_sha256'],
            'evidence_member_changed')
    for interruption in manifest.get('interrupted_attempts',[]):
        prefix=interruption['prefix']
        note=strict_json(blobs[prefix+'interruption.json'].decode())
        entries=strict_json(blobs[prefix+'transports.json'].decode())['requests'] if prefix+'transports.json' in blobs else []
        require(note['trial']==interruption['trial'] and note['interrupted_before_complete'] and not note['reused_in_restart']
                and prefix+'result.json' not in blobs and sum(e['status']=='received' for e in entries)==interruption['finished_http_receipts'],
                'interruption_accounting_mismatch')
    _, expected, public, sources, _ = load_corpus()
    private = {n: [v for d,v in expected if d == n] for n in sources}
    holdout = strict_json(blobs['holdout/fixture.json'].decode())
    prior = strict_json((here.parent/'four-models-fresh-tuning/manifest.json').read_text())
    require(checksum(blobs['holdout/fixture.json']) == prior['holdout_fixture_sha256'], 'holdout_changed')
    with tarfile.open(here.parent/'four-models-fresh-tuning/evidence.tar.gz') as previous_archive:
        old_plans = {model: strict_json(previous_archive.extractfile(info['plan_member']).read().decode())
                     for model,info in prior['selected_profiles'].items()}
        old_blobs={m.name:previous_archive.extractfile(m).read() for m in previous_archive.getmembers()
                   if m.isfile() and m.name.startswith('runs/') and (m.name.endswith('/transports.json') or m.name.endswith('/mask.json'))}
    def selected_positions(blobs,prefix,docs,source,priv,pub):
        fully=set(); intervals=set(); public_lost=set()
        for name in docs:
            paths=[k for k in blobs if k.startswith(prefix+'apply/model_masks_added/') and k.endswith('-'+Path(name).stem+'/mask.json')]
            require(len(paths)==1,'mask_to_compare_missing')
            m=strict_json(blobs[paths[0]].decode())
            spans=[(r['start'],r['end']) for r in m['replacements']]
            intervals.update((name,x,y) for x,y in spans)
            offsets=[0]
            for ch in source[name]:offsets.append(offsets[-1]+len(ch.encode()))
            for v in priv[name]:
                for match in occurrences(source[name],v):
                    x,y=offsets[match.start()],offsets[match.end()]
                    if sum(max(0,min(y,b)-max(x,a)) for a,b in spans)==y-x:
                        fully.add((name,v,x,y))
            for v in pub[name]:
                for match in occurrences(source[name],v):
                    x,y=offsets[match.start()],offsets[match.end()]
                    if any(a<y and b>x for a,b in spans):public_lost.add((name,v,x,y))
        return fully,intervals,public_lost
    def contents(entries):
        return [(e['request_sha256'],e['raw_reply']['content'] if 'json_schema' in e['request']
                 else e['raw_reply']['choices'][0]['message']['content']) for e in entries]
    corpora = {'main': (sources, private, public), 'holdout': tuple(holdout[k] for k in ('sources','private','public'))}
    ranking_path = repo/'docs/experiments/model-search-summary-60-30-15.csv'
    with ranking_path.open(newline='') as fp:
        ranking = {(r['model'],r['profile']): r for r in csv.DictReader(fp) if r['series']=='fresh_speed'}
    prompt, _, kinds = prompts_and_kinds()
    schema = extraction_schema(kinds,'typed',24)
    validator = Draft202012Validator(schema)
    with (ranking_path.parent/'model-search-speed.csv').open(newline='') as fp:
        time_rows={(r['model'],r['trial']):r for r in csv.DictReader(fp)}
    selection = strict_json(blobs['selection.json'].decode())
    require(selection == manifest['selection'], 'selection_mismatch')
    retained_positions = {}
    seen = set()
    n_calls = 0
    valid_trials = 0
    profiles = {r['trial_directory']:r for r in manifest['trials']}
    for label, trial in profiles.items():
        prefix = 'runs/'+label+'/'
        def load(name):
            return strict_json(blobs[prefix+name].decode())
        plan = load('plan.json')
        args = plan['args']
        result = load('result.json')
        journal = load('transports.json')
        server = load('server.json')
        argv = server['argv']
        def flag(name):
            return argv[argv.index(name)+1]
        require(server['affinity'] == plan['cpu_affinity'] and len(server['affinity']) == max(args['threads'],args['batch'])
                and flag('-t') == str(args['threads']) and flag('-tb') == str(args['batch']) and flag('-np')=='1'
                and flag('-ngl')=='0' and flag('--cache-ram')=='0','cpu_or_runtime_changed')
        old_plan=old_plans[plan['model']]
        require(old_plan['artifact']==plan['model_artifact'] and old_plan['runtime_sha256']==plan['runtime_sha256']
                and old_plan['binary_sha256']==plan['api_sha256'], 'model_weights_or_binary_changed')
        settings=plan['profile']
        if settings['reasoning_budget']!=-1:
            require(flag('--reasoning-budget')==str(settings['reasoning_budget']), 'reasoning_budget_changed')
        if args.get('cpu_list'):
            require([int(v) for v in args['cpu_list'].split(',')]==plan['cpu_affinity'],'placement_not_applied')
            if args['label'].startswith('separate-l2'):
                groups={plan['cpu_cache_groups'][str(i)]['2-Unified'] for i in plan['cpu_affinity']}
                require(len(groups)==len(plan['cpu_affinity']),'cpu_threads_share_l2')
        flags = plan['extra_server_flags']
        require(not flags or argv[-len(flags):] == flags,'runtime_flags_changed')
        require(plan['runner_sha256'] in manifest['producer_hashes'],'unrecognized_runner')
        require(plan['model']==journal['model']==result['model']==trial['model'] and plan['kinds']==kinds
                and checksum(prompt.encode()) == plan['prompt_sha256'],'unexpected_extraction_task')
        source, priv, pub = corpora['holdout' if args['sample']=='holdout' else 'main']
        stage = load('discovery.json')
        docs = plan['source_sha256']
        require(all(checksum(source[n].encode())==h for n,h in docs.items()),'unexpected_document')
        records = journal['requests']
        seconds = {n:0. for n in docs}
        parts = {n:[] for n in docs}
        found = {n:{} for n in docs}
        invalid = []
        for index,e in enumerate(records):
            req, rec = e['request'], e['record']
            name = rec['measurement_stage'].removeprefix('discovery:')
            require(name in docs and not e.get('reused') and rec['physical_request_index']==index and
                    req['cache_prompt'] is False and req['seed']==42 and digest(req)==e['request_sha256'],
                    'fresh_request_or_hash_mismatch')
            require(rec['http_wall_seconds']>0,'invalid_receipt_time')
            seconds[name] += rec['http_wall_seconds']
            messages = e['template_request']['messages'] if 'json_schema' in req else req['messages']
            actual_schema = req.get('json_schema') or req['response_format']['json_schema']['schema']
            input_data = strict_json(messages[-1]['content'])
            fragment = input_data['source']
            require(messages[0]['content']==prompt and actual_schema==schema and fragment in source[name],
                    'prompt_schema_or_fragment_changed')
            desired_temp = 0 if args['key']=='nano' and not name.endswith('.log') else settings['temperature']
            if not args.get('short_card'):
                require((req['temperature'],req['top_p'],req['top_k'])==(desired_temp,settings['top_p'],settings['top_k']),
                        'unexpected_sampling')
            if args['key']=='lfm':
                wanted=1.0 if name.endswith('.log') else settings['ordinary_repeat_penalty']
                require(req['repeat_penalty']==wanted and (not name.endswith('.log') or not input_data['context']),
                        'repeat_penalty_or_log_hint_changed')
            if args.get('short_budget') is not None:
                observed=stage[name]
                wanted=args['short_budget'] if observed['source_tokens_for_budget']<=1024 else args['budget']
                require(req['reasoning_budget_tokens']==wanted==observed['reasoning_budget_tokens']
                        and rec.get('reasoning_text_tokens',0)<=wanted+8,'model_ignored_per_request_budget')
            parts[name].append(fragment)
            if e['status']!='received':
                invalid.append(index)
                continue
            transport=e['raw_transport']
            reply=strict_json(base64.b64decode(transport['body_base64'], validate=True).decode())
            require(reply==e['raw_reply'] and transport['status']==200,'invalid_http_reply')
            content=reply['content'] if 'json_schema' in req else reply['choices'][0]['message']['content']
            parsed=strict_json(content)
            if rec.get('error') or rec.get('finish_reason')!='stop' or not validator.is_valid(parsed):
                invalid.append(index)
                continue
            for c in parsed['candidates']:
                v=c['value']
                if v in fragment and v==v.strip() and 3<=len(v.encode())<=512:
                    found[name].setdefault(v,c)
        n_calls += len(records)
        require(len(records)==result['requests'] and near(sum(seconds.values()),result['http_seconds']) and
                all(near(seconds[n],v['http_seconds']) for n,v in stage.items()),'fresh_time_mismatch')
        require(result['complete']==trial['complete'],'trial_status_mismatch')
        if not result['complete']:
            require(invalid or any(d['errors'] for d in stage.values()),'missing_failure_evidence')
            continue
        valid_trials += 1
        require(not invalid and set(stage)==set(docs) and all(not v['errors'] for v in stage.values()),'incomplete_discovery')
        for n in docs:
            offset=0
            for fragment in parts[n]:
                if source[n].startswith(fragment,offset):
                    offset+=len(fragment)
            require(offset==len(source[n]),'missing_text_in_search')
            require(found[n]=={v['value']:v for v in stage[n]['values']},'ungrounded_values')
        app=load('apply/result.json')
        h=p=r=count=0
        total_h=total_p=0
        for ar in app['documents']:
            name,mode=ar['file'],ar['mode']
            paths=[k for k in blobs if k.startswith(prefix+'apply/'+mode+'/') and k.endswith('-'+Path(name).stem+'/mask.json')]
            require(len(paths)==1,'missing_mask')
            mask=strict_json(blobs[paths[0]].decode())
            restored=strict_json(blobs[paths[0].removesuffix('mask.json')+'restore.json'].decode())['text']
            observed=coverage(source[name],mask['text'],priv[name],pub[name])
            observed.update(span_coverage(source[name],priv[name],mask['replacements'],mask['text']))
            require(restored==source[name] and ar['restore_exact'] and ar['persistent_rules_unchanged']
                    and all(ar[k]==v for k,v in observed.items()), 'mask_or_restoration_mismatch')
            count+=1
            if mode=='model_masks_added':
                h+=observed['private_spans_fully_covered']
                p+=observed['public_occurrences_lost']
                total_h+=observed['private_occurrences_before']
                total_p+=observed['public_occurrences_before']
                if not priv[name]: r+=len(mask['replacements'])
        require(count==result['exact_restores']==len(docs)*2 and h==result['H'] and p==result['P']
                and total_h==result['private_total'] and total_p==result['public_total']
                and (r if any(not priv[n] for n in docs) else None)==result['R'],'wrong_run_totals')
        timing=time_rows[(plan['model'],args['label'])]
        require((timing['sample'],int(timing['generation_threads']),int(timing['prompt_threads']))==
                (args['sample'],args['threads'],args['batch']) and timing['cpu_affinity']==','.join(map(str,plan['cpu_affinity']))
                and timing['isolated']==str(bool(args.get('isolated'))).lower() and int(timing['requests'])==len(records)
                and (int(timing['private_hidden']),int(timing['public_lost']),int(timing['public_guide_replacements']))==(h,p,r)
                and near(float(timing['http_minutes']),result['http_seconds']/60),'speed_table_mismatch')
        names=('01-cost-estimate.md','02-application.log','08-public-reference.md') if args['sample']=='full' else ('handover-cost.md','mirror-worker.log','http-guide.md')
        require(all(near(float(timing[col]),seconds[doc]) for col,doc in zip(('cost_seconds','log_seconds','reference_seconds'),names) if doc in docs),
                'speed_table_document_times_mismatch')
        if 'vs_previous' in trial:
            prefix_old='runs/'+args['key']+'/'+args['sample']+'-'+prior['selected_profiles'][plan['model']]['option']+'/'
            older,oi,po=selected_positions(old_blobs,prefix_old,docs,source,priv,pub)
            now,ni,pn=selected_positions(blobs,prefix,docs,source,priv,pub)
            retained_positions[label] = (now,pn)
            require(len(pn)==p and int(timing['previous_public_recovered'])==len(po-pn) and
                    int(timing['previous_public_newly_lost'])==len(pn-po), 'public_position_totals_changed')
            old_calls=strict_json(old_blobs[prefix_old+'transports.json'].decode())['requests']
            old_parsed=contents(old_calls); new_parsed=contents(records)
            exact=sum(o[1]==n[1] for o,n in zip(old_parsed,new_parsed))
            require(trial['vs_previous']=={'private_gained':len(now-older),'private_lost':len(older-now),
                  'public_recovered':len(po-pn),'public_newly_lost':len(pn-po),
                  'raw_matches':exact,'matched_contents':exact==len(old_parsed)==len(new_parsed),
                  'matched_requests':[a for a,b in old_parsed]==[a for a,b in new_parsed],
                  'all_mask_intervals_identical':oi==ni},'incorrect_comparison_to_previous_profile')
        if args['sample']=='full':
            require((total_h,total_p)==(193,87),'wrong_primary_corpus')
            score=(60*h/193+25*(87-p)/87+5/(1+r)+15*693/(693+result['http_seconds']))/1.05
            require(near(score,result['score']) and timing['score_60_30_15_100']==f'{score:.2f}','wrong_score')
        totals = [sum(e['record']['usage'][key] for e in records if e['record'].get('usage'))
                  for key in ('prompt_tokens','completion_tokens')]
        require(totals == [int(timing[k]) for k in ('prompt_tokens','completion_tokens')], 'token_usage_mismatch')
        if trial.get('ranked_profile'):
            key=(result['model'],trial['ranked_profile'])
            csv_row=ranking[key]
            application=(repo/csv_row['application_or_table']).read_bytes()
            require(key not in seen and checksum(application)==csv_row['application_sha256']
                    and gzip.decompress(application)==blobs[prefix+'apply/result.json']
                    and near(float(csv_row['http_minutes']),result['http_seconds']/60)
                    and (h,p,r)==tuple(int(csv_row[k]) for k in ('private_full_193','public_lost_87','guide_replacements'))
                    and csv_row['score_60_30_15_100']==f'{score:.2f}','ranking_mismatch')
            seen.add(key)
    require(n_calls==manifest['fresh_requests'] and seen==set(ranking) and valid_trials==len(time_rows),'wrong_run_count')
    for key, label in selection['recommended'].items():
        best_id = key + '/' + label
        ctrl_id = key + '/' + selection['controls'][key]
        best, ctrl = profiles[best_id], profiles[ctrl_id]
        require(best['complete'] and ctrl['complete'] and best['sample']==ctrl['sample']=='full' and
                best['http_seconds']<=ctrl['http_seconds'] and retained_positions[ctrl_id][0]<=retained_positions[best_id][0] and
                retained_positions[best_id][1]<=retained_positions[ctrl_id][1] and
                best['vs_previous']['private_lost']==best['vs_previous']['public_newly_lost']==0 and best['P']<=ctrl['P'] and best['R']<=ctrl['R'],
                'selected_profile_regressed')
        if best_id != ctrl_id:
            alias = {'one-line-two':'one-line-holdout','ngram-two':'ngram-holdout'}
            short_id = key + '/' + alias.get(label, label+'-holdout')
            short = profiles[short_id]
            require(short['complete'] and short['sample']=='holdout' and short['vs_previous']['private_lost']==short['vs_previous']['public_newly_lost']==0,
                    'selected_profile_failed_short_corpus')
    print(f'Verified {n_calls} fresh HTTP receipts, {valid_trials} completed trials and {len(seen)} new ranked profiles.')

if __name__=='__main__':
    main()
