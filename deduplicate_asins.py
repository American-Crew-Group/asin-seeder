#!/usr/bin/env python3
"""Remove duplicate ASIN rows from an explicitly selected CSV, keeping the first."""

import argparse
import csv
import fcntl
from pathlib import Path
import shutil
import sys

from send_asins import checkpoint


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True,
                        help='path to the CSV file to deduplicate (required)')
    parser.add_argument('--dry-run', action='store_true',
                        help='report duplicate counts without changing the CSV or creating a backup')
    return parser.parse_args()


def run(args):
    path = args.csv.resolve(strict=True)
    # Share the sender's lock, which stays valid across atomic replacement.
    with open(str(path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another process is already using this CSV') from None
        with path.open(encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file, strict=True)
            fields = reader.fieldnames
            if not fields or len(fields) != len(set(fields)) or 'asin' not in fields:
                raise ValueError('CSV requires unique headers including asin')
            seen = set()
            rows = []
            removed = 0
            for line, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f'malformed CSV row {line}')
                asin = row['asin'].strip()
                if not asin:
                    raise ValueError(f'empty ASIN at CSV row {line}')
                if asin in seen:
                    removed += 1
                else:
                    seen.add(asin)
                    rows.append(row)
        if args.dry_run:
            print(f'Would remove {removed} duplicate ASIN rows; keep {len(rows)} of {len(rows) + removed} rows')
            return 0
        if not removed:
            print(f'No duplicates found: {len(rows)} rows; CSV unchanged')
            return 0
        backup = Path(str(path) + '.dedupe.bak')
        try:
            with backup.open('xb') as target, path.open('rb') as source:
                shutil.copyfileobj(source, target)
        except FileExistsError:
            pass
        checkpoint(path, fields, rows)
        print(f'Removed {removed} duplicate ASIN rows; saved {len(rows)} unique rows to {path}')
        return 0


if __name__ == '__main__':
    try:
        sys.exit(run(arguments()))
    except (csv.Error, OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
        sys.exit(130)
