import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('build_payload.py')
HEADER = 'asin,sent,reason,priority,defer_variations,scrape_kind\n'


class PayloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv = self.root / 'asins.csv'
        self.output = self.root / 'json_payloads'

    def invoke(self, *flags):
        return subprocess.run([sys.executable, str(SCRIPT), '--csv', str(self.csv),
                               '--output-dir', str(self.output), *flags],
                              capture_output=True, text=True)

    def payloads(self):
        return sorted(self.output.glob('*.json')) if self.output.exists() else []

    def test_writes_next_pending_asins_without_changing_csv(self):
        content = (HEADER + 'A1,true,,1,true,UPDATE\nB2,false,,5,false,NEW\nC3,false,API error,1,true,NEW\n'
                   'D4,FALSE,,2,true,UPDATE\nB2,false,,5,false,NEW\nE5,false,,3,true,NEW\n')
        self.csv.write_text(content)
        result = self.invoke('--count', '2', '--task-batch-id', 'batch-test')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Pending: 3 unique ASINs; selected 2', result.stderr)
        # Default priority order: D4 (priority 2, UPDATE) then E5 (priority 3), ahead of B2 (priority 5).
        [path] = self.payloads()
        self.assertTrue(path.name.startswith('payload-batch-test-'))
        self.assertTrue(path.name.endswith('-2.json'))
        self.assertEqual(json.loads(path.read_text()), {
            'version': '1.6', 'task_batch_id': 'batch-test',
            'seeds': [{'asin': 'D4', 'priority': 2, 'defer_variations': True, 'scrape_kind': 'UPDATE'},
                      {'asin': 'E5', 'priority': 3, 'defer_variations': True, 'scrape_kind': 'NEW'}]})
        self.assertEqual(self.csv.read_text(), content)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['asins.csv', 'json_payloads'])

    def test_order_matches_sender(self):
        self.csv.write_text(HEADER + 'A1,false,,999,false,NEW\nB2,false,,1,false,NEW\nC3,false,,1,true,UPDATE\n')
        for flags, expected in [((), ['C3', 'B2', 'A1']), (('--order', 'csv'), ['A1', 'B2', 'C3'])]:
            with self.subTest(flags=flags):
                result = self.invoke('--count', '3', '--task-batch-id', f'batch-{len(flags)}', *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                [path] = [p for p in self.payloads() if p.name.startswith(f'payload-batch-{len(flags)}-')]
                self.assertEqual([seed['asin'] for seed in json.loads(path.read_text())['seeds']], expected)

    def test_count_larger_than_pending_selects_all(self):
        self.csv.write_text(HEADER + 'A1,false,,1,true,NEW\n')
        result = self.invoke('--count', '100')
        self.assertEqual(result.returncode, 0, result.stderr)
        [path] = self.payloads()
        self.assertEqual(len(json.loads(path.read_text())['seeds']), 1)

    def test_no_pending_asins_writes_no_file(self):
        self.csv.write_text(HEADER + 'A1,true,,1,true,UPDATE\n')
        result = self.invoke('--count', '5')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payloads(), [])

    def test_count_is_required_and_positive(self):
        self.csv.write_text(HEADER + 'A1,false,,1,true,NEW\n')
        for flags in [(), ('--count', '0'), ('--count', 'x')]:
            with self.subTest(flags=flags):
                result = self.invoke(*flags)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.payloads(), [])

    def test_invalid_csv_writes_no_file(self):
        for content, message in [('asin,sent,reason\nA1,false,\n', 'missing required columns'),
                                 ('asin,sent,reason,priority,defer_variations\nA1,false,,1,true\n',
                                  'missing required columns: scrape_kind'),
                                 (HEADER + 'A1,true,,high,true,NEW\n', 'priority is required'),
                                 (HEADER + 'A1,false,,1,true,OLD\n', 'scrape_kind is required'),
                                 (HEADER + 'A1,false,,1,true,NEW\nA1,false,,2,true,NEW\n', 'conflicting priority')]:
            with self.subTest(message=message):
                self.csv.write_text(content)
                result = self.invoke('--count', '5')
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.payloads(), [])


if __name__ == '__main__':
    unittest.main()
