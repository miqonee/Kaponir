#!/usr/bin/env python3
"""Проверка установленного из архива бинарника; никакой публикации и сетевых запросов."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile

from check_docs import check as check_documentation
from check_docs import markdown_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith('KAPONIR_')}
    version = subprocess.check_output([str(binary), '--version'], env=env, text=True).strip()
    args.output.mkdir(parents=True, exist_ok=True)
    artifact = args.output / f'kaponir-{platform.system().lower()}-{platform.machine().lower()}.tar.gz'
    if artifact.exists():
        raise SystemExit(f'Архив уже существует: {artifact}')
    with tempfile.TemporaryDirectory(prefix='kaponir-release-') as tmp:
        tmp = Path(tmp)
        staged = tmp / 'package'
        staged.mkdir()
        shutil.copy2(binary, staged / 'kaponir')
        root = Path(__file__).resolve().parent.parent
        for name in ('README.md', 'README.ru.md', 'CONTRIBUTING.md', 'LICENSE'):
            shutil.copy2(root / name, staged / name)
        shutil.copytree(root / 'docs', staged / 'docs',
                        ignore=shutil.ignore_patterns('WORK_LOG.md'))
        shutil.copytree(root / 'examples', staged / 'examples')
        with tarfile.open(artifact, 'w:gz') as archive:
            archive.add(staged, arcname='kaponir')
        installed = tmp / 'installed'
        with tarfile.open(artifact) as archive:
            archive.extractall(installed, filter='data')
        executable = installed / 'kaponir' / 'kaponir'
        documentation_root = installed / 'kaponir'
        documentation_errors = check_documentation(documentation_root)
        if documentation_errors:
            raise RuntimeError('Release documentation check failed:\n' + '\n'.join(documentation_errors))
        documentation_count = len(markdown_files(documentation_root))
        home = tmp / 'home'
        def call(*arguments, text=None):
            return subprocess.check_output([str(executable), '--home', str(home), *arguments],
                                           input=text, text=True, env=env, stderr=subprocess.PIPE)
        call('init')
        original = 'password: release_fixture_private_482\n'
        masked = call('mask', text=original)
        if 'release_fixture_private_482' in masked or 'ANON_' not in masked:
            raise RuntimeError('Release masking failed')
        if call('restore', text=masked) != original:
            raise RuntimeError('Release roundtrip failed')
        # Повторная установка поверх существующих данных не должна менять ключи.
        keys = {p.name: p.read_bytes() for p in home.glob('*.key')}
        if not keys:
            raise RuntimeError('No key files after init')
        call('init')
        if keys != {p.name: p.read_bytes() for p in home.glob('*.key')}:
            raise RuntimeError('Repeated init changed keys')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix(artifact.suffix + '.sha256').write_text(f'{digest}  {artifact.name}\n')
    result = {'version': version, 'platform': platform.platform(), 'archive': artifact.name,
              'sha256': digest, 'archive_roundtrip': True, 'init_preserves_keys': True,
              'documentation_files_checked': documentation_count,
              'documentation_links_valid': True}
    (args.output / 'release-check.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
