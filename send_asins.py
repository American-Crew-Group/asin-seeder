#!/usr/bin/env python3
"""Submit pending CSV ASINs and checkpoint explicit per-item results."""

import argparse
import csv
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    # Do not forward the Authorization header to another endpoint.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be zero or greater')
    return number


def nonnegative_float(value):
    number = float(value)
    if not 0 <= number < float('inf'):
        raise argparse.ArgumentTypeError('must be finite and nonnegative')
    return number


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True,
                        help='path to the CSV file to read and update (required)')
    parser.add_argument('--batch-size', type=positive_int, default=100)
    iteration = parser.add_mutually_exclusive_group()
    iteration.add_argument('--iterate', dest='iterate', action='store_true', default=True,
                           help='process all eligible batches (default)')
    iteration.add_argument('--no-iterate', dest='iterate', action='store_false',
                           help='send one batch, save its results, and exit')
    parser.add_argument('--interval', type=nonnegative_float, default=1.0,
                        help='minimum seconds between request starts (default: 1)')
    parser.add_argument('--timeout', type=positive_int, default=60)
    parser.add_argument('--task-batch-id', default=f'batch-{date.today().isoformat()}')
    parser.add_argument('--results-key', default='results',
                        help='dot-separated path to response result array; empty for root array')
    parser.add_argument('--asin-key', default='asin')
    parser.add_argument('--result-key', default='result', help='item field containing created/duplicate/revived or a failure status')
    parser.add_argument('--reason-key', default='reason')
    parser.add_argument('--errors-csv', type=Path,
                        help='CSV that counts each failure reason (default: <csv name>.errors.csv next to --csv)')
    parser.add_argument('--max-consecutive-failures', type=nonnegative_int, default=5,
                        help='stop after this many consecutive failed batches; 0 never stops (default: 5)')
    parser.add_argument('--order', choices=ORDERS, default='priority',
                        help='priority: lowest priority number first, UPDATE before NEW, then CSV order (default); '
                             'csv: CSV order')
    parser.add_argument('--dry-run', action='store_true', help='print first payload without sending or editing CSV')
    return parser.parse_args()


def seed_from_row(row, line):
    try:
        priority = int(row['priority'])
    except ValueError:
        raise ValueError(f'CSV row {line}: priority is required and must be an integer') from None
    defer_variations = row['defer_variations'].strip().lower()
    if defer_variations not in {'true', 'false'}:
        raise ValueError(f'CSV row {line}: defer_variations is required and must be true or false')
    scrape_kind = row['scrape_kind'].strip().upper()
    if scrape_kind not in {'NEW', 'UPDATE'}:
        raise ValueError(f'CSV row {line}: scrape_kind is required and must be NEW or UPDATE')
    return {'asin': row['asin'].strip(), 'priority': priority,
            'defer_variations': defer_variations == 'true', 'scrape_kind': scrape_kind}


def pending_seeds(rows):
    """Map each eligible ASIN to its rows and its seed, validating every row."""
    pending = {}
    seeds = {}
    for line, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'malformed CSV row {line}')
        seed = seed_from_row(row, line)
        if row['sent'].strip().lower() == 'false' and not row['reason'].strip():
            asin = seed['asin']
            if not asin:
                raise ValueError(f'empty ASIN at CSV row {line}')
            if asin in seeds and seeds[asin] != seed:
                raise ValueError(f'CSV row {line}: conflicting priority, defer_variations or scrape_kind for ASIN {asin!r}')
            seeds[asin] = seed
            pending.setdefault(asin, []).append(row)
    return pending, seeds


ORDERS = ('priority', 'csv')


def send_order(pending, seeds, order='priority'):
    """Return unique pending ASINs in sending order.

    'priority' sorts by priority number (1 first), then UPDATE before NEW. The
    sort is stable, so ties keep CSV order. It is recomputed on every run, so a
    resumed run continues with the most important ASINs still pending.
    """
    asins = list(pending)
    if order == 'priority':
        asins.sort(key=lambda asin: (seeds[asin]['priority'], seeds[asin]['scrape_kind'] != 'UPDATE'))
    return asins


def payload(seeds, batch_id):
    return {'version': '1.6', 'task_batch_id': batch_id,
            'seeds': seeds}


def checkpoint(path, fields, rows):
    """Replace only after a complete, durable CSV has been written."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, prefix=f'.{path.name}.', delete=False) as file:
            temporary = Path(file.name)
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


ERROR_FIELDS = ['error', 'count', 'first_seen', 'last_seen', 'example_asin']


def load_errors(path):
    """Read the error summary as {reason: row}; a missing file starts empty."""
    try:
        with path.open(encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != ERROR_FIELDS:
                raise ValueError(f'{path}: expected columns {", ".join(ERROR_FIELDS)}')
            errors = {}
            for line, row in enumerate(reader, start=2):
                try:
                    row['count'] = int(row['count'])
                except (TypeError, ValueError):
                    raise ValueError(f'{path} row {line}: count must be an integer') from None
                errors[row['error']] = row
            return errors
    except FileNotFoundError:
        return {}


def record_errors(errors, outcomes):
    """Count each failed ASIN under its reason; return the reasons seen in this batch."""
    now = datetime.now().isoformat(timespec='seconds')
    batch = {}
    for asin, (success, reason) in outcomes.items():
        if success:
            continue
        entry = errors.setdefault(reason, {'error': reason, 'count': 0, 'first_seen': now,
                                           'last_seen': now, 'example_asin': asin})
        entry['count'] += 1
        entry['last_seen'] = now
        batch[reason] = batch.get(reason, 0) + 1
    return batch


def save_errors(path, errors):
    if not path.exists():
        path.touch()
    rows = sorted(errors.values(), key=lambda row: -row['count'])
    checkpoint(path, ERROR_FIELDS, rows)


def http_error_detail(error):
    """Summarize an HTTP error body so its reason says why the batch was rejected."""
    try:
        body = error.read(4096).decode('utf-8', 'replace')
    except OSError:
        return ''
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        detail = data.get('reason') or data.get('error') or data.get('message') or data.get('detail')
        if detail:
            body = detail if isinstance(detail, str) else json.dumps(detail)
    return ' '.join(body.split())[:300]


def parse_results(body, asins, args):
    results = json.loads(body)
    for key in args.results_key.split('.') if args.results_key else []:
        results = results[key]
    if not isinstance(results, list):
        raise ValueError('expected an array of per-ASIN results')
    indexed = {}
    requested = set(asins)
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get(args.asin_key), str):
            raise ValueError('result missing a string ASIN field')
        asin = item[args.asin_key].strip()
        if asin not in requested or asin in indexed:
            raise ValueError('response contains an unexpected or repeated ASIN')
        result = item.get(args.result_key)
        if not isinstance(result, str) or not result.strip():
            raise ValueError('result missing a nonempty string result field')
        success = result in {'created', 'duplicate', 'revived'}
        reason = item.get(args.reason_key) or item.get('error') or item.get('message') or item.get('errors')
        if not success:
            if isinstance(reason, list) and all(isinstance(error, str) for error in reason):
                reason = '; '.join(reason)
            reason = (reason if isinstance(reason, str) else json.dumps(reason)) if reason else f'API result: {result}'
        indexed[asin] = (success, '' if success else reason)
    missing = requested - indexed.keys()
    for asin in missing:
        indexed[asin] = (False, 'UNKNOWN: ASIN omitted from API response; verify before retrying')
    return indexed, bool(missing)


def run(args):
    path = args.csv.resolve(strict=True)
    # A separate lock remains valid across atomic CSV replacement.
    with open(str(path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another sender is already using this CSV') from None
        with path.open(encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file)
            fields = reader.fieldnames
            if not fields or len(fields) != len(set(fields)):
                raise ValueError('CSV requires unique headers including asin, sent, reason, priority, defer_variations, scrape_kind')
            missing_fields = {'asin', 'sent', 'reason', 'priority', 'defer_variations', 'scrape_kind'} - set(fields)
            if missing_fields:
                raise ValueError(f'CSV missing required columns: {", ".join(sorted(missing_fields))}')
            rows = list(reader)
        pending, seeds = pending_seeds(rows)
        asins = send_order(pending, seeds, args.order)
        print(f'Pending: {len(asins)} unique ASINs across {sum(map(len, pending.values()))} rows; order: {args.order}',
              file=sys.stderr)
        if not asins:
            return 0
        if args.dry_run:
            print(json.dumps(payload([seeds[asin] for asin in asins[:args.batch_size]], args.task_batch_id), indent=2))
            return 0
        endpoint, authorization = os.environ.get('APIENDPOINT'), os.environ.get('AUTHENDPOINT')
        if not endpoint or not authorization:
            raise ValueError('APIENDPOINT and AUTHENDPOINT must be exported environment variables')
        if not endpoint.startswith(('http://', 'https://')):
            raise ValueError('APIENDPOINT must be an HTTP(S) URL')
        errors_path = args.errors_csv or path.with_name(f'{path.stem}.errors.csv')
        errors = load_errors(errors_path)
        backup = Path(str(path) + '.bak')
        try:
            with backup.open('xb') as target, path.open('rb') as source:
                shutil.copyfileobj(source, target)
        except FileExistsError:
            pass
        opener = build_opener(NoRedirect)
        last_start = None
        failures = 0
        unconfirmed = 0
        consecutive = 0
        for offset in range(0, len(asins), args.batch_size):
            batch = asins[offset:offset + args.batch_size]
            if last_start is not None:
                time.sleep(max(0, args.interval - (time.monotonic() - last_start)))
            request = Request(endpoint, data=json.dumps(payload([seeds[asin] for asin in batch], args.task_batch_id)).encode(),
                              headers={'Content-Type': 'application/json', 'Authorization': authorization,
                                       'User-Agent': 'seed-with-priority/1.0'}, method='POST')
            last_start = time.monotonic()
            incomplete = failed = interrupted = False
            try:
                with opener.open(request, timeout=args.timeout) as response:
                    body = response.read()
                outcomes, incomplete = parse_results(body, batch, args)
            except HTTPError as error:
                # A batch-level error does not establish individual outcomes.
                detail = http_error_detail(error)
                reason = f'UNKNOWN: HTTP {error.code}{f" {detail}" if detail else ""}; verify batch outcome before retrying'
                error.close()
                outcomes = {asin: (False, reason) for asin in batch}
                failed = True
            except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
                reason = f'UNKNOWN: request or response could not be confirmed ({type(error).__name__}); verify before retrying'
                outcomes = {asin: (False, reason) for asin in batch}
                failed = True
            except KeyboardInterrupt:
                outcomes = {asin: (False, 'UNKNOWN: interrupted during request; verify before retrying') for asin in batch}
                interrupted = True
            for asin, (success, reason) in outcomes.items():
                for row in pending[asin]:
                    row['sent'] = 'true' if success else 'false'
                    row['reason'] = reason
            checkpoint(path, fields, rows)
            batch_errors = record_errors(errors, outcomes)
            if batch_errors:
                save_errors(errors_path, errors)
            successful = sum(success for success, _ in outcomes.values())
            failures += len(batch) - successful
            print(f'Batch {offset // args.batch_size + 1}: {successful} sent, {len(batch) - successful} failed/unknown; CSV saved', flush=True)
            for reason, count in batch_errors.items():
                print(f'  {count} x {reason}', file=sys.stderr)
            if interrupted:
                print('Stopped: interrupted during a request. Review CSV reasons before continuing.', file=sys.stderr)
                return 2
            unconfirmed += failed or incomplete
            consecutive = consecutive + 1 if failed else 0
            if args.max_consecutive_failures and consecutive >= args.max_consecutive_failures:
                print(f'Stopped: {consecutive} consecutive batches failed. Check the API and {errors_path} before continuing.',
                      file=sys.stderr)
                return 2
            if not args.iterate:
                break
        if failures:
            print(f'{failures} failed/unknown ASINs; error counts saved to {errors_path}', file=sys.stderr)
        return 2 if unconfirmed else 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.exit(run(arguments()))
    except (OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
        sys.exit(130)
