"""Opt-in regression for the verified workbook; never used as a cleaning rule."""

import argparse
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from src.model import model_schemas, model_primary_keys
from src.persistence import read_model_csv, sha256_file, sql_column
from src.cleaning import sum_known_amounts


SAMPLE_SHA256 = "82549a897f4829d3e06cfb21b8b1f9100a10cba268ceb3c4597f71ee558a9db3"
SAMPLE_ROWS = {
    "DimAirline": 5, "DimRoute": 31, "DimFlightDate": 4, "DimBookingDate": 366,
    "DimPassenger": 1000, "FactFlights": 1002, "FactBookings": 1000, "FactPayments": 1000,
}


def compare_snapshot(actual_directory, baseline_directory):
    """Compare every CSV cell and SQLite column/value/storage type without displaying rows."""
    with closing(sqlite3.connect(actual_directory / "asg_airlines.db")) as actual_db, closing(sqlite3.connect(baseline_directory / "asg_airlines.db")) as baseline_db:
        for name in model_schemas:
            actual = read_model_csv(name, actual_directory / f"{name}.csv")
            baseline = read_model_csv(name, baseline_directory / f"{name}.csv")
            assert actual.equals(baseline), name + ": CSV values or dtypes differ"
            pragma = f'PRAGMA table_info("{name}")'
            assert actual_db.execute(pragma).fetchall() == baseline_db.execute(pragma).fetchall(), name + ": SQL schema differs"
            foreign_keys = f'PRAGMA foreign_key_list("{name}")'
            assert actual_db.execute(foreign_keys).fetchall() == baseline_db.execute(foreign_keys).fetchall(), name + ": SQL relationships differ"
            columns = [sql_column(name, column) for column in model_schemas[name]]
            projections = [f'"{column}"' for column in columns] + [f'typeof("{column}")' for column in columns]
            query = f'SELECT {", ".join(projections)} FROM "{name}" ORDER BY "{model_primary_keys[name]}"'
            assert actual_db.execute(query).fetchall() == baseline_db.execute(query).fetchall(), name + ": SQL values or storage types differ"
    print("All eight tables match the baseline: every CSV cell/dtype and SQLite schema, FK, value, and storage type.")


def check_sample(input_file, output_directory, baseline_directory=None):
    assert sha256_file(input_file) == SAMPLE_SHA256, "Different extract: use generic pipeline validation, not these sample checkpoints"
    tables = {name: read_model_csv(name, output_directory / f"{name}.csv") for name in model_schemas}
    assert {name: len(frame) for name, frame in tables.items()} == SAMPLE_ROWS
    for name, table in tables.items():
        assert table[model_primary_keys[name]].is_unique and table[model_primary_keys[name]].notna().all()
    flights, bookings, payments = (tables[name] for name in ["FactFlights", "FactBookings", "FactPayments"])
    assert flights["is_overnight"].sum() == 122
    assert abs(flights["duration_minutes"].mean() - 164.53705998003994) < 1e-10
    assert bookings["status"].eq("CANCELLED").sum() == 314
    assert bookings["status"].eq("UNKNOWN").sum() == 75
    assert payments["amount_usable"].sum() == 922
    assert payments["usable_amount"].isna().sum() == 78
    assert bookings["known_payment_amount"].isna().sum() == 394
    assert sum_known_amounts(payments["usable_amount"]) == Decimal("7385142.98")
    assert sum_known_amounts(bookings["known_payment_amount"]) == Decimal("7385142.98")
    with closing(sqlite3.connect(output_directory / "asg_airlines.db")) as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT SUM(usable_amount_minor) FROM FactPayments").fetchone()[0] == 738514298
        assert connection.execute("SELECT SUM(known_payment_amount_minor) FROM FactBookings").fetchone()[0] == 738514298
    summary = json.loads((output_directory / "run_summary.json").read_text())
    assert summary["status"] == "success" and summary["input"]["sha256"] == SAMPLE_SHA256
    assert len(summary["outputs"]) == 9
    for record in summary["outputs"]:
        assert sha256_file(output_directory / record["filename"]) == record["sha256"], "Manifest output hash differs"
    if baseline_directory is not None:
        compare_snapshot(output_directory, baseline_directory)
    print("Supplied-extract counts, durations, exact money, nulls, keys, and output hashes passed.")
    print("Rows:", SAMPLE_ROWS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/raw/UseCase - Airlines.xlsx"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--baseline-dir", type=Path)
    args = parser.parse_args()
    try:
        check_sample(args.input, args.output_dir, args.baseline_dir)
    except Exception as error:
        # Assertion messages contain only schema/table names, never records.
        print("Regression failed:", str(error) if isinstance(error, AssertionError) else type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
