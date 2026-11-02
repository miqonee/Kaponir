#!/usr/bin/env python3
"""Render saved S1 comparison evidence as a self-contained local HTML report."""
import json
from pathlib import Path
import runpy

ROOT=Path(__file__).resolve().parents[1]
M=runpy.run_path(str(ROOT/'scripts/compare_s1.py'))
OUT=M['OUT']; SOURCE=M['SOURCE']
summary=M['summarize']()
docs=json.loads((SOURCE/'manifest.json').read_text())
cases=[]
for doc in docs:
    case={'id':doc['id'],'original':(SOURCE/doc['file']).read_text(),'modes':{}}
    for mode in M['MODES']:
        info=json.loads((OUT/f'{mode}/local/results.json').read_text())
        row=next(r for r in info if r['id']==doc['id'])
        case['modes'][mode]={'masked':(OUT/f'{mode}/local/{doc["id"]}.masked.txt').read_text(),'checks':row}
        remote=OUT/f'{mode}/remote/{doc["id"]}/result.json'
        if remote.exists():
            result=json.loads(remote.read_text())['attempts'][-1]
            folder=remote.parent/f'attempt-{result["attempt"]}'
            item={'result':result}
            for name,file in [('seen','llm-seen.txt'),('echo','raw-model-echo.txt'),('restored','restored-user-answer.txt')]:
                p=folder/file; item[name]=p.read_text() if p.exists() else 'Ответ не получен.'
            case['modes'][mode]['remote']=item
    cases.append(case)
payload=json.dumps({'summary':summary,'cases':cases},ensure_ascii=False).replace('<','\\u003c')
page=r'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kaponir — сравнение с S1 и без S1</title><style>
body{font:15px/1.6 system-ui,sans-serif;color:#182838;background:#f1f5f8;max-width:1500px;margin:auto;padding:30px}h1{letter-spacing:-1px;font-size:32px}p{max-width:1150px}section,article{background:white;border:1px solid #d5dfe7;border-radius:12px;padding:20px;margin:16px 0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}article{margin:0;min-width:0}.wide{grid-column:1/-1}pre{font:12px/1.65 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:10px;text-align:left;border-bottom:1px solid #dce5ed}.notice{border-left:4px solid #c78028;padding:10px 20px;background:#fff7ea}.nav{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:22px 0}button,select{font:inherit;padding:9px;border:1px solid #b6c4d2;border-radius:7px;background:white}button{cursor:pointer}.meta{color:#607080}a{color:#1a5da6}@media(max-width:900px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}body{padding:16px}section{overflow:auto}}
</style><body><p class="meta">29 октября 2026 · 64 документа · 1,11 МБ · SystemOne oc/jev-1.13-free · L0</p>
<h1>Маски и восстановление: без S1, теневой, активный</h1>
<p>Три режима используют одинаковые исходники, правила и начальный ключ замен. Проверяются текст целиком, отдельные токены, варианты записи переменных, JSON-экранирование и восстановление по порциям.</p>
<section><table><thead><tr><th>Режим</th><th>Текст точно</th><th>Токены точно</th><th>Варианты записи</th><th>JSON</th><th>Открытые значения</th><th>Оценки S1</th><th>Другие маски</th></tr></thead><tbody id="totals"></tbody></table></section>
<p id="s1-stats"></p><p id="remote-stats"></p>
<div class="notice"><strong>Границы сравнения</strong><p>Одинаковые наборы признаков и вопросов используют записанный ответ реального S1. При смене порядка кандидатов оценки сопоставляются по полному содержимому признаков и вопроса; происхождение и перестановки сохранены. Это сравнение решений с фиксированными оценками, а не замер задержки или стабильности внешнего сервиса.</p>
<p>S1 получает только L0 и оценивает уже обнаруженных спорных кандидатов. Необнаруженные суммы и фамилии он не добавляет. Полный корпус проходит три локальных режима; реальная Gemini получает по восемь документов без S1 и с активным S1. Теневой вариант не отправляется в Gemini повторно.</p></div>
<div class="nav"><button id="prev">← Назад</button><label>Документ <select id="case"></select></label><button id="next">Далее →</button></div>
<div class="grid"><article class="wide"><h3>Исходник</h3><pre id="original"></pre></article>
<article><h3>Без S1</h3><p id="off-checks"></p><pre id="off-text"></pre></article>
<article><h3>S1: теневой режим</h3><p id="shadow-checks"></p><pre id="shadow-text"></pre></article>
<article><h3>S1: активный режим</h3><p id="active-checks"></p><pre id="active-text"></pre></article>
<article class="wide"><h3>Открытые размеченные значения в активном режиме</h3><pre id="leaks"></pre></article></div>
<div class="nav"><label>Ответ реальной модели <select id="remote-mode"><option value="off">Без S1</option><option value="active">С активным S1</option></select></label></div>
<p id="remote-case"></p><div class="grid"><article><h3>Текст для Gemini</h3><pre id="seen"></pre></article><article><h3>Сырой ответ до восстановления</h3><pre id="echo"></pre></article><article><h3>После восстановления</h3><pre id="restored"></pre></article></div>
<p><a href="README.md">Методика и выводы</a> · <a href="summary.json">Метрики JSON</a> · <a href="stream-and-fallback.json">Потоки и сбой S1</a> · <a href="s1/calls.json">История оценок</a></p>
<script id="data" type="application/json">PAYLOAD</script><script>
const d=JSON.parse(document.querySelector('#data').textContent), $=s=>document.querySelector(s), labels={off:'Без S1',shadow:'Теневой',active:'Активный'};
for(const [mode,s]of Object.entries(d.summary.modes)){const tr=document.createElement('tr');for(const x of [labels[mode],s.whole_exact+'/64',s.individual_exact+'/64 ('+s.individual_tokens+' токенов)',s.aliases_exact+'/64 ('+s.alias_tokens+' форм)',s.json_escape_exact+'/64',s.exposed_values+'/1344',s.s1_scores,s.masks_different_from_off.length]){const td=document.createElement('td');td.textContent=x;tr.append(td)}$('#totals').append(tr)}
const s=d.summary.s1;$('#s1-stats').textContent='В финальном корпусе S1: '+s.final_local_calls+' батчей, '+s.final_local_provider_calls+' обращений за оценками, '+s.final_local_cache_hits+' повторных использований записанных ответов; неудачных оценок '+s.final_local_failed_calls+'. Полная история, включая подготовку, сохранена отдельно.';
$('#remote-stats').textContent=['off','active'].map(mode=>{const r=d.summary.modes[mode].remote;return labels[mode]+': '+r.responses+'/'+r.attempted+' ответов; восстановление точно '+r.restored_exact+', без конечного переноса строки '+r.restored_except_final_newline+'.'}).join(' ');
d.cases.forEach((c,i)=>{const o=document.createElement('option');o.value=i;o.textContent=c.id;$('#case').append(o)});
function show(){const c=d.cases[+$('#case').value];$('#original').textContent=c.original;for(const mode of ['off','shadow','active']){const m=c.modes[mode],s=m.checks;$('#'+mode+'-text').textContent=m.masked;$('#'+mode+'-checks').textContent='Текст: '+(s.restore_exact?'точно':'не совпал')+'; отдельные токены: '+(s.individual_exact?'точно':'не совпали')+'; формы: '+(s.aliases_exact?'точно':'не совпали')+'; оценки S1: '+s.s1_scores+'.'}$('#leaks').textContent=c.modes.active.checks.leaks.map(e=>e.kind+': '+e.value).join('\n')||'Размеченных пропусков нет.';const r=c.modes[$('#remote-mode').value].remote;for(const key of ['seen','echo','restored'])$('#'+key).textContent=r?r[key]:'Документ не входит в выборку из восьми живых запросов.';$('#remote-case').textContent=r?(r.result.http_ok?'Ответ получен. Восстановление: '+(r.result.restore_exact?'точно':r.result.restore_except_final_newline?'отличается только конечным переносом строки':'содержимое отличается')+'.':'Ответ не получен после двух сетевых попыток.'):'Живого запроса для этого документа не было.'}
$('#case').onchange=show;$('#remote-mode').onchange=show;$('#prev').onclick=()=>{$('#case').value=(+$('#case').value+d.cases.length-1)%d.cases.length;show()};$('#next').onclick=()=>{$('#case').value=(+$('#case').value+1)%d.cases.length;show()};show();
</script></body></html>'''.replace('PAYLOAD',payload)
page=page.replace(r".join('\\n')",r".join('\n')")
(OUT/'index.html').write_text(page)
print(json.dumps(summary,ensure_ascii=False,indent=2))
