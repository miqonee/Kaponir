"""Build the WebUI's aggregate-only A–D measurements from the published experiment records."""
import argparse
import csv
import json
from pathlib import Path
import tarfile
import tempfile

from summarize_privacy_policy import load_run

ROOT = Path(__file__).resolve().parents[1]
STUDIES = ROOT / 'docs/experiments'
CHECKPOINTS = ROOT / 'research/privacy-policy-phase2/checkpoints'
OUTPUT = ROOT / 'crates/kaponir-proxy/assets/scenario-benchmarks.json'


def measurement(metrics, minutes, answers):
    return dict(private_mask=metrics['private_mask'], public_keep=metrics['public_keep'],
                answers=answers, minutes=None if minutes is None else round(minutes, 5))


def build():
    historical = json.loads((STUDIES / 'single-model-scoring.json').read_text())
    with (STUDIES / 'model-search-final.csv').open() as source:
        models = {row['model']: dict(recommended=row['best_abcd_variant'], series='archive', variants={})
                  for row in csv.DictReader(source)}
    for row in historical['abcd']:
        minutes = None if row['duration_is_lower_bound'] else row['minutes']
        models[row['model']]['variants'][row['variant']] = measurement(row, minutes, row['delivered'])

    native_cohorts = set()
    for archive in sorted(CHECKPOINTS.glob('native-retest-*/evidence.tar.gz')):
        with tarfile.open(archive) as bundle:
            model = archive.parent.name.removeprefix('native-retest-')
            prefix = ('context-supplements/' if model == 'tiny-aya-global' else '') + f'abcd/{model}/'
            if prefix + 'result.json' not in bundle.getnames():
                continue
            with tempfile.TemporaryDirectory() as folder:
                for filename in ('plan.json', 'result.json'):
                    (Path(folder) / filename).write_bytes(bundle.extractfile(prefix + filename).read())
                run = load_run(Path(folder) / 'result.json')
        assert run['model'] == model
        native_cohorts.add(run['cohort_sha256'])
        variants = {}
        for row in run['rows']:
            seconds = row['performance']['http_wall_seconds_including_retries']
            variants[row['variant']] = measurement(row['quality'], None if seconds is None else seconds/60,
                                                   row['outcome_evidence']['delivered_scored_cases'])
        models[model].update(series='native', variants=variants)
    if len(native_cohorts) != 1:
        raise ValueError('Native averages require the same cases, sources, annotations and policy')

    # Only D was measured with these two application presets.
    with tarfile.open(CHECKPOINTS / 'granite-nanbeige-tuning/evidence.tar.gz') as bundle:
        for model, preset in [('granite42-3b', 'low-effort-384'), ('nanbeige42-3b', 'direct-greedy-schema')]:
            row = json.load(bundle.extractfile(f'{model}/full/{preset}/result.json'))['D']
            metrics = row['metrics']
            missing = metrics['technical_error_counts'].get('timed out', 0)
            models[model].update(series='tuned', variants={'D': measurement(metrics, row['http_seconds']/60,
                                                                           metrics['attempted_cases'] - missing)})
    training = json.loads((ROOT / 'research/privacy-training/reports/epoch2/abcd.json').read_text())
    variants = {}
    for variant, pair in training['metrics'].items():
        row = pair['epoch2']
        assert row['received'] == row['requests'] == 5
        variants[variant] = measurement(row, row['http_minutes'], 32)
    models['kaponir-privacy-v4-epoch-2'] = dict(series='epoch2', recommended='D', variants=variants)
    return models


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    result = json.dumps(build(), indent=2, sort_keys=True) + '\n'
    if args.check:
        if OUTPUT.read_text() != result:
            raise SystemExit('Rebuild scenario-benchmarks.json from the experiment records')
    else:
        OUTPUT.write_text(result)
