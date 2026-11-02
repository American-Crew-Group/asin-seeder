#!/usr/bin/env python3
"""Write the API payload for the next pending ASINs to a JSON file without sending it."""

import argparse
import csv
from datetime import date, datetime
import json
from pathlib import Path
import sys

from send_asins import ORDERS, payload, pending_seeds, positive_int, send_order

BASE_DIR = Path(__file__).resolve().parent
REQUIRED = {'asin', 'sent', 'reason', 'priority', 'defer_variations', 'scrape_kind'}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=positive_int, required=True,
                        help='maximum number of unique pending ASINs in the payload (required)')
    parser.add_argument('--csv', type=Path, default=BASE_DIR / 'csv' / 'formatted' / 'asins.csv',
                        help='sender CSV to read (default: csv/formatted/asins.csv); never modified')
    parser.add_argument('--output-dir', type=Path, default=BASE_DIR / 'json_payloads',
                        help='folder for payload files (default: json_payloads)')
    parser.add_argument('--task-batch-id', default=f'batch-{date.today().isoformat()}')
    parser.add_argument('--order', choices=ORDERS, default='priority',
                        help='selection order, as in send_asins.py (default: priority)')
    return parser.parse_args()


def run(args):
    path = args.csv.resolve(strict=True)
    with path.open(encoding='utf-8-sig', newline='') as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)):
            raise ValueError('CSV requires unique headers including asin, sent, reason, priority, defer_variations, scrape_kind')
        missing_fields = REQUIRED - set(fields)
        if missing_fields:
            raise ValueError(f'CSV missing required columns: {", ".join(sorted(missing_fields))}')
        rows = list(reader)
    # Same selection and validation as send_asins.py, so the file matches its next request.
    pending, seeds = pending_seeds(rows)
    selected = send_order(pending, seeds, args.order)[:args.count]
    print(f'Pending: {len(pending)} unique ASINs; selected {len(selected)}', file=sys.stderr)
    if not selected:
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    output = args.output_dir / f'payload-{args.task_batch_id}-{stamp}-{len(selected)}.json'
    # 'x' never overwrites an earlier payload file.
    with output.open('x', encoding='utf-8') as file:
        json.dump(payload([seeds[asin] for asin in selected], args.task_batch_id), file, indent=2)
        file.write('\n')
    print(f'Wrote {len(selected)} ASINs to {output}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(run(arguments()))
    except FileExistsError as error:
        print(f'Error: payload file already exists: {error.filename}; retry in a second', file=sys.stderr)
        sys.exit(2)
    except (csv.Error, OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(2)
