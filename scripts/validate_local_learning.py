#!/usr/bin/env python3
"""Real CPU learning smoke test using already downloaded and verified model files."""
import argparse,json,os,subprocess,sys,tempfile,time,urllib.request
from pathlib import Path
from validate_console import free_port,wait_for


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path);parser.add_argument('--model-dir',type=Path,required=True)
    parser.add_argument('--model',default='qwen3.5-0.8b');parser.add_argument('--parallelism',type=int,default=2)
    parser.add_argument('--screenshots',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('artifacts/local-learning'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True);binary=args.binary.resolve()
    env={k:v for k,v in os.environ.items() if not k.startswith('KAPONIR_')}
    env.update(KAPONIR_WORKER_PYTHON=sys.executable,KAPONIR_MODEL_DIR=str(args.model_dir.resolve()),KAPONIR_WORKER_LISTEN=f'127.0.0.1:{free_port()}')
    with tempfile.TemporaryDirectory(prefix='kaponir-local-learning-') as directory:
        home=Path(directory);subprocess.run([binary,'--home',home,'init'],env=env,capture_output=True,check=True)
        base=f'http://127.0.0.1:{free_port()}'
        def api(path,body=None,method=None):
            request=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'},method=method)
            with urllib.request.urlopen(request,timeout=30) as r:return json.load(r)
        log=(args.output/'server.log').open('w')
        process=subprocess.Popen([binary,'--home',home,'serve','--listen',base.removeprefix('http://')],env=env,stdout=log,stderr=log)
        try:
            wait_for(lambda:api('/api/v1/ui/info'))
            cat=api('/api/v1/ui/categorization')['settings'];cat.update(provider='local',model=args.model)
            api('/api/v1/ui/categorization',{'settings':cat},'PUT')
            analysis=api('/api/v1/ui/analysis')['settings'];analysis.update(enabled=True,provider='local',model=args.model,parallelism=args.parallelism)
            api('/api/v1/ui/analysis',{'settings':analysis},'PUT');api('/api/v1/ui/worker/start',{})
            assert api('/api/v1/ui/worker')['models'][args.model]['downloaded']
            started=time.monotonic()
            api('/api/v1/ui/analysis/documents',{'documents':[{'name':'synthetic-ru.txt','text':'Наш внутренний проект называется Лунная Слива. Это закрытое внутреннее название.'},{'name':'synthetic-en.txt','text':'Our confidential internal project is called Cedar Marble. This is a private code name.'}]})
            def completed():
                value=api('/api/v1/ui/learning');return value if value['summary']['totals']['processed']>=2 or value['summary']['totals']['failed'] else None
            result=wait_for(completed,timeout=180)
            worker=api('/api/v1/ui/worker')
            for value in ['Лунная Слива','Cedar Marble']:
                entry=next(e for e in result['entries'] if e['value']==value)
                assert entry['state']=='active',entry
                masked=api('/api/v1/anonymize',{'text':value})['text']
                assert value not in masked
                assert api('/api/v1/deanonymize',{'text':masked})['text']==value
            if args.screenshots:
                from playwright.sync_api import sync_playwright
                import shutil
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1440,'height':1000},locale='ru-RU',color_scheme='dark')
                    page.goto(base+'/ui/#analysis')
                    page.wait_for_function("() => document.querySelector('#learning-metrics')?.textContent.includes('2')")
                    page.screenshot(path=str(args.output/'local-analysis-dark.png'),full_page=True)
                    page.locator('#learning-open-history').click()
                    page.locator('#learning-history .learning-entry').first.wait_for()
                    page.screenshot(path=str(args.output/'local-history-dark.png'),full_page=True)
                    browser.close()

            assert result['summary']['totals']['failed']==0,result
            assert {'Лунная Слива','Cedar Marble'}<={e['value'] for e in result['entries']},result
            analysis['enabled']=False;api('/api/v1/ui/analysis',{'settings':analysis},'PUT')
            wait_for(lambda:api('/api/v1/ui/worker')['resources']['model_rss_bytes']==0)
            report={'passed':True,'model':args.model,'parallelism':args.parallelism,'seconds':round(time.monotonic()-started,2),'resources':worker['resources'],'summary':result['summary'],'findings':[{'value':e['value'],'state':e['state'],'confidence':e['confidence'],'kind':e['kind'],'reason':e['reason']} for e in result['entries']],'model_memory_after_pause':api('/api/v1/ui/worker')['resources']['model_rss_bytes']}
            (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))
        finally:
            process.terminate();process.wait(timeout=20);log.close()


if __name__=='__main__':main()
