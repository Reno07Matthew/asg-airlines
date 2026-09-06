# ASG Airlines: local assessment pipeline

This project profiles an airline workbook, keeps unresolved quality issues visible,
and builds a restricted, pseudonymized reporting snapshot. The notebook is the
learning walkthrough; the CLI calls the same functions for a repeatable local run.
Nothing publishes, uploads, or builds a Power BI dashboard.

## Architecture

The implementation is deliberately a small package using pandas and the Python
standard library, with no new framework or duplicate notebook implementation:

| Module | Responsibility |
| --- | --- |
| `src/ingestion.py` | Read-only Excel ingestion and required sheet/column validation |
| `src/cleaning.py` | Flight classification, booking/payment standardization, reference checks, booking payment aggregation |
| `src/privacy.py` | Passenger agreement rules, explicit HMAC setup/loading, allowlisted reporting views |
| `src/model.py` | Explicit reporting schemas, snapshot dimension keys and fact construction |
| `src/persistence.py` | Generic validation, Decimal/SQL conversion, staged full refresh and full read-back verification |
| `src/pipeline.py` | CLI, safe stage logging, orchestration and run summaries |

Data flows from `load_workbook` through flight checks and standardization, then
booking relationships and payment coverage. Passenger assessment resolves only
agreed analytical attributes; HMAC creates consistent relationship tokens.
`build_dimensions` and `build_model` select the reporting allowlists.
`validate_model` checks business invariants; `publish_model` validates a complete
candidate before publishing it. Functions return frames, never display records.

## Environment and source placement

Run commands from the checkout root. Every explicitly supplied relative CLI path
is resolved against the current working directory, not the package directory.
No home-directory path is hardcoded. Use the existing `.venv` when available.

For a fresh environment (the verified environment uses Python 3.14):

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The pipeline uses the existing pandas, numpy and openpyxl dependencies; SQLite,
Decimal, HMAC, hashing, argument parsing, logging and tests use the standard
library. Notebook dependencies are already pinned in `requirements.txt`.
SQLite must support STRICT tables (3.37 or newer). Do not use Python `-O`:
assertions are part of the validation contract and the CLI rejects optimized runs.

Place an authorized copy of the workbook at
`data/raw/UseCase - Airlines.xlsx`. Keep the source restricted and unchanged;
the pipeline opens it read-only. Required sheets are `flights`, `bookings`,
`payments`, and `passengers`; required columns are listed in
`src/ingestion.py`. Extra source columns stay restricted and do not automatically
enter reporting exports.

## First-time HMAC setup

Normal execution never creates or replaces a key. If this checkout already has
its key, skip initialization. A fresh evaluator must explicitly run:

```sh
.venv/bin/python -m src.pipeline --init-key --key-file .secrets/passenger_hmac.key
```

Initialization requires `.secrets/` to be the last active `.gitignore` rule,
creates 32 random bytes exclusively, and refuses to overwrite an existing file.
The directory is mode 0700 and the key mode 0600. Loading rejects symlinks,
inappropriate ownership/permissions, and invalid key length. Store the key in a
project-local directory named `.secrets`; normal runs reuse it. Never print,
commit, email, or copy someone else's key. Protect a backup under your own secure
access policy: losing/changing the key changes all passenger tokens. Each
evaluator's independent key produces different tokens.

## Run the pipeline

```sh
.venv/bin/python -m src.pipeline \
  --input "data/raw/UseCase - Airlines.xlsx" \
  --output-dir data/processed \
  --key-file .secrets/passenger_hmac.key
```

Exit status 0 means the complete candidate passed validation and publication;
nonzero means failure. Logs show stage names, counts and elapsed times; the run
summary records warning counts, not raw warning text. Neither includes source
rows, tokens or secrets. Missing files,
invalid workbooks, missing sheets/columns and missing keys have safe setup
messages. Other failures identify the stage and exception type without echoing
possibly sensitive values. Inspect the shared function and aggregate diagnostics
locally to investigate; do not paste restricted frames into logs.

Row-level quality issues follow the documented rules: flights with dated-time or
ambiguous-ID problems are quarantined in memory; unknown labels and unusable
amounts remain visible. They are not silently corrected to make a run pass.
Unexpected conflicts that violate required primary/foreign keys, unusable date
dimension bounds, or usable amounts with extra precision stop model publication.
These are reviewable contract failures, not permission to choose a record or
round an amount. A new extract may have different counts.

## Tests and supplied-extract regression

Synthetic tests do not access the source workbook or real HMAC key:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The optional sample regression is separate from all production cleaning rules:

```sh
.venv/bin/python -m tests.check_sample \
  --input "data/raw/UseCase - Airlines.xlsx" --output-dir data/processed
```

It checks the verified input SHA-256 before applying extract-specific counts and
KPIs. A different extract should use the generic pipeline validations instead.
To compare against a locally preserved, restricted baseline from the same key,
add `--baseline-dir /path/to/private/baseline`. This compares every CSV value and
dtype plus SQLite schemas, foreign keys, values and storage types. The run
identifier/timestamps and SQLite file bytes need not be identical. The pipeline
also compares every candidate cell against its in-memory tables on every run.

## Notebook use

Launch Jupyter from the project root:

```sh
.venv/bin/python -m jupyterlab notebooks/01_data_profiling.ipynb
```

Select this checkout's `.venv` interpreter. Run the notebook top-to-bottom; it
supports the project root or `notebooks/` as its working directory. Each stage
calls a named shared helper, retains Markdown explanations, aggregate profiling
and synthetic teaching checks, and leaves the intermediate frames available for
restricted local study. Running the final cells refreshes the same model outputs.
Do not run it at the same time as the CLI.

For headless execution, put the environment first on PATH so the Python kernel
uses this interpreter:

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m nbconvert \
  --to notebook --execute --inplace --ExecutePreprocessor.timeout=120 \
  notebooks/01_data_profiling.ipynb
```

Jupyter needs local kernel sockets. Keep the notebook server local and restricted;
do not expose it to other users. Never preview passenger tokens or personal rows.

## Outputs, refreshes and run evidence

The default restricted `data/processed/` directory contains eight model CSVs,
`asg_airlines.db`, and `run_summary.json`. Existing unrelated files are left alone.
The complete schemas and relationships are in
[docs/reporting_model.md](docs/reporting_model.md).

`run_summary.json` contains a run ID, UTC start/end, input filename/SHA-256,
Python/package versions, source/model row counts, aggregate quality/reference
coverage, reconciled KPIs, stage timings, and the nine model-file names/hashes.
It contains no key, key hash, tokens, or personal records. The summary does not
hash itself. Failed attempts write `runs/<run-id>.json` when the output location
is writable, leaving the last successful manifest/model untouched. A path or
permission failure can prevent a disk report; the nonzero exit and safe error
still signal failure. Successful notebook execution also writes a manifest, with
its end-to-end timestamps and persistence timing rather than per-cell timings.

Full refresh builds candidates in a private temporary directory inside the output
directory. SQLite loading enables foreign keys and uses an explicit transaction;
CSV and SQLite candidates are read back and checked before publication. It never
appends records. Previous target files are backed up until publication finishes;
ordinary replacement errors restore them. Outputs have mode 0600 inside mode
0700 directories.

The nine data-file replacements are **not atomic as a set**. The manifest is
published last. Run only one writer, close consuming applications, and wait for
successful completion before reading. An abrupt kill/power failure during
replacement can leave a mixed snapshot; rerun successfully and verify hashes.
This local workflow has no concurrent-reader snapshot protocol or distributed
lock. Temporary data remaining after a crash is also restricted and must be
handled as sensitive, not uploaded for troubleshooting.

## Power BI handoff and interpretation

Import `FactFlights.csv`, `FactBookings.csv`, `DimAirline.csv`, `DimRoute.csv`,
`DimFlightDate.csv`, `DimBookingDate.csv`, and `DimPassenger.csv` from a single
completed run. `FactPayments.csv` is a transaction audit table for later use,
not part of the initial relationship model. Dimensions filter facts one-to-many,
single-direction; do not directly relate flight and booking facts.

Use Text for natural IDs/tokens, Whole Number for keys/counts, Date/Date-Time for
ISO dates/timestamps, and Fixed Decimal Number for CSV monetary columns (dot
decimal separator). SQLite uses exact integer minor units at scale 100, a numeric
representation rather than a currency declaration. Currency is unspecified.
Known payment amounts are incomplete observed usable amounts, not established
net revenue. Unknown amounts stay null, not zero; absent payment records are not
proof of nonpayment. No payment date is invented.

## Assumptions and privacy limitations

- Timestamps share a common timezone. Full signed dates determine durations;
  negative intervals are never repaired by adding 24 hours.
- Airline/airport/flight-ID maps are dataset-specific working rules, not universal
  registries. Unresolved flight references retain overall booking coverage but
  lack flight-date attribution; route/date filters therefore omit that coverage.
- Passenger age is reported, not verified against an authoritative date. The
  configurable 0–120 plausibility range is not proof of accuracy. Conflicting
  attributes become unknown; an HMAC token identifies a source ID, not a verified
  person, and does not resolve identity conflicts.
- Deterministic dimension keys apply to a full-refresh snapshot. Incremental
  production loading would need persistent key management and additional controls.
- Outputs are **pseudonymized, not anonymous**. Raw/intermediate data, source
  files, notebook sessions, secrets, and model files require restricted access.
  Raw files, processed files, quarantine directories, secrets, environments,
  notebook checkpoints and logs are ignored by Git. Ignoring does not untrack
  an already tracked file or impose access controls. Review any existing tracked
  sensitive files privately; none are deleted or untracked by this pipeline.

This packaging stage adds no passenger corrections, new business rules,
dashboard, publication, commit or push.
