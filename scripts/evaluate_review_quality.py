#!/usr/bin/env python3
"""Real CPU evaluation of discovery/category stages. Requires verified Qwen3.5 GGUF + runtime.

No model downloads, no real user data, and no rules applied. Each run uses a fresh temporary Vault.
"""
import argparse,json,os,subprocess,tempfile,time,urllib.request
from pathlib import Path
from validate_console import free_port,wait_for

def detector_review(api):
    api('/api/v1/ui/mask',{'text':'Read /srv/zorgbilling/release.log','commit':True})
    target=next(item for item in api('/api/v1/ui/review')['items'] if item['origin']=='detector' and item['value']=='zorgbilling')
    review=api('/api/v1/ui/review/jobs',{'keys':[target['key']],'model_source':'auto'})
    completed=wait_for(lambda:(job if (job:=api('/api/v1/ui/review/jobs/'+review['id']))['state'] in ['succeeded','failed'] else None),timeout=400)
    reviewed=next(item for item in api('/api/v1/ui/review')['items'] if item['key']==target['key'])
    assert reviewed['probability']==target['probability'] and reviewed['state']=='open'
    return {'state':completed['state'],'error':completed.get('error',''),'source':completed['source'],
            'recommendation':reviewed.get('recommendation'), 'original_detector_score':target['probability']}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path)
    parser.add_argument('--model-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--review-only',action='store_true',help='Only run explicit model review, without document discovery')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parent.parent
    texts=[{'name':'01-cost-estimate.md','text':(root/'tests/docs/input/01-cost-estimate.md').read_text()},
           {'name':'public-control.txt','text':'Public documentation: Python, Linux, TCP/IP. Date: Thu Nov 12 10:40:00 2026. Public price: 10 USD. Example quantity: 96 lines.'}]
    reports=[]
    expected_doc=next(document for document in json.loads((root/'tests/docs/expected.json').read_text())['documents'] if document['file']=='01-cost-estimate.md')
    expected={item['value'] for item in expected_doc['sensitive']}
    for category in ['none','local']:
        with tempfile.TemporaryDirectory(prefix='kaponir-real-quality-') as directory:
            home=Path(directory);binary=args.binary.resolve();env={k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')}
            env.update(KAPONIR_MODEL_DIR=str(args.model_dir.resolve()),KAPONIR_WORKER_LISTEN=f'127.0.0.1:{free_port()}')
            subprocess.run([binary,'--home',home,'init'],env=env,capture_output=True,check=True)
            base=f'http://127.0.0.1:{free_port()}'
            log=(args.output/f'{category}.log').open('w')
            process=subprocess.Popen([binary,'--home',home,'serve','--listen',base.split('://')[1]],env=env,stdout=log,stderr=log)
            def api(path,body=None,method=None):
                request=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'},method=method)
                with urllib.request.urlopen(request,timeout=40) as response:return json.load(response)
            try:
                wait_for(lambda:api('/api/v1/ui/info'))
                analysis=api('/api/v1/ui/analysis')['settings'];analysis.update(enabled=True,provider='local',model='qwen3.5-0.8b',auto_apply=False,parallelism=1,timeout_seconds=300)
                categorization=api('/api/v1/ui/categorization')['settings'];categorization.update(provider=category,model='qwen3.5-0.8b')
                api('/api/v1/ui/categorization',{'settings':categorization},'PUT');api('/api/v1/ui/analysis',{'settings':analysis},'PUT');api('/api/v1/ui/worker/start',{})
                wait_for(lambda:api('/api/v1/ui/worker')['models']['qwen3.5-0.8b']['downloaded'])
                if args.review_only:
                    report={'categorization':category,'explicit_detector_review':detector_review(api)}
                    (args.output/f'{category}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                    reports.append(report)
                    continue
                started=time.monotonic();api('/api/v1/ui/analysis/documents',{'documents':texts})
                data=wait_for(lambda:(d if (d:=api('/api/v1/ui/learning'))['summary']['totals']['processed']+d['summary']['totals']['failed']>=2 else None),timeout=1500)
                entries=data['entries'];financial=[e for e in entries if any(c in e['value'] for c in ['₽','%'])]
                report={'categorization':category,'elapsed_seconds':round(time.monotonic()-started,2),'entries':entries,'summary':data['summary'],
                        'invalid_financial_types':sum(e['kind'] not in ['AMOUNT','CUSTOM'] for e in financial),
                        'public_control_findings':sum(e['source']=='public-control.txt' for e in entries),
                        'stage_states':sorted(set(e.get('categorization_state','legacy') for e in entries)),
                        'expected_sensitive_values':len(expected),'matched_expected_values':len(expected & {entry['value'] for entry in entries}),
                        'missed_expected_values':sorted(expected-{entry['value'] for entry in entries}),
                        'unmatched_proposals':sorted({entry['value'] for entry in entries}-expected),
                        'raw_model_financial_type_conflicts':sum(any(c in entry['value'] for c in ['₽','%']) and entry.get('proposed_kind',entry['kind']) not in ['AMOUNT','CUSTOM'] for entry in entries)}
                (args.output/f'{category}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                assert data['summary']['totals']['failed']==0,report['summary']
                assert report['invalid_financial_types']==0
                assert all(e['state']!='active' for e in entries)
                if category=='none':assert all(e['confidence'] is None and e['categorization_state']=='disabled' for e in entries)
                else:assert all(e['categorization_state'] in ['succeeded','failed'] for e in entries)
                report['explicit_detector_review']=detector_review(api)
                (args.output/f'{category}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                reports.append({k:v for k,v in report.items() if k!='entries'})
            finally:process.terminate();process.wait(timeout=20);log.close()
    if args.baseline:
        baseline=json.loads(args.baseline.read_text());values={entry['value'] for entry in baseline['entries']}
        reports.insert(0,{'baseline_entries':len(baseline['entries']),'baseline_matched_expected_values':len(expected&values),
          'baseline_unmatched_proposals':sorted(values-expected),'baseline_elapsed_seconds':baseline.get('elapsed_seconds'),
          'baseline_invalid_financial_types':sum(any(c in e['value'] for c in ['₽','%']) and e['kind'] not in ['AMOUNT','CUSTOM'] for e in baseline['entries'])})
    (args.output/'result.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n');print(json.dumps(reports,ensure_ascii=False))

if __name__=='__main__':main()
