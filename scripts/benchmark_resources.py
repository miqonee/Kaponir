"""Read-only, sampled Linux telemetry for reproducible local-model benchmarks.

RSS peaks are observed samples, not exact request high-water marks. Cgroup data
describes the complete group; it must never be attributed solely to the model.
Unavailable counters stay null instead of masquerading as zero resource use.
"""
from collections import Counter
import math
import os
from pathlib import Path
import platform
import re
import threading
import time


def _text(path):
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return None


def _number(value):
    try:
        return int(value) if value is not None and value.strip() != 'max' else None
    except (ValueError, AttributeError):
        return None


def _pairs(value):
    result = {}
    for line in (value or '').splitlines():
        columns = line.split()
        if len(columns) == 2:
            number = _number(columns[1])
            if number is not None:
                result[columns[0].rstrip(':')] = number
    return result


def _cgroup_directory(proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup')):
    for line in (_text(proc_root / 'self/cgroup') or '').splitlines():
        if line.startswith('0::'):
            relative = Path(line[3:].lstrip('/'))
            if '..' not in relative.parts:
                candidate = cgroup_root / relative
                if (candidate / 'cgroup.controllers').exists() or (candidate / 'memory.current').exists():
                    return candidate
    return cgroup_root


def _hierarchy(directory, root):
    while directory.is_relative_to(root):
        yield directory
        if directory == root:
            break
        directory = directory.parent


def _quota(value):
    values = (value or '').split()
    if len(values) != 2:
        return None
    quota, period = map(_number, values)
    return quota / period if quota is not None and quota >= 0 and period and period > 0 else None


def hardware_snapshot(proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup')):
    """Hardware and limits only: no names, UUIDs, current load or free-memory data."""
    proc_root, cgroup_root = Path(proc_root), Path(cgroup_root)
    cpuinfo = _text(proc_root / 'cpuinfo') or ''
    cpus = []
    for block in re.split(r'\n\s*\n', cpuinfo.strip()):
        cpus.append(dict((key.strip(), value.strip()) for line in block.splitlines()
                         if ':' in line for key, value in [line.split(':', 1)]))
    cpu_models = sorted({row.get('model name') or row.get('Processor')
                         for row in cpus if row.get('model name') or row.get('Processor')})
    physical = {(row['physical id'], row['core id']) for row in cpus
                if 'physical id' in row and 'core id' in row}
    flags = set().union(*(set((row.get('flags') or row.get('Features') or '').split()) for row in cpus))
    memory = {}
    for line in (_text(proc_root / 'meminfo') or '').splitlines():
        match = re.fullmatch(r'(MemTotal|SwapTotal):\s+(\d+) kB', line)
        if match:
            memory[match[1]] = int(match[2]) * 1024
    directory = _cgroup_directory(proc_root, cgroup_root)
    quotas, limits = [], []
    cpu_limits_readable = memory_limits_readable = False
    for ancestor in _hierarchy(directory, cgroup_root):
        cpu_max, memory_max = _text(ancestor / 'cpu.max'), _text(ancestor / 'memory.max')
        quota = _quota(cpu_max)
        limit = _number(memory_max)
        cpu_limits_readable |= bool(cpu_max and (quota is not None or cpu_max.strip().startswith('max ')))
        memory_limits_readable |= bool(memory_max and (limit is not None or memory_max.strip() == 'max'))
        if quota is not None:
            quotas.append(quota)
        if limit is not None:
            limits.append(limit)
    try:
        affinity_count = len(os.sched_getaffinity(0))
    except (OSError, AttributeError):
        affinity_count = None
    return {'system': platform.system(), 'architecture': platform.machine(),
            'kernel_release': platform.release(), 'cpu_models': cpu_models or None,
            'host_logical_cpus': sum('processor' in row for row in cpus) or os.cpu_count(),
            'host_physical_cpu_cores': len(physical) or None,
            'cpu_topology_scope': 'visible_proc_cpuinfo_topology',
            'affinity_logical_cpus': affinity_count,
            'instruction_sets': sorted(flags & {'avx', 'avx2', 'avx512f', 'fma', 'f16c', 'neon', 'sve', 'asimd'}),
            'host_ram_bytes': memory.get('MemTotal'), 'host_swap_bytes': memory.get('SwapTotal'),
            'effective_cgroup_cpu_quota_cores': min(quotas) if quotas else None,
            'effective_cgroup_memory_limit_bytes': min(limits) if limits else None,
            'cgroup_cpu_limit_status': 'limited' if quotas else 'unlimited' if cpu_limits_readable else 'unavailable',
            'cgroup_memory_limit_status': 'limited' if limits else 'unlimited' if memory_limits_readable else 'unavailable',
            'cgroup_version': 2 if (directory / 'cgroup.controllers').exists() else None,
            'gpu_measurement': 'not_collected_cpu_only_benchmark'}


def read_process(pid, proc_root=Path('/proc')):
    """Read one process identity and cumulative counters; comm may contain spaces/)."""
    stat = _text(Path(proc_root) / str(pid) / 'stat')
    if stat is None:
        return {'available': False, 'error': 'process_unavailable'}
    try:
        fields = stat[stat.rindex(')') + 2:].split()
        hz, page = os.sysconf('SC_CLK_TCK'), os.sysconf('SC_PAGE_SIZE')
        io = _pairs(_text(Path(proc_root) / str(pid) / 'io'))
        return {'available': True, 'pid': pid, 'start_time_ticks': int(fields[19]),
                'rss_bytes': int(fields[21]) * page,
                'cpu_seconds': (int(fields[11]) + int(fields[12])) / hz,
                'cpu_user_seconds': int(fields[11]) / hz,
                'cpu_system_seconds': int(fields[12]) / hz,
                'minor_faults': int(fields[7]), 'major_faults': int(fields[9]),
                'read_bytes': io.get('read_bytes'), 'write_bytes': io.get('write_bytes')}
    except (ValueError, IndexError, OSError):
        return {'available': False, 'error': 'process_stat_unavailable'}


def cgroup_snapshot(proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup')):
    directory = _cgroup_directory(Path(proc_root), Path(cgroup_root))
    return {'memory_current_bytes': _number(_text(directory / 'memory.current')),
            'memory_lifetime_peak_bytes': _number(_text(directory / 'memory.peak')),
            'swap_current_bytes': _number(_text(directory / 'memory.swap.current')),
            'swap_lifetime_peak_bytes': _number(_text(directory / 'memory.swap.peak')),
            'memory_events': _pairs(_text(directory / 'memory.events')) or None,
            'cpu_stat': _pairs(_text(directory / 'cpu.stat')) or None}


def _difference(first, last):
    if isinstance(first, dict) and isinstance(last, dict):
        return {key: _difference(first.get(key), last.get(key)) for key in first.keys() | last.keys()}
    if isinstance(first, (int, float)) and isinstance(last, (int, float)) and last >= first:
        return last - first
    return None


class ProcessSampler:
    """Sample role->PID providers without making unavailable telemetry fatal.

    CPU values are differences between observed cumulative counters. Short-lived
    processes and activity outside the observed interval may be missed. RSS sums
    can count shared pages more than once; they are not a minimum-RAM estimate.
    """
    def __init__(self, pid_provider, interval_seconds=0.25, *, process_reader=read_process,
                 cgroup_reader=cgroup_snapshot, clock=time.monotonic):
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError('invalid_sampling_interval')
        self.pid_provider, self.interval_seconds = pid_provider, interval_seconds
        self.process_reader, self.cgroup_reader, self.clock = process_reader, cgroup_reader, clock
        self._event, self._lock = threading.Event(), threading.Lock()
        self._thread = None
        self._started = self._stopped = None
        self._records, self._initial, self._current = {}, {}, {}
        self._cgroup_first = self._cgroup_last = None
        self._cgroup_peak = self._swap_peak = self._total_peak = None
        self._samples, self._errors = 0, Counter()
        self._summary = None

    def start(self):
        if self._started is not None:
            return self
        self._started = self.clock()
        self.sample()
        try:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        except RuntimeError:
            self._errors['sampling_thread_unavailable'] += 1
            self._thread = None
        return self

    def _run(self):
        while not self._event.wait(self.interval_seconds):
            self.sample()

    def sample(self):
        with self._lock:
            self._samples += 1
            try:
                pids = self.pid_provider()
                if not isinstance(pids, dict):
                    raise TypeError()
            except Exception:
                self._errors['pid_provider_unavailable'] += 1
                pids = {}
            current, unique = {}, {}
            for role, pid in pids.items():
                if not isinstance(role, str):
                    self._errors['invalid_process_role'] += 1
                    continue
                self._records.setdefault(role, {})
                snapshot = None
                if type(pid) is int and pid > 0:
                    try:
                        value = self.process_reader(pid)
                        if (value.get('available') and all(_finite_nonnegative(value.get(key))
                                for key in ['rss_bytes', 'cpu_seconds', 'start_time_ticks'])):
                            snapshot = dict(value)
                        else:
                            self._errors[role + ':' + value.get('error', 'process_unavailable')] += 1
                    except Exception:
                        self._errors[role + ':process_reader_unavailable'] += 1
                current[role] = snapshot['rss_bytes'] if snapshot else None
                if role not in self._initial:
                    self._initial[role] = current[role]
                if snapshot is None:
                    continue
                identity = (pid, snapshot['start_time_ticks'])
                unique[identity] = snapshot['rss_bytes']
                records = self._records[role]
                if identity not in records:
                    records[identity] = {'first': snapshot, 'last': snapshot,
                        'observed_peak_rss_bytes': snapshot['rss_bytes'], 'samples': 1}
                else:
                    record = records[identity]
                    record['last'] = snapshot
                    record['samples'] += 1
                    record['observed_peak_rss_bytes'] = max(record['observed_peak_rss_bytes'], snapshot['rss_bytes'])
            self._current = current
            if unique:
                total = sum(unique.values())
                self._total_peak = total if self._total_peak is None else max(self._total_peak, total)
            try:
                group = self.cgroup_reader()
                if not isinstance(group, dict):
                    raise TypeError()
                if self._cgroup_first is None:
                    self._cgroup_first = group
                self._cgroup_last = group
                memory, swap = group.get('memory_current_bytes'), group.get('swap_current_bytes')
                if memory is not None:
                    self._cgroup_peak = memory if self._cgroup_peak is None else max(self._cgroup_peak, memory)
                if swap is not None:
                    self._swap_peak = swap if self._swap_peak is None else max(self._swap_peak, swap)
            except Exception:
                self._errors['cgroup_reader_unavailable'] += 1

    def stop(self):
        if self._summary is not None:
            return self._summary
        if self._started is None:
            self.start()
        self._event.set()
        if self._thread is not None:
            self._thread.join()
        self.sample()
        self._stopped = self.clock()
        duration = max(0, self._stopped - self._started)
        roles = {}
        for role, records in self._records.items():
            processes = []
            for (pid, start), row in records.items():
                counters = {key: _difference(row['first'].get(key), row['last'].get(key))
                            if row['samples'] >= 2 else None for key in
                            ['cpu_seconds', 'cpu_user_seconds', 'cpu_system_seconds',
                             'minor_faults', 'major_faults', 'read_bytes', 'write_bytes']}
                processes.append({'pid': pid, 'start_time_ticks': start, 'samples': row['samples'],
                                  'rss_bytes_first': row['first']['rss_bytes'],
                                  'rss_bytes_last': row['last']['rss_bytes'],
                                  'observed_peak_rss_bytes': row['observed_peak_rss_bytes'], **counters})
            cpu_values = [row['cpu_seconds'] for row in processes if row['cpu_seconds'] is not None]
            cpu = sum(cpu_values) if cpu_values else None
            cpu_parts = {}
            for key in ['cpu_user_seconds', 'cpu_system_seconds']:
                values = [row[key] for row in processes if row[key] is not None]
                cpu_parts[key] = sum(values) if values else None
            peaks = [row['observed_peak_rss_bytes'] for row in processes]
            roles[role] = {'processes': processes, 'rss_bytes_start': self._initial.get(role),
                'rss_bytes_end': self._current.get(role), 'observed_peak_rss_bytes': max(peaks) if peaks else None,
                'cpu_seconds': cpu,
                **cpu_parts,
                'average_cpu_cores': cpu / duration if cpu is not None and duration else None,
                'average_cpu_percent_one_core': 100 * cpu / duration if cpu is not None and duration else None,
                'cpu_counter_coverage': 'observed_deltas_only',
                'processes_with_cpu_delta': len(cpu_values), 'observed_processes': len(processes)}
        self._summary = {'duration_seconds': duration, 'sample_count': self._samples,
            'sample_interval_seconds': self.interval_seconds, 'roles': roles,
            'cpu_counter_resolution_seconds': 1 / os.sysconf('SC_CLK_TCK'),
            'observed_peak_total_rss_bytes': self._total_peak, 'rss_peak_method': 'maximum_observed_samples',
            'sample_errors': dict(self._errors),
            'cgroup': {'scope': 'whole_cgroup_including_other_processes_and_page_cache',
                'baseline': self._cgroup_first, 'end': self._cgroup_last,
                'counter_deltas': {key: _difference((self._cgroup_first or {}).get(key),
                                                   (self._cgroup_last or {}).get(key))
                                   for key in ['memory_events', 'cpu_stat']},
                'observed_peak_memory_current_bytes': self._cgroup_peak,
                'observed_peak_swap_current_bytes': self._swap_peak},
            'limits': ['RSS sums may count shared pages more than once.',
                       'Sampled peaks may miss short spikes; missing processes are not zero use.',
                       'CPU counters have tick granularity; very short intervals have coarse rates.',
                       'Cgroup lifetime peaks do not describe a request-specific peak.']}
        return self._summary

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def _finite_nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def throughput_metrics(raw_reply, request_seconds):
    """Native prefill/decode rates separately from whole nonstreaming latency."""
    raw_reply = raw_reply if isinstance(raw_reply, dict) else {}
    timing = raw_reply.get('timings') or {}
    timing = timing if isinstance(timing, dict) else {}
    result = {}
    for name, prefix in [('native_prompt', 'prompt'), ('native_prediction', 'predicted')]:
        tokens, milliseconds = timing.get(prefix + '_n'), timing.get(prefix + '_ms')
        supplied_rate = timing.get(prefix + '_per_second')
        tokens = tokens if _finite_nonnegative(tokens) else None
        seconds = milliseconds / 1000 if _finite_nonnegative(milliseconds) else None
        rate = tokens / seconds if tokens is not None and seconds else (
            supplied_rate if _finite_nonnegative(supplied_rate) else None)
        result[name] = ({'tokens': tokens, 'seconds': seconds, 'tokens_per_second': rate}
                        if any(value is not None for value in [tokens, seconds, rate]) else None)
        if name == 'native_prediction' and result[name] is not None:
            result[name]['includes_reasoning'] = True
    usage = raw_reply.get('usage') or {}
    completion = usage.get('completion_tokens') if isinstance(usage, dict) else None
    completion = completion if _finite_nonnegative(completion) else None
    duration = request_seconds if _finite_nonnegative(request_seconds) else None
    result['whole_request'] = {'seconds': duration, 'completion_tokens': completion,
        'completion_tokens_per_second': completion / duration if completion is not None and duration else None}
    result.update(ttft_seconds=None, ttft_available=False,
                  timing_scope='nonstreaming; whole-request throughput is not native decode throughput')
    return result
