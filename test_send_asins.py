import csv
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from send_asins import arguments, parse_results, pending_seeds, run, send_order

SCRIPT = Path(__file__).with_name('send_asins.py')
HEADER = 'asin,sent,reason,priority,defer_variations,scrape_kind\n'


class CsvSeedTests(unittest.TestCase):
    def dry_run_csv(self, content):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'selected file.csv'
            path.write_text(content)
            result = subprocess.run([sys.executable, str(SCRIPT), '--csv', str(path), '--dry-run'],
                                    capture_output=True, text=True)
            self.assertEqual(path.read_text(), content)
            self.assertFalse(Path(str(path) + '.bak').exists())
            return result

    def test_seed_columns_are_required(self):
        for content, missing in [
            ('asin,sent,reason\nA,false,\n', 'defer_variations, priority, scrape_kind'),
            ('asin,sent,reason,priority,scrape_kind\nA,false,,3,NEW\n', 'defer_variations'),
            ('asin,sent,reason,defer_variations,scrape_kind\nA,false,,false,NEW\n', 'priority'),
            ('asin,sent,reason,priority,defer_variations\nA,false,,3,false\n', 'scrape_kind'),
        ]:
            with self.subTest(missing=missing):
                result = self.dry_run_csv(content)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f'CSV missing required columns: {missing}', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_seed_values_are_required_and_validated_on_every_row(self):
        for sent, reason in [('false', ''), ('true', ''), ('false', 'previous failure')]:
            for priority, defer_variations, scrape_kind, field in [
                ('', 'false', 'NEW', 'priority'), (' ', 'false', 'NEW', 'priority'),
                ('invalid', 'false', 'NEW', 'priority'), ('1.5', 'false', 'NEW', 'priority'),
                ('3', '', 'NEW', 'defer_variations'), ('3', ' ', 'NEW', 'defer_variations'),
                ('3', 'yes', 'NEW', 'defer_variations'), ('3', '1', 'NEW', 'defer_variations'),
                ('3', 'false', '', 'scrape_kind'), ('3', 'false', ' ', 'scrape_kind'),
                ('3', 'false', 'OLD', 'scrape_kind'),
            ]:
                with self.subTest(sent=sent, reason=reason, priority=priority, defer_variations=defer_variations,
                                  scrape_kind=scrape_kind):
                    result = self.dry_run_csv(HEADER + 'A,false,,7,true,NEW\n'
                                             + f'B,{sent},{reason},{priority},{defer_variations},{scrape_kind}\n')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(f'CSV row 3: {field} is required', result.stderr)
                    self.assertEqual(result.stdout, '')
        result = self.dry_run_csv(HEADER + 'A,false,,3,false\n')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('malformed CSV row 2', result.stderr)

    def test_dry_run_reads_typed_values_and_deduplicates_matching_settings(self):
        result = self.dry_run_csv(HEADER + 'A,false,,7,false,NEW\nB,false,,2,true,UPDATE\n'
                                 + 'A,false,, 7 , FALSE , new \nC,true,,5,false,NEW\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        seeds = json.loads(result.stdout)['seeds']
        self.assertEqual(json.loads(result.stdout)['version'], '1.6')
        # Default priority order puts B (priority 2) before A (priority 7).
        self.assertEqual(seeds, [{'asin': 'B', 'priority': 2, 'defer_variations': True, 'scrape_kind': 'UPDATE'},
                                 {'asin': 'A', 'priority': 7, 'defer_variations': False, 'scrape_kind': 'NEW'}])
        self.assertIs(type(seeds[1]['priority']), int)
        self.assertIs(seeds[1]['defer_variations'], False)
        self.assertIs(seeds[0]['defer_variations'], True)

    def test_conflicting_duplicate_settings_are_rejected(self):
        for settings in ('4,false,NEW', '3,true,NEW', '3,false,UPDATE'):
            with self.subTest(settings=settings):
                result = self.dry_run_csv(HEADER + 'A,false,,3,false,NEW\n' + f'A,false,,{settings}\n')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("CSV row 3: conflicting priority, defer_variations or scrape_kind for ASIN 'A'",
                              result.stderr)
                self.assertEqual(result.stdout, '')

    def test_requests_and_saved_progress_preserve_csv_settings(self):
        for single in (False, True):
            with self.subTest(single=single), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'asins.csv'
                original = (HEADER + 'A,false,,3,false,NEW\nB,false,,7,true,UPDATE\nA,false,,3,false,NEW\n'
                            + 'C,false,,2,false,NEW\nD,true,,1,true,UPDATE\nE,false,previous failure,4,false,NEW\n')
                path.write_text(original)
                # These checks follow CSV order; priority ordering is covered by SendOrderTests.
                command = [str(SCRIPT), '--csv', str(path), '--batch-size', '2', '--interval', '0', '--order', 'csv']
                if single:
                    command.append('--no-iterate')
                with patch.object(sys, 'argv', command):
                    args = arguments()
                requests = []

                def respond(request, timeout):
                    body = json.loads(request.data)
                    requests.append(body)
                    items = [{'asin': seed['asin'],
                              'result': {'A': 'duplicate', 'B': 'error', 'C': 'created'}[seed['asin']],
                              'reason': 'invalid ASIN' if seed['asin'] == 'B' else ''}
                             for seed in body['seeds']]
                    return BytesIO(json.dumps({'results': items}).encode())

                with patch.dict(os.environ, {'APIENDPOINT': 'http://example.invalid', 'AUTHENDPOINT': 'test-token'}), \
                     patch('send_asins.build_opener', return_value=SimpleNamespace(open=respond)), \
                     redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(run(args), 1)
                    self.assertEqual(len(requests), 1 if single else 2)
                    args.iterate = True
                    self.assertEqual(run(args), 0)
                    self.assertEqual(run(args), 0)
                self.assertEqual(len(requests), 2)
                self.assertEqual([seed for body in requests for seed in body['seeds']], [
                    {'asin': 'A', 'priority': 3, 'defer_variations': False, 'scrape_kind': 'NEW'},
                    {'asin': 'B', 'priority': 7, 'defer_variations': True, 'scrape_kind': 'UPDATE'},
                    {'asin': 'C', 'priority': 2, 'defer_variations': False, 'scrape_kind': 'NEW'},
                ])
                with path.open(newline='') as file:
                    rows = list(csv.DictReader(file))
                for before, after in zip(csv.DictReader(StringIO(original)), rows):
                    for field in ('asin', 'priority', 'defer_variations', 'scrape_kind'):
                        self.assertEqual(after[field], before[field])
                self.assertEqual(rows[0], rows[2])
                self.assertEqual([row['sent'] for row in rows], ['true', 'false', 'true', 'true', 'true', 'false'])
                self.assertEqual(rows[1]['reason'], 'invalid ASIN')
                self.assertEqual(rows[5]['reason'], 'previous failure')
                self.assertEqual(Path(str(path) + '.bak').read_text(), original)


class SenderTests(unittest.TestCase):
    def test_failure_details_and_status_fallback(self):
        args = SimpleNamespace(results_key='results', asin_key='asin', result_key='result', reason_key='reason')
        for details, expected in [({'reason': 'Rejected'}, 'Rejected'),
                                  ({'error': {'code': 'INVALID'}}, '{"code": "INVALID"}'),
                                  ({'message': 'Invalid ASIN'}, 'Invalid ASIN'),
                                  ({'errors': ['asin must match ^[A-Za-z0-9]+$']}, 'asin must match ^[A-Za-z0-9]+$'),
                                  ({'errors': ['bad format', 'too long']}, 'bad format; too long'),
                                  ({'errors': [{'code': 'INVALID'}]}, '[{"code": "INVALID"}]'),
                                  ({'errors': []}, 'API result: error'),
                                  ({'message': 'Invalid ASIN', 'errors': ['ignored']}, 'Invalid ASIN'),
                                  ({}, 'API result: error')]:
            with self.subTest(details=details):
                body = json.dumps({'results': [{'asin': 'A', 'result': 'error', **details}]})
                outcomes, stop = parse_results(body, ['A'], args)
                self.assertEqual(outcomes, {'A': (False, expected)})
                self.assertFalse(stop)

    def test_success_results(self):
        args = SimpleNamespace(results_key='results', asin_key='asin', result_key='result', reason_key='reason')
        for result in ('created', 'duplicate', 'revived'):
            with self.subTest(result=result):
                body = json.dumps({'results': [{'asin': 'A', 'result': result}]})
                self.assertEqual(parse_results(body, ['A'], args), ({'A': (True, '')}, False))

    def test_batches_partial_failure_duplicates_and_resume(self):
        self.exercise('normal')

    def test_unrecognized_response_records_unknown_and_continues(self):
        self.exercise('invalid')

    def test_missing_result_keeps_confirmed_success(self):
        self.exercise('missing')

    def test_single_batch_saves_results_and_leaves_remaining_pending(self):
        self.exercise('single')

    def exercise(self, mode):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((body, self.headers['Authorization']))
                items = [{'asin': seed['asin'],
                          'result': {'A': 'duplicate', 'B': 'error', 'C': 'created'}[seed['asin']],
                          'reason': 'invalid ASIN' if seed['asin'] == 'B' else ''}
                         for seed in body['seeds']]
                response = {'version': '1.5', 'status': 'TASKS_SEEDED', 'results': items}
                if mode == 'invalid':
                    response = {'accepted': True}
                elif mode == 'missing':
                    response = {'results': items[:1]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            original = (HEADER + 'A,false,,3,false,NEW\nB,false,,7,true,UPDATE\nA,false,,3,false,NEW\n'
                        + 'C,false,,2,false,NEW\nD,true,,1,true,UPDATE\nE,false,previous failure,4,false,NEW\n')
            path.write_text(original)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = {**os.environ, 'APIENDPOINT': f'http://127.0.0.1:{server.server_port}', 'AUTHENDPOINT': 'test-token'}
                # These checks follow CSV order; priority ordering is covered by SendOrderTests.
                command = [sys.executable, str(SCRIPT), '--csv', str(path), '--batch-size', '2', '--interval', '0',
                           '--order', 'csv']
                if mode == 'single':
                    command.append('--no-iterate')
                dry = subprocess.run(command + ['--dry-run'], env=env, capture_output=True, text=True)
                self.assertEqual(dry.returncode, 0, dry.stderr)
                self.assertEqual(path.read_text(), original)
                self.assertEqual(requests, [])
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1 if mode in {'normal', 'single'} else 2, result.stderr)
                with path.open(newline='') as file:
                    rows = list(csv.DictReader(file))
                self.assertEqual(Path(str(path) + '.bak').read_text(), original)
                self.assertEqual(rows[0]['sent'], 'false' if mode == 'invalid' else 'true')
                self.assertEqual(rows[0], rows[2])
                self.assertEqual(rows[1]['sent'], 'false')
                self.assertTrue(rows[1]['reason'])
                if mode == 'normal':
                    self.assertEqual(rows[1]['reason'], 'invalid ASIN')
                # Failed or incomplete batches do not stop the run, so C is still attempted unless single-batch.
                self.assertEqual(rows[3]['sent'], 'true' if mode in {'normal', 'missing'} else 'false')
                if mode == 'invalid':
                    self.assertTrue(rows[3]['reason'].startswith('UNKNOWN:'))
                self.assertEqual(rows[4]['sent'], 'true')
                self.assertEqual(rows[5]['reason'], 'previous failure')
                for before, after in zip(csv.DictReader(StringIO(original)), rows):
                    for field in ('priority', 'defer_variations', 'scrape_kind'):
                        self.assertEqual(after[field], before[field])
                self.assertEqual(len(requests), 1 if mode == 'single' else 2)
                if mode == 'single':
                    self.assertEqual(rows[3], {'asin': 'C', 'sent': 'false', 'reason': '',
                                               'priority': '2', 'defer_variations': 'false', 'scrape_kind': 'NEW'})
                    self.assertEqual([seed['asin'] for seed in requests[0][0]['seeds']], ['A', 'B'])
                for body, auth in requests:
                    self.assertEqual(auth, 'test-token')
                    self.assertEqual(body['version'], '1.6')
                    for seed in body['seeds']:
                        priority, defer_variations, scrape_kind = {'A': (3, False, 'NEW'), 'B': (7, True, 'UPDATE'),
                                                                   'C': (2, False, 'NEW')}[seed['asin']]
                        self.assertEqual(seed['priority'], priority)
                        self.assertIs(seed['defer_variations'], defer_variations)
                        self.assertEqual(seed['scrape_kind'], scrape_kind)
                if mode == 'normal':
                    again = subprocess.run(command, env=env, capture_output=True, text=True)
                    self.assertEqual(again.returncode, 0, again.stderr)
                    self.assertEqual(len(requests), 2)
                elif mode == 'single':
                    # Explicitly re-enable iteration and resume the untouched rows.
                    again = subprocess.run(command[:-1] + ['--iterate'], env=env, capture_output=True, text=True)
                    self.assertEqual(again.returncode, 0, again.stderr)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual([seed['asin'] for seed in requests[1][0]['seeds']], ['C'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


class ContinueAndErrorLogTests(unittest.TestCase):

    def send(self, path, respond, *extra):
        with patch.object(sys, 'argv', [str(SCRIPT), '--csv', str(path), '--batch-size', '2', '--interval', '0', *extra]):
            args = arguments()
        with patch.dict(os.environ, {'APIENDPOINT': 'http://example.invalid', 'AUTHENDPOINT': 'test-token'}), \
             patch('send_asins.build_opener', return_value=SimpleNamespace(open=respond)), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return run(args)

    @staticmethod
    def reject_batches_with(asin):
        def respond(request, timeout):
            seeds = json.loads(request.data)['seeds']
            if any(seed['asin'] == asin for seed in seeds):
                raise HTTPError(request.full_url, 400, 'Bad Request', {},
                                BytesIO(b'{"message": "invalid asin format"}'))
            items = [{'asin': seed['asin'], 'result': 'error' if seed['asin'] == 'E' else 'created',
                      'reason': 'not found' if seed['asin'] == 'E' else ''} for seed in seeds]
            return BytesIO(json.dumps({'results': items}).encode())
        return respond

    @staticmethod
    def read(path):
        with path.open(newline='') as file:
            return list(csv.DictReader(file))

    def test_failed_batch_does_not_stop_and_errors_are_counted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            path.write_text(HEADER + ''.join(f'{asin},false,,1,true,NEW\n' for asin in 'ABCDEF'))
            self.assertEqual(self.send(path, self.reject_batches_with('B')), 2)
            rows = self.read(path)
            reason = 'UNKNOWN: HTTP 400 invalid asin format; verify batch outcome before retrying'
            self.assertEqual([(row['sent'], row['reason']) for row in rows], [
                ('false', reason), ('false', reason), ('true', ''), ('true', ''),
                ('false', 'not found'), ('true', '')])
            errors = self.read(path.with_name('asins.errors.csv'))
            self.assertEqual([(row['error'], row['count'], row['example_asin']) for row in errors],
                             [(reason, '2', 'A'), ('not found', '1', 'E')])

            # A later run adds to the existing counts.
            for row in rows:
                if row['asin'] in 'AE':
                    row['reason'] = ''
            with path.open('w', newline='') as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(self.send(path, self.reject_batches_with('B')), 1)
            errors = self.read(path.with_name('asins.errors.csv'))
            self.assertEqual({row['error']: row['count'] for row in errors}, {reason: '2', 'not found': '2'})
            self.assertEqual(self.read(path)[0]['sent'], 'true')

    def test_consecutive_failed_batches_stop_the_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            path.write_text(HEADER + ''.join(f'{asin},false,,1,true,NEW\n' for asin in 'ABCDEFGH'))
            requests = []

            def unavailable(request, timeout):
                requests.append(request)
                raise HTTPError(request.full_url, 503, 'Unavailable', {}, BytesIO(b'down'))

            errors_path = Path(temporary) / 'errors.csv'
            self.assertEqual(self.send(path, unavailable, '--max-consecutive-failures', '3',
                                       '--errors-csv', str(errors_path)), 2)
            self.assertEqual(len(requests), 3)
            self.assertEqual([row['reason'] == '' for row in self.read(path)], [False] * 6 + [True] * 2)
            self.assertEqual(self.read(errors_path)[0]['count'], '6')


class SendOrderTests(unittest.TestCase):
    # Pending ASINs named after their expected position in priority order.
    ROWS = ('P7,false,,999,false,NEW\n'      # rest: 999 NEW
            'P2,false,,1,false,NEW\n'        # priority 1 NEW, first in CSV
            'P4,false,,2,true,UPDATE\n'      # rest: 2 UPDATE
            'P1,false,,1,true,UPDATE\n'      # priority 1 UPDATE
            'SENT,true,,1,true,UPDATE\n'     # already sent: excluded
            'P6,false,,999,false,UPDATE\n'   # rest: 999 UPDATE
            'P3,false,, 1 ,false, new \n'    # priority 1 NEW, second in CSV; whitespace and case accepted
            'FAILED,false,earlier error,1,true,UPDATE\n'  # carries a reason: excluded
            'P5,false,,10,false,NEW\n'       # 10 sorts numerically after 2, not before it
            'P2,false,,1,false,NEW\n')       # duplicate row: sent once, keeps its first position
    PRIORITY = ['P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7']

    def pending(self):
        return pending_seeds(list(csv.DictReader(StringIO(HEADER + self.ROWS))))

    def test_priority_order_groups_update_before_new_and_keeps_csv_order_within_ties(self):
        pending, seeds = self.pending()
        self.assertEqual(send_order(pending, seeds), self.PRIORITY)
        self.assertEqual(send_order(pending, seeds, 'priority'), self.PRIORITY)

    def test_csv_order_is_available(self):
        pending, seeds = self.pending()
        self.assertEqual(send_order(pending, seeds, 'csv'), ['P7', 'P2', 'P4', 'P1', 'P6', 'P3', 'P5'])

    def test_order_flag_defaults_to_priority_and_rejects_unknown_values(self):
        with patch.object(sys, 'argv', [str(SCRIPT), '--csv', 'asins.csv']):
            self.assertEqual(arguments().order, 'priority')
        with patch.object(sys, 'argv', [str(SCRIPT), '--csv', 'asins.csv', '--order', 'random']), \
             redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            arguments()

    def test_dry_run_shows_first_batch_in_priority_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            path.write_text(HEADER + self.ROWS)
            result = subprocess.run([sys.executable, str(SCRIPT), '--csv', str(path), '--batch-size', '3', '--dry-run'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('order: priority', result.stderr)
            self.assertEqual([seed['asin'] for seed in json.loads(result.stdout)['seeds']], ['P1', 'P2', 'P3'])

    def test_batches_follow_priority_order_and_resume_reorders_new_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'asins.csv'
            path.write_text(HEADER + self.ROWS)
            batches = []

            def respond(request, timeout):
                seeds = json.loads(request.data)['seeds']
                batches.append([seed['asin'] for seed in seeds])
                return BytesIO(json.dumps({'results': [{'asin': seed['asin'], 'result': 'created'}
                                                       for seed in seeds]}).encode())

            def send(*extra):
                with patch.object(sys, 'argv', [str(SCRIPT), '--csv', str(path), '--batch-size', '2',
                                                '--interval', '0', *extra]):
                    args = arguments()
                with patch.dict(os.environ, {'APIENDPOINT': 'http://example.invalid', 'AUTHENDPOINT': 'test-token'}), \
                     patch('send_asins.build_opener', return_value=SimpleNamespace(open=respond)), \
                     redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    return run(args)

            self.assertEqual(send('--no-iterate'), 0)
            self.assertEqual(batches, [['P1', 'P2']])
            # A priority-1 UPDATE merged after the first run goes ahead of everything still pending.
            with path.open('a') as file:
                file.write('LATE,false,,1,false,UPDATE\n')
            self.assertEqual(send(), 0)
            self.assertEqual(batches, [['P1', 'P2'], ['LATE', 'P3'], ['P4', 'P5'], ['P6', 'P7']])
            rows = list(csv.DictReader(path.open(newline='')))
            # The CSV keeps its row order; only sent/reason change.
            self.assertEqual([row['asin'] for row in rows],
                             ['P7', 'P2', 'P4', 'P1', 'SENT', 'P6', 'P3', 'FAILED', 'P5', 'P2', 'LATE'])
            self.assertEqual([row['asin'] for row in rows if row['sent'] != 'true'], ['FAILED'])


if __name__ == '__main__':
    unittest.main()
