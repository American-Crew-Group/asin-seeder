import csv
import fcntl
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('deduplicate_asins.py')


class DeduplicationTests(unittest.TestCase):
    def invoke(self, path, *flags):
        return subprocess.run([sys.executable, str(SCRIPT), '--csv', str(path), *flags],
                              capture_output=True, text=True)

    def test_csv_argument_is_required_even_when_former_default_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ASINS-to-update 2026-09-09.csv'
            content = 'asin,sent,reason\nA,false,\nA,false,\n'
            path.write_text(content)
            result = subprocess.run([sys.executable, str(SCRIPT)], cwd=temporary,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('the following arguments are required: --csv', result.stderr)
            self.assertEqual(path.read_text(), content)
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_keeps_first_asin_row_preserves_columns_and_creates_backup(self):
        content = ('\ufeffasin,sent,reason,priority,defer_variations,note\n'
                   ' A ,false,,7,false,"first, note"\n'
                   'B,true,previous failure,2,true,"multiple\nlines"\n'
                   'A,true,duplicate details,1,true,later\n'
                   ' B ,false,,9,false,dropped\n')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins with spaces.csv'
            path.write_text(content)
            original = path.read_bytes()
            result = self.invoke(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Removed 2 duplicate ASIN rows; saved 2 unique rows', result.stdout)
            with path.open(encoding='utf-8', newline='') as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows, list(csv.DictReader(StringIO(content.lstrip('\ufeff'))))[:2])
            self.assertEqual(Path(str(path) + '.dedupe.bak').read_bytes(), original)
            saved = path.read_bytes()
            again = self.invoke(path)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn('No duplicates found', again.stdout)
            self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(Path(str(path) + '.dedupe.bak').read_bytes(), original)

    def test_dry_run_reports_counts_without_editing_or_backing_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            original = b'asin,sent,reason\r\nA,false,\r\n A ,true,old reason\r\nB,false,\r\n'
            path.write_bytes(original)
            result = self.invoke(path, '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Would remove 1 duplicate ASIN rows; keep 2 of 3 rows', result.stdout)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(Path(str(path) + '.dedupe.bak').exists())

    def test_no_duplicates_leaves_file_bytes_unchanged_without_backup(self):
        for original in (b'asin,note\r\nA,"quoted"\r\na,lowercase\r\n', b'asin,note\r\n'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'asins.csv'
                path.write_bytes(original)
                result = self.invoke(path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('No duplicates found', result.stdout)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(Path(str(path) + '.dedupe.bak').exists())

    def test_invalid_input_errors_before_editing_or_creating_backup(self):
        for content, message in [
            ('', 'CSV requires unique headers including asin'),
            ('sent,reason\nfalse,\n', 'CSV requires unique headers including asin'),
            ('asin,asin\nA,A\n', 'CSV requires unique headers including asin'),
            ('asin,note\nA,first\nA,duplicate\nB\n', 'malformed CSV row 4'),
            ('asin\nA\nA\nB,extra\n', 'malformed CSV row 4'),
            ('asin,note\nA,first\nA,duplicate\n ,blank\n', 'empty ASIN at CSV row 4'),
            ('asin,note\nA,first\nA,"unterminated\n', 'unexpected end of data'),
        ]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'asins.csv'
                path.write_text(content)
                original = path.read_bytes()
                result = self.invoke(path)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(Path(str(path) + '.dedupe.bak').exists())

    def test_existing_backup_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            path.write_text('asin\nA\nA\n')
            backup = Path(str(path) + '.dedupe.bak')
            backup.write_text('existing backup')
            result = self.invoke(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(backup.read_text(), 'existing backup')
            with path.open(newline='') as file:
                self.assertEqual(list(csv.DictReader(file)), [{'asin': 'A'}])

    def test_respects_the_sender_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            original = 'asin\nA\nA\n'
            path.write_text(original)
            with open(str(path) + '.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.invoke(path)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('another process is already using this CSV', result.stderr)
            self.assertEqual(path.read_text(), original)
            self.assertFalse(Path(str(path) + '.dedupe.bak').exists())


if __name__ == '__main__':
    unittest.main()
