#!/usr/bin/env python3
"""Merge new raw ASIN exports into the single sender CSV.

Every ASIN in a new raw file is a request to scrape it again: new ASINs are
added with scrape_kind=NEW, and existing ones get the file's priority, are
queued again (sent=false, empty reason) and become scrape_kind=UPDATE. A NEW
ASIN that was never sent stays NEW. Processed raw files are recorded in a
manifest by content hash, so later runs only merge files that were not merged
before.
"""

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys

from send_asins import checkpoint

BASE_DIR = Path(__file__).resolve().parent
FIELDS = ['asin', 'sent', 'reason', 'priority', 'defer_variations', 'scrape_kind']
LEGACY_FIELDS = FIELDS[:-1]
MANIFEST_NAME = 'processed.json'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--raw-dir', type=Path, default=BASE_DIR / 'csv' / 'raw',
                        help='folder with raw CSV exports (default: csv/raw)')
    parser.add_argument('--master', type=Path, default=BASE_DIR / 'csv' / 'formatted' / 'asins.csv',
                        help='sender CSV to create or update (default: csv/formatted/asins.csv)')
    parser.add_argument('--default-priority', type=int, default=999,
                        help='priority for rows without one (default: 999)')
    defer = parser.add_mutually_exclusive_group()
    defer.add_argument('--defer-variations', dest='defer_variations', action='store_true', default=False,
                       help='add new ASINs with defer_variations=true')
    defer.add_argument('--no-defer-variations', dest='defer_variations', action='store_false',
                       help='add new ASINs with defer_variations=false (default)')
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would change without writing the CSV or the manifest')
    return parser.parse_args()


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_raw(path, default_priority):
    """Return {asin: priority}; a later row for the same ASIN wins."""
    with path.open(encoding='utf-8-sig', newline='') as file:
        # Missing trailing fields read as empty; extra fields are an error.
        reader = csv.DictReader(file, restval='', strict=True)
        fields = reader.fieldnames or []
        if 'asin' in fields:
            column = 'asin'
        elif len(fields) == 1:
            column = fields[0]
        else:
            raise ValueError(f'{path.name}: needs an asin column or a single column')
        asins = {}
        for line, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f'{path.name}: malformed CSV row {line}')
            asin = row[column].strip()
            if not asin:
                raise ValueError(f'{path.name}: empty ASIN at CSV row {line}')
            priority = (row.get('priority') or '').strip()
            try:
                asins[asin] = int(priority) if priority else default_priority
            except ValueError:
                raise ValueError(f'{path.name}: CSV row {line}: priority must be an integer') from None
        return asins


def is_legacy(path):
    """True when the master CSV predates the scrape_kind column."""
    if not path.exists():
        return False
    with path.open(encoding='utf-8-sig', newline='') as file:
        return next(csv.reader(file), None) == LEGACY_FIELDS


def read_master(path):
    if not path.exists():
        return {}
    with path.open(encoding='utf-8-sig', newline='') as file:
        reader = csv.DictReader(file, strict=True)
        legacy = reader.fieldnames == LEGACY_FIELDS
        if reader.fieldnames != FIELDS and not legacy:
            raise ValueError(f'{path} must have exactly the columns {",".join(FIELDS)}')
        rows = {}
        for line, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'{path}: malformed CSV row {line}')
            if legacy:
                # Rows saved before scrape_kind existed: sent ones were scraped
                # already, pending ones were added as new.
                row['scrape_kind'] = 'UPDATE' if row['sent'].strip().lower() == 'true' else 'NEW'
            elif row['scrape_kind'] not in {'NEW', 'UPDATE'}:
                raise ValueError(f'{path}: CSV row {line}: scrape_kind must be NEW or UPDATE')
            if row['asin'] in rows:
                raise ValueError(f'{path}: duplicate ASIN {row["asin"]!r} at CSV row {line}')
            rows[row['asin']] = row
        return rows


def save_manifest(path, manifest):
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    os.replace(temporary, path)


def run(args):
    raw_dir = args.raw_dir.resolve(strict=True)
    master = args.master.resolve()
    manifest_path = raw_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    # Oldest first, so the newest request sets an ASIN's priority.
    pending = {}
    for path in sorted(raw_dir.glob('*.csv'), key=lambda p: (p.stat().st_mtime, p.name)):
        digest = sha256(path)
        if digest not in manifest:
            pending.setdefault(digest, path)
    if not pending and not is_legacy(master):
        print(f'No new raw files in {raw_dir}; {master.name} unchanged')
        return 0
    master.parent.mkdir(parents=True, exist_ok=True)
    # Share the sender's lock so a merge never overlaps a send of the same CSV.
    with open(str(master) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another process is already using the master CSV') from None
        rows = read_master(master)
        defer_variations = 'true' if args.defer_variations else 'false'
        entries = {}
        for digest, path in pending.items():
            asins = read_raw(path, args.default_priority)
            added = changed = 0
            for asin, priority in asins.items():
                row = rows.get(asin)
                if row is None:
                    rows[asin] = {'asin': asin, 'sent': 'false', 'reason': '',
                                  'priority': str(priority), 'defer_variations': defer_variations,
                                  'scrape_kind': 'NEW'}
                    added += 1
                    continue
                if row['priority'].strip() != str(priority):
                    row['priority'] = str(priority)
                    changed += 1
                # A NEW ASIN that was never sent has nothing to update yet.
                if row['sent'].strip().lower() == 'true':
                    row['scrape_kind'] = 'UPDATE'
                row['sent'] = 'false'
                row['reason'] = ''
            requeued = len(asins) - added
            entries[digest] = {'file': path.name, 'asins': len(asins), 'added': added,
                               'requeued': requeued, 'priority_changed': changed,
                               'processed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            print(f'{path.name}: {len(asins)} ASINs; {added} added, {requeued} queued again '
                  f'({changed} with new priority)')
        if args.dry_run:
            print(f'Dry run: would save {len(rows)} rows to {master}; nothing written')
            return 0
        if not master.exists():
            master.touch()
        checkpoint(master, FIELDS, rows.values())
        # Recorded only after the CSV is saved, so a failed merge is retried.
        save_manifest(manifest_path, {**manifest, **entries})
        print(f'Saved {len(rows)} rows to {master}; recorded {len(entries)} files in {manifest_path.name}')
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
