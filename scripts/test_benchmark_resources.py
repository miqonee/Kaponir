import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark_resources import (ProcessSampler, cgroup_snapshot, hardware_snapshot,
                                  read_process, throughput_metrics)


class BenchmarkResourceTests(unittest.TestCase):
    def test_proc_stat_command_spaces_parentheses_and_cpu_field_indices(self):
        # The parenthesized comm is allowed to contain both spaces and ')'.
        fields = ['S'] + ['0'] * 21
        for index, value in {7: '7', 9: '3', 11: '125', 12: '75', 19: '999', 21: '8'}.items():
            fields[index] = value
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '42').mkdir()
            (root / '42/stat').write_text('42 (llama worker ) task) ' + ' '.join(fields))
            (root / '42/io').write_text('read_bytes: 1024\nwrite_bytes: 64\n')
            with patch('benchmark_resources.os.sysconf', side_effect=lambda name: 100 if name == 'SC_CLK_TCK' else 4096):
                result = read_process(42, root)
        self.assertTrue(result['available'])
        self.assertEqual(result['cpu_seconds'], 2)
        self.assertEqual(result['cpu_user_seconds'], 1.25)
        self.assertEqual(result['cpu_system_seconds'], .75)
        self.assertEqual(result['rss_bytes'], 8 * 4096)
        self.assertEqual(result['start_time_ticks'], 999)
        self.assertEqual((result['minor_faults'], result['major_faults']), (7, 3))
        self.assertEqual((result['read_bytes'], result['write_bytes']), (1024, 64))

    def test_hardware_limits_include_stricter_parent_without_identifying_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc, group = root / 'proc', root / 'cgroup'
            (proc / 'self').mkdir(parents=True)
            (proc / 'self/cgroup').write_text('0::/private-container-id/leaf\n')
            (proc / 'cpuinfo').write_text('processor : 0\nmodel name : Test CPU\nphysical id : 0\ncore id : 0\nflags : avx2 fma other\n\nprocessor : 1\nmodel name : Test CPU\nphysical id : 0\ncore id : 0\n')
            (proc / 'meminfo').write_text('MemTotal: 8192 kB\nMemFree: 1111 kB\nSwapTotal: 4096 kB\n')
            leaf = group / 'private-container-id/leaf'
            leaf.mkdir(parents=True)
            (leaf / 'cgroup.controllers').write_text('memory cpu')
            for directory, quota, memory in [(group, '400000 100000', '2097152'),
                                             (leaf.parent, '200000 100000', '1048576'),
                                             (leaf, 'max 100000', 'max')]:
                (directory / 'cpu.max').write_text(quota)
                (directory / 'memory.max').write_text(memory)
            with patch('benchmark_resources.os.sched_getaffinity', return_value={0, 1, 2}):
                result = hardware_snapshot(proc, group)
        self.assertEqual(result['effective_cgroup_cpu_quota_cores'], 2)
        self.assertEqual(result['effective_cgroup_memory_limit_bytes'], 1048576)
        self.assertEqual((result['host_logical_cpus'], result['host_physical_cpu_cores']), (2, 1))
        self.assertEqual(result['host_ram_bytes'], 8192 * 1024)
        self.assertEqual(result['host_swap_bytes'], 4096 * 1024)
        self.assertEqual(result['instruction_sets'], ['avx2', 'fma'])
        serialized = json.dumps(result)
        self.assertNotIn('private-container-id', serialized)
        self.assertNotIn('MemFree', serialized)
        self.assertNotIn('hostname', serialized)

    def test_missing_process_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = read_process(1234, Path(temporary))
        self.assertFalse(result['available'])
        self.assertNotIn('rss_bytes', result)

    def test_sampler_cpu_delta_rss_peak_and_pid_restart_have_separate_identities(self):
        now = [0.0]
        current = {'pid': 10, 'start_time_ticks': 50, 'rss_bytes': 100, 'cpu_seconds': 7.0,
                   'cpu_user_seconds': 5.0, 'cpu_system_seconds': 2.0}
        group = {'memory_current_bytes': 1000, 'memory_lifetime_peak_bytes': 99999,
                 'swap_current_bytes': 0, 'memory_events': {'oom': 0},
                 'cpu_stat': {'usage_usec': 5000, 'nr_throttled': 1}}

        def process(_):
            return {'available': True, 'minor_faults': 0, 'major_faults': 0,
                    'read_bytes': None, 'write_bytes': None, **current}

        sampler = ProcessSampler(lambda: {'model': current['pid'], 'ocr': None}, 1000,
            process_reader=process, cgroup_reader=lambda: dict(group), clock=lambda: now[0]).start()
        now[0] = 2
        current.update(cpu_seconds=10.0, cpu_user_seconds=7.0, cpu_system_seconds=3.0, rss_bytes=500)
        group.update(memory_current_bytes=2000, memory_events={'oom': 1},
                     cpu_stat={'usage_usec': 12000, 'nr_throttled': 2})
        sampler.sample()
        # Reuse the same PID for a different process. Never subtract its new
        # counters from the old instance's larger accumulated values.
        now[0] = 3
        current.update(start_time_ticks=80, cpu_seconds=1.0, cpu_user_seconds=.5,
                       cpu_system_seconds=.5, rss_bytes=200)
        sampler.sample()
        now[0] = 4
        current.update(cpu_seconds=2.0, cpu_user_seconds=1.5, cpu_system_seconds=.5, rss_bytes=300)
        group['memory_current_bytes'] = 1500
        result = sampler.stop()
        self.assertIs(sampler.stop(), result)
        model = result['roles']['model']
        self.assertEqual(model['observed_peak_rss_bytes'], 500)
        self.assertEqual(model['cpu_seconds'], 4)
        self.assertEqual(model['cpu_user_seconds'], 3)
        self.assertEqual(model['cpu_system_seconds'], 1)
        self.assertEqual(model['average_cpu_cores'], 1)
        self.assertEqual(model['average_cpu_percent_one_core'], 100)
        self.assertEqual(len(model['processes']), 2)
        self.assertIsNone(result['roles']['ocr']['observed_peak_rss_bytes'])
        self.assertIsNone(result['roles']['ocr']['cpu_seconds'])
        self.assertEqual(result['cgroup']['observed_peak_memory_current_bytes'], 2000)
        self.assertEqual(result['cgroup']['end']['memory_lifetime_peak_bytes'], 99999)
        self.assertEqual(result['cgroup']['counter_deltas']['memory_events'], {'oom': 1})
        self.assertEqual(result['cgroup']['counter_deltas']['cpu_stat']['nr_throttled'], 1)
        self.assertNotIn('memory_lifetime_peak_bytes', result['cgroup']['counter_deltas'])

    def test_sampler_provider_and_process_failures_do_not_replace_model_outcome(self):
        reader = lambda _: (_ for _ in ()).throw(PermissionError())
        group = lambda: (_ for _ in ()).throw(FileNotFoundError())
        with self.assertRaisesRegex(ValueError, 'model_failure'):
            with ProcessSampler(lambda: {'model': 42}, 1000, process_reader=reader,
                                cgroup_reader=group) as sampler:
                raise ValueError('model_failure')
        result = sampler.stop()
        self.assertIsNone(result['roles']['model']['observed_peak_rss_bytes'])
        self.assertIn('model:process_reader_unavailable', result['sample_errors'])
        self.assertIn('cgroup_reader_unavailable', result['sample_errors'])
        bad = ProcessSampler(lambda: (_ for _ in ()).throw(OSError()), 1000).start()
        self.assertIn('pid_provider_unavailable', bad.stop()['sample_errors'])

    def test_shared_pid_is_not_double_counted_in_total_observed_rss(self):
        value = {'available': True, 'pid': 1, 'start_time_ticks': 5,
                 'cpu_seconds': 2, 'rss_bytes': 100}
        sampler = ProcessSampler(lambda: {'worker': 1, 'model': 1}, 1000,
            process_reader=lambda _: value.copy(), cgroup_reader=lambda: {}).start()
        result = sampler.stop()
        self.assertEqual(result['observed_peak_total_rss_bytes'], 100)
        self.assertEqual(result['roles']['model']['observed_peak_rss_bytes'], 100)

    def test_cgroup_current_and_lifetime_peak_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / 'group'
            group.mkdir()
            (group / 'memory.current').write_text('400')
            (group / 'memory.peak').write_text('9999')
            (group / 'memory.events').write_text('oom 0\noom_kill 0\n')
            result = cgroup_snapshot(root / 'missing-proc', group)
        self.assertEqual(result['memory_current_bytes'], 400)
        self.assertEqual(result['memory_lifetime_peak_bytes'], 9999)
        self.assertIsNone(result['swap_current_bytes'])

    def test_native_token_rates_are_separate_from_whole_request_throughput(self):
        reply = {'timings': {'prompt_n': 1000, 'prompt_ms': 500,
                              'predicted_n': 200, 'predicted_ms': 2000},
                 'usage': {'completion_tokens': 200}}
        result = throughput_metrics(reply, 4)
        self.assertEqual(result['native_prompt'], {'tokens': 1000, 'seconds': .5, 'tokens_per_second': 2000})
        self.assertEqual(result['native_prediction']['tokens_per_second'], 100)
        self.assertTrue(result['native_prediction']['includes_reasoning'])
        self.assertEqual(result['whole_request']['completion_tokens_per_second'], 50)
        self.assertFalse(result['ttft_available'])
        self.assertIsNone(result['ttft_seconds'])
        fallback = throughput_metrics({'usage': {'completion_tokens': 50}}, 5)
        self.assertIsNone(fallback['native_prediction'])
        self.assertEqual(fallback['whole_request']['completion_tokens_per_second'], 10)

    def test_invalid_timing_values_do_not_fabricate_rates(self):
        for reply in [None, {'timings': {'predicted_n': -1, 'predicted_ms': 0}},
                      {'timings': {'prompt_n': True, 'prompt_ms': float('nan')}},
                      {'usage': {'completion_tokens': '100'}}]:
            result = throughput_metrics(reply, 0)
            self.assertIsNone(result['whole_request']['completion_tokens_per_second'])
            self.assertFalse(result['ttft_available'])


if __name__ == '__main__':
    unittest.main()
