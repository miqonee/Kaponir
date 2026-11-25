#!/usr/bin/env python3
"""Проверка установленного из архива бинарника; никакой публикации и сетевых запросов."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from check_docs import check as check_documentation
from check_docs import FENCE, markdown_files, targets


def qualify_source_links(source_root, package_root, revision):
    """Keep packaged docs local; bind omitted research/code links to this source revision."""
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('source_revision_required')
    source_root, package_root = Path(source_root).resolve(), Path(package_root).resolve()
    changed = 0
    for path in markdown_files(package_root):
        lines, fenced = [], False
        source_path = source_root / path.relative_to(package_root)
        for line in path.read_text(encoding='utf-8').splitlines(keepends=True):
            if FENCE.match(line):
                fenced = not fenced
            replacements = []
            if not fenced and not FENCE.match(line):
                for raw, start, end in targets(line, with_spans=True):
                    parsed = urlsplit(raw)
                    if parsed.scheme or parsed.netloc or not parsed.path:
                        continue
                    destination = (path.parent / unquote(parsed.path)).resolve()
                    if destination.is_file() and destination.is_relative_to(package_root):
                        continue
                    source = (source_path.parent / unquote(parsed.path)).resolve()
                    if source.is_file() and source.is_relative_to(source_root):
                        url = 'https://github.com/miqonee/Kaponir/blob/' + revision + '/' + quote(source.relative_to(source_root).as_posix())
                        parts = urlsplit(url)
                        replacements.append((start, end, urlunsplit((parts.scheme, parts.netloc, parts.path,
                                                                    parsed.query, parsed.fragment))))
            for start, end, url in reversed(replacements):
                line = line[:start] + url + line[end:]
                changed += 1
            lines.append(line)
        path.write_text(''.join(lines), encoding='utf-8')
    return changed


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
        source_errors = check_documentation(root)
        if source_errors:
            raise RuntimeError('Source documentation check failed:\n' + '\n'.join(source_errors))
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        for name in ('README.md', 'README.ru.md', 'CONTRIBUTING.md', 'LICENSE'):
            shutil.copy2(root / name, staged / name)
        shutil.copytree(root / 'docs', staged / 'docs')
        shutil.copytree(root / 'examples', staged / 'examples')
        source_links = qualify_source_links(root, staged, revision)
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
              'documentation_links_valid': True, 'source_revision': revision,
              'source_revision_links': source_links}
    (args.output / 'release-check.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
