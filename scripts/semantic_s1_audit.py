#!/usr/bin/env python3
"""Blind paired-context SystemOne audit using the production Rust HTTP adapter."""
import argparse
from collections import Counter
import concurrent.futures
import hashlib
import http.server
import json
import os
from pathlib import Path
import random
import runpy
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/s1-semantic-2026-10-30'
BASE=runpy.run_path(str(ROOT/'scripts/corporate_audit.py'))
BIN=ROOT/'target/debug/examples/semantic_probe'
LOCK=threading.Lock()
COUNTS=Counter()


def save(path,value):
    p=OUT/path;p.parent.mkdir(parents=True,exist_ok=True)
    raw=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)
    p.write_text(raw)


def corpus():
    # Surface/category are identical inside each pair. Labels never enter API requests.
    pairs=[
    ('codename','Ласточка-Резерв','infra','Внутреннее кодовое имя ещё не объявленной программы миграции: Ласточка-Резерв. Не публиковать до согласования.','Название опубликованного учебного упражнения: Ласточка-Резерв. Текст свободно доступен на сайте курса.'),
    ('project','kedrledger','infra','kedrledger — закрытый проект нашего клиента; имя известно только участникам по NDA.','kedrledger — название вымышленного проекта из общедоступного руководства, никаких реальных клиентов.'),
    ('company','ООО «Полярный Контур»','infra','Заказчик по закрытому, ещё не объявленному договору: ООО «Полярный Контур». Название под NDA.','ООО «Полярный Контур» — вымышленная компания в публичном образце заполнения формы.'),
    ('amount','1801234.37','financial','Сумма предложения нашему клиенту: 1801234.37 RUB. Коммерческая тайна, предложение не опубликовано.','Открытый прайс на сайте продавца: 1801234.37 RUB. Эта цена опубликована для всех.'),
    ('surname_ru','голошанову','identity','Личное дело сотрудника: передайте голошанову уведомление о его медицинском обследовании. Доступ только у HR.','В открытом учебнике приведён вымышленный пример склонения: передайте голошанову письмо. Реального человека нет.'),
    ('name_en','Alice Brown','identity','Employee Alice Brown has a confidential disciplinary record in our private HR system.','Alice Brown is a fictional placeholder in a public English grammar exercise, not an actual person.'),
    ('project_en','project_zephyr','infra','project_zephyr is our confidential acquisition codename. No public announcement has been made.','project_zephyr is a fictional sample repository name in a publicly licensed tutorial.'),
    ('ipv4','10.72.1.19','network','10.72.1.19 — действующий адрес внутренней платёжной БД; раскрытие топологии запрещено.','10.72.1.19 — придуманный адрес в публичном учебнике по RFC1918, не адрес реального сервера.'),
    ('email','support@polar-ledger.example','contact','support@polar-ledger.example — закрытый адрес эскалации для специального клиента; не публикуется.','support@polar-ledger.example — вымышленный адрес из публичной документации, домен example предназначен для примеров.'),
    ('path','otelcollector','infra','Закрытый репозиторий нашего заказчика: /workspace/otelcollector/k8s. Не раскрывать имя репозитория.','Публичный пример сборки open-source компонента: /workspace/otelcollector/k8s. Имя не конфиденциально.'),
    ('public_word_private','Kubernetes','infra','Kubernetes — секретное кодовое имя нового договора нашего отдела. Здесь это имя договора, а не название ПО.','Kubernetes — публичная система оркестрации контейнеров. Здесь речь о стандартном открытом ПО.'),
    ('tool','nginx','infra','nginx is the private codename of an unannounced merger in this memo, not the web server. Keep the codename confidential.','nginx is the name of the public open-source web server in this installation manual.'),
    ('merger','Q4-ORION','infra','Проект Q4-ORION — непубличное поглощение компании. Список участников ограничен NDA.','Q4-ORION — выдуманный идентификатор задания на открытом учебном соревновании.'),
    ('customer_id','Rubin','infra','Rubin — внутренний идентификатор нашего единственного VIP-клиента; соответствие не публикуется.','Rubin — название цвета в общедоступном каталоге красок.'),
    ('ambiguous','Delta','infra','Delta — закрытое имя программы сокращения штата, которое нельзя раскрывать сотрудникам до объявления.','Delta — общедоступное обозначение греческой буквы в статье по математике.'),
    ('host','prod-eu17','infra','Рабочий узел платёжного контура: prod-eu17; имена инфраструктуры не подлежат разглашению.','prod-eu17 — заведомо вымышленное имя узла в открытом примере документации.'),
    ('number','9050000','financial','Персональная годовая премия сотрудника — 9050000 рублей, данные из закрытой ведомости зарплат.','В общедоступной статье указан результат подсчёта 9050000 звёзд; это публичная научная статистика.'),
    ('date','2026-11-21','infra','Необъявленная дата релиза продукта — 2026-11-21. Дата под эмбарго, доступна только команде.','В опубликованном календаре конференции указана дата 2026-11-21; расписание общедоступно.'),
    ('passport','45 19 100042','identity','Паспорт сотрудника: 45 19 100042. Извлечено из его закрытого личного дела.','45 19 100042 — вымышленный тестовый набор цифр в открытом образце; никаких документов реального человека.'),
    ('phrase','A17-K9','infra','A17-K9 is the confidential internal project identifier in the restricted client memo.','A17-K9 is a publicly listed spare-part SKU in an open product catalog.'),
    ('reserve','Резерв-7','infra','Резерв-7 — имя непубличной площадки аварийного восстановления. Топология только для администраторов.','Резерв-7 — название команды в опубликованной таблице школьного турнира, общедоступная информация.'),
    ('orion','Орион','infra','Орион — кодовое имя клиента в закрытом договоре. Не раскрывать название до публичного объявления.','Орион — название созвездия в общедоступном учебнике астрономии.'),
    ('login','Mira','identity','Mira is the account alias identifying a real employee in a confidential internal performance review.','Mira is a publicly known star name in an astronomy textbook, not a person or an account.'),
    ('account','XQ-7742','financial','XQ-7742 — номер закрытого расчётного проекта нашего клиента, идентификатор только для внутреннего использования.','XQ-7742 — вымышленный номер детали в публичном каталоге учебного магазина.'),
    ]
    rows=[]
    for pair,surface,category,private,public in pairs:
        for sensitive,context in [(True,private),(False,public)]:
            rows.append({'id':pair+('-private' if sensitive else '-public'),'pair':pair,
                         'surface':surface,'category':category,'context':context,'sensitive':sensitive})
    random.Random(31026).shuffle(rows)
    for i,row in enumerate(rows):row['blind_id']=f'case-{i+1:03}'
    save('corpus.json',rows)
    return rows


class Relay(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        data=self.rfile.read(int(self.headers['Content-Length']))
        path=self.path.strip('/').split('/'); tag='-'.join(path)
        with LOCK: COUNTS[tag]+=1; count=COUNTS[tag]
        folder=f'requests/{tag}/attempt-{count}'
        request=json.loads(data);save(folder+'/request.json',request)
        t=time.monotonic(); record={'path':self.path}
        req=urllib.request.Request('https://route.fn.x3dev.ru/v1/systemone',data=data,headers={
            'Content-Type':'application/json','Authorization':self.headers.get('Authorization','')})
        try:
            with urllib.request.urlopen(req,timeout=50) as response:
                raw=response.read(1048577); value=json.loads(raw)
                record['status']=response.status; save(folder+'/response.json',value)
                self.send_response(response.status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        except Exception as e:
            record['error']=type(e).__name__+': '+str(e)
            try:self.send_error(502)
            except (BrokenPipeError,ConnectionResetError):pass
        finally: record['seconds']=round(time.monotonic()-t,3);save(folder+'/transport.json',record)


def semantic(base,rows,levels=None,folder='semantic',request_tag='semantic'):
    levels=levels or ['l0','l1','l2']
    def run(job):
        level,batch,items=job; results=[]
        # Only blind ids, text and categories are passed to the production adapter.
        body={'cases':[{'id':r['blind_id'],'surface':r['surface'],'context':r['context'],'category':r['category']} for r in items],
              'http':{'url':f'{base}/{request_tag}/{level}/{batch}','protocol':'systemone','model':'oc/jev-1.13-free',
                      'timeout_ms':60000,'disclosure':level,'token_env':'KAPONIR_S1_HTTP_TOKEN'}}
        for attempt in range(1,3):
            p=subprocess.run([str(BIN)],input=json.dumps(body),capture_output=True,text=True,timeout=75)
            try:value=json.loads(p.stdout)
            except ValueError:value={'provider_ok':False,'error':p.stderr[:1000]}
            results.append({'attempt':attempt,'provider_ok':value.get('provider_ok',False),'error':value.get('error')})
            save(f'{folder}/{level}/batch-{batch}.json',{'attempts':results,'result':value})
            if value.get('provider_ok'):break
        print(json.dumps({'level':level,'batch':batch,'ok':value.get('provider_ok'),'attempts':len(results)}),flush=True)
        return level,items,value
    jobs=[(level,i//8,rows[i:i+8]) for level in levels for i in range(0,len(rows),8)]
    out={level:[] for level in levels}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for level,items,value in pool.map(run,jobs):
            byid={r['id']:r for r in value.get('rows',[])}
            for item in items:
                out[level].append({**item,'scored':item['blind_id'] in byid,'result':byid.get(item['blind_id'])})
            save(f'{folder}/results.json',out)


def integration(base):
    # Existing detectors; no custom business rules or injected candidates.
    cases=[
        {'id':'private-path','text':'Закрытый репозиторий клиента /workspace/otelcollector/k8s. Не раскрывать имя репозитория.','target':'otelcollector','sensitive':True},
        {'id':'public-path','text':'Публичный пример сборки open-source: /workspace/otelcollector/k8s. Имя не конфиденциально.','target':'otelcollector','sensitive':False},
        {'id':'private-domain','text':'Рабочая внутренняя инфраструктура: https://api.kedrledger.internal. Имя проекта под NDA.','target':'kedrledger','sensitive':True},
        {'id':'public-domain','text':'Вымышленный пример документации: https://api.kedrledger.example. Сайт не принадлежит реальной компании.','target':'kedrledger','sensitive':False},
        {'id':'missed-surname','text':'Личное дело сотрудника: передайте голошанову закрытое уведомление.','target':'голошанову','sensitive':True},
        {'id':'missed-amount','text':'Сумма закрытого предложения клиенту 1801234.37 RUB. Коммерческая тайна.','target':'1801234.37','sensitive':True},
    ]
    save('integration/cases.json',cases)
    all_rows={}
    with tempfile.TemporaryDirectory(prefix='kaponir-semantic-cli-') as temp:
        temp=Path(temp);seed=temp/'seed';BASE['cli'](seed,['init'])
        def run(level):
            home=temp/level;shutil.copytree(seed,home)
            cfg=f'''owner = "local"
[s1]
provider = "{'none' if level=='off' else 'http'}"
shadow = false
weight = 1.0
keep_below = 0.1
[s1.http]
url = "{base}/integration/{level}"
protocol = "systemone"
model = "oc/jev-1.13-free"
token_env = "KAPONIR_S1_HTTP_TOKEN"
timeout_ms = 60000
disclosure = "{'l0' if level=='off' else level}"
'''
            (home/'config.toml').write_text(cfg);save(f'integration/{level}/config.toml',cfg)
            out=[]
            for case in cases:
                # Hard secret beside ambiguous candidates must not be disclosed to S1.
                text=case['text']+'\npassword: Synthetic_Only_Q7!'
                attempts=[]
                for attempt in range(1,3):
                    masked=json.loads(BASE['cli'](home,['mask','--json'],text))
                    scored=[c for c in masked['explain'] if any(e['source'].startswith('s1:') for e in c['evidence'])]
                    calls_needed=any(c['verdict']=='uncertain' and c['recognizer'] not in ['config-keys','secrets'] for c in masked['explain'])
                    attempts.append({'attempt':attempt,'scored_candidates':len(scored)})
                    if level=='off' or scored or not calls_needed:break
                restored=BASE['cli'](home,['restore'],masked['text'])
                pairs=list(dict.fromkeys((r['token'],r['original']) for r in masked['replacements']))
                token_result=BASE['cli'](home,['restore'],'\n'.join(t for t,_ in pairs))
                row={**case,'attempts':attempts,'masked':masked,'restore_exact':restored==text,
                     'tokens_exact':token_result=='\n'.join(v for _,v in pairs),
                     'target_hidden':case['target'] not in masked['text'],'secret_hidden':'Synthetic_Only_Q7!' not in masked['text']}
                out.append(row);save(f'integration/{level}/{case["id"]}.json',row)
                print(json.dumps({'integration':level,'case':case['id'],'target_hidden':row['target_hidden'],'s1_scored':len(scored),'restore_exact':row['restore_exact']}),flush=True)
            return level,out
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for level,rows in pool.map(run,['off','l0','l1','l2']):all_rows[level]=rows;save('integration/results.json',all_rows)
    save('cleanup.json',{'temporary_vaults_and_keys_removed':True,'proxy_not_started':True})


def confusion(rows,predict):
    result={'tp':0,'tn':0,'fp':0,'fn':0}
    for r in rows:
        y=r['sensitive'];pred=predict(r);result['tp' if y and pred else 'fn' if y else 'fp' if pred else 'tn']+=1
    result['total']=len(rows);result['accuracy']=(result['tp']+result['tn'])/len(rows) if rows else None
    return result


def summarize():
    data=json.loads((OUT/'semantic/results.json').read_text()); summary={'semantic':{},'integration':{}}
    for level,rows in data.items():
        good=[r for r in rows if r['scored']]
        metrics={'scored':len(good),'failed':len(rows)-len(good),
                 'probability_ge_0_5':confusion(good,lambda r:r['result']['probability']>=0.5)}
        for mode in ['off','shadow','active']:
            metrics[mode]=confusion(good,lambda r:next(v for v in r['result']['variants'] if v['mode']==mode)['target_masked'])
        metrics['restore_exact']=sum(v['restore_exact'] for r in good for v in r['result']['variants'])
        metrics['tokens_exact']=sum(v['tokens_exact'] for r in good for v in r['result']['variants'])
        metrics['hard_secret_controls']=sum(v['hard_secret_masked'] and v['hard_hidden_from_s1'] for r in good for v in r['result']['variants'])
        metrics['pairs_both_correct_at_0_5']=sum(all(r['result']['probability']>=0.5 if r['sensitive'] else r['result']['probability']<0.5 for r in good if r['pair']==pair)
            for pair in {r['pair'] for r in good} if sum(r['pair']==pair for r in good)==2)
        summary['semantic'][level]=metrics
    integration=json.loads((OUT/'integration/results.json').read_text())
    for level,rows in integration.items():
        summary['integration'][level]={'classification':confusion(rows,lambda r:r['target_hidden']),
            'restore_exact':sum(r['restore_exact'] for r in rows),'tokens_exact':sum(r['tokens_exact'] for r in rows),
            'hard_secret_hidden':sum(r['secret_hidden'] for r in rows),
            'scored_candidates':sum(any(e['source'].startswith('s1:') for e in c['evidence']) for r in rows for c in r['masked']['explain'])}
    request_checks=[]
    for f in sorted((OUT/'requests').glob('*/attempt-*/request.json')):
        req=json.loads(f.read_text());candidates=json.loads(req['state'])['candidates']
        assert 'sensitive' not in req['state'] and 'blind_id' not in req['state']
        assert 'Synthetic_Only_Q7!' not in req['state']
        level=next(word for part in f.parts if part.startswith(('semantic-','integration-')) for word in part.split('-') if word in ['l0','l1','l2'])
        assert all(c['surface'] is None and c['context'] is None for c in candidates) if level=='l0' else True
        assert all(c['context'] is None for c in candidates) if level=='l1' else True
        request_checks.append({'path':str(f.relative_to(OUT)),'level':level,'candidates':len(candidates)})
    transports=[json.loads(f.read_text()) for f in (OUT/'requests').glob('*/attempt-*/transport.json')]
    summary['requests']={'count':len(transports),'successful_http':sum(r.get('status')==200 for r in transports),'errors':sum('error' in r for r in transports),'disclosure_checks_passed':len(request_checks)}
    repeat=OUT/'repeat-l2/results.json'
    if repeat.exists():
        rows=json.loads(repeat.read_text())['l2'];good=[r for r in rows if r['scored']]
        summary['repeat_l2']={'scored':len(good),'probability_ge_0_5':confusion(good,lambda r:r['result']['probability']>=0.5),
             'active':confusion(good,lambda r:r['result']['variants'][2]['target_masked']),
             'restore_exact':sum(v['restore_exact'] for r in good for v in r['result']['variants']),
             'tokens_exact':sum(v['tokens_exact'] for r in good for v in r['result']['variants'])}
        initial={r['id']:r for r in data['l2']}
        summary['repeat_l2']['changed_at_0_5']=[r['id'] for r in good if (r['result']['probability']>=.5)!=(initial[r['id']]['result']['probability']>=.5)]
    save('request-checks.json',request_checks);save('summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2));return summary


def report():
    summary=summarize(); rows=json.loads((OUT/'semantic/results.json').read_text())
    examples=[]
    for row in rows['l2']:
        item={k:row[k] for k in ['id','pair','surface','context','sensitive']};item['levels']={}
        for level in ['l0','l1','l2']:
            r=next(r for r in rows[level] if r['id']==row['id']);item['levels'][level]=r['result']
        examples.append(item)
    payload=json.dumps({'summary':summary,'examples':examples,'integration':json.loads((OUT/'integration/results.json').read_text())},ensure_ascii=False).replace('<','\\u003c')
    page=r'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>S1 — проверка смысла и контекста</title>
<style>body{max-width:1300px;margin:auto;padding:30px;font:15px/1.6 system-ui;background:#f4f6f9;color:#172a3b}table{width:100%;border-collapse:collapse;background:white}td,th{padding:10px;border-bottom:1px solid #dce4ec;text-align:left}article{padding:20px;background:white;border:1px solid #dce4ec;border-radius:10px;margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}select,button{font:inherit;padding:9px;border:1px solid #bcc9d7;border-radius:6px;background:white}.notice{padding:15px;background:#fff4df;border-left:4px solid #c68029}h1{letter-spacing:-1px}a{color:#1c5ba2}.nav{display:flex;gap:10px;align-items:center;margin:24px 0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}@media(max-width:850px){.grid{grid-template-columns:1fr}body{padding:15px}}</style>
<body><p>30 октября 2026 · oc/jev-1.13-free · реальные запросы SystemOne</p><h1>Может ли S1 отличать конфиденциальное по контексту?</h1>
<p>48 синтетических примеров: 24 значения, каждое в публичном и конфиденциальном контексте. L0 — признаки, L1 — строка, L2 — строка и контекст. Метки ответа в запрос не передавались.</p>
<div class="notice">Кандидаты в первой проверке заданы разметкой. Это проверка классификации, а не автоматического обнаружения. Ниже отдельно показан проход через штатные детекторы Kaponir. Ошибка с низкой оценкой может раскрыть чувствительный кандидат в активном режиме.</div>
<h2>Классификация на пороге 0,5</h2><table><thead><tr><th>Уровень</th><th>Ответы</th><th>Верно скрыть TP</th><th>Верно оставить TN</th><th>Лишние маски FP</th><th>Пропуски FN</th><th>Точность</th></tr></thead><tbody id="scores"></tbody></table>
<h2>Решения действующего движка</h2><table><thead><tr><th>Уровень</th><th>Верно скрыть</th><th>Верно оставить</th><th>Лишние маски</th><th>Пропуски</th><th>Текст восстановлен</th><th>Токены восстановлены</th></tr></thead><tbody id="engine"></tbody></table>
<p>Порог 0,5 выше — диагностическая метрика классификации. В движке применяются исходная вероятность кандидата 0,5, вес S1=1, стандартные пороги и keep_below=0,1. Это разные правила принятия решения. Результаты не являются независимой оценкой калибровки.</p>
<p id="repeat"></p>
<div class="nav"><button id="prev">←</button><label>Пример <select id="case"></select></label><button id="next">→</button></div><article><h3 id="expected"></h3><pre id="context"></pre></article><div class="grid" id="examples"></div>
<h2>Штатные детекторы и реальный HTTP-провайдер</h2><p>Шесть текстов, без подстановки кандидатов разметкой. Для фамилии и суммы отдельно видно, появился ли кандидат до обращения к S1. Пароль рядом с кандидатами должен оставаться скрытым и не попадать во внешний контекст.</p><table><thead><tr><th>Сценарий</th><th>Нужно скрыть</th><th>Без S1</th><th>L0</th><th>L1</th><th>L2</th></tr></thead><tbody id="integration"></tbody></table>
<p id="requests"></p><p><a href="README.md">Методика и выводы</a> · <a href="summary.json">Метрики</a> · <a href="corpus.json">Корпус и разметка</a></p>
<script id="data" type="application/json">PAYLOAD</script><script>const d=JSON.parse(document.querySelector('#data').textContent),$=s=>document.querySelector(s);function line(table,items){const tr=document.createElement('tr');for(const x of items){const td=document.createElement('td');td.textContent=x;tr.append(td)}$(table).append(tr)}
for(const [level,m]of Object.entries(d.summary.semantic)){const a=m.probability_ge_0_5,b=m.active;line('#scores',[level.toUpperCase(),m.scored+'/48',a.tp,a.tn,a.fp,a.fn,(100*a.accuracy).toFixed(1)+'%']);line('#engine',[level.toUpperCase(),b.tp,b.tn,b.fp,b.fn,m.restore_exact+'/'+(m.scored*3),m.tokens_exact+'/'+(m.scored*3)])}
if(d.summary.repeat_l2){const r=d.summary.repeat_l2,c=r.probability_ge_0_5;$('#repeat').textContent='Повторный L2 без изменения корпуса: '+(c.tp+c.tn)+'/'+r.scored+' верных решений на пороге 0,5; пропусков '+c.fn+', лишних масок '+c.fp+'. Активный движок: пропусков '+r.active.fn+', лишних масок '+r.active.fp+'.';}
d.examples.forEach((e,i)=>{const o=document.createElement('option');o.value=i;o.textContent=e.id;$('#case').append(o)});function show(){const e=d.examples[+$('#case').value];$('#expected').textContent=e.surface+' — '+(e.sensitive?'нужно скрыть':'публичный пример');$('#context').textContent=e.context;$('#examples').replaceChildren();for(const level of ['l0','l1','l2']){const a=document.createElement('article'),h=document.createElement('h3'),p=document.createElement('p'),pre=document.createElement('pre'),r=e.levels[level];h.textContent=level.toUpperCase();if(r){const v=r.variants.find(v=>v.mode==='active');p.textContent='p='+r.probability.toFixed(3)+'; активное решение: '+(v.target_masked?'маскировать':'оставить')+'; восстановление: '+(v.restore_exact?'точное':'ошибка');pre.textContent=v.masked.text}else p.textContent='Оценка не получена';a.append(h,p,pre);$('#examples').append(a)}}$('#case').onchange=show;$('#prev').onclick=()=>{$('#case').value=(+$('#case').value+d.examples.length-1)%d.examples.length;show()};$('#next').onclick=()=>{$('#case').value=(+$('#case').value+1)%d.examples.length;show()};show();
for(const c of d.integration.off){line('#integration',[c.id,c.sensitive?'да':'нет',...['off','l0','l1','l2'].map(l=>{const r=d.integration[l].find(r=>r.id===c.id);return r.target_hidden?'скрыто':'открыто'})])}const r=d.summary.requests;$('#requests').textContent='HTTP-попыток: '+r.count+'; успешных HTTP: '+r.successful_http+'; сетевых ошибок: '+r.errors+'. Проверены ограничения раскрытия во всех '+r.disclosure_checks_passed+' запросах.';</script></body></html>'''.replace('PAYLOAD',payload)
    save('index.html',page)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',action='store_true');p.add_argument('--repeat-l2',action='store_true');p.add_argument('--output',type=Path,default=OUT)
    args=p.parse_args();OUT=args.output.resolve();OUT.mkdir(parents=True,exist_ok=True)
    if args.report:report()
    else:
        if not os.environ.get('KAPONIR_S1_HTTP_TOKEN'):raise RuntimeError('Set KAPONIR_S1_HTTP_TOKEN')
        if (OUT/'summary.json').exists() and not args.repeat_l2:raise RuntimeError('Use a fresh output directory')
        rows=json.loads((OUT/'corpus.json').read_text()) if args.repeat_l2 else corpus()
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Relay);server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            if args.repeat_l2:semantic(f'http://127.0.0.1:{server.server_port}',rows,levels=['l2'],folder='repeat-l2',request_tag='semantic-repeat')
            else:semantic(f'http://127.0.0.1:{server.server_port}',rows);integration(f'http://127.0.0.1:{server.server_port}')
        finally:server.shutdown();server.server_close()
        report()
