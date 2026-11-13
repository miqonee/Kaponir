#!/usr/bin/env python3
"""Plot recorded runtime memory; requires matplotlib, never loads model weights."""
import argparse
import json
from pathlib import Path

from compare_local_models import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT/'docs/VALIDATION_RESULTS.json')
    parser.add_argument('--output', type=Path, default=ROOT/'docs/assets/validation-memory.svg')
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    model = report['primary_matrix']['models']['qwen3.5-2b']
    control = report['runtime_cache_diagnostic']
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': 10, 'svg.fonttype': 'none'})
    figure, axes = plt.subplots(1, 2, figsize=(11.2, 3.8), layout='constrained')
    samples = [(i+1, value/2**30) for i, value in enumerate(model['rss_by_request_bytes']) if value is not None]
    axes[0].plot(*zip(*samples), color='#315fd9', linewidth=2)
    axes[0].axhline(8, linestyle=':', color='#9b5266', label='Лимит среды: 8 GiB')
    for segment in model.get('runtime_cache_history') or []:
        if segment.get('cache_ram_mib') == 0:
            boundary = segment['from_completed_call']-.5
            axes[0].axvline(boundary, linestyle='--', color='#c16c0b')
            axes[0].annotate('Архив отключён,\nruntime перезапущен', (boundary, 5.4),
                xytext=(-122, 10), textcoords='offset points', color='#9a5306')
    axes[0].set(title='Qwen: длинный прогон', xlabel='Завершённый запрос', ylabel='RSS процесса модели, GiB', ylim=(1.6, 8.5))
    axes[0].legend(loc='upper left', frameon=False)
    for run in control['runs']:
        disabled = run['cache_ram_mib'] == 0
        axes[1].plot([row['request'] for row in run['samples']],
            [row['memory']['model_rss_bytes']/2**30 for row in run['samples']],
            color='#19816d' if disabled else '#c16c0b', marker='o', markersize=3,
            label='Архив отключён' if disabled else 'Архив 8192 MiB')
    axes[1].set(title='Контроль: ответ ограничен одним токеном', xlabel='Завершённый запрос', ylabel='RSS процесса модели, GiB')
    axes[1].legend(frameon=False)
    for axis in axes:
        axis.grid(axis='y', alpha=.18)
        axis.spines[['top', 'right']].set_visible(False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, metadata={'Date': None})
    print(args.output)


if __name__ == '__main__':
    main()
