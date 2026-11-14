#!/usr/bin/env python3
"""Controlled chunking and independent classification experiments on synthetic fixtures.

Local planning uses the loaded GGUF's actual chat template and tokenizer. Every
call records token usage and finish reason. No automatic rule changes are made.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import time
from kaponir_worker import Worker, configuration, http_json, schema_prompt
from kaponir_inference import plan_text, split_span, document_hint
from compare_local_models import ROOT, DOCUMENTS, score_application_review
from evaluate_external_model import ExternalClient, parse_model_json, explicit_json_contract


def prompts_and_kinds():
    code=(ROOT/'crates/kaponir-proxy/src/learning.rs').read_text()
    extract=re.search(r'const EXTRACT_PROMPT: &str = "([^"]+)";',code).group(1)
    review=re.search(r'const PROMPT: &str = "([^"]+)";',code).group(1)
    kinds=re.findall(r'"([A-Z_]+)"',(ROOT/'crates/kaponir-core/src/kinds.rs').read_text().split('pub const STANDARD:')[1].split('];')[0])
    return extract,review,kinds


def sampling_settings(value=None):
    defaults={'temperature':0.0,'top_p':0.95,'top_k':40,'min_p':0.0}
    value={} if value is None else value
    if not isinstance(value,dict) or set(value)-set(defaults)-{'seed'}:raise ValueError('invalid_sampling_settings')
    result={**defaults,**value}
    for name,lower,upper in [('temperature',0,2),('top_p',0,1),('min_p',0,1)]:
        v=result[name]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not lower<=v<=upper or name=='top_p' and v==0:raise ValueError('invalid_sampling_settings')
    if type(result['top_k']) is not int or not 0<=result['top_k']<=10000:raise ValueError('invalid_sampling_settings')
    if result.get('seed') is None:result.pop('seed',None)
    elif type(result['seed']) is not int or not 0<=result['seed']<=2**32-1:raise ValueError('invalid_sampling_settings')
    return result


class LocalClient:
    def __init__(self, worker, model, context_tokens, output_tokens, response_format='grammar', sampling=None):
        self.sampling=sampling_settings(sampling)
        self.worker,self.model,self.context_tokens,self.output_tokens=worker,model,context_tokens,output_tokens
        deadline=time.monotonic()+30
        while not worker.status()['models'][model]['downloaded']:
            if time.monotonic()>deadline:raise ValueError('model_not_ready')
            time.sleep(.05)
        job=worker.submit('evaluate',model=model)
        worker.start_runtime(job['id'],model,1,context_tokens=context_tokens)
        self.output_mode=('prompt' if worker.process_supports_json_schema is False else 'grammar') if response_format=='auto' else response_format
        self.planning_schema={}
        self.records=[]

    def token_count(self,text):return self.worker.count_text_tokens(text)
    def prepared_prompt(self,prompt,schema=None):
        return schema_prompt(prompt,self.planning_schema if schema is None else schema) if self.output_mode=='prompt' else prompt
    def input_tokens(self,prompt,value,schema=None):return self.worker.prompt_token_count(self.prepared_prompt(prompt,schema),value)
    def infer(self,prompt,value,schema,output_tokens):
        prepared=self.prepared_prompt(prompt,schema)
        started=time.monotonic();record={'input_tokens':self.worker.prompt_token_count(prepared,value),'max_output_tokens':output_tokens,'structured_output':self.output_mode}
        try:
            payload={
                'model':'qwen-cpu','messages':[{'role':'system','content':prepared},{'role':'user','content':json.dumps(value,ensure_ascii=False)}],
                **self.sampling,'max_tokens':output_tokens,'stream':False,'cache_prompt':False,
                'chat_template_kwargs':{'enable_thinking':False},
                'response_format':{'type':'json_schema','json_schema':{'name':'learning','strict':True,'schema':schema}}}
            if self.output_mode=='prompt':payload.pop('response_format')
            reply=http_json(self.worker.ocr_base+'/v1/chat/completions',payload,timeout=self.worker.cfg['ocr_timeout_seconds'])
            choice=reply['choices'][0];record.update(finish_reason=choice.get('finish_reason'),usage=reply.get('usage'),memory=self.worker.resources())
            if choice.get('finish_reason')!='stop':raise ValueError('model_response_truncated')
            return json.loads(choice['message']['content'])
        except Exception as exc:record['error']=type(exc).__name__;raise
        finally:record['seconds']=round(time.monotonic()-started,3);self.records.append(record)


class RemoteClient:
    def __init__(self,url,model,key,timeout,retries=0):self.client=ExternalClient(url,model,key,timeout);self.records=self.client.records;self.retries=retries;self.planning_schema={}
    # Byte count is a conservative fallback only; remote tokenizer is not advertised.
    def token_count(self,text):return len(text.encode())
    def input_tokens(self,prompt,value):return self.token_count(explicit_json_contract(prompt,self.planning_schema))+self.token_count(json.dumps(value,ensure_ascii=False))
    def infer(self,prompt,value,schema,output_tokens):
        prompt=explicit_json_contract(prompt,schema)
        for attempt in range(self.retries+1):
            try:
                reply=self.client.raw({'model':self.client.model,'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(value,ensure_ascii=False)}],
                    'temperature':0,'max_tokens':output_tokens,'stream':False,'response_format':{'type':'json_schema','json_schema':{'name':'learning','strict':True,'schema':schema}}})
                break
            except ValueError:
                if attempt==self.retries or self.records[-1].get('http_status') not in [429,500,502,503,504]:raise
                time.sleep(2**attempt)
        choice=reply['choices'][0]
        if choice.get('finish_reason')!='stop':raise ValueError('model_response_truncated')
        return parse_model_json(choice['message']['content'].replace(self.client.key,'[REDACTED]'))


def extraction_schema(kinds,mode,count):
    props={'value':{'type':'string'},'confidence':{'type':'number','minimum':0,'maximum':1}}
    if mode=='typed':props['kind']={'type':'string','enum':kinds[:16]}
    return json.loads(json.dumps({'type':'object','properties':{'candidates':{'type':'array','maxItems':count,'items':{
        'type':'object','properties':props,'required':list(props),'additionalProperties':False}}},'required':['candidates'],'additionalProperties':False},sort_keys=True))


class DiscoveryCheckpointError(ValueError):
    """Persistent experiment state is unavailable; do not score it as model output."""


def discover(client,text,prompt,kinds,args,checkpoint=lambda:None,progress=None):
    schema=extraction_schema(kinds,args.candidate_mode,args.max_candidates)
    if isinstance(client,(RemoteClient,LocalClient)):client.planning_schema=schema
    if args.candidate_mode=='lean':
        prompt=re.search(r'const EXTRACT_VALUES_PROMPT: &str = "([^"]+)";', (ROOT/'crates/kaponir-proxy/src/learning.rs').read_text()).group(1)
    hints={}
    def context(offset):
        if offset not in hints:hints[offset]=document_hint(text,offset,args.hint_tokens,client.token_count)
        return hints[offset]
    def fits(value,ctx):
        return client.input_tokens(prompt,{'context':ctx,'source':value})+args.output_tokens+args.reserve_tokens<=args.context_tokens and (not args.target_input_tokens or client.input_tokens(prompt,{'context':ctx,'source':value})<=args.target_input_tokens)
    if args.strategy=='fixed':
        raw=text.encode();pieces=[];start=0
        while start<len(raw):
            end=min(start+args.fixed_bytes,len(raw))
            while True:
                try:fragment=raw[start:end].decode();break
                except UnicodeDecodeError:end-=1
            pieces.append({'source':fragment,'context':''})
            if end==len(raw):break
            start=max(0,end-args.fixed_overlap_bytes)
            while raw[start]&0xc0==0x80:start+=1
    else:
        spans=plan_text(text,fits,context,strategy=args.strategy,max_fragments=args.max_fragments)
        pieces=[{'source':text[s['start']:s['end']],'context':s['context']} for s in spans]
    found={};errors=[];calls=0
    responses=progress.setdefault('responses',{}) if progress is not None else None
    def persist():
        try:checkpoint()
        except Exception as exc:raise DiscoveryCheckpointError('discovery_checkpoint_write_failed') from exc
    def consume(fragment,ctx):
        nonlocal calls
        if calls>=args.max_fragments:raise ValueError('document_fragment_limit')
        calls+=1
        key=hashlib.sha256(json.dumps([prompt,{'context':ctx,'source':fragment},schema,
            args.output_tokens,getattr(client,'model',None),getattr(client,'output_mode',None),
            getattr(client,'sampling',None)],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        cached=responses.get(key) if responses is not None else None
        if cached is not None:
            index=cached.get('record_index')
            if type(index) is not int or not 0<=index<len(client.records) or 'reply' not in cached:
                raise DiscoveryCheckpointError('discovery_checkpoint_record_missing')
        try:
            if cached is None:
                reply=client.infer(prompt,{'context':ctx,'source':fragment},schema,args.output_tokens)
                record=client.records[-1]
                if responses is not None:
                    responses[key]={'reply':reply,'record_index':len(client.records)-1}
                    # Persist the parent before candidate-limit recursion starts.
                    persist()
            else:
                reply=cached['reply'];record=client.records[cached['record_index']]
                progress['cache_hits']=progress.get('cache_hits',0)+1
            candidates=reply['candidates']
            if not isinstance(candidates,list):raise ValueError('invalid_model_response')
            grounded=set()
            for value in candidates:
                quoted=value.get('value','')
                if isinstance(quoted,str) and 3<=len(quoted.encode())<=512 and quoted.strip()==quoted and quoted in fragment:
                    grounded.add(quoted)
                    found.setdefault(quoted,{'value':quoted,'kind':value.get('kind','CUSTOM'),'confidence':value.get('confidence')})
            record.update(candidate_count=len(candidates),unique_grounded_count=len(grounded))
            if args.strategy!='fixed' and len(grounded)>=args.max_candidates and len(fragment)>1:
                left,right=split_span(fragment,0,len(fragment));consume(fragment[left[0]:left[1]],ctx);consume(fragment[right[0]:right[1]],ctx)
        except DiscoveryCheckpointError:raise
        except Exception as exc:
            if args.strategy!='fixed' and (isinstance(exc,json.JSONDecodeError) or str(exc) in ['model_response_truncated','invalid_model_response']) and len(fragment)>1:
                left,right=split_span(fragment,0,len(fragment));consume(fragment[left[0]:left[1]],ctx);consume(fragment[right[0]:right[1]],ctx)
            else:errors.append({'error':type(exc).__name__,'code':str(exc)[:80],'fragment_sha256':hashlib.sha256(fragment.encode()).hexdigest()})
        persist()
    for piece in pieces:consume(piece['source'],piece['context'])
    return {'values':list(found.values()),'errors':errors,'fragments':len(pieces),'calls':calls}


def probe_reviews(client,prompt,kinds,args):
    cases=json.loads((ROOT/'tests/fixtures/local-model-review.json').read_text());out=[]
    for start in range(0,len(cases),args.review_batch):
        batch=cases[start:start+args.review_batch]
        ids={c['id']:hashlib.sha256(('kaponir-review:'+c['id']).encode()).hexdigest()[:24] if args.opaque_review_ids else c['id'] for c in batch}
        fields={'id':{'type':'string','enum':list(ids.values())},'action':{'type':'string','enum':['mask','keep']},'kind':{'type':'string','enum':kinds},'scope':{'type':'string','enum':['personal','team','global']},'confidence':{'type':'number','minimum':0,'maximum':1},'reason':{'type':'string','minLength':1,'maxLength':400}}
        schema={'type':'object','properties':{'decisions':{'type':'array','minItems':len(batch),'maxItems':len(batch),'items':{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}}},'required':['decisions'],'additionalProperties':False}
        keys=['id','value','context']+([] if args.blind_review else ['kind'])
        try:
            reply=client.infer(prompt,{'items':[{**{k:c[k] for k in keys},'id':ids[c['id']]} for c in batch]},json.loads(json.dumps(schema,sort_keys=True)),args.output_tokens)
            for c in batch:
                matches=[a for a in reply.get('decisions',[]) if a.get('id')==ids[c['id']]];answer=matches[0] if len(matches)==1 else {};p=answer.get('confidence')
                valid=len(matches)==1 and type(p) in [int,float] and 0<=p<=1 and answer.get('kind') in kinds and answer.get('scope') in ['personal','team','global'] and answer.get('action') in ['mask','keep']
                out.append({'case':c,'answer':answer,'valid':valid,'action_correct':valid and answer['action']==c['expected_action'],
                    'kind_correct':None if 'expected_kind' not in c else valid and answer['kind']==c['expected_kind'],
                    'scope_correct':None if 'expected_scope' not in c else valid and answer['scope']==c['expected_scope']})
        except Exception as exc:out.extend({'case':c,'valid':False,'action_correct':False,'error':type(exc).__name__} for c in batch)
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model',required=True);p.add_argument('--model-dir',type=Path);p.add_argument('--runtime',type=Path)
    p.add_argument('--url');p.add_argument('--key-file',type=Path);p.add_argument('--timeout',type=int,default=300)
    p.add_argument('--transport-retries',type=int,default=0)
    p.add_argument('--context-tokens',type=int,default=4096);p.add_argument('--output-tokens',type=int,default=1600)
    p.add_argument('--target-input-tokens',type=int,default=0);p.add_argument('--reserve-tokens',type=int,default=64)
    p.add_argument('--hint-tokens',type=int,default=0);p.add_argument('--max-candidates',type=int,default=24);p.add_argument('--max-fragments',type=int,default=1000)
    p.add_argument('--fixed-bytes',type=int,default=6000);p.add_argument('--fixed-overlap-bytes',type=int,default=256)
    p.add_argument('--strategy',choices=['fixed','auto','paragraph'],default='auto');p.add_argument('--candidate-mode',choices=['typed','lean'],default='typed')
    p.add_argument('--blind-review',action='store_true');p.add_argument('--review-only',action='store_true');p.add_argument('--skip-review',action='store_true');p.add_argument('--review-batch',type=int,default=2)
    p.add_argument('--opaque-review-ids',action=argparse.BooleanOptionalAction,default=True,help='Replace semantic case IDs with hashes, as in the application; disable only to reproduce historical calibration')
    p.add_argument('--documents',nargs='+',default=DOCUMENTS);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if args.context_tokens<=args.output_tokens+args.reserve_tokens:p.error('output/reserve must leave room for input')
    worker=None;temp=None
    if args.url:
        if not args.key_file:p.error('--key-file is required with --url')
        client=RemoteClient(args.url,args.model,args.key_file.read_text().strip(),args.timeout,args.transport_retries)
    else:
        if not args.runtime or not args.model_dir:p.error('--runtime and --model-dir are required locally')
        temp=tempfile.TemporaryDirectory(prefix='kaponir-inference-lab-');home=Path(temp.name)
        worker=Worker(home,configuration(home,{'model_dir':str(args.model_dir.resolve()),'runtime':str(args.runtime.resolve()),'model':args.model,'threads':2,'ocr_timeout_seconds':args.timeout}),'unused')
        client=LocalClient(worker,args.model,args.context_tokens,args.output_tokens)
    extract,review,kinds=prompts_and_kinds()
    result={'model':args.model,'settings':{k:v for k,v in vars(args).items() if k not in ['key_file','output','runtime','model_dir']},'documents':{},'status':'running','metrics_layer':'grounded model proposals, before application weak-value filtering'}
    def save():
        result['calls']=client.records
        (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    save()
    try:
        gold={d['file']:{v['value'] for v in d['sensitive']} for d in json.loads((ROOT/'tests/docs/expected.json').read_text())['documents']}
        if not args.review_only:
            for name in args.documents:
                text=(ROOT/'tests/docs/input'/name).read_text();started=time.monotonic()
                doc=discover(client,text,extract,kinds,args,save);found={item['value'] for item in doc['values']}
                doc.update(matched=len(gold[name]&found),expected=len(gold[name]),unmatched=sorted(found-gold[name]),missed=sorted(gold[name]-found),seconds=round(time.monotonic()-started,2))
                result['documents'][name]=doc;save();print(json.dumps({'model':args.model,'document':name,'matched':doc['matched'],'expected':doc['expected'],'unmatched':len(doc['unmatched']),'errors':len(doc['errors']),'seconds':doc['seconds']}),flush=True)
        if not args.skip_review:
            result['reviews']=probe_reviews(client,review,kinds,args)
            result['review_summary']={'valid':sum(r['valid'] for r in result['reviews']),'action_correct':sum(r['action_correct'] for r in result['reviews']),
                'kind_correct':sum(r.get('kind_correct') is True for r in result['reviews']),'scope_correct':sum(r.get('scope_correct') is True for r in result['reviews'])}
        result['status']='completed';save()
    finally:
        save()
        if worker:worker.stop.set();worker.stop_runtime()
        if temp:temp.cleanup()
    print(json.dumps(result.get('review_summary',{})),flush=True)


if __name__=='__main__':main()
