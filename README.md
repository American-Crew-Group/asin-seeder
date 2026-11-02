# asin-seeder

Merge raw ASIN exports into one CSV and submit its pending ASINs to the seeding API, most important first. Python standard library only; no packages to install.

| Script | Purpose |
| --- | --- |
| `convert_asins.py` | Merge new raw exports from `csv/raw/` into `csv/formatted/asins.csv`. |
| `send_asins.py` | Send pending ASINs to the API in batches and save each result to the CSV. |
| `build_payload.py` | Write the next request body to `json_payloads/` without sending it. |
| `deduplicate_asins.py` | Remove duplicate ASIN rows from a CSV. |

```text
raw export ──► csv/raw/ ──convert_asins.py──► csv/formatted/asins.csv ──send_asins.py──► seeding API
                                                 (queue: sent, reason,          │
                                                  priority, scrape_kind)  ◄─────┘ result per ASIN
```

## Workflow

First time? Do the [Setup](#setup) below.

### 1. Load new ASINs

```bash
cp ~/Downloads/new_export.csv csv/raw/    # see Raw export format below
python3 convert_asins.py --dry-run        # shows added / queued again per file; writes nothing
python3 convert_asins.py                  # merges into csv/formatted/asins.csv
```

Under the hood, `convert_asins.py`:

1. Picks the files in `csv/raw/` whose content hash is not yet in `csv/raw/processed.json`, oldest first.
2. Reads each file's `asin` and `priority` (999 when missing).
3. Adds ASINs not in the CSV as pending: `sent=false`, `scrape_kind=NEW`, `defer_variations=false`.
4. Queues ASINs already in the CSV again: `sent=false`, `reason` cleared, the file's priority, and `scrape_kind=UPDATE` if they had been sent before.
5. Saves the CSV atomically, then records the files in `processed.json` so they are never merged twice.

It does not check that values are real ASINs, so review them next.

### 2. Check the data

```bash
python3 send_asins.py --csv csv/formatted/asins.csv --batch-size 50 --dry-run
```

The dry run validates the whole CSV, prints how many ASINs are pending and shows the first batch. Exclude values that are not ASINs (SKUs, barcodes, Excel scientific notation, …) by giving them a `reason`; see [Valid ASIN values](#valid-asin-values).

### 3. Send

```bash
set -a; source .env; set +a             # load credentials into this shell
python3 send_asins.py --csv csv/formatted/asins.csv --batch-size 50 --no-iterate   # one batch to test
python3 send_asins.py --csv csv/formatted/asins.csv --batch-size 50                # everything
```

Under the hood, `send_asins.py`:

1. Locks the CSV and validates every row.
2. Selects pending rows (`sent=false`, empty `reason`), one per unique ASIN, and orders them: priority 1 `UPDATE`, priority 1 `NEW`, then the same for each following priority (see [Sending order](#sending-order)).
3. Copies the CSV to `asins.csv.bak` the first time (never overwritten).
4. For each batch: sends one `POST`, reads the result of every ASIN, marks successes `sent=true` and failures with a `reason`, saves the CSV atomically and adds failures to `asins.errors.csv`.
5. Ends when nothing is pending, on Ctrl+C, or after 5 batches in a row fail as a whole (API down, bad credentials).

Rerun the same command to resume: only rows still pending are sent, most important first. To retry a failed row, fix the cause, clear its `reason` and rerun.

## Setup

**Requirements:** Python 3 (tested with 3.14) on Linux or macOS.

```bash
git clone <repository-url> asin-seeder
cd asin-seeder
cp .env.example .env    # then fill in the values
```

1. **Credentials.** The sender reads two exported environment variables; it does not read `.env` by itself.

   | Variable | Value |
   | --- | --- |
   | `APIENDPOINT` | Full `http(s)://` URL of the seeding endpoint. |
   | `AUTHENDPOINT` | Sent verbatim as the `Authorization` header. Include a `Bearer ` prefix if the API needs one. |

   Either export them from your shell profile, or fill in `.env` and load it into the current shell:

   ```bash
   set -a; source .env; set +a
   ```

   Quote values that contain spaces (`AUTHENDPOINT="Bearer abc..."`). `.env` is git-ignored.

2. **Folders.** Included in the repository (empty). Their contents are data and are git-ignored, so the CSVs, backups and payloads stay local.

   ```text
   csv/raw/                 raw exports to merge, plus processed.json (merged files)
   csv/formatted/asins.csv  the sender CSV, plus its .bak, .lock and .errors.csv companions
   csv/old/                 earlier files kept for reference; not read by any script
   json_payloads/           payload files written by build_payload.py
   ```

3. **Verify.** Run the tests, then a dry run (no credentials needed, nothing sent or saved):

   ```bash
   python3 -m unittest
   python3 send_asins.py --csv csv/formatted/asins.csv --batch-size 50 --dry-run
   ```

Run commands from the project folder. `--csv` paths are relative to the current directory; the defaults of `convert_asins.py` and `build_payload.py` are relative to the scripts' folder.

## Raw export format

Files dropped in `csv/raw/` for `convert_asins.py`:

```csv
asin,priority
B0CW9QHKRT,1
B001F0R6EW,
B07D3KJBVY,2
```

| Column | Values |
| --- | --- |
| `asin` | Required. If the file has a single column with another name (for example `stdValue`), that column is used. |
| `priority` | Optional integer. Empty or missing column means `--default-priority` (999). |

- The first line must be the header; without one the first ASIN is taken as the column name.
- Other columns are ignored when `asin` is present. UTF-8, with or without BOM.
- An empty ASIN, a non-integer priority or a malformed row rejects the whole file; nothing is merged.
- An ASIN repeated in the same file takes the priority of its last row.
- Every ASIN in the file is a new request to scrape it, so an ASIN already in `asins.csv` gets the file's priority (999 if the file has none, even if it had 1 before) and its `reason` is cleared, including reasons set by hand to exclude it.

## CSV format

`csv/formatted/asins.csv` is maintained by the scripts: `convert_asins.py` adds and requeues rows, `send_asins.py` records results. Edit it by hand only to exclude, retry or fix a row (`reason`, or the `asin` value per [Valid ASIN values](#valid-asin-values)), after making a copy and with no script running.

```csv
asin,sent,reason,priority,defer_variations,scrape_kind
B0CW9QHKRT,false,,1,true,UPDATE
B001F0R6EW,false,,1,false,NEW
B07D3KJBVY,true,,2,true,UPDATE
MK-0383,false,INVALID_ASIN_FORMAT,999,false,NEW
```

| Column | Values |
| --- | --- |
| `asin` | The ASIN, sent as written (surrounding whitespace removed). See [Valid ASIN values](#valid-asin-values). |
| `sent` | `true` once the API confirmed the ASIN, otherwise `false`. |
| `reason` | Empty, or why the ASIN failed or was excluded. A nonempty reason keeps the row from being sent. |
| `priority` | Integer; lower is more important (1 before 2 before 999). |
| `defer_variations` | `true` or `false`. |
| `scrape_kind` | `NEW` (never scraped) or `UPDATE` (scrape again). |

- A row is **pending** when `sent=false` and `reason` is empty. In the example, only the first two rows are pending.
- Every column is required on every row, including sent ones; values are case-insensitive and may have surrounding whitespace. Missing or invalid values stop the script before any request or write.
- An ASIN that appears on several pending rows is sent once and must have the same `priority`, `defer_variations` and `scrape_kind` on each; its result is written to all of them. Batch sizes count unique ASINs.
- Saved files are UTF-8; quoting and line endings may be normalized (for example `""` becomes empty).

## Valid ASIN values

The API accepts only `^[A-Za-z0-9]+$` and rejects anything else per ASIN with `result: "invalid"`. Real Amazon ASINs have two shapes:

| Shape | Example |
| --- | --- |
| `B` + 9 uppercase letters or digits | `B0CW9QHKRT` |
| ISBN-10 (books): 10 digits, or 9 digits + `X`, with a valid check digit | `1476745420`, `840917748X` |

`convert_asins.py` does not validate ASINs, so raw exports can bring in values that are not ASINs. Problems found in past exports:

| Problem | Example | Fix |
| --- | --- | --- |
| Barcode turned into scientific notation by Excel | `5.05617E+12` | Digits are lost; recover the value from the original export. |
| ISBN-10 that lost its leading zero | `316532681` | Prepend `0` only if the check digit is then valid (`0316532681`). |
| Lowercase ASIN | `b0cj96vtwt` | Uppercase it, unless the uppercase ASIN is already in the CSV. |
| Prefixed ASIN | `AUB000ORQY0E`, `0EB000UPEF88` | Contains a real ASIN, but the prefix may mean another marketplace; confirm before extracting. |
| SKU, UPC/EAN, other store IDs, junk | `SKU-…`, `098787133325`, `MLM3484024240`, `ASIN` | Not an ASIN; exclude it. |

To exclude a row, set a `reason` such as `INVALID_ASIN_FORMAT` or `NOT_AN_ASIN`; it stays in the CSV for reference and is never sent. Before editing the CSV by hand, copy it (for example `cp asins.csv asins.csv.pre-<change>.bak`) and make sure no sender or merge is running.

## Sending order

Pending ASINs are sent like a priority queue (`--order priority`, the default):

1. Lowest `priority` number first.
2. Within a priority, `UPDATE` before `NEW`.
3. Ties keep CSV order.

So: priority 1 `UPDATE` → priority 1 `NEW` → priority 2 `UPDATE` → priority 2 `NEW` → … → priority 999 `NEW`.

- The order is computed from the pending rows at the start of every run; the CSV's row order never changes. A resumed run picks up the most important ASINs still pending, and newly merged ASINs join their group instead of waiting at the end of the file.
- A batch can contain two groups at a boundary.
- `--order csv` sends in CSV row order instead.
- The order only decides when an ASIN is submitted. Each seed also carries its `priority` and `scrape_kind`, which the receiving system may use to schedule its own work.

## API

### Request

Each batch is a `POST` to `APIENDPOINT` with `Content-Type: application/json` and `Authorization: <AUTHENDPOINT>`:

```json
{
  "version": "1.6",
  "task_batch_id": "batch-2026-11-02",
  "seeds": [
    {"asin": "B0CW9QHKRT", "priority": 1, "defer_variations": true, "scrape_kind": "UPDATE"},
    {"asin": "B001F0R6EW", "priority": 1, "defer_variations": false, "scrape_kind": "NEW"}
  ]
}
```

`task_batch_id` defaults to `batch-<today>` and is the same for every request in a run. Redirects are rejected so the `Authorization` header never reaches another host.

### Response

The API answers per ASIN; an invalid ASIN is rejected on its own and the rest of the batch is still processed:

```json
{
  "version": "1.6",
  "status": "TASKS_SEEDED",
  "results": [
    {"asin": "B0CW9QHKRT", "result": "duplicate", "task_id": "existing-task-id"},
    {"asin": "B001F0R6EW", "result": "created", "task_id": "new-task-id"},
    {"asin": "MK-0383", "result": "invalid", "errors": ["asin must match ^[A-Za-z0-9]+$"]}
  ]
}
```

| Outcome | CSV update |
| --- | --- |
| `created`, `duplicate`, `revived` | `sent=true`, `reason` cleared. |
| Any other `result` (e.g. `invalid`) | `sent=false`, `reason` = first of `reason`, `error`, `message`, `errors` (a list is joined with `; `), else `API result: <result>`. |
| ASIN missing from `results` | `sent=false`, `reason` starts with `UNKNOWN`. |
| HTTP error, network error, timeout, unrecognized body | Whole batch `sent=false`, `reason` starts with `UNKNOWN` and includes the error detail. |

- A 2xx status or `TASKS_SEEDED` alone is not treated as success; only per-ASIN results are.
- Resending an ASIN is safe: the API answers `duplicate` while its task exists, or `created` if the earlier task has already closed.
- If the response uses other field names, use `--results-key` (dot path; `''` for a root array), `--asin-key`, `--result-key` and `--reason-key`.

## Script reference

### `send_asins.py`

```bash
python3 send_asins.py --csv csv/formatted/asins.csv [options]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--csv PATH` | required | CSV to read and update. |
| `--batch-size N` | `100` | Unique ASINs per request. |
| `--iterate` / `--no-iterate` | iterate | Send all pending batches, or just one and exit. |
| `--interval SECONDS` | `1` | Minimum time between request starts. |
| `--timeout SECONDS` | `60` | HTTP timeout per request. |
| `--order priority\|csv` | `priority` | See [Sending order](#sending-order). |
| `--max-consecutive-failures N` | `5` | Stop after N batches in a row fail as a whole (`0` = never). |
| `--task-batch-id ID` | `batch-YYYY-MM-DD` | Batch identifier sent with every request. |
| `--errors-csv PATH` | `<csv name>.errors.csv` | Error summary file. |
| `--results-key`, `--asin-key`, `--result-key`, `--reason-key` | `results`, `asin`, `result`, `reason` | Response field names. |
| `--dry-run` | off | Print the first payload; send and save nothing; no credentials needed. |

- Requests run one at a time. After each batch the full CSV is saved atomically, so a stop loses at most the batch in flight.
- A batch with per-ASIN failures is a normal batch. Only whole-batch failures (HTTP/network errors, unrecognized responses) count toward `--max-consecutive-failures`, which exists to stop marking thousands of rows `UNKNOWN` while the API is down or the credentials are wrong.

| Exit code | Meaning |
| --- | --- |
| `0` | All selected ASINs succeeded, nothing pending, or dry run. |
| `1` | Finished with per-ASIN failures. |
| `2` | Error, stop after consecutive failures, interrupted during a request, or at least one `UNKNOWN` outcome. |
| `130` | Interrupted with Ctrl+C between requests. |

With `--no-iterate`, exit `0` only means that batch succeeded.

### `convert_asins.py`

```bash
python3 convert_asins.py [--dry-run]
```

Reads every `*.csv` directly in `csv/raw/` that has not been merged before. A raw file needs an `asin` column or exactly one column (for example `stdValue`); a `priority` column is used when present.

- New ASINs: added as `sent=false`, `scrape_kind=NEW`, `defer_variations=false`.
- ASINs already in the CSV: queued again (`sent=false`, empty `reason`) with the raw file's priority; `scrape_kind` becomes `UPDATE` if they had been sent; `defer_variations` is kept.
- Files are merged oldest first (by modification time), so the newest file sets the priority.
- Merged files are recorded by content hash in `csv/raw/processed.json`; renamed copies are skipped, edited files count as new. Delete an entry to merge that file again.
- Converts a CSV without `scrape_kind` to the current format (`UPDATE` if sent, else `NEW`).
- Refuses to run while a sender holds the CSV lock.

| Flag | Default | Description |
| --- | --- | --- |
| `--raw-dir PATH` | `csv/raw` | Raw exports and `processed.json`. |
| `--master PATH` | `csv/formatted/asins.csv` | CSV to create or update. |
| `--default-priority N` | `999` | Priority for rows without one. |
| `--defer-variations` / `--no-defer-variations` | no | `defer_variations` for new ASINs. |
| `--dry-run` | off | Report changes without writing. |

Exit codes: `0` success, dry run or nothing new; `2` error; `130` interrupted.

### `build_payload.py`

```bash
python3 build_payload.py --count 50
# Wrote 50 ASINs to json_payloads/payload-batch-2026-11-02-20261102-073317-50.json
```

Writes the body `send_asins.py` would send next: same validation, same order. It never changes the CSV, so ASINs from a payload you send by hand stay pending and will be sent again (the API then answers `duplicate`).

| Flag | Default | Description |
| --- | --- | --- |
| `--count N` | required | Maximum unique ASINs. |
| `--csv PATH` | `csv/formatted/asins.csv` | CSV to read. |
| `--output-dir PATH` | `json_payloads` | Output folder; files are never overwritten. |
| `--order priority\|csv` | `priority` | Selection order. |
| `--task-batch-id ID` | `batch-YYYY-MM-DD` | Batch identifier. |

### `deduplicate_asins.py`

```bash
python3 deduplicate_asins.py --csv csv/formatted/asins.csv [--dry-run]
```

Keeps the first row of each ASIN (exact match after trimming whitespace; case-sensitive) and removes later rows, even if their values differ. Column order and row order are kept. Before changing the file it creates `<csv>.dedupe.bak` if none exists. Uses the sender's lock. Exit codes: `0` success or dry run; `2` error; `130` interrupted.

## Files, recovery and retries

| File | Purpose |
| --- | --- |
| `asins.csv.bak` | Created by the sender before its first real request if missing; never overwritten, so it holds the oldest copy. Make your own dated copy before manual edits. |
| `asins.csv.lock` | Prevents a send, merge or dedupe from overlapping on the same CSV. It may remain after exit; do not delete it while a script runs. |
| `asins.errors.csv` | One row per failure reason: `error,count,first_seen,last_seen,example_asin`. Counts accumulate across runs (a batch of 50 rejected adds 50); delete the file to reset. |

- **Stop:** Ctrl+C. Finished batches are saved; a batch interrupted mid-request is marked `UNKNOWN`.
- **Resume:** run the same command again.
- **Retry a failure:** fix the cause, clear the row's `reason` (keep `sent=false`; for repeated ASINs clear every row), and rerun.
- **`UNKNOWN` rows:** the API may have accepted them. Resending is safe because the API deduplicates (`duplicate`), so you can clear the reason and rerun. A clear rejection such as HTTP 400 means nothing in that batch was accepted.
- Do not edit the CSV with another program while a script is running.

## Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| `APIENDPOINT and AUTHENDPOINT must be exported` | Export them in the same shell (see [Setup](#setup)). |
| HTTP 401 / 403 | Wrong endpoint or authorization value; check any required `Bearer ` prefix. |
| HTTP 503 `No server is available` | The API is down. Do not keep sending: each batch would be marked `UNKNOWN`. Wait, then test with `--no-iterate`. |
| HTTP 429 or other 5xx | Use a longer `--interval` or smaller `--batch-size`; clear the `UNKNOWN` reasons and rerun. |
| `Stopped: N consecutive batches failed` | Check the top of `asins.errors.csv` and the API, fix, clear the `UNKNOWN` reasons, rerun. |
| Many `invalid` results | Values that are not ASINs; see [Valid ASIN values](#valid-asin-values). |
| `CSV missing required columns` or `CSV row N: … is required` | Add the column or fix the value on that row (every row, sent or not). |
| `conflicting priority, defer_variations or scrape_kind` | Make all pending rows of that ASIN match, or run `deduplicate_asins.py`. |
| `another sender is already using this CSV` / merge refuses to run | Another script holds the lock; wait for it to finish. |
| `Pending: 0` | No row has `sent=false` and an empty `reason`. |
| Merge says no new raw files | Every file in `csv/raw/` is already in `processed.json`. |

## Tests

```bash
python3 -m unittest            # all suites
python3 -m unittest -v test_send_asins
```

| Suite | Covers |
| --- | --- |
| `test_send_asins.py` | CSV validation, sending order, response parsing (including `errors`), per-ASIN and whole-batch failures, consecutive-failure stop, error summary, resume, dry run, single batch. |
| `test_convert_asins.py` | Adding and re-queuing ASINs, priorities, `scrape_kind`, old-format migration, manifest, invalid input, lock. |
| `test_build_payload.py` | Selection and order, count limit, unchanged CSV, invalid input. |
| `test_deduplicate_asins.py` | Keeping the first row, backups, dry run, invalid CSVs, lock. |

Tests use temporary files and a mocked or local HTTP server. They never touch `csv/` or `json_payloads/` and never call the real API.
