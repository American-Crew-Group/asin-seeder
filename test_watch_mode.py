import csv
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from send_asins import arguments, run

SCRIPT = Path(__file__).with_name('send_asins.py')
HEADER = 'asin,sent,reason,priority,defer_variations,scrape_kind\n'


class Stop(Exception):
    """Ends the endless watch loop from inside a test."""


class WatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.csv = self.root / 'asins.csv'
        self.batches = []
        self.sleeps = []
        self.clock = time.time() - 3600

    def add_raw(self, name, content, age=None):
        path = self.raw / name
        path.write_text(content)
        # Distinct, increasing modification times fix the merge order.
        self.clock += 60
        stamp = time.time() - age if age is not None else self.clock
        os.utime(path, (stamp, stamp))
        return path

    def rows(self):
        with self.csv.open(newline='') as file:
            return {row['asin']: row for row in csv.DictReader(file)}

    @staticmethod
    def created(seeds):
        return BytesIO(json.dumps({'results': [{'asin': seed['asin'], 'result': 'created'}
                                               for seed in seeds]}).encode())

    def watch(self, respond, on_sleep, *extra):
        """Run --watch until on_sleep raises Stop; return (stdout, stderr)."""
        def opener(request, timeout):
            seeds = json.loads(request.data)['seeds']
            self.batches.append([seed['asin'] for seed in seeds])
            return respond(request, seeds)

        def sleep(seconds):
            if seconds:  # Skip the zero --interval pauses between requests.
                self.sleeps.append(seconds)
            on_sleep(seconds)

        argv = [str(SCRIPT), '--csv', str(self.csv), '--raw-dir', str(self.raw), '--watch',
                '--batch-size', '2', '--interval', '0', '--poll-interval', '5', *extra]
        with patch.object(sys, 'argv', argv):
            args = arguments()
        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {'APIENDPOINT': 'http://example.invalid', 'AUTHENDPOINT': 'test-token'}), \
             patch('send_asins.build_opener', return_value=SimpleNamespace(open=opener)), \
             patch('send_asins.time.sleep', sleep), redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Stop):
                run(args)
        return out.getvalue(), err.getvalue()

    @staticmethod
    def stop_when_idle(seconds):
        if seconds == 5:
            raise Stop

    def test_merges_raw_files_between_batches_and_waits_when_idle(self):
        self.csv.write_text(HEADER + ''.join(f'{asin},false,,5,false,NEW\n' for asin in ('Q1', 'Q2', 'Q3', 'Q4')))
        self.add_raw('first.csv', 'asin,priority\nR1,3\n')

        def respond(request, seeds):
            if len(self.batches) == 1:
                # Dropped while the sender is running: merged before the next batch.
                self.add_raw('urgent.csv', 'asin,priority\nU1,1\nQ4,1\n')
            return self.created(seeds)

        out, _ = self.watch(respond, self.stop_when_idle)
        # Priority 1 jumps ahead of the rest; within it, CSV order (Q4 is an older row than U1).
        self.assertEqual(self.batches, [['R1', 'Q1'], ['Q4', 'U1'], ['Q2', 'Q3']])
        rows = self.rows()
        self.assertTrue(all(row['sent'] == 'true' for row in rows.values()))
        self.assertEqual((rows['U1']['priority'], rows['Q4']['priority']), ('1', '1'))
        manifest = json.loads((self.raw / 'processed.json').read_text())
        self.assertEqual(sorted(entry['file'] for entry in manifest.values()), ['first.csv', 'urgent.csv'])
        self.assertIn('urgent.csv: 2 ASINs; 1 added, 1 queued again', out)
        self.assertIn('Idle: nothing pending', out)
        self.assertEqual(Path(str(self.csv) + '.bak').read_text(),
                         HEADER + ''.join(f'{asin},false,,5,false,NEW\n' for asin in ('Q1', 'Q2', 'Q3', 'Q4')))

    def test_idle_watch_picks_up_a_new_file(self):
        self.csv.write_text(HEADER + 'DONE,true,,1,false,UPDATE\n')
        polls = []

        def on_sleep(seconds):
            polls.append(seconds)
            if len(polls) == 1:
                self.add_raw('late.csv', 'asin\nL1\n')
            elif seconds == 5:
                raise Stop

        out, _ = self.watch(lambda request, seeds: self.created(seeds), on_sleep)
        self.assertEqual(self.batches, [['L1']])
        self.assertEqual(self.rows()['L1']['sent'], 'true')
        self.assertEqual(out.count('Idle: nothing pending'), 2)

    def test_whole_batch_failures_keep_rows_pending_and_back_off(self):
        self.csv.write_text(HEADER + 'A,false,,1,false,NEW\nB,false,,1,false,NEW\nC,false,,2,false,NEW\n')
        failures = [HTTPError('http://example.invalid', 503, 'Unavailable', {}, BytesIO(b'No server is available')),
                    URLError('connection refused'), HTTPError('http://example.invalid', 401, 'Unauthorized', {},
                                                              BytesIO(b'{"message": "bad token"}'))]

        def respond(request, seeds):
            if failures:
                raise failures.pop(0)
            return self.created(seeds)

        _, err = self.watch(respond, self.stop_when_idle)
        self.assertEqual(self.batches, [['A', 'B']] * 4 + [['C']])
        self.assertEqual(self.sleeps[:3], [60, 120, 240])
        self.assertTrue(all(row['sent'] == 'true' and row['reason'] == '' for row in self.rows().values()))
        self.assertIn('HTTP 503 No server is available', err)
        self.assertIn('HTTP 401 bad token', err)
        self.assertFalse(self.csv.with_name('asins.errors.csv').exists())

    def test_backoff_is_capped_and_resets_after_success(self):
        self.csv.write_text(HEADER + 'A,false,,1,false,NEW\nB,false,,1,false,NEW\nC,false,,2,false,NEW\n')
        outcomes = ['fail'] * 8 + ['ok', 'fail', 'ok']

        def respond(request, seeds):
            if outcomes.pop(0) == 'fail':
                raise URLError('down')
            return self.created(seeds)

        self.watch(respond, self.stop_when_idle)
        self.assertEqual(self.sleeps[:9], [60, 120, 240, 480, 960, 1920, 3600, 3600, 60])

    def test_per_asin_failures_are_recorded_as_usual(self):
        self.csv.write_text(HEADER + 'GOOD,false,,1,false,NEW\nMK-0383,false,,1,false,NEW\n')

        def respond(request, seeds):
            items = [{'asin': 'GOOD', 'result': 'created'},
                     {'asin': 'MK-0383', 'result': 'invalid', 'errors': ['asin must match ^[A-Za-z0-9]+$']}]
            return BytesIO(json.dumps({'results': items}).encode())

        self.watch(respond, self.stop_when_idle)
        rows = self.rows()
        self.assertEqual(rows['GOOD']['sent'], 'true')
        self.assertEqual((rows['MK-0383']['sent'], rows['MK-0383']['reason']),
                         ('false', 'asin must match ^[A-Za-z0-9]+$'))
        with self.csv.with_name('asins.errors.csv').open(newline='') as file:
            self.assertEqual([row['count'] for row in csv.DictReader(file)], ['1'])

    def test_invalid_raw_file_is_skipped_once_and_others_are_merged(self):
        self.csv.write_text(HEADER)
        self.add_raw('bad.csv', 'asin,priority\nX1,high\n')
        self.add_raw('good.csv', 'asin\nG1\n')
        polls = []

        def on_sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:
                raise Stop

        _, err = self.watch(lambda request, seeds: self.created(seeds), on_sleep)
        self.assertEqual(self.batches, [['G1']])
        self.assertEqual(err.count('Skipped raw file bad.csv: CSV row 2: priority must be an integer'), 1)
        manifest = json.loads((self.raw / 'processed.json').read_text())
        self.assertEqual([entry['file'] for entry in manifest.values()], ['good.csv'])

    def test_recently_modified_raw_file_waits_until_it_settles(self):
        self.csv.write_text(HEADER)
        copying = self.add_raw('copying.csv', 'asin\nC1\n', age=0)
        polls = []

        def on_sleep(seconds):
            polls.append(seconds)
            if len(polls) == 1:
                self.assertEqual(self.batches, [])
                old = time.time() - 60
                os.utime(copying, (old, old))
            elif seconds == 5:
                raise Stop

        self.watch(lambda request, seeds: self.created(seeds), on_sleep)
        self.assertEqual(self.batches, [['C1']])

    def test_interrupt_during_request_keeps_the_batch_pending(self):
        self.csv.write_text(HEADER + 'A,false,,1,false,NEW\n')
        argv = [str(SCRIPT), '--csv', str(self.csv), '--raw-dir', str(self.raw), '--watch', '--interval', '0']

        def interrupted(request, timeout):
            raise KeyboardInterrupt

        with patch.object(sys, 'argv', argv):
            args = arguments()
        with patch.dict(os.environ, {'APIENDPOINT': 'http://example.invalid', 'AUTHENDPOINT': 'test-token'}), \
             patch('send_asins.build_opener', return_value=SimpleNamespace(open=interrupted)), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(run(args), 130)
        self.assertEqual((self.rows()['A']['sent'], self.rows()['A']['reason']), ('false', ''))

    def test_watch_rejects_dry_run_and_single_batch(self):
        for flag in ('--dry-run', '--no-iterate'):
            with self.subTest(flag=flag):
                result = subprocess.run([sys.executable, str(SCRIPT), '--csv', str(self.csv), '--watch', flag],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--watch cannot be combined', result.stderr)

    def test_sigterm_stops_cleanly(self):
        self.csv.write_text(HEADER + 'DONE,true,,1,false,UPDATE\n')
        env = {**os.environ, 'APIENDPOINT': 'http://127.0.0.1:9', 'AUTHENDPOINT': 'test-token',
               'PYTHONUNBUFFERED': '1'}
        process = subprocess.Popen([sys.executable, str(SCRIPT), '--csv', str(self.csv), '--raw-dir', str(self.raw),
                                    '--watch'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn('Watching', process.stdout.readline())
            self.assertIn('Pending: 0', process.stdout.readline())
            self.assertIn('Idle', process.stdout.readline())
            process.send_signal(signal.SIGTERM)
            _, err = process.communicate(timeout=10)
        finally:
            process.kill()
        self.assertEqual(process.returncode, 130, err)
        self.assertIn('Stopped.', err)


if __name__ == '__main__':
    unittest.main()
