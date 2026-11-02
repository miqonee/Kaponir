#!/usr/bin/env python3
"""Transfer real settings from a running audit server to a fresh CLI home.
Keys are read without echo; full exports exist only in a private temporary directory.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from live_validation import free_port, request


def run(args, llm_key, s1_key):
    base = args.base.rstrip('/')
    api = lambda path, body=None, method=None: request(base + path, body, method)
    if args.connection_id:
        connection = next(c for c in api('/api/v1/connections')['connections'] if c['id'] == args.connection_id)
        model = next(m for m in connection['models'] if 'gemini' in m and 'flash' in m)
    else:
        connection = api('/api/v1/connections', {
            'kind': 'openai', 'name': 'Route · перенос реальных настроек',
            'base_url': args.upstream, 'api_key': llm_key,
            'enabled': True, 'pool': False, 'auto_models': True})
        model = next(m for m in connection['models'] if 'gemini' in m and 'flash' in m)
        api('/api/v1/ui/s1', {'settings': {
            'provider': 'http', 'shadow': True, 'context_bytes': 160,
            'http': {'url': args.upstream.rstrip('/') + '/systemone',
                     'protocol': 'systemone', 'model': 'oc/jev-1.13-free',
                     'timeout_ms': 20000, 'disclosure': 'l2', 'token_env': None}},
            'key': s1_key}, 'PUT')
    if args.review_ui:
        print('READY ' + base + '/ui/#connect', flush=True)
        input('Review/save the connection and S1 in the browser, then press Enter: ')
    redacted = api('/api/v1/ui/config/export', {'include_secrets': False})
    full = api('/api/v1/ui/config/export', {'include_secrets': True})
    assert llm_key not in json.dumps(redacted) and s1_key not in json.dumps(redacted)
    assert full['s1_key'] == s1_key
    assert next(c for c in full['providers']['connections'] if c['id'] == connection['id'])['api_key'] == llm_key
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    # Redacted exports still contain user rules, so this audit uses synthetic data only.
    (out / 'export-without-keys.json').write_text(json.dumps(redacted, ensure_ascii=False, indent=2))
    binary = str(Path(args.binary).resolve())
    result = {'export_without_keys': True, 'export_with_keys': True, 'connection_id': connection['id'], 'connection_name': connection['name'], 'model': model}
    with tempfile.TemporaryDirectory(prefix='kaponir-live-transfer-') as temp:
        home = Path(temp) / 'target'
        bundle = Path(temp) / 'settings.json'
        fd = os.open(bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(full, f)
        subprocess.run([binary, '--home', str(home), 'init'], check=True, capture_output=True)
        imported = subprocess.run([binary, '--home', str(home), 'config', 'import', str(bundle)], capture_output=True)
        assert imported.returncode == 0, 'CLI import failed'
        bundle.unlink()
        result['cli_import'] = True
        port = free_port()
        target = f'http://127.0.0.1:{port}'
        with (out / 'target-server.log').open('wb') as log:
            proc = subprocess.Popen([binary, '--home', str(home), 'serve', '--listen', f'127.0.0.1:{port}'], stdout=log, stderr=log)
            try:
                for _ in range(150):
                    try:
                        request(target + '/api/v1/ui/info')
                        break
                    except OSError:
                        if proc.poll() is not None:
                            raise RuntimeError('target exited')
                        time.sleep(.1)
                target_api = lambda path, body=None, method=None: request(target + path, body, method)
                restored_config = target_api('/api/v1/ui/config/export', {'include_secrets': True})
                result['s1_key_preserved'] = restored_config['s1_key'] == s1_key
                result['provider_key_preserved'] = next(c for c in restored_config['providers']['connections'] if c['id'] == connection['id'])['api_key'] == llm_key
                result['rules_preserved'] = restored_config['rules'] == full['rules']
                original = 'password: Synthetic_Transfer_Q8!\nHost: db.aurora.internal'
                masked = target_api('/api/v1/ui/mask', {'text': original, 'commit': True})
                restored = target_api('/api/v1/ui/restore', {'text': masked['text']})
                result['fresh_vault_roundtrip'] = restored['text'] == original
                reply = target_api(f"/p/{connection['id']}/v1/chat/completions", {
                    'model': model, 'messages': [{'role': 'user', 'content': 'Echo exactly this block. No markdown or explanations.\n' + original}],
                    'temperature': 0, 'max_tokens': 300, 'stream': False})
                result['real_request_after_import'] = reply['choices'][0]['message']['content'] == original
                s1_sample = 'Закрытый репозиторий клиента /workspace/auroraledger/k8s. Не раскрывать имя репозитория.'
                scored = target_api('/api/v1/ui/mask', {'text': s1_sample})
                result['s1_after_import'] = any(e['source'].startswith('s1') for c in scored['explain'] for e in c['evidence'])
                (out / 's1-after-import.json').write_text(json.dumps(scored, ensure_ascii=False, indent=2))
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                result['target_stopped'] = proc.poll() is not None
    result['temporary_files_removed'] = not Path(temp).exists()
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    assert all(v for v in result.values()), 'one or more checks failed'


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', required=True)
    p.add_argument('--binary', default='target/release/kaponir')
    p.add_argument('--upstream', default='https://route.fn.x3dev.ru/v1')
    p.add_argument('--output', required=True)
    p.add_argument('--review-ui', action='store_true')
    p.add_argument('--connection-id')
    args = p.parse_args()
    llm_key = getpass.getpass('LLM key (hidden): ')
    s1_key = getpass.getpass('S1 key (hidden): ')
    try:
        run(args, llm_key, s1_key)
    except Exception as exc:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'failure.json').write_text(json.dumps({'type': type(exc).__name__, 'status': getattr(exc, 'code', None)}))
        raise
