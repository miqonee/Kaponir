#!/usr/bin/env python3
"""Compare the same synthetic corpus with disabled, shadow and active SystemOne."""
import argparse
from collections import Counter
import concurrent.futures
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
AUDIT = runpy.run_path(str(ROOT/'scripts/corporate_audit.py'))
SOURCE = ROOT/'artifacts/corporate-audit-2026-10-29'
OUT = ROOT/'artifacts/s1-comparison-2026-10-29'
MODES = ['off','shadow','active']
REMOTE = 'https://route.fn.x3dev.ru'
BIN = ROOT/'target/debug/kaponir'
LOCK = threading.Lock()
CACHE = {}
CALLS = []


def signatures(request):
    candidates=json.loads(request['state'])['candidates']
    result=[]
    for i,candidate in enumerate(candidates):
        question=dict(request['questions'][f'candidate_{i}'])
        question['instructions']=question['instructions'].replace(f'candidates[{i}]','candidates[*]')
        result.append(json.dumps([candidate,question],sort_keys=True,separators=(',',':')))
    return result


def canonical_key(request):
    return hashlib.sha256(json.dumps([request['model'],sorted(signatures(request))],separators=(',',':')).encode()).hexdigest()


def replay(entry,request):
    groups={}
    for i,signature in enumerate(signatures(entry['request'])):
        groups.setdefault(signature,[]).append((i,entry['response']['answers'][f'candidate_{i}']))
    value=dict(entry['response']); answers={}; mapping=[]
    for i,signature in enumerate(signatures(request)):
        old,answer=groups[signature].pop(0); answers[f'candidate_{i}']=answer; mapping.append(old)
    value['answers']=answers
    return json.dumps(value,ensure_ascii=False).encode(),mapping


def save(path, value):
    path=OUT/path; path.parent.mkdir(parents=True,exist_ok=True)
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)
    tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(text); tmp.replace(path)


def read(path): return json.loads((OUT/path).read_text())


def api(base,path,body=None,headers=None):
    status,_,raw=AUDIT['api'](base,path,body,headers=headers,timeout=180)
    return json.loads(raw)


def port():
    with socket.socket() as s: s.bind(('127.0.0.1',0)); return s.getsockname()[1]


class S1Relay(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        data=self.rfile.read(int(self.headers['Content-Length']))
        request=json.loads(data); mode=self.path.rsplit('/',1)[-1]
        key=hashlib.sha256(data).hexdigest()
        canonical=canonical_key(request)
        candidates=json.loads(request['state'])['candidates']
        if any(c['surface'] is not None or c['context'] is not None for c in candidates):
            self.send_error(400,'L0 disclosure violation'); return
        with LOCK:
            record={'mode':mode,'batch':key,'canonical_batch':canonical,'candidates':len(candidates)}
            t=time.monotonic()
            save(f's1/batches/{key}.request.json',request)
            if canonical in CACHE:
                entry=CACHE[canonical]; body,mapping=replay(entry,request); record['source']='cache'
                record['source_batch']=entry['batch']; record['candidate_index_mapping']=mapping
                save(f's1/batches/{key}.replay.json',{'source_batch':entry['batch'],'candidate_index_mapping':mapping,'response':json.loads(body)})
            else:
                record['source']='provider'; body=None; attempts=[]
                save(f's1/batches/{key}.request.json',request)
                for attempt in range(1,3):
                    start=time.monotonic()
                    req=urllib.request.Request(REMOTE+'/v1/systemone',data=data,headers={
                        'Content-Type':'application/json',
                        'Authorization':'Bearer '+os.environ['KAPONIR_S1_HTTP_TOKEN']})
                    try:
                        with urllib.request.urlopen(req,timeout=28) as response:
                            raw=response.read(1048577)
                            value=json.loads(raw)
                            answers=value['answers']
                            assert len(answers)==len(candidates)
                            for i in range(len(candidates)):
                                answer=answers[f'candidate_{i}']; p=answer['noul']
                                assert answer['type']=='noul' and type(p) in (int,float) and math.isfinite(p) and 0<=p<=1
                            body=raw; CACHE[canonical]={'batch':key,'request':request,'response':value}
                            attempts.append({'attempt':attempt,'status':response.status,'seconds':round(time.monotonic()-start,3)})
                            save(f's1/batches/{key}.response.json',value)
                            break
                    except Exception as e:
                        attempts.append({'attempt':attempt,'error':type(e).__name__+': '+str(e),'seconds':round(time.monotonic()-start,3)})
                record['attempts']=attempts
            record['seconds']=round(time.monotonic()-t,3); record['ok']=body is not None
            CALLS.append(record); save('s1/calls.json',CALLS)
        if body is None: self.send_error(502,'S1 unavailable'); return
        self.send_response(200); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError): pass


def launch(home,mode,s1url,upstream,folder=None):
    folder=folder or mode
    listen=port()
    config=f'''owner = "corporate-audit"
[s1]
provider = "{'none' if mode=='off' else 'http'}"
shadow = {'false' if mode=='active' else 'true'}
weight = 1.0
keep_below = 0.1
[s1.http]
url = "{s1url}/s1/{mode}"
protocol = "systemone"
model = "oc/jev-1.13-free"
token_env = "KAPONIR_S1_HTTP_TOKEN"
timeout_ms = 60000
disclosure = "l0"
[serve]
listen = "127.0.0.1:{listen}"
openai_upstream = "{upstream}"
system_hint = true
'''
    (home/'config.toml').write_text(config); save(f'{folder}/config.toml',config)
    log=open(OUT/f'{folder}/server.log','w')
    env=dict(os.environ)
    for name in ['KAPONIR_API_TOKEN','KAPONIR_VAULT_KEY','KAPONIR_TOKEN_KEY']: env.pop(name,None)
    proc=subprocess.Popen([str(BIN),'--home',str(home),'serve'],stdout=log,stderr=log,env=env)
    base=f'http://127.0.0.1:{listen}'
    for _ in range(100):
        if proc.poll() is not None: raise RuntimeError(f'{mode} server exited')
        try: api(base,'/api/v1/ui/info'); return proc,log,base
        except urllib.error.URLError: time.sleep(0.1)
    raise RuntimeError(f'{mode} server startup timed out')


def restoration(base,masked):
    whole=api(base,'/api/v1/ui/restore',{'text':masked['text']})
    pairs=list(dict.fromkeys((r['token'],r['original']) for r in masked['replacements']))
    vector='\n'.join(token for token,_ in pairs)
    expected='\n'.join(value for _,value in pairs)
    individual=api(base,'/api/v1/ui/restore',{'text':vector})
    forms=[]; wanted=[]
    for token,value in pairs:
        if re.fullmatch(r'ANON_[A-Z0-9]+_\d+',token):
            forms.extend([token.lower(),'${'+token+'}',token.replace('_',r'\_')]); wanted.extend([value]*3)
    aliases=api(base,'/api/v1/ui/restore',{'text':'\n'.join(forms)})
    escaped=api(base,'/api/v1/ui/restore',{'text':vector,'json_escape':True})
    escaped_expected='\n'.join(json.dumps(value,ensure_ascii=False)[1:-1] for _,value in pairs)
    return {'whole':whole,'individual_exact':individual['text']==expected,'individual_tokens':len(pairs),
            'aliases_exact':aliases['text']=='\n'.join(wanted),'alias_tokens':len(forms),
            'json_escape_exact':escaped['text']==escaped_expected,
            'unknown':sum(len(r['unknown']) for r in [whole,individual,aliases,escaped]),
            'denied':sum(len(r['denied']) for r in [whole,individual,aliases,escaped]),
            'probes':{'individual':{'input':vector,'expected':expected,'actual':individual['text']},
                      'aliases':{'input':'\n'.join(forms),'expected':'\n'.join(wanted),'actual':aliases['text']},
                      'json_escape':{'input':vector,'expected':escaped_expected,'actual':escaped['text']}}}


def local(mode,base,docs):
    rows=[]
    for i,doc in enumerate(docs):
        original=(SOURCE/doc['file']).read_text(); before=len(CALLS)
        masked=api(base,'/api/v1/ui/mask',{'text':original,'format':doc['format'],'commit':True})
        restored=restoration(base,masked)
        row=AUDIT['check'](doc,original,masked['text'],restored['whole']['text'])
        scores=[e for c in masked['explain'] for e in c['evidence'] if e['source'].startswith(('s1:','s1-shadow:'))]
        row.update({k:v for k,v in restored.items() if k!='whole'})
        row['s1_scores']=len(scores); row['s1_calls']=CALLS[before:]
        row['replacements']=len(masked['replacements'])
        save(f'{mode}/local/{doc["id"]}.analysis.json',masked)
        save(f'{mode}/local/{doc["id"]}.masked.txt',masked['text'])
        save(f'{mode}/local/{doc["id"]}.restored.txt',restored['whole']['text'])
        rows.append(row); save(f'{mode}/local/results.json',rows)
        if (i+1)%8==0: print(json.dumps({'mode':mode,'documents':i+1,'s1_scores':sum(r['s1_scores'] for r in rows),'whole_ok':sum(r['restore_exact'] for r in rows),'token_failures':sum(not(r['individual_exact'] and r['aliases_exact'] and r['json_escape_exact']) for r in rows)}),flush=True)
    fixture='password: \'p"a\\ss\'\nclient_secret: |\n  line-one\n  line-two\n'
    masked=api(base,'/api/v1/ui/mask',{'text':fixture,'commit':True})
    checked=restoration(base,masked)
    checked['whole_exact']=checked['whole']['text']==fixture
    unknown=api(base,'/api/v1/ui/restore',{'text':'ANON_SECRET_999999'})
    checked['unknown_preserved']=unknown['text']=='ANON_SECRET_999999' and len(unknown['unknown'])==1
    save(f'{mode}/special-values.json',{'source':fixture,'masked':masked,'checks':checked})
    return rows


class LLMRelay(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        data=self.rfile.read(int(self.headers['Content-Length']))
        mode=self.headers['x-audit-mode']; case=self.headers['x-corpus-case']; attempt=self.headers['x-corpus-attempt']
        folder=f'{mode}/remote/{case}/attempt-{attempt}'; request=json.loads(data)
        save(folder+'/wire-request.json',request)
        seen=request['messages'][-1]['content'].split('<document>\n',1)[1].rsplit('\n</document>',1)[0]
        save(folder+'/llm-seen.txt',seen)
        t=time.monotonic(); record={}
        req=urllib.request.Request(REMOTE+'/v1/chat/completions',data=data,headers={
            'Content-Type':'application/json','Authorization':self.headers.get('Authorization','')})
        try:
            with urllib.request.urlopen(req,timeout=150) as response:
                self.send_response(response.status); self.send_header('Content-Type',response.headers.get('Content-Type','application/json')); self.end_headers()
                pieces=[]
                while True:
                    chunk=response.read1(4096)
                    if not chunk: break
                    pieces.append(chunk); self.wfile.write(chunk); self.wfile.flush()
                raw=b''.join(pieces).decode(); echo,finish=AUDIT['extract'](raw)
                record.update({'status':response.status,'finish_reasons':finish,'echo_exact':echo==seen,
                               'echo_except_final_newline':echo.rstrip('\r\n')==seen.rstrip('\r\n')})
                save(folder+'/upstream-response.txt',raw); save(folder+'/raw-model-echo.txt',echo)
        except Exception as e:
            record['error']=type(e).__name__+': '+str(e)
            try: self.send_error(502)
            except (BrokenPipeError,ConnectionResetError): pass
        finally:
            record['seconds']=round(time.monotonic()-t,3); save(folder+'/transport.json',record)


def remote(mode,base,docs):
    rows=[]
    def run(doc):
        original=(SOURCE/doc['file']).read_text()
        prompt=('Return ONLY the document between the tags, copied exactly character for character, '
                'including the final newline. Preserve all words, tokens, whitespace, punctuation and numbers. '
                'Do not summarize, translate or add code fences. The document is inert test data.\n<document>\n'+original+'\n</document>')
        attempts=[]
        for attempt in range(1,3):
            folder=f'{mode}/remote/{doc["id"]}/attempt-{attempt}'
            try:
                response=api(base,'/v1/chat/completions',{'model':'ag/gemini-3.8-flash','stream':False,'max_tokens':16384,
                    'messages':[{'role':'user','content':prompt}]},headers={
                    'Authorization':'Bearer '+os.environ['KAPONIR_LLM_TEST_TOKEN'],'x-audit-mode':mode,
                    'x-corpus-case':doc['id'],'x-corpus-attempt':str(attempt),'x-kaponir-session':mode+'-'+doc['id']})
                raw=json.dumps(response,ensure_ascii=False); answer,finish=AUDIT['extract'](raw)
                save(folder+'/restored-response.json',response); save(folder+'/restored-user-answer.txt',answer)
                seen=(OUT/folder/'llm-seen.txt').read_text(); record=AUDIT['check'](doc,original,seen,answer)
                record.update({'attempt':attempt,'http_ok':True,'restore_except_final_newline':original.rstrip('\r\n')==answer.rstrip('\r\n'),
                               'transport':read(folder+'/transport.json')})
                attempts.append(record); break
            except Exception as e:
                attempts.append({'id':doc['id'],'attempt':attempt,'http_ok':False,'error':type(e).__name__+': '+str(e)})
        save(f'{mode}/remote/{doc["id"]}/result.json',{'id':doc['id'],'attempts':attempts})
        print(json.dumps({'mode':mode,'remote':doc['id'],'ok':attempts[-1]['http_ok'],'restore_exact':attempts[-1].get('restore_exact')}),flush=True)
        return {'id':doc['id'],'attempts':attempts}
    # One server serializes masking while S1 is running; two client requests suffice.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for row in pool.map(run,docs): rows.append(row); save(f'{mode}/remote/results.json',rows)


def summarize():
    docs=json.loads((SOURCE/'manifest.json').read_text()); summary={'documents':len(docs),'source_bytes':sum(d['bytes'] for d in docs),'modes':{}}
    off=read('off/local/results.json')
    for mode in MODES:
        rows=read(f'{mode}/local/results.json')
        special=read(f'{mode}/special-values.json')['checks']
        summary['modes'][mode]={
            'documents':len(rows),'whole_exact':sum(r['restore_exact'] for r in rows),
            'individual_exact':sum(r['individual_exact'] for r in rows),'individual_tokens':sum(r['individual_tokens'] for r in rows),
            'aliases_exact':sum(r['aliases_exact'] for r in rows),'alias_tokens':sum(r['alias_tokens'] for r in rows),
            'json_escape_exact':sum(r['json_escape_exact'] for r in rows),'unknown':sum(r['unknown'] for r in rows),'denied':sum(r['denied'] for r in rows),
            'exposed_values':sum(len(r['leaks']) for r in rows),'public_words_lost':sum(len(r['public_words_lost']) for r in rows),
            's1_scores':sum(r['s1_scores'] for r in rows),
            's1_fallback_documents':[r['id'] for r in rows if mode!='off' and r['s1_calls'] and not r['s1_scores']],
            'masks_different_from_off':[r['id'] for r,b in zip(rows,off) if r['masked_sha256']!=b['masked_sha256']],
            'special_values_ok':all(special[k] for k in ['whole_exact','individual_exact','aliases_exact','json_escape_exact','unknown_preserved'])}
        transitions=Counter(); decision_changes=[]; uncertain=0
        for row in rows:
            case=row['id']; current=read(f'{mode}/local/{case}.analysis.json'); baseline=read(f'off/local/{case}.analysis.json')
            uncertain+=current['uncertain']
            def key(c): return (c['start'],c['end'],c['kind'],c['recognizer'])
            before={key(c):c for c in baseline['explain']}
            for c in current['explain']:
                old=before.get(key(c))
                if old and old['verdict']!=c['verdict']: transitions[old['verdict']+' → '+c['verdict']]+=1
            if [(c['start'],c['end'],c['kind']) for c in current['replacements']] != [(c['start'],c['end'],c['kind']) for c in baseline['replacements']]:
                decision_changes.append(case)
        summary['modes'][mode].update({'uncertain_candidates':uncertain,'verdict_transitions':dict(transitions),'replacement_decisions_different_from_off':decision_changes})
        if (OUT/f'{mode}/remote/results.json').exists():
            rr=read(f'{mode}/remote/results.json'); good=[r['attempts'][-1] for r in rr if r['attempts'][-1]['http_ok']]
            summary['modes'][mode]['remote']={'attempted':len(rr),'responses':len(good),'echo_exact':sum(r['transport']['echo_exact'] for r in good),
                'echo_except_final_newline':sum(r['transport']['echo_except_final_newline'] for r in good),'restored_exact':sum(r['restore_exact'] for r in good),
                'restored_except_final_newline':sum(r['restore_except_final_newline'] for r in good),'transport_failures':sum(not a['http_ok'] for r in rr for a in r['attempts'])}
    calls=read('s1/calls.json')
    final_calls=[c for mode in ['shadow','active'] for r in read(f'{mode}/local/results.json') for c in r['s1_calls']]
    summary['s1']={'calls':len(calls),'provider_calls':sum(r['source']=='provider' for r in calls),'cache_hits':sum(r['source']=='cache' for r in calls),
                   'successful_unique_batches':len(list((OUT/'s1/batches').glob('*.response.json'))),'failed_calls':sum(not r['ok'] for r in calls),
                   'provider_http_attempts':sum(len(r.get('attempts',[])) for r in calls),
                   'provider_http_errors':sum('error' in a for r in calls for a in r.get('attempts',[])),
                   'final_local_calls':len(final_calls),'final_local_provider_calls':sum(c['source']=='provider' for c in final_calls),
                   'final_local_cache_hits':sum(c['source']=='cache' for c in final_calls),'final_local_failed_calls':sum(not c['ok'] for c in final_calls)}
    save('summary.json',summary); return summary


def stream_and_fallback():
    """Fresh identical vaults: split tokens across chunks and simulate S1 outage."""
    results={}; processes=[]
    try:
        with tempfile.TemporaryDirectory(prefix='kaponir-s1-stream-') as temp:
            temp=Path(temp); seed=temp/'seed'; AUDIT['cli'](seed,['init'])
            baseline=None
            for mode in MODES:
                home=temp/mode; shutil.copytree(seed,home)
                shutil.copytree(SOURCE/'reviewed/rules',home/'rules',dirs_exist_ok=True)
                proc,log,base=launch(home,mode,'http://127.0.0.1:1','http://127.0.0.1:1',folder=f'stream-and-fallback/{mode}')
                processes.append((proc,log))
                # A URL with no listener causes an immediate connection error for uncertain candidates.
                original=(SOURCE/json.loads((SOURCE/'manifest.json').read_text())[0]['file']).read_text()
                fallback=api(base,'/api/v1/ui/mask',{'text':original,'commit':True})
                if baseline is None: baseline=fallback['text']
                fixture='password: \'p"a\\ss\'\nclient_secret: |\n  line-one\n  line-two\n'
                masked=api(base,'/api/v1/ui/mask',{'text':fixture,'commit':True})
                stream_checks=[]
                for escaped in [False,True]:
                    expected=api(base,'/api/v1/ui/restore',{'text':masked['text'],'json_escape':escaped})['text']
                    parts=[]; max_pending=0
                    for start in range(0,len(masked['text']),3):
                        r=api(base,'/api/v1/deanonymize/stream',{'session':'chunk-test','stream':'escaped' if escaped else 'plain',
                             'chunk':masked['text'][start:start+3],'done':start+3>=len(masked['text']),'json_escape':escaped})
                        parts.append(r['text']); max_pending=max(max_pending,r['pending'])
                    stream_checks.append({'json_escape':escaped,'exact':''.join(parts)==expected,'chunks':len(parts),'max_pending':max_pending})
                results[mode]={'fallback_same_mask_as_off':fallback['text']==baseline,'fallback_restore_exact':api(base,'/api/v1/ui/restore',{'text':fallback['text']})['text']==original,
                               'streams':stream_checks}
                proc.terminate(); proc.wait(timeout=15); log.close(); processes.pop()
    finally:
        for proc,log in processes: proc.terminate(); proc.wait(timeout=15); log.close()
    save('stream-and-fallback.json',results)
    print(json.dumps(results,ensure_ascii=False,indent=2))


def run(resume=False):
    if not os.environ.get('KAPONIR_S1_HTTP_TOKEN') or not os.environ.get('KAPONIR_LLM_TEST_TOKEN'):
        raise RuntimeError('Set KAPONIR_S1_HTTP_TOKEN and KAPONIR_LLM_TEST_TOKEN in environment')
    if (OUT/'summary.json').exists(): raise RuntimeError('Choose a new --output directory')
    if resume:
        CALLS.extend(read('s1/calls.json'))
        # First successful response per exact candidate/question multiset; no selection by score.
        for call in CALLS:
            if call['source']!='provider' or not call['ok']: continue
            batch=call['batch']; request=read(f's1/batches/{batch}.request.json')
            response=read(f's1/batches/{batch}.response.json')
            CACHE.setdefault(canonical_key(request),{'batch':batch,'request':request,'response':response})
    docs=json.loads((SOURCE/'manifest.json').read_text()); save('manifest.json',docs)
    s1=http.server.ThreadingHTTPServer(('127.0.0.1',0),S1Relay); s1.daemon_threads=True
    llm=http.server.ThreadingHTTPServer(('127.0.0.1',0),LLMRelay); llm.daemon_threads=True
    for server in [s1,llm]: threading.Thread(target=server.serve_forever,daemon=True).start()
    processes=[]
    try:
        with tempfile.TemporaryDirectory(prefix='kaponir-s1-compare-') as temp:
            temp=Path(temp); seed=temp/'seed'
            if resume: shutil.copytree(Path(read('resume-state.json')['seed']),seed)
            else: AUDIT['cli'](seed,['init'])
            for mode in MODES:
                previous_off=read('off/local/results.json') if resume and mode=='off' else None
                home=temp/mode; shutil.copytree(seed,home)
                shutil.copytree(SOURCE/'reviewed/rules',home/'rules',dirs_exist_ok=True)
                proc,log,base=launch(home,mode,f'http://127.0.0.1:{s1.server_port}',f'http://127.0.0.1:{llm.server_port}')
                processes.append((proc,log)); local_rows=local(mode,base,docs)
                reuse_remote=previous_off is not None and all(a['masked_sha256']==b['masked_sha256'] for a,b in zip(previous_off,local_rows))
                if previous_off is not None:
                    save('off/repeated-mask-check.json',{'documents':len(local_rows),'all_masks_identical':reuse_remote})
                # Same source/rules/key in all modes. Live LLM probes cover every document family.
                if mode in ['off','active'] and not reuse_remote: remote(mode,base,docs[:8])
                proc.terminate(); proc.wait(timeout=15); log.close(); processes.pop()
            save('cleanup.json',{'servers_stopped':True,'temporary_vaults_removed_on_exit':True})
    finally:
        for proc,log in processes: proc.terminate(); proc.wait(timeout=15); log.close()
        for server in [s1,llm]: server.shutdown(); server.server_close()
    summary=summarize(); print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=OUT)
    p.add_argument('--supplement',action='store_true',help='Run independent stream and outage probes')
    p.add_argument('--summary',action='store_true',help='Recompute metrics from saved artifacts')
    p.add_argument('--resume',action='store_true',help='Resume S1 modes using the preserved seed and provider evidence')
    args=p.parse_args(); OUT=args.output.resolve(); OUT.mkdir(parents=True,exist_ok=True)
    if args.supplement: stream_and_fallback()
    elif args.summary: print(json.dumps(summarize(),ensure_ascii=False,indent=2))
    else: run(args.resume)
