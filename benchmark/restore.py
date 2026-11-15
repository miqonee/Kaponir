#!/usr/bin/env python3
"""Restore captured artifacts into a NEW directory for offline report generation.

Does not install models, replay historical commands, or write to original paths.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

from verify import content, safe_path, verify


def restore(root: Path, output: Path, include_ui: bool = False) -> int:
    verify(root)
    output = output.resolve()
    if output.is_relative_to(root.resolve()):
        raise ValueError('Output must be outside the immutable benchmark package')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Output must be a new or empty directory')
    output.mkdir(parents=True, exist_ok=True)
    count = 0

    def write(relative: str, raw: bytes) -> None:
        nonlocal count
        target = safe_path(output, relative)
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError('Conflicting captured versions: ' + relative)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        count += 1

    published = json.loads((root / 'data/validation/manifest.json').read_text())
    for relative, row in published['files'].items():
        raw = gzip.decompress((root / 'data/validation' / row['archive']).read_bytes())
        write('artifacts/validation-schemes/' + relative, raw)

    collection = json.loads((root / 'data/COLLECTION.json').read_text())
    catalog = {row['path']: row for row in collection['files']}
    for row in [*collection['files'], *collection['aliases']]:
        source = row['source']
        if not source.startswith('artifacts/'):
            continue
        if Path(source).suffix in ['.png', '.svg'] and not include_ui:
            continue
        write(source, content(root, catalog[row['path']]))
    (output / 'reports').mkdir()
    state = {'source_manifest_sha256': hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest(),
             'files_restored': count, 'include_ui': include_ui,
             'model_weights_restored': False, 'compiled_exporter_restored': False,
             'historical_commands_executed': False, 'new_inference_calls': 0}
    (output / 'RESTORED.json').write_text(json.dumps(state, indent=2) + '\n')
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--include-ui', action='store_true', help='Also restore captured screenshots')
    args = parser.parse_args()
    try:
        count = restore(Path(__file__).resolve().parent, args.output, args.include_ui)
    except (ValueError, OSError, KeyError, EOFError) as error:
        print('Restore failed: ' + str(error))
        return 1
    print(json.dumps({'files_restored': count, 'output': str(args.output.resolve()), 'new_inference_calls': 0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
