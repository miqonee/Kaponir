#!/usr/bin/env python3
"""Save and verify the synthetic research checkpoint without model inference."""
import argparse
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tarfile
import tempfile

from apply_measured_privacy import coverage, span_coverage
from check_learning_corpus import load_corpus
from compare_local_models import DOCUMENTS, ROOT
from export_privacy_snapshot import (STUDY, contained, decompress, export as export_legacy,
                                    safe_relative, verify as verify_legacy)
from report_complete_research import _csv, legacy_summary, table
from report_new_models_first import build, render
from report_reference_abcd import build as build_abcd, completed_run as read_completed_abcd
from report_reference_abcd import write_report as write_abcd_report
from run_privacy_policy_suite import atomic_json, file_sha, utc
from summarize_privacy_policy import load_run, require, strict_json
from summarize_privacy_recovery import load_supplement

NAMES = {'plan.json', 'result.json', 'scheme-plan.json', 'transports.json', 'execution.json',
    'verification.json', 'detectors.json', 'native-budget-observations.json',
    'native-mode-observations.json', 'preflight.json', 'config.json', 'status.json',
    'prepared-index.json', 'recovery-plan.json', 'recovery-result.json',
    'mask.json', 'restore.json', 'masked.txt', 'controller.py', 'queue.json',
    'supervisor.py', 'continue_recovery.py', 'diagnose_nanbeige.py', 'full_controller.py',
    'supervisor-status.json', 'resume-manifest.json', 'launch-record.json', 'launch-receipt.json',
    'full-config.json', 'full-status.json', 'nanbeige-diagnostic-config.json'}
TREES = ('new-models-reference-v1', 'new-models-reference-v1-application',
    'new-models-reference-v2', 'new-models-reference-v2-application', 'application-v2',
    'application-transport-v1', 'application-resume-v1', 'abcd-reference-v1', 'abcd-reference-v1-application',
    'abcd-reference-v2', 'abcd-reference-v2-application',
    'priority-eight-v1', 'suite-v1', 'recovery-v1', 'recovery-resume-v1',
    'complete-v1', 'fresh-v1', 'policy-v2', 'gemma-it-v1', 'analysis/requested-auto-v2-handoff',
    'analysis/selected-d32-prepared-v1', 'analysis/lfm26-off-profile-incompatible-v1')
EXTRA = ('analysis/requested-json-mode-audit-v1.json', 'analysis/existing-new-resource-context-v1.json',
    'analysis/work-continuation.json', 'analysis/lfm26-native-off-observation-v1.json',
    'analysis/pilot-eight-portable-v1.tar.gz', 'analysis/pilot-eight-portable-v1.tar.binding.json',
    'analysis/new-corpus-first-full-portable-v1.tar.gz',
    'analysis/new-corpus-first-full-portable-v1.tar.binding.json')


def old_calibration():
    rows = []
    queue = strict_json((STUDY / 'recovery-resume-v1/queue.json').read_text())
    for item in queue['runs']:
        original = Path(item['result']);supplement = Path(item['output']) / 'recovery-result.json'
        run = load_supplement(supplement, original) if supplement.is_file() else load_run(original)
        rows.append({'model': item['model'], 'original_sha256': file_sha(original),
            'supplement_sha256': file_sha(supplement) if supplement.is_file() else None,
            'rows': run.get('effective_rows', run.get('rows'))})
    return {'new_inference_calls': 0, 'models': 7, 'variants': ['A', 'B', 'C', 'D'], 'runs': rows}


def render_calibration(report):
    lines = ['## Отдельная проверка A/B/C/D старых моделей', '',
        'В каждом варианте 32 прежние цели: 16 приватных и 16 публичных. '
        'Полученные дополнения после тайм-аутов включены; исходные попытки сохранены. '
        'Это проверка решений по заранее найденным целям, а не поиск по 92 значениям. '
        'Исторические sampling и timeout различались; единый профиль всех старых моделей не заявляется.', '']
    values, csv_rows = [], []
    columns = ['model', 'variant', 'private_mask', 'private_keep', 'public_mask',
               'explicit_review', 'inapplicable_received', 'unobserved']
    for run in report['runs']:
        for row in run['rows']:
            q, e = row['quality'], row['outcome_evidence']
            value = [run['model'], row['variant'], q['private_mask'], q['private_keep'], q['public_mask'],
                q['explicit_review'], e['delivered_inapplicable_cases'], e['unobserved_scored_cases']]
            values.append(value);csv_rows.append(dict(zip(columns, value)))
    lines += table(['Модель', 'Вариант', 'Private MASK /16', 'Private KEEP', 'Public MASK /16',
                    'Явный REVIEW', 'Полученных неприменимых целей', 'Целей без ответа'], values)
    lines += ['', 'Нулевой MASK при 32 целях без ответа у Nanbeige не является оценкой качества. '
        'Gemma D — 14 private MASK и 0 public MASK; MiniCPM B — 11 private MASK, '
        '0 public MASK и 6 неприменимых решений. D не считается универсальным победителем.', '']
    return '\n'.join(lines), csv_rows, columns


def pack_producer(config):
    root = Path(config['runner_root']);stream = BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for name, expected in sorted(config['producer_inputs_sha256'].items()):
            safe_relative(name);path = contained(root / name, root)
            raw = path.read_bytes();require(hashlib.sha256(raw).hexdigest() == expected, 'checkpoint_producer_changed')
            info = tarfile.TarInfo(name);info.size = len(raw);info.mode = 0o644;info.mtime = 0
            tar.addfile(info, BytesIO(raw))
    return gzip.compress(stream.getvalue(), mtime=0)


def create(output):
    output = Path(output).resolve()
    require(not output.exists(), 'checkpoint_exists_no_overwrite')
    require(not output.is_relative_to(ROOT / 'benchmark'), 'old_benchmark_must_stay_immutable')
    output.mkdir(parents=True)
    legacy = export_legacy(STUDY, output / 'legacy', STUDY / 'recovery-resume-v1/queue.json')
    report = build(STUDY / 'new-models-reference-v2', STUDY / 'application-v2',
                   STUDY / 'new-models-reference-v2-application')
    require(not report['rejected_inputs'], 'checkpoint_comparison_has_rejected_sources')
    abcd = None
    if (STUDY / 'abcd-reference-v2-application/status.json').is_file():
        abcd = build_abcd(STUDY / 'abcd-reference-v2', STUDY / 'abcd-reference-v2-application')
    manifest = {'version': 1, 'created_at_utc': utc(), 'writer_sha256': file_sha(Path(__file__)),
        'new_inference_calls': 0,
        'user_rules_changed': 0, 'legacy_runs': len(legacy['runs']), 'captured': [], 'closed_schemes': [],
        'closed_abcd': [],
        'active_snapshots_are_not_completed_runs_or_resume_sources': True,
        'snapshot_files_have_individual_revisions': True, 'weights_or_binaries_or_vault_included': False}

    def capture(source, raw=None):
        relative = source.relative_to(STUDY).as_posix();safe_relative(relative)
        source = contained(source, STUDY)
        if raw is None:
            raw = source.read_bytes()
        archive = 'evidence/' + relative + '.gz';destination = output / archive
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(gzip.compress(raw, mtime=0))
        manifest['captured'].append({'source': relative, 'archive': archive,
            'raw_sha256': hashlib.sha256(raw).hexdigest(), 'raw_bytes': len(raw), 'captured_at_utc': utc()})

    # Capture owner states before copying results. A job closing while its files
    # are being copied belongs to the following checkpoint.
    captured_status_paths = set()
    for series, manifest_key in (('new-models-reference-v2', 'closed_schemes'),
                                 ('abcd-reference-v2', 'closed_abcd')):
        status_path = STUDY / series / 'status.json'
        if status_path.is_file():
            status_raw = contained(status_path, STUDY).read_bytes()
            capture(status_path, status_raw)
            captured_status_paths.add(status_path)
            for model, row in strict_json(status_raw.decode())['jobs'].items():
                if row['status'].startswith('completed'):
                    manifest[manifest_key].append({'model': model,
                        'source': f'{series}/runs/{model}/result.json', 'result_sha256': row['result_sha256']})
    for tree in TREES:
        folder = STUDY / tree
        if folder.is_dir():
            for source in sorted(folder.rglob('*')):
                eligible = (source.name in NAMES or source.name.startswith('session-') and source.suffix == '.json'
                            or tree.startswith('analysis/') and source.suffix == '.json')
                if eligible and source not in captured_status_paths and source.is_file():
                    capture(source)
    for name in EXTRA:
        if (STUDY / name).is_file():capture(STUDY / name)
    for group in ('new-models-reference-v2', 'abcd-reference-v1', 'abcd-reference-v2'):
        cfg = STUDY / group / 'config.json'
        if cfg.exists():
            config = strict_json(cfg.read_text());destination = output / 'producers' / (group + '.tar.gz')
            destination.parent.mkdir(exist_ok=True);destination.write_bytes(pack_producer(config))
    report_dir = output / 'reports';report_dir.mkdir()
    atomic_json(report_dir / 'schemes.json', report)
    old = old_calibration();atomic_json(report_dir / 'old-abcd.json', old)
    text, rows, columns = render_calibration(old)
    _csv(report_dir / 'old-abcd.csv', rows, columns)
    csv_path = report_dir / 'old-abcd.csv'
    csv_path.write_bytes(csv_path.read_bytes().replace(b'\r\n', b'\n'))
    (report_dir / 'REPORT.md').write_text(render(report) + '\n' + text)
    if abcd is not None:
        write_abcd_report(abcd, report_dir / 'new-abcd')
    manifest['files'] = {p.relative_to(output).as_posix(): {'sha256': file_sha(p), 'bytes': p.stat().st_size}
                         for p in sorted(output.rglob('*')) if p.is_file()}
    atomic_json(output / 'manifest.json', manifest)
    return verify(output, rescore=True)


def verify_applications(root, known_sources):
    _, expected, public, sources, _ = load_corpus();jobs, restores = 0, 0
    for result in root.rglob('result.json'):
        data = strict_json(result.read_text())
        if data.get('status') != 'completed' or data.get('source_phase') not in (
                'six_scheme_calibration', 'calibration_controls', 'calibration_transport_supplement'):
            continue
        # Application source phases have changed names over time; only objects
        # with the explicit mask/restore contract are eligible here.
        if 'documents' not in data or 'exact_roundtrips' not in data:continue
        require(data['new_inference_calls'] == data['user_rules_changed'] == 0,
                'checkpoint_application_contract_changed')
        require(data['source_result_sha256'] in known_sources, 'checkpoint_application_source_missing')
        for doc in data['documents']:
            name, mode = doc['file'], doc['mode'];require(name in DOCUMENTS, 'checkpoint_unknown_document')
            directory = result.parent / mode / (str(DOCUMENTS.index(name)+1).zfill(2)+'-'+Path(name).stem)
            mask = strict_json((directory / 'mask.json').read_text())
            restored = strict_json((directory / 'restore.json').read_text())
            original = sources[name];private = [v for f,v in expected if f == name]
            require(restored['text'] == original and (directory / 'masked.txt').read_text() == mask['text']
                    and doc['restore_exact'] and doc['persistent_rules_unchanged'], 'checkpoint_restore_changed')
            rescored = {**coverage(original, mask['text'], private, public.get(name, [])),
                        **span_coverage(original, private, mask['replacements'], mask['text'])}
            require(all(doc[k] == v for k,v in rescored.items()), 'checkpoint_application_metrics_changed')
            restores += 1
        require(len(data['documents']) == data['exact_roundtrips'], 'checkpoint_restore_count_changed')
        jobs += 1
    return jobs, restores


def verify(directory, rescore=False):
    directory = Path(directory).resolve();manifest = strict_json((directory/'manifest.json').read_text())
    require(manifest['version'] == 1 and manifest['new_inference_calls'] == manifest['user_rules_changed'] == 0,
            'checkpoint_contract_changed')
    require({p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
            == set(manifest['files']) | {'manifest.json'}, 'checkpoint_file_set_changed')
    for name, binding in manifest['files'].items():
        safe_relative(name);path = contained(directory/name, directory)
        require(path.stat().st_size == binding['bytes'] and file_sha(path) == binding['sha256'],
                'checkpoint_file_changed:' + name)
    old = verify_legacy(directory/'legacy', rescore=rescore)
    jobs, restores, closed, abcd_closed = 0, 0, 0, 0
    with tempfile.TemporaryDirectory(prefix='kaponir-checkpoint-read-') as temporary:
        root = Path(temporary);known = set()
        legacy = strict_json((directory/'legacy/manifest.json').read_text())
        for run in legacy['runs']:
            for record in run['files'].values():known.add(record['raw_sha256'])
        for source in (ROOT/'benchmark/data/validation').glob('*calibration--result.json.gz'):
            known.add(hashlib.sha256(gzip.decompress(source.read_bytes())).hexdigest())
        for row in manifest['captured']:
            safe_relative(row['source']);safe_relative(row['archive'])
            raw = decompress(contained(directory/row['archive'], directory).read_bytes())
            require(len(raw) == row['raw_bytes'] and hashlib.sha256(raw).hexdigest() == row['raw_sha256'],
                    'checkpoint_raw_changed')
            path = root/row['source'];path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(raw)
            known.add(row['raw_sha256'])
        if rescore:
            for row in manifest['closed_schemes']:
                path = root/safe_relative(row['source']);require(file_sha(path) == row['result_sha256'], 'checkpoint_closed_changed')
                run = legacy_summary(path);require(run['status'] == 'completed' and len(run['rows']) == 6,
                                                  'checkpoint_closed_incomplete');closed += 1
            for row in manifest.get('closed_abcd', []):
                read_completed_abcd(root/safe_relative(row['source']), row['model'], row['result_sha256'])
                abcd_closed += 1
            jobs, restores = verify_applications(root, known)
    return {'files': len(manifest['files']), 'legacy_runs': old['runs'], 'closed_new_scheme_models': closed,
            'closed_new_abcd_models': abcd_closed,
            'verified_application_jobs': jobs, 'exact_roundtrips': restores,
            'new_inference_calls': 0, 'rescored': rescore}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path);group.add_argument('--verify', type=Path)
    parser.add_argument('--rescore', action='store_true')
    args = parser.parse_args()
    print(json.dumps(create(args.output) if args.output else verify(args.verify, args.rescore)))


if __name__ == '__main__':main()
