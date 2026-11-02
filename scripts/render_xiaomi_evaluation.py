#!/usr/bin/env python3
"""Render recorded local model results; does not run inference."""
import argparse
import html
import json
from pathlib import Path
from evaluate_xiaomi_ocr import evaluate


def render(root):
    rows = []
    runs = []
    for name in ['q4', 'q4-schema', 'q5', 'q4-json-object', 'q4-constrained', 'q4-lists']:
        directory = root / name
        if not (directory / 'summary.json').exists():
            continue
        summary = json.loads((directory / 'summary.json').read_text())
        pages = {p['language']: p for p in json.loads((directory / 'ground-truth.json').read_text())}
        runs.append({'name': name, 'peak_rss_kib': max([summary['peak_memory']['rss_kib']] + [c.get('memory', {}).get('VmHWM', 0) for c in summary['cases']]),
                     'load_seconds': summary.get('load_seconds'), 'server_stopped': summary.get('server_stopped', False)})
        for case in summary['cases']:
            language, task = case['name'].split('-', 1)
            row = dict(case)
            row['run'] = name
            row['language'] = language
            row['task'] = task
            row['image'] = f'{name}/document-{language}.png'
            row['expected'] = pages[language]['expected']
            row['metrics'] = evaluate(task, row.get('text', ''), pages[language])
            rows.append(row)
    combined = {'normalization': 'Whitespace collapsed; case and punctuation preserved. No JSON repair.', 'runs': runs, 'cases': rows}
    (root / 'comparison.json').write_text(json.dumps(combined, ensure_ascii=False, indent=2))
    data = json.dumps(rows, ensure_ascii=False).replace('</', '<\\/')
    ocr_found = sum(c['metrics']['values_found'] for c in rows if c['run']=='q4' and c['task']=='ocr')
    ocr_total = sum(c['metrics']['values_total'] for c in rows if c['run']=='q4' and c['task']=='ocr')
    maximum = max(r['peak_rss_kib'] for r in runs) / 1024 / 1024
    low = min(r['seconds'] for r in rows)
    high = max(r['seconds'] for r in rows)
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Xiaomi-OCR-0 — локальная проверка</title>
<style>body{font:16px/1.6 system-ui;color:#192d34;background:#f2f5f5;max-width:1260px;margin:32px auto;padding:24px}h1{line-height:1.2}article,.stat{background:white;border:1px solid #dbe4e4;border-radius:12px;padding:20px;margin-bottom:18px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.stat b{display:block;font-size:27px;color:#246154}.columns{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start}img{max-width:100%;height:auto;border:1px solid #dbe4e4}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf2f3;border-radius:8px;padding:16px;font:13px/1.6 monospace}select{font:inherit;padding:10px;max-width:100%;margin-bottom:16px}table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:9px;text-align:left;border-bottom:1px solid #dbe4e4}.scroll{overflow:auto}.note{color:#50646b}a{color:#2459a0}@media(max-width:800px){.columns,.stats{grid-template-columns:1fr}}</style>
<h1>Xiaomi-OCR-0: проверка на CPU</h1><p class="note">2 ноября 2026 · Q4_K_M и Q5_K_M · llama.cpp b12038 · 3 потока · две синтетические страницы EN/RU</p>
<div class="stats"><div class="stat"><b>'''+f'{ocr_found}/{ocr_total}'+'''</b>контрольных значений в OCR Q4</div><div class="stat"><b>'''+f'{low:.0f}–{high:.0f} с'+'''</b>на запрос с изображением</div><div class="stat"><b>'''+f'{maximum:.2f} ГиБ'+'''</b>максимальный RSS процесса</div></div>
<article><p>Q4 распознала контрольные значения на двух чистых страницах и ответила на вопросы о названиях проектов. Структурированный вывод и выбор «скрыть / оставить» оцениваются отдельно: правильное распознавание текста ещё не означает соблюдение схемы или правильную классификацию.</p><p>Смотрите исходную страницу рядом с фактическим ответом. Все данные вымышленные. Это маленький проверочный набор, не оценка качества на произвольных договорах и сканах.</p></article>
<article><label for="case">Проверка</label><br><select id="case"></select><p id="summary"></p><div class="columns"><div><img id="document" alt="Синтетическая страница, переданная локальной модели"></div><div><h2>Фактический ответ</h2><pre id="answer"></pre><h2>Проверка результата</h2><pre id="metrics"></pre></div></div></article>
<article><h2>Все измерения</h2><div class="scroll"><table><thead><tr><th>Запуск</th><th>Документ / задача</th><th>Секунды</th><th>Результат</th></tr></thead><tbody id="rows"></tbody></table></div></article>
<p><a href="README.md">Подробный протокол</a> · <a href="comparison.json">Данные сравнения</a> · <a href="kaponir-integration.json">Проба связки с Kaponir</a></p>
<script>const cases='''+data+''';const select=document.querySelector('#case');for(const [index,c] of cases.entries()){const o=document.createElement('option');o.value=index;o.textContent=c.run+' · '+c.language.toUpperCase()+' · '+c.task;select.append(o);const row=document.createElement('tr');for(const text of [c.run,c.language+' / '+c.task,c.seconds,JSON.stringify(c.metrics)]){const td=document.createElement('td');td.textContent=text;row.append(td);}document.querySelector('#rows').append(row);}function show(){const c=cases[Number(select.value)];document.querySelector('#document').src=c.image;document.querySelector('#answer').textContent=c.text||c.error;document.querySelector('#metrics').textContent=JSON.stringify(c.metrics,null,2);document.querySelector('#summary').textContent=c.seconds+' с · '+c.finish_reason+' · '+((c.memory?.VmHWM||0)/1024).toFixed(0)+' МиБ RSS';}select.onchange=show;show();</script></html>'''
    (root / 'index.html').write_text(page)
    print(json.dumps({'runs': runs, 'requests': len(rows), 'latency_seconds': [low, high]}, ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    render(p.parse_args().root)
