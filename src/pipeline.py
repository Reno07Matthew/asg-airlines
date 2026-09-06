"""Single-command orchestration; logs and summaries contain aggregates only."""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import logging
import os
from pathlib import Path
import platform
import sqlite3
import time
import uuid
import warnings

import pandas as pd

from . import cleaning, privacy
from .ingestion import InputError, REQUIRED_COLUMNS, load_workbook
from .model import build_dimensions, build_model
from .persistence import publish_model, sha256_file, validate_model, write_json


def new_run_summary(input_file):
    return {
        "run_id": uuid.uuid4().hex,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "ended_utc": None,
        "input": {"filename": Path(input_file).name, "sha256": None},
        "versions": {
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            **{name: version(name) for name in ("pandas", "numpy", "openpyxl")},
        },
        "input_rows": {}, "output_rows": {}, "quality_counts": {},
        "kpi_reconciliation": {}, "stages": [], "outputs": [],
        "status": "running", "error": None,
    }


def quality_summary(raw_tables, flight_checks, bookings, payments, passengers, passenger_dimension):
    """Count flags and relationships; never include identifier values or records."""
    def flag_counts(frame):
        return {column: int(frame[column].sum())
                for column in frame.select_dtypes(include=["bool", "boolean"]).columns}

    return {
        "missing_cells_by_table": {
            name: int(raw_tables[name].isna().sum().sum()) for name in REQUIRED_COLUMNS
        },
        "raw_exact_duplicate_excess": {
            name: int(raw_tables[name].duplicated().sum()) for name in REQUIRED_COLUMNS
        },
        "quarantined_flights": int(flight_checks[list(cleaning.issue_labels)].any(axis=1).sum()),
        "flags": {
            "flights": flag_counts(flight_checks), "bookings": flag_counts(bookings),
            "payments": flag_counts(payments), "passengers": flag_counts(passengers),
        },
        "passenger_record_conflict_groups": int(passenger_dimension["record_conflict"].sum()),
        "passenger_repeated_id_groups": int(passenger_dimension["source_record_count"].gt(1).sum()),
        "passenger_excess_id_records": int((passenger_dimension["source_record_count"] - 1).sum()),
        "flight_reference_coverage": {
            label: int(bookings["flight_reference_status"].eq(label).sum())
            for label in ("missing", "accepted", "ambiguous", "quarantined", "unmatched")
        },
        "passenger_reference_coverage": {
            label: int(bookings["passenger_reference_status"].eq(label).sum())
            for label in ("missing", "unmatched", "existing-unique", "existing-nonunique")
        },
        "payment_booking_reference_coverage": {
            label: int(payments["booking_reference_status"].eq(label).sum())
            for label in ("missing", "unmatched", "uniquely_matched")
        },
    }


def run_pipeline(input_file, output_directory, key_file):
    # Normalize dot segments without silently following output/key symlinks.
    input_file = Path(os.path.abspath(input_file))
    output_directory = Path(os.path.abspath(output_directory))
    key_file = Path(os.path.abspath(key_file))
    summary = new_run_summary(input_file)
    current_stage = "paths"

    def stage(name, operation):
        nonlocal current_stage
        current_stage = name
        started = time.perf_counter()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = operation()
        seconds = round(time.perf_counter() - started, 4)
        record = {"name": name, "seconds": seconds, "warning_count": len(caught)}
        if isinstance(result, pd.DataFrame):
            record["rows"] = len(result)
        if name != "persistence":  # The publisher records this before writing its manifest.
            summary["stages"].append(record)
        logging.info("[%s] complete in %.3fs%s", name, seconds,
                     " rows=" + str(record["rows"]) if "rows" in record else "")
        return result

    try:
        if not __debug__:
            raise InputError("Run without Python -O; validation checks must remain enabled.")
        if output_directory == Path.cwd() or output_directory == Path(output_directory.anchor):
            raise InputError("Choose a dedicated --output-dir, not the project or filesystem root.")
        if output_directory == key_file.parent or output_directory.is_relative_to(key_file.parent):
            raise InputError("The output directory must be separate from secret storage.")
        if input_file.resolve() == key_file.resolve() or ".secrets" in input_file.resolve().parts:
            raise InputError("Input must be a source workbook, not a file in secret storage.")
        if input_file.suffix.lower() != ".xlsx":
            raise InputError("Input must be an .xlsx workbook; check --input.")
        if input_file.is_file():
            summary["input"]["sha256"] = sha256_file(input_file)
        key = stage("key", lambda: privacy.load_passenger_hmac_key(key_file))
        raw = stage("ingestion", lambda: load_workbook(input_file))
        summary["input_rows"] = {name: len(raw[name]) for name in REQUIRED_COLUMNS}
        logging.info("[ingestion] rows=%s", summary["input_rows"])

        removed, unique = cleaning.split_exact_duplicates(raw["flights"])
        checks = stage("flight_checks", lambda: cleaning.check_flights(unique))
        quarantined, accepted = cleaning.partition_flights(checks)
        flights = stage("flight_standardization", lambda: cleaning.standardize_flights(accepted))
        bookings, _ = stage("booking_standardization", lambda: cleaning.standardize_bookings(raw["bookings"]))
        bookings, _, _, _, _ = stage(
            "flight_references", lambda: cleaning.attach_flight_references(bookings, checks, flights)
        )
        bookings = stage("passenger_references", lambda: cleaning.attach_passenger_references(
            bookings, raw["passengers"]["passenger_id"]
        ))
        payments, _ = stage("payment_standardization", lambda: cleaning.standardize_payments(raw["payments"]))
        payments = stage("booking_references", lambda: cleaning.attach_booking_references(payments, bookings))
        booking_payments, _, _ = stage("payment_coverage", lambda: cleaning.summarize_booking_payments(payments, bookings))
        passengers, dimension, _ = stage("passenger_quality", lambda: privacy.assess_passengers(raw["passengers"]))
        passengers_reporting, bookings_reporting = stage(
            "pseudonymization", lambda: privacy.build_reporting_views(dimension, bookings, key)
        )
        dimensions = stage("dimensions", lambda: build_dimensions(flights, bookings_reporting, passengers_reporting))
        model = stage("facts", lambda: build_model(flights, bookings_reporting, payments, booking_payments, dimensions))
        summary["quality_counts"] = quality_summary(raw, checks, bookings, payments, passengers, dimension)
        summary["quality_counts"]["removed_flight_copies"] = len(removed)
        summary["quality_counts"]["booking_payment_coverage"] = {
            label: int(booking_payments["amount_coverage"].eq(label).sum())
            for label in ("no payment record in this extract", "payment records but no usable amounts",
                          "partial amount coverage", "complete amount coverage")
        }
        summary["kpi_reconciliation"] = stage("validation", lambda: validate_model(model))
        if summary["input"]["sha256"] != sha256_file(input_file):
            raise InputError("Input workbook changed during execution; retry with a stable source file.")
        stage("persistence", lambda: publish_model(model, output_directory, summary))
        logging.info("[complete] output_rows=%s", summary["output_rows"])
        return 0, summary
    except Exception as error:
        safe_error = str(error) if isinstance(error, (InputError, privacy.KeySetupError)) else (
            f"{current_stage} failed ({type(error).__name__}); inspect the named stage and input contract. No records were logged."
        )
        summary["status"] = "failure"
        summary["ended_utc"] = datetime.now(timezone.utc).isoformat()
        summary["error"] = {"stage": current_stage, "message": safe_error}
        summary["outputs"] = []
        # Failed attempts never replace the last successful run_summary or model.
        if current_stage != "paths":
            try:
                history = output_directory / "runs"
                history.mkdir(parents=True, mode=0o700, exist_ok=True)
                history.chmod(0o700)
                write_json(history / f'{summary["run_id"]}.json', summary)
            except OSError:
                logging.error("[summary] failure report could not be written; check output permissions")
        logging.error("[%s] %s", current_stage, safe_error)
        return 1, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/raw/UseCase - Airlines.xlsx")
    parser.add_argument("--output-dir", default="data/processed")
    parser.add_argument("--key-file", default=".secrets/passenger_hmac.key")
    parser.add_argument("--init-key", action="store_true", help="Initialize a new private key only; never overwrite")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.init_key:
        try:
            privacy.initialize_hmac_key(Path(args.key_file).absolute())
        except (privacy.KeySetupError, OSError):
            logging.error("[key] Initialization failed; ensure .secrets/ is ignored, writable, and the key does not already exist.")
            return 1
        logging.info("[key] New private key initialized. Normal runs will reuse it.")
        return 0
    return run_pipeline(args.input, args.output_dir, args.key_file)[0]


if __name__ == "__main__":
    raise SystemExit(main())
