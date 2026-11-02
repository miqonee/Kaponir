#!/usr/bin/env python3
"""CPU audit of a local Xiaomi-OCR-0 GGUF. Synthetic page images, no hosted inference.
Requires Pillow; --runtime/--model/--projector point at separately downloaded files.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from PIL import Image, ImageDraw, ImageFont


def request(base, path, body=None, timeout=300):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}: {exc.read().decode()[:1200]}') from exc


def stats(pid):
    try:
        status = Path(f'/proc/{pid}/status').read_text()
        memory = {k: int(v) for k, v in re.findall(r'^(VmRSS|VmHWM|VmSwap):\s+(\d+) kB', status, re.M)}
        fields = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
        memory['cpu_seconds'] = (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
        return memory
    except (OSError, ValueError):
        return {}


def fixtures(out):
    pages = []
    for lang in ['en', 'ru']:
        expected = {
            'company': 'Aster Labs' if lang == 'en' else 'Полярный Маяк',
            'project': 'North Vector' if lang == 'en' else 'Северный Вектор',
            'contact_name': 'Elena Morozova' if lang == 'en' else 'Ирина Соколова',
            'customer_email': 'elena.morozova@example.net' if lang == 'en' else 'irina.sokolova@example.net',
            'contract_id': 'CN-48291' if lang == 'en' else 'ДГ-48291',
            'private_price': '184250.75 USD' if lang == 'en' else '184250,75 RUB',
            'database_password': 'Cedar-47!Plum',
            'public_website': 'https://example.org/pricing',
            'public_product': 'Kaponir',
        }
        labels = ['Company', 'Project', 'Client contact', 'Client email', 'Contract ID', 'Internal purchase price', 'Database password', 'Public website', 'Public product'] if lang == 'en' else ['Компания', 'Проект', 'Контакт клиента', 'Email клиента', 'Номер договора', 'Внутренняя закупочная цена', 'Пароль базы данных', 'Публичный сайт', 'Публичный продукт']
        lines = (["INTERNAL PROCUREMENT NOTE", "Private: first seven fields. Public: last two fields."] if lang == 'en' else ["ВНУТРЕННЯЯ ЗАПИСКА О ЗАКУПКЕ", "Первые семь полей закрытые, последние два публичные."])
        lines += [f'{label}: {value}' for label, value in zip(labels, expected.values())]
        lines += ["Synthetic test data; no real credentials." if lang == 'en' else "Тестовые данные; настоящих паролей здесь нет."]
        image = Image.new('RGB', (992, 1024), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 24)
        title = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 30)
        y = 45
        for index, line in enumerate(lines):
            chosen = title if index == 0 else font
            assert draw.textbbox((0, 0), line, font=chosen)[2] < 920, line
            draw.text((38, y), line, fill='#111111', font=chosen)
            y += 74 if index < 2 else 62
        path = out / f'document-{lang}.png'
        image.save(path)
        (out / f'document-{lang}.txt').write_text('\n'.join(lines))
        pages.append({'language': lang, 'path': str(path), 'expected': expected,
                      'mask': list(expected.values())[:7], 'keep': list(expected.values())[7:]})
    (out / 'ground-truth.json').write_text(json.dumps(pages, ensure_ascii=False, indent=2))
    return pages


def normalized(value):
    return re.sub(r'\s+', ' ', str(value)).strip()


def json_object(text):
    # Preserve raw response too; extracting the outer JSON object only removes code fences.
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except ValueError:
        return None


def evaluate(kind, text, page):
    expected = page['expected']
    if kind == 'vqa':
        return {'exact_answer': normalized(text) == normalized(expected['project'])}
    if kind == 'ocr':
        matches = {k: normalized(v) in normalized(text) for k, v in expected.items()}
        return {'values_found': sum(matches.values()), 'values_total': len(matches), 'fields': matches}
    parsed = json_object(text)
    if kind == 'kie':
        matches = {k: isinstance(parsed, dict) and normalized(parsed.get(k, '')) == normalized(v) for k, v in expected.items()}
        return {'json_valid': isinstance(parsed, dict), 'fields_exact': sum(matches.values()), 'fields_total': len(matches), 'fields': matches}
    if not isinstance(parsed, dict):
        return {'json_valid': False}
    def values(key):
        entries = parsed.get(key, [])
        if not isinstance(entries, list):
            return set()
        return {normalized(x.get('value', '')) if isinstance(x, dict) else normalized(x) for x in entries}
    schema_valid = all(isinstance(parsed.get(k), list) and all(isinstance(x, dict) and isinstance(x.get('value'), str) and isinstance(x.get('reason'), str) for x in parsed[k]) for k in ['mask', 'keep'])
    if kind == 'privacy_lists':
        schema_valid = all(isinstance(parsed.get(k), list) and all(isinstance(x, str) for x in parsed[k]) for k in ['mask', 'keep'])
    masked, kept = values('mask'), values('keep')
    private, public = {normalized(v) for v in page['mask']}, {normalized(v) for v in page['keep']}
    return {'json_valid': True, 'schema_valid': schema_valid, 'source_values_in_response': sum(normalized(v) in normalized(text) for v in expected.values()), 'private_found': len(private & masked), 'private_total': len(private),
            'public_kept': len(public & kept), 'public_total': len(public),
            'public_wrongly_masked': sorted(public & masked), 'private_wrongly_kept': sorted(private & kept),
            'private_values_contained_in_mask': sorted(v for v in private if any(v in item for item in masked)),
            'public_values_contained_in_keep': sorted(v for v in public if any(v in item for item in kept)),
            'public_values_contained_in_mask': sorted(v for v in public if any(v in item for item in masked)),
            'private_values_contained_in_keep': sorted(v for v in private if any(v in item for item in kept)),
            'unexpected_values': sorted((masked | kept) - private - public)}


def run(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    pages = fixtures(out)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    command = [args.runtime, '-m', args.model, '--mmproj', args.projector,
               '--host', '127.0.0.1', '--port', str(port), '-c', '4096', '-np', '1',
               '-t', '3', '-tb', '3', '-b', '512', '-ub', '256', '-ngl', '0',
               '--no-mmproj-offload', '--no-warmup', '--reasoning', 'off',
               '--chat-template-kwargs', '{"enable_thinking":false}']
    summary = {'command': command, 'hardware': {'cpu_visible': os.cpu_count(), 'gpu_used': False}, 'cases': []}
    (out / 'configuration.json').write_text(json.dumps(summary, indent=2))
    started = time.monotonic()
    peaks = {'rss_kib': 0, 'swap_kib': 0}
    done = threading.Event()
    with (out / 'server.log').open('wb') as log:
        proc = subprocess.Popen(command, stdout=log, stderr=log)
        def monitor():
            while not done.wait(.2):
                s = stats(proc.pid)
                peaks['rss_kib'] = max(peaks['rss_kib'], s.get('VmRSS', 0))
                peaks['swap_kib'] = max(peaks['swap_kib'], s.get('VmSwap', 0))
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            while True:
                if proc.poll() is not None:
                    raise RuntimeError('llama-server exited; inspect server.log')
                try:
                    request(base, '/health', timeout=2)
                    break
                except (OSError, RuntimeError):
                    if time.monotonic() - started > 120:
                        raise RuntimeError('model readiness timeout')
                    time.sleep(.25)
            summary['load_seconds'] = round(time.monotonic() - started, 2)
            print('READY ' + json.dumps({'load_seconds': summary['load_seconds'], 'pid': proc.pid}), flush=True)
            for page in pages:
                if page['language'] not in args.languages.split(','):
                    continue
                for kind in args.tasks.split(','):
                    prompt = {
                        'vqa': 'What is the exact project name in this document? Answer using a single word or phrase.',
                        'ocr': 'Extract the text in the image.',
                        'kie': 'Extract key information in the image\n\nPlease output the key information in JSON format according to the following schema:\n' + json.dumps(dict.fromkeys(page['expected'], ''), indent=4),
                        'privacy_lists': 'Extract key information in the image. Return only a JSON object with two arrays of strings: mask and keep. In mask, copy the exact VALUES of the first seven fields, which the document labels private. In keep, copy the exact VALUES of the last two fields, which the document labels public. Copy field values, not field names. Preserve the original spelling. Do not invent information.',
                        'privacy_schema': 'Extract key information in the image\n\nClassify exact field VALUES (not field labels) before sharing the document with an external contractor. Hide the internal company and project, client details, contract ID, internal purchase price, and password. Keep the explicitly public website and product. Return ONLY a JSON object in this shape: {\"mask\":[{\"value\":\"exact value copied from the document\",\"reason\":\"brief justification\"}],\"keep\":[{\"value\":\"exact public value copied from the document\",\"reason\":\"brief justification\"}]}. Include every relevant value, copying its spelling exactly. Do not put field names in the value property.',
                        'privacy': 'Review this document before sharing it with an external contractor. Follow its confidentiality labels: internal company/project, client details, contract ID, internal purchase price and password must be hidden; explicitly public website and product may remain. Extract exact values from the image. Return only JSON with keys "mask" and "keep", each containing a list of objects with "value" and "reason". Do not invent or normalize values.',
                    }[kind]
                    budget = {'vqa': 80, 'ocr': 700, 'kie': 600, 'privacy': 1000, 'privacy_schema': 1000, 'privacy_lists': 500}[kind]
                    payload = {'model': 'xiaomi-ocr-0', 'messages': [{'role': 'user', 'content': [
                        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(Path(page['path']).read_bytes()).decode()}},
                        {'type': 'text', 'text': prompt}]}], 'max_tokens': budget, 'temperature': 0, 'top_p': .8,
                        'presence_penalty': 0, 'repeat_penalty': 1, 'stream': False, 'cache_prompt': False,
                        'chat_template_kwargs': {'enable_thinking': False}}
                    if args.constrain_json and kind in ('kie', 'privacy_schema', 'privacy_lists'):
                        if kind == 'kie':
                            schema = {'type':'object','properties':{k:{'type':'string','maxLength':160} for k in page['expected']},'required':list(page['expected']),'additionalProperties':False}
                        elif kind == 'privacy_lists':
                            schema = {'type':'object','properties':{k:{'type':'array','items':{'type':'string','maxLength':160},'maxItems':12} for k in ['mask','keep']},'required':['mask','keep'],'additionalProperties':False}
                        else:
                            item = {'type':'object','properties':{'value':{'type':'string','maxLength':160},'reason':{'type':'string','maxLength':200}},'required':['value','reason'],'additionalProperties':False}
                            schema = {'type':'object','properties':{k:{'type':'array','items':item,'maxItems':12} for k in ['mask','keep']},'required':['mask','keep'],'additionalProperties':False}
                        payload['response_format'] = {'type':'json_schema','json_schema':{'name':'document_audit','strict':True,'schema':schema}}
                    name = f"{page['language']}-{kind}"
                    print('START ' + name, flush=True)
                    before = stats(proc.pid)
                    start = time.monotonic()
                    try:
                        response = request(base, '/v1/chat/completions', payload, timeout=300)
                        text = response['choices'][0]['message'].get('content') or ''
                        row = {'name': name, 'prompt': prompt, 'constrained_json': args.constrain_json, 'seconds': round(time.monotonic() - start, 2),
                               'finish_reason': response['choices'][0].get('finish_reason'), 'text': text,
                               'metrics': evaluate(kind, text, page), 'usage': response.get('usage'),
                               'timings': response.get('timings'), 'memory': stats(proc.pid)}
                        row['cpu_seconds'] = round(row['memory'].get('cpu_seconds', 0) - before.get('cpu_seconds', 0), 2)
                        (out / f'{name}-response.json').write_text(json.dumps(response, ensure_ascii=False, indent=2))
                    except Exception as exc:
                        row = {'name': name, 'prompt': prompt, 'constrained_json': args.constrain_json, 'seconds': round(time.monotonic() - start, 2), 'error': str(exc)}
                    (out / f'{name}.json').write_text(json.dumps(row, ensure_ascii=False, indent=2))
                    summary['cases'].append(row)
                    summary['peak_memory'] = peaks.copy()
                    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
                    print('RESULT ' + json.dumps({k: v for k, v in row.items() if k not in ['text', 'timings', 'usage']}, ensure_ascii=False), flush=True)
                    if 'error' in row:
                        raise RuntimeError('request failed; stopping this run')
        finally:
            done.set()
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            watcher.join(timeout=2)
            summary['peak_memory'] = peaks
            summary['server_stopped'] = proc.poll() is not None
            (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--projector', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--tasks', default='vqa,ocr,kie,privacy')
    p.add_argument('--languages', default='en,ru')
    p.add_argument('--constrain-json', action='store_true')
    run(p.parse_args())
