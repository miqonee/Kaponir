import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from evaluate_s1 import load, released, run, wilson_upper


def row(id, label, probability, group=None):
    return dict(id=id, group=group or id, label=label, probability=probability,
                local_probability=.5, category='infra')


class EvaluationTests(unittest.TestCase):
    def test_actual_keep_gate(self):
        self.assertFalse(released(row('a', True, .2), 1, 0, .1))
        self.assertTrue(released(row('a', False, .001), 1, 0, .1))
        r = row('a', False, .09)
        r['local_probability'] = .69
        self.assertFalse(released(r, 1, 0, .1, weight=.1))

    def test_independent_split_and_no_automatic_approval(self):
        training = [row('a', True, .9), row('b', False, .01)]
        evaluation = [row('c', True, .001), row('d', False, .9)]
        result = run(training, evaluation)
        self.assertFalse(result['automatically_approved'])
        self.assertEqual(result['evaluation_set']['private_released'], 1)
        self.assertGreater(wilson_upper(0, 20), 0)
        with self.assertRaises(ValueError):
            run(training, [row('c', True, .9, group='a'), row('d', False, .1)])
        with self.assertRaises(ValueError):
            run(training, [row('a', True, .9, group='c'), row('d', False, .1)])

    def test_cli_records_provenance_and_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name, rows in [('calibration', [row('a', True, .9), row('b', False, .01)]),
                               ('evaluation', [row('c', True, .8), row('d', False, .02)])]:
                (tmp / (name + '.jsonl')).write_text('\n'.join(map(json.dumps, rows)))
            report = tmp / 'report.json'
            command = [sys.executable, str(Path(__file__).with_name('evaluate_s1.py')),
                       '--calibration', str(tmp / 'calibration.jsonl'),
                       '--evaluation', str(tmp / 'evaluation.jsonl'), '--output', str(report)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = report.read_bytes()
            data = json.loads(original)
            self.assertEqual(len(data['input_sha256']['evaluation']), 64)
            self.assertFalse(data['automatically_approved'])
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(report.read_bytes(), original)

    def test_bad_inputs_fail_closed(self):
        invalid = [row('a', True, float('nan')), row('a', True, 2),
                   row('a', 'private', .5), dict(row('a', True, .5), local_probability=.9)]
        for bad in invalid:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'data.jsonl'
                path.write_text(json.dumps(bad)+'\n'+json.dumps(row('b', False, .1)))
                with self.assertRaises(ValueError):
                    load(path)


if __name__ == '__main__':
    unittest.main()
