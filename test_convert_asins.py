import csv
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('convert_asins.py')
HEADER = 'asin,sent,reason,priority,defer_variations,scrape_kind\n'
# Master CSVs saved before scrape_kind existed; convert_asins.py migrates them.
LEGACY_HEADER = 'asin,sent,reason,priority,defer_variations\n'


class MergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.master = self.root / 'formatted' / 'asins.csv'
        self.manifest = self.raw / 'processed.json'
        self.clock = 1_700_000_000

    def add_raw(self, name, content):
        # Distinct, increasing modification times fix the merge order.
        path = self.raw / name
        path.write_text(content)
        self.clock += 60
        os.utime(path, (self.clock, self.clock))
        return path

    def invoke(self, *flags):
        return subprocess.run([sys.executable, str(SCRIPT), '--raw-dir', str(self.raw),
                               '--master', str(self.master), *flags],
                              capture_output=True, text=True)

    def rows(self):
        with self.master.open(newline='') as file:
            return {row['asin']: row for row in csv.DictReader(file)}

    def test_adds_new_asins_with_defaults_and_records_file(self):
        self.add_raw('export.csv', '"asin","priority","totalSales"\nA1,1,165\n"B2",2,3\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.master.read_text(),
                         HEADER + 'A1,false,,1,false,NEW\nB2,false,,2,false,NEW\n')
        manifest = json.loads(self.manifest.read_text())
        [entry] = manifest.values()
        self.assertEqual(entry['file'], 'export.csv')
        self.assertEqual((entry['asins'], entry['added'], entry['requeued']), (2, 2, 0))

    def test_rerun_without_new_files_leaves_everything_unchanged(self):
        self.add_raw('export.csv', 'asin,priority\nA1,1\n')
        self.assertEqual(self.invoke().returncode, 0)
        before = (self.master.read_text(), self.manifest.read_text())
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No new raw files', result.stdout)
        self.assertEqual((self.master.read_text(), self.manifest.read_text()), before)

    def test_existing_asins_are_queued_again_with_new_priority(self):
        self.master.parent.mkdir()
        self.master.write_text(LEGACY_HEADER + 'A1,true,,1,false\nB2,false,API result: revived,5,true\n'
                               'C3,true,,1,true\n')
        self.add_raw('again.csv', 'asin,priority\nA1,1\nB2,7\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('0 added, 2 queued again (1 with new priority)', result.stdout)
        rows = self.rows()
        self.assertEqual(rows['A1'], {'asin': 'A1', 'sent': 'false', 'reason': '',
                                      'priority': '1', 'defer_variations': 'false', 'scrape_kind': 'UPDATE'})
        self.assertEqual(rows['B2'], {'asin': 'B2', 'sent': 'false', 'reason': '',
                                      'priority': '7', 'defer_variations': 'true', 'scrape_kind': 'NEW'})
        self.assertEqual((rows['C3']['sent'], rows['C3']['scrape_kind']), ('true', 'UPDATE'))
        self.assertEqual(list(rows), ['A1', 'B2', 'C3'])

    def test_single_column_and_missing_priorities_use_default(self):
        self.add_raw('std.csv', '"stdValue"\r\n"006065581X"\r\n')
        self.add_raw('short.csv', 'asin,sent,reason,priority,defer_variation\nB07KZ35Q15\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.rows()
        self.assertEqual(rows['006065581X']['priority'], '999')
        self.assertEqual(rows['B07KZ35Q15']['priority'], '999')

    def test_default_priority_and_defer_variations_flags(self):
        header = 'asin,sent,reason,priority,defer_variations,scrape_kind\n'
        self.add_raw('std.csv', 'asin\nA1\n')
        result = self.invoke('--default-priority', '3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.master.read_text(), header + 'A1,false,,3,false,NEW\n')
        self.add_raw('more.csv', 'asin\nB2\n')
        result = self.invoke('--default-priority', '3', '--defer-variations')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.master.read_text(), header + 'A1,false,,3,false,NEW\nB2,false,,3,true,NEW\n')

    def test_newest_file_and_last_row_set_priority(self):
        self.add_raw('b-older.csv', 'asin,priority\nA1,1\nA1,2\n')
        self.add_raw('a-newer.csv', 'asin,priority\nA1,4\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(result.stdout.index('b-older.csv'), result.stdout.index('a-newer.csv'))
        self.assertEqual(self.rows()['A1']['priority'], '4')

    def test_renamed_copy_is_skipped_but_edited_file_is_merged(self):
        self.add_raw('export.csv', 'asin\nA1\n')
        self.assertEqual(self.invoke().returncode, 0)
        self.add_raw('copy.csv', 'asin\nA1\n')
        self.assertIn('No new raw files', self.invoke().stdout)
        self.add_raw('export.csv', 'asin\nA1\nB2\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('export.csv: 2 ASINs; 1 added, 1 queued again', result.stdout)
        self.assertEqual(len(json.loads(self.manifest.read_text())), 2)

    def test_identical_new_files_are_merged_once(self):
        self.add_raw('one.csv', 'asin\nA1\n')
        self.add_raw('two.csv', 'asin\nA1\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('two.csv', result.stdout)
        self.assertEqual(len(json.loads(self.manifest.read_text())), 1)

    def test_invalid_raw_file_writes_nothing(self):
        self.add_raw('good.csv', 'asin\nA1\n')
        self.assertEqual(self.invoke().returncode, 0)
        before = (self.master.read_text(), self.manifest.read_text())
        self.add_raw('new.csv', 'asin\nB2\n')
        for content, message in [('a,b\n1,2\n', 'needs an asin column'),
                                 ('asin,priority\nC3,high\n', 'priority must be an integer'),
                                 ('asin\n\n" "\n', 'empty ASIN'),
                                 ('asin\nC3,extra\n', 'malformed CSV row 2')]:
            with self.subTest(message=message):
                self.add_raw('bad.csv', content)
                result = self.invoke()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual((self.master.read_text(), self.manifest.read_text()), before)

    def test_invalid_master_is_rejected(self):
        self.master.parent.mkdir()
        for content, message in [('asin,sent,reason\nA1,true,\n', 'must have exactly the columns'),
                                 (LEGACY_HEADER + 'A1,true,,1,true\nA1,false,,2,true\n', 'duplicate ASIN')]:
            with self.subTest(message=message):
                self.master.write_text(content)
                self.add_raw(f'{message}.csv', 'asin\nB2\n')
                result = self.invoke()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.master.read_text(), content)
                self.assertFalse(self.manifest.exists())

    def test_dry_run_reports_without_writing(self):
        self.add_raw('export.csv', 'asin\nA1\n')
        result = self.invoke('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('export.csv: 1 ASINs; 1 added', result.stdout)
        self.assertIn('nothing written', result.stdout)
        self.assertFalse(self.master.exists())
        self.assertFalse(self.manifest.exists())

    def test_refuses_to_run_while_master_lock_is_held(self):
        self.add_raw('export.csv', 'asin\nA1\n')
        self.master.parent.mkdir()
        with open(str(self.master) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('already using the master CSV', result.stderr)
        self.assertFalse(self.master.exists())
        self.assertFalse(self.manifest.exists())


if __name__ == '__main__':
    unittest.main()
