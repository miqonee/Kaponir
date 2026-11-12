#!/usr/bin/env python3
"""Compare pinned CPU GGUFs using Kaponir's actual discovery pipeline and fixed review cases.

Downloads require --download. Each model uses an isolated Vault, the same documents,
temperature 0, 4096-token context, one slot, two CPU threads, and 1600 output tokens.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from kaponir_worker import Worker, configuration, handler, FILES, RUNTIME_SHA
from validate_console import free_port, wait_for

ROOT = Path(__file__).resolve().parent.parent
MODELS = ['minicpm5-2b', 'qwen3.5-2b', 'huihui-qwen3.5-2b-abliterated']
DOCUMENTS = ['01-cost-estimate.md', '02-application.log', '08-public-reference.md']


def request(base, path, body=None, method=None):
    req = urllib.request.Request(base+path, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type':'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.load(response)


def review_cases(worker, model, include_kind=False):
    cases = json.loads((ROOT/'tests/fixtures/local-model-review.json').read_text())
    kinds = re.findall(r'"([A-Z_]+)"', (ROOT/'crates/kaponir-core/src/kinds.rs').read_text().split('pub const STANDARD:')[1].split('];')[0])
    prompt = re.search(r'const PROMPT: &str = "([^"]+)";', (ROOT/'crates/kaponir-proxy/src/learning.rs').read_text()).group(1)
    results = []
    for start in range(0, len(cases), 2):
        batch = cases[start:start+2]
        props = {'id':{'type':'string','enum':[c['id'] for c in batch]},
                 'action':{'type':'string','enum':['mask','keep']}, 'kind':{'type':'string','enum':kinds},
                 'scope':{'type':'string','enum':['personal','team','global']},
                 'confidence':{'type':'number','minimum':0,'maximum':1},
                 'reason':{'type':'string','minLength':1,'maxLength':400}}
        schema = {'type':'object','properties':{'decisions':{'type':'array','minItems':len(batch),'maxItems':len(batch),
                  'items':{'type':'object','properties':props,'required':list(props),'additionalProperties':False}}},
                  'required':['decisions'],'additionalProperties':False}
        started = time.monotonic()
        try:
            reply = worker.evaluate({'model':model,'parallelism':1,'prompt':prompt,
                'schema':json.loads(json.dumps(schema,sort_keys=True)),
                'input':{'items':[{k:c[k] for k in ['id','value','context']+(['kind'] if include_kind else [])} for c in batch]}})
            decisions = reply.get('decisions', [])
            valid = isinstance(decisions,list) and len(decisions)==len(batch)
            for case in batch:
                matches = [d for d in decisions if isinstance(d,dict) and d.get('id')==case['id']] if isinstance(decisions,list) else []
                answer = matches[0] if len(matches)==1 else {}
                p = answer.get('confidence')
                valid_case = valid and len(matches)==1 and type(p) in [int,float] and 0<=p<=1 and answer.get('kind') in kinds and answer.get('action') in ['mask','keep'] and answer.get('scope') in ['personal','team','global'] and isinstance(answer.get('reason'),str) and 0<len(answer['reason'])<=400
                results.append({'case':case,'answer':answer,'valid':bool(valid_case),
                    'action_correct':bool(valid_case and answer['action']==case['expected_action']),
                    'kind_correct':None if 'expected_kind' not in case else bool(valid_case and answer['kind']==case['expected_kind']),
                    'scope_correct':None if 'expected_scope' not in case else bool(valid_case and answer['scope']==case['expected_scope']),
                    'batch_elapsed_seconds':round(time.monotonic()-started,2)})
        except Exception as exc:
            results.extend({'case':case,'valid':False,'action_correct':False,'error':str(exc)[:200]} for case in batch)
    return results


def score_application_review(results):
    """Apply the server's independent type-format guard, including rejection of a bad batch."""
    financial = re.compile(r'^[+-]?\d(?:[\d\s,.]*\d)?\s*(?:₽|руб\.?|RUB|USD|EUR|[$€%])$',re.I)
    for start in range(0,len(results),2):
        batch=results[start:start+2]
        for result in batch:
            answer=result.get('answer',{});kind=answer.get('kind','');value=result['case']['value']
            conflict=(bool(financial.fullmatch(value.strip())) and kind not in ['AMOUNT','CUSTOM']) or (kind in ['PERSON','FNAME','LNAME','MNAME'] and any(c in '0123456789' for c in value))
            result['type_format_valid']=not conflict
            result['application_valid']=bool(result['valid'] and not conflict)
        accepted=all(result['application_valid'] for result in batch)
        for result in batch:result['batch_accepted']=accepted
    return {'application_valid':sum(r['application_valid'] for r in results),
            'batch_accepted':sum(r['batch_accepted'] for r in results)}


def run_model(args, model):
    artifact = FILES[model]
    report = {'model':model,'artifact':{'file':artifact[0],'bytes':artifact[1],'sha256':artifact[2],'url':artifact[3]},
        'settings':{'runtime_sha256':RUNTIME_SHA,'threads':2,'slots':1,'context_tokens':4096,'temperature':0,'max_output_tokens':1600,'gpu_layers':0,'thinking':False},
        'documents':DOCUMENTS,'status':'running'}
    destination = args.output/(model+'.json')
    def save(): destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save()
    with tempfile.TemporaryDirectory(prefix='kaponir-model-comparison-') as tmp:
        home = Path(tmp)
        worker = Worker(home/'worker', configuration(home/'worker', {'model_dir':str(args.model_dir.resolve()),
                        'runtime':str(args.runtime.resolve()),'model':model,'threads':2}), str(args.binary.resolve()))
        process = None
        server = None
        sampler_stop = threading.Event()
        peak = {'model_rss_bytes':0,'total_rss_bytes':0}
        def sample():
            while not sampler_stop.wait(.1):
                memory = worker.resources()
                for key in peak: peak[key] = max(peak[key],memory.get(key) or 0)
        sampler = threading.Thread(target=sample,daemon=True)
        try:
            path = args.model_dir/artifact[0]
            if not path.exists():
                if not args.download: raise ValueError('model missing; pass --download to install')
                install = worker.submit('download',model=model)
                worker.install(install['id'],model)
            wait_for(lambda:worker.status()['models'][model]['downloaded'],timeout=60)
            server = ThreadingHTTPServer(('127.0.0.1',0),handler(worker,''))
            threading.Thread(target=server.serve_forever,daemon=True).start()
            env = {k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')}
            env['KAPONIR_WORKER_URL'] = f'http://127.0.0.1:{server.server_port}'
            subprocess.run([args.binary.resolve(),'--home',home,'init'],env=env,capture_output=True,check=True)
            base = f'http://127.0.0.1:{free_port()}'
            with (args.output/(model+'.log')).open('w') as log:
                process = subprocess.Popen([args.binary.resolve(),'--home',home,'serve','--listen',base.split('://')[1]],env=env,stdout=log,stderr=log)
                api = lambda path,body=None,method=None: request(base,path,body,method)
                wait_for(lambda:api('/api/v1/ui/info'))
                analysis = api('/api/v1/ui/analysis')['settings']
                analysis.update(provider='local',model=model,enabled=True,background=False,auto_apply=False,parallelism=1,timeout_seconds=300)
                category = api('/api/v1/ui/categorization')['settings'];category['provider']='none'
                api('/api/v1/ui/categorization',{'settings':category},'PUT')
                api('/api/v1/ui/analysis',{'settings':analysis},'PUT')
                sampler.start()
                started = time.monotonic()
                api('/api/v1/ui/analysis/documents',{'documents':[{'name':name,'text':(ROOT/'tests/docs/input'/name).read_text()} for name in DOCUMENTS]})
                data = wait_for(lambda:(d if (d:=api('/api/v1/ui/learning'))['summary']['totals']['processed']+d['summary']['totals']['failed']>=len(DOCUMENTS) else None),timeout=3600)
                report['discovery_seconds'] = round(time.monotonic()-started,2)
                report['entries'], report['jobs'], report['learning_summary'] = data['entries'],data['jobs'],data['summary']
                expected_documents = json.loads((ROOT/'tests/docs/expected.json').read_text())['documents']
                expected = {(d['file'],v['value']) for d in expected_documents if d['file'] in DOCUMENTS for v in d['sensitive']}
                found = {(e['source'],e['value']) for e in data['entries']}
                report['discovery'] = {'expected':len(expected),'proposed':len(found),'matched':len(expected&found),
                    'missed':len(expected-found),'unmatched':len(found-expected),
                    'missed_values':sorted(expected-found),'unmatched_values':sorted(found-expected),
                    'public_document_findings':sum(source=='08-public-reference.md' for source,_ in found),
                    'raw_financial_type_conflicts':sum(any(c in e['value'] for c in ['₽','%']) and e['proposed_kind'] not in ['AMOUNT','CUSTOM'] for e in data['entries']),
                    'applied_rules':sum(e['state']=='active' for e in data['entries'])}
                save()
                report['review_cases'] = review_cases(worker,model)
                report['review_summary'] = {'cases':len(report['review_cases']),
                    'valid':sum(r['valid'] for r in report['review_cases']),
                    'action_correct':sum(r['action_correct'] for r in report['review_cases']),
                    'masked_type_correct':sum(r.get('kind_correct') is True for r in report['review_cases']),
                    'masked_scope_correct':sum(r.get('scope_correct') is True for r in report['review_cases'])}
                report['review_summary'].update(score_application_review(report['review_cases']))
                report['status'] = 'completed'
        except Exception as exc:
            report['status'],report['error'] = 'failed',str(exc)[:240]
        finally:
            sampler_stop.set()
            if sampler.is_alive(): sampler.join(timeout=2)
            report['peak_memory'] = peak
            worker.stop.set();worker.stop_runtime()
            if process: process.terminate();process.wait(timeout=20)
            if server: server.shutdown();server.server_close()
            save()
            if args.remove_after and model!=args.models[-1]:
                (args.model_dir/artifact[0]).unlink(missing_ok=True)
    print(json.dumps({k:v for k,v in report.items() if k in ['model','status','error','discovery','review_summary','peak_memory','discovery_seconds']},ensure_ascii=False),flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path)
    parser.add_argument('--model-dir',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('artifacts/model-comparison'))
    parser.add_argument('--models',nargs='+',choices=MODELS,default=MODELS)
    parser.add_argument('--download',action='store_true')
    parser.add_argument('--remove-after',action='store_true',help='Remove each completed model except the last to bound disk usage')
    parser.add_argument('--rescore',action='store_true',help='Rebuild application validation metrics from saved raw responses, without inference')
    args = parser.parse_args();args.output.mkdir(parents=True,exist_ok=True);args.model_dir.mkdir(parents=True,exist_ok=True)
    if args.rescore:
        results=[]
        for model in args.models:
            path=args.output/(model+'.json');result=json.loads(path.read_text())
            result.setdefault('review_summary',{}).update(score_application_review(result.get('review_cases',[])))
            result['settings']['runtime_binary_sha256']=hashlib.sha256(args.runtime.read_bytes()).hexdigest()
            result['discovery']['failed_documents']={job['name']:job['error'] for job in result.get('jobs',[]) if job['state']=='failed'}
            result['discovery']['successful_documents']=sum(job['state']=='succeeded' for job in result.get('jobs',[]))
            result['corpus_sha256']={name:hashlib.sha256((ROOT/'tests/docs/input'/name).read_bytes()).hexdigest() for name in DOCUMENTS}
            path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');results.append(result)
    else: results = [run_model(args,model) for model in args.models]
    (args.output/'summary.json').write_text(json.dumps([{k:v for k,v in result.items() if k not in ['entries','jobs','review_cases']} for result in results],ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__': main()
