#!/usr/bin/env python3
"""Run the same synthetic discovery corpus and review probes against an OpenAI-compatible API.

The key is read from a file or environment, never command-line text or an artifact.
A local relay records token usage/finish reasons while Kaponir runs its actual pipeline.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from compare_local_models import DOCUMENTS, ROOT, request, review_cases, score_application_review
from validate_console import free_port, wait_for


class ScopedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            raise ValueError('provider_redirect_rejected')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def parse_model_json(text):
    text=text.strip()
    if text.startswith('```json\n') or text.startswith('```\n'):
        text=text.split('\n',1)[1]
        if not text.endswith('```'):raise ValueError('invalid_model_json')
        text=text[:-3].strip()
    return json.loads(text)


def explicit_json_contract(prompt,schema):
    properties=schema.get('properties',{})
    descriptions=[]
    for key,field in properties.items():
        item_keys=list(field.get('items',{}).get('properties',{}))
        descriptions.append(key+(': array of objects with '+', '.join(item_keys) if item_keys else ''))
    return prompt+'\nReturn exactly one JSON object. Required top-level fields: '+'; '.join(descriptions)+'. No Markdown fences or bare arrays. JSON Schema: '+json.dumps(schema,ensure_ascii=False,separators=(',',':'))


class ExternalClient:
    def __init__(self, url, model, key, timeout=300, output_override=None):
        self.url=url.rstrip('/')
        if not self.url.endswith('/chat/completions'):self.url+='/chat/completions'
        self.model,self.key,self.timeout,self.output_override=model,key,timeout,output_override
        self.records=[];self.lock=threading.Lock()
        self.on_record=None
        self.opener=urllib.request.build_opener(ScopedRedirect())

    def raw(self, body):
        body={**body,'model':self.model}
        if self.output_override:body['max_tokens']=self.output_override
        record={'requested_model':self.model,'max_output_tokens':body.get('max_tokens'),
                'input_sha256':hashlib.sha256(json.dumps(body['messages'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()}
        started=time.monotonic()
        try:
            req=urllib.request.Request(self.url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+self.key})
            with self.opener.open(req,timeout=self.timeout) as response:
                raw=response.read(4*1024*1024+1)
                if len(raw)>4*1024*1024:raise ValueError('model_response_too_large')
                value=json.loads(raw)
            content=value.get('choices',[{}])[0].get('message',{}).get('content','')
            record['content_format']='json_fence' if isinstance(content,str) and content.strip().startswith('```') else 'plain'
            try:parse_model_json(content);record['content_json_valid']=True
            except (ValueError,TypeError,AttributeError):record['content_json_valid']=False
            usage=value.get('usage',{})
            record.update(model=value.get('model'),finish_reason=value.get('choices',[{}])[0].get('finish_reason'),
                usage={k:v for k,v in usage.items() if isinstance(v,(int,float)) or k.endswith('_tokens_details') and isinstance(v,dict) and all(isinstance(n,(int,float)) for n in v.values())})
            return value
        except urllib.error.HTTPError as exc:
            record['http_status']=exc.code
            raise ValueError('model_http_error') from None
        except (OSError,urllib.error.URLError):
            record['error']='model_connection_failed'
            raise ValueError('model_connection_failed') from None
        finally:
            record['elapsed_seconds']=round(time.monotonic()-started,3)
            with self.lock:self.records.append(record)
            if self.on_record:self.on_record()

    def evaluate(self, body):
        value=self.raw({'model':self.model,'temperature':0,'max_tokens':1600,'stream':False,
            'messages':[{'role':'system','content':body['prompt']},{'role':'user','content':json.dumps(body['input'],ensure_ascii=False)}],
            'response_format':{'type':'json_schema','json_schema':{'name':'learning','strict':True,'schema':body['schema']}}})
        choice=value['choices'][0]
        if choice.get('finish_reason')!='stop':raise ValueError('model_response_truncated')
        return parse_model_json(choice['message']['content'].replace(self.key,'[REDACTED]'))


def relay_handler(client):
    class Relay(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=3*1024*1024:raise ValueError('invalid_request')
                response=client.raw(json.loads(self.rfile.read(size)))
                status=200
            except Exception as exc:
                response={'error':type(exc).__name__};status=502
            raw=json.dumps(response).replace(client.key,'[REDACTED]').encode()
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    return Relay


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path)
    parser.add_argument('--url',required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--key-file',type=Path)
    parser.add_argument('--key-env',default='KAPONIR_EVAL_API_KEY')
    parser.add_argument('--timeout',type=int,default=300)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-output-tokens',type=int,help='Explicit experimental override; omitted uses the current application budget')
    parser.add_argument('--documents',nargs='+',default=DOCUMENTS)
    parser.add_argument('--profile',type=Path,help='JSON containing inference and chunking settings for the actual application')
    parser.add_argument('--skip-review',action='store_true')
    parser.add_argument('--retry-failed',type=int,default=0,help='Explicitly retry failed HTTP/connection jobs through the application API; every attempt remains recorded')
    parser.add_argument('--resume-home',type=Path,help='Resume this evaluator\'s isolated home after an interrupted process')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    key=args.key_file.read_text().strip() if args.key_file else os.environ.get(args.key_env,'')
    if not key:parser.error('API key is missing')
    client=ExternalClient(args.url,args.model,key,args.timeout,args.max_output_tokens)
    server=ThreadingHTTPServer(('127.0.0.1',0),relay_handler(client))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    result={'requested_model':args.model,'documents':args.documents,'status':'running','output_override':args.max_output_tokens}
    def save():
        result['calls']=client.records
        (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2).replace(key,'[REDACTED]')+'\n')
    save()
    client.on_record=save
    extra_server=None
    context=contextlib.nullcontext(args.resume_home) if args.resume_home else tempfile.TemporaryDirectory(prefix='kaponir-external-eval-')
    with context as tmp:
        home=Path(tmp);env={k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')}
        if args.resume_home:
            if not (home/'vault.db').is_file():raise ValueError('resume_home_missing')
            (home/'worker'/'activation.json').write_text('{"enabled":false}\n')
        else:subprocess.run([args.binary.resolve(),'--home',home,'init'],env=env,check=True,capture_output=True)
        result['home']=str(home)
        base=f'http://127.0.0.1:{free_port()}';log=(args.output/'server.log').open('w')
        process=subprocess.Popen([args.binary.resolve(),'--home',home,'serve','--listen',base.split('://')[1]],env=env,stdout=log,stderr=log)
        api=lambda path,body=None,method=None:request(base,path,body,method)
        try:
            wait_for(lambda:api('/api/v1/ui/info'))
            category=api('/api/v1/ui/categorization')['settings'];category['provider']='none'
            api('/api/v1/ui/categorization',{'settings':category},'PUT')
            analysis=api('/api/v1/ui/analysis')['settings']
            relay_url=f'http://127.0.0.1:{server.server_port}/v1/chat/completions'
            if args.resume_home:
                previous=urllib.parse.urlsplit(analysis['url'])
                if previous.hostname not in ['127.0.0.1','localhost'] or not previous.port:raise ValueError('invalid_resume_relay')
                extra_server=ThreadingHTTPServer(('127.0.0.1',previous.port),relay_handler(client))
                threading.Thread(target=extra_server.serve_forever,daemon=True).start()
                relay_url=analysis['url']
                result['resumed']=True
                result['previous_jobs']=api('/api/v1/ui/learning')['jobs']
            analysis.update(enabled=True,background=False,auto_apply=False,provider='api',model=args.model,url=relay_url,timeout_seconds=min(args.timeout,300),parallelism=1)
            if args.profile:
                profile=json.loads(args.profile.read_text())
                for field in ['inference','chunking']:
                    if field in profile:analysis[field]=profile[field]
            result['profile']={field:analysis.get(field) for field in ['inference','chunking']}
            api('/api/v1/ui/analysis',{'settings':analysis},'PUT')
            api('/api/v1/ui/worker/start',{})
            started=time.monotonic()
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':name,'text':(ROOT/'tests/docs/input'/name).read_text()} for name in args.documents]})
            def idle():
                latest=api('/api/v1/ui/learning')
                return latest if not any(j['state'] in ['queued','running'] for j in latest['jobs']) else None
            data=wait_for(idle,timeout=args.timeout*30)
            result['initial_failures']={job['name']:job['error'] for job in data['jobs'] if job['state']=='failed'}
            result['retry_rounds']=0
            for _ in range(args.retry_failed):
                failed=[job for job in data['jobs'] if job['state']=='failed' and job['error'] in ['model_http_error','model_connection_failed','model_timeout']]
                if not failed:break
                result['retry_rounds']+=1
                for job in failed:api('/api/v1/ui/learning/'+urllib.parse.quote(job['id'],safe='')+'/retry',{})
                data=wait_for(idle,timeout=args.timeout*30)
            result.update(discovery_seconds=round(time.monotonic()-started,2),entries=data['entries'],jobs=data['jobs'],learning_summary=data['summary'])
            gold=json.loads((ROOT/'tests/docs/expected.json').read_text())['documents']
            expected={(d['file'],v['value']) for d in gold if d['file'] in args.documents for v in d['sensitive']}
            found={(e['source'],e['value']) for e in data['entries']}
            result['discovery']={'expected':len(expected),'matched':len(expected&found),'proposed':len(found),'missed':sorted(expected-found),'unmatched':sorted(found-expected),
                'failed_documents':{job['name']:job['error'] for job in data['jobs'] if job['state']=='failed'},'public_document_findings':sum(source=='08-public-reference.md' for source,_ in found)}
            save()
            result['review_cases']=[] if args.skip_review else review_cases(client,args.model)
            result['review_summary']={'cases':len(result['review_cases']),'valid':sum(r['valid'] for r in result['review_cases']),'action_correct':sum(r['action_correct'] for r in result['review_cases']),
                'kind_correct':sum(r.get('kind_correct') is True for r in result['review_cases']),'scope_correct':sum(r.get('scope_correct') is True for r in result['review_cases']),**score_application_review(result['review_cases'])}
            result['status']='completed'
        except Exception as exc:
            result['status']='failed';result['error']=type(exc).__name__
            raise
        finally:
            save();process.terminate();process.wait(timeout=20);server.shutdown();server.server_close();log.close()
            if extra_server:extra_server.shutdown();extra_server.server_close()
    print(json.dumps({k:v for k,v in result.items() if k in ['requested_model','status','discovery','review_summary','discovery_seconds']},ensure_ascii=False))


if __name__=='__main__':main()
