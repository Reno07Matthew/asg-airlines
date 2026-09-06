"""Typed staging, exact read-back verification, and recoverable full refreshes."""

from datetime import datetime, timezone
from contextlib import closing
from decimal import Decimal, localcontext, Inexact
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

import pandas as pd

from .cleaning import sum_known_amounts
from .model import model_schemas, model_primary_keys, model_foreign_keys, model_money_columns

MONEY_SCALE = 100


def minor_units(value):
    if pd.isna(value):
        return None
    assert isinstance(value, Decimal) and value.is_finite(), "Monetary values must be finite Decimal values"
    assert value.as_tuple().exponent >= -2, "Extra monetary precision requires review; do not round"
    with localcontext() as context:
        context.traps[Inexact] = True
        scaled = value * MONEY_SCALE
    assert scaled == scaled.to_integral_value(), "Amount cannot be represented exactly at scale 100"
    result = int(scaled)
    assert 0 <= result <= 2**63 - 1, "Usable amount is outside SQLite integer range"
    return result


def sql_column(table_name, column):
    return column + "_minor" if column == model_money_columns.get(table_name) else column


def csv_value(table_name, column, value):
    if pd.isna(value):
        return None
    if column == model_money_columns.get(table_name):
        minor_units(value)  # Validate precision before formatting.
        return format(value, ".2f")
    kind = model_schemas[table_name][column].split()[0]
    if kind == "INTEGER":
        assert value == int(value), "Non-integral value in an integer export column"
        return int(value)
    if kind == "REAL":
        number = float(value)
        assert float("-inf") < number < float("inf"), "Nonfinite reporting measurement"
        return number
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def prepare_model_tables(model_tables):
    """Validate the allowlists and create CSV values and typed SQL tuples."""
    assert set(model_tables) == set(model_schemas), "Reporting table set differs from the contract"
    model_csv_tables = {}
    model_sql_rows = {}
    for table_name, table in model_tables.items():
        assert table.columns.tolist() == list(model_schemas[table_name]), "Unexpected export columns"
        primary_key = model_primary_keys[table_name]
        assert table[primary_key].notna().all() and table[primary_key].is_unique, "Resolve model key conflicts before export"
        safe_table = pd.DataFrame({
            column: pd.Series([
                csv_value(table_name, column, value) for value in table[column]
            ], dtype=object)
            for column in model_schemas[table_name]
        })
        model_csv_tables[table_name] = safe_table
        money_column = model_money_columns.get(table_name)
        model_sql_rows[table_name] = [
            tuple(
                minor_units(Decimal(value)) if column == money_column and value is not None else value
                for column, value in zip(safe_table.columns, row)
            )
            for row in safe_table.itertuples(index=False, name=None)
        ]
        for column, definition in model_schemas[table_name].items():
            if "NOT NULL" in definition:
                assert safe_table[column].notna().all(), "Required model field is missing"
    for table_name, relationships in model_foreign_keys.items():
        for column, parent_table, parent_column in relationships:
            assert model_tables[table_name][column].dropna().isin(
                model_tables[parent_table][parent_column]
            ).all(), "Model contains an unresolved enforced foreign key"
    for table in model_tables.values():
        forbidden_model_columns = {
            "passenger_id", "reported_age", "first_name", "last_name", "email", "phone",
            "aadhaar_id", "date_of_birth", "passport_number", "seat_number",
            "emergency_contact_name", "emergency_contact_phone", "issue_reasons",
        }
        assert not set(table.columns) & forbidden_model_columns, "Forbidden field in export schema"
        assert not any(column.endswith("_raw") for column in table.columns), "Raw field in export schema"
    return model_csv_tables, model_sql_rows


def read_model_csv(table_name, file_path):
    # Explicit schema; empty fields alone mean null. Never infer IDs/tokens as numbers.
    frame = pd.read_csv(file_path, dtype="string", keep_default_na=False, na_values=[""])
    assert frame.columns.tolist() == list(model_schemas[table_name]), "CSV schema mismatch"
    for column, definition in model_schemas[table_name].items():
        if column == model_money_columns.get(table_name):
            frame[column] = frame[column].map(lambda value: None if pd.isna(value) else Decimal(value))
        elif definition.startswith("INTEGER"):
            frame[column] = pd.to_numeric(frame[column], errors="raise").astype("Int64")
        elif definition.startswith("REAL"):
            frame[column] = frame[column].map(lambda value: pd.NA if pd.isna(value) else float(value)).astype("Float64")
    return frame


def same_export_values(table_name, actual, model_csv_tables):
    actual_values = [
        tuple(csv_value(table_name, column, value) for column, value in zip(actual.columns, row))
        for row in actual.itertuples(index=False, name=None)
    ]
    expected_values = list(model_csv_tables[table_name].itertuples(index=False, name=None))
    return actual_values == expected_values


def verify_persisted_model(database_file, model_tables):
    model_csv_tables, model_sql_rows = prepare_model_tables(model_tables)
    payments = model_tables["FactPayments"]
    known_payment_total = sum_known_amounts(payments["usable_amount"])
    known_matched_payment_total = sum_known_amounts(
        payments.loc[payments["matched_booking_id"].notna(), "usable_amount"]
    )
    csv_tables = {}
    sql_tables = {}
    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not connection.execute("PRAGMA foreign_key_check").fetchall(), "Persisted foreign-key violation"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table_name, schema in model_schemas.items():
            csv_frame = read_model_csv(table_name, database_file.parent / f"{table_name}.csv")
            primary_key = model_primary_keys[table_name]
            assert len(csv_frame) == len(model_tables[table_name]), "CSV row-count mismatch"
            assert csv_frame[primary_key].notna().all() and csv_frame[primary_key].is_unique
            assert same_export_values(table_name, csv_frame, model_csv_tables), 'CSV values or nulls changed'
            column_names = [sql_column(table_name, column) for column in schema]
            actual_schema = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            assert [row[1] for row in actual_schema] == column_names, "SQLite column mismatch"
            assert [row[2] for row in actual_schema] == [definition.split()[0] for definition in schema.values()]
            assert [row[1] for row in actual_schema if row[5]] == [primary_key]
            quoted_columns = ", ".join(f'"{column}"' for column in column_names)
            rows = connection.execute(
                f'SELECT {quoted_columns} FROM "{table_name}" ORDER BY "{primary_key}"'
            ).fetchall()
            assert rows == model_sql_rows[table_name], "SQLite values or nulls changed"
            csv_tables[table_name] = csv_frame
            sql_tables[table_name] = pd.DataFrame(rows, columns=column_names)
        for table_name, relationships in model_foreign_keys.items():
            for column, parent, parent_column in relationships:
                assert csv_tables[table_name][column].dropna().isin(csv_tables[parent][parent_column]).all()
        sql_payment_total = connection.execute('SELECT SUM(usable_amount_minor) FROM FactPayments').fetchone()[0]
        sql_booking_total = connection.execute('SELECT SUM(known_payment_amount_minor) FROM FactBookings').fetchone()[0]
        assert sql_payment_total == minor_units(known_payment_total), "Transaction money changed"
        assert sql_booking_total == minor_units(known_matched_payment_total), "Matched booking money changed"
        usable_count = connection.execute(
            'SELECT COUNT(*) FROM FactPayments WHERE usable_amount_minor IS NOT NULL'
        ).fetchone()[0]
        assert usable_count == int(payments["amount_usable"].sum())
    return csv_tables, sql_tables


def validate_model(model_tables):
    """Generic business reconciliation; there are no extract-specific checkpoints."""
    prepare_model_tables(model_tables)
    flights = model_tables["FactFlights"]
    bookings = model_tables["FactBookings"]
    payments = model_tables["FactPayments"]
    accepted = bookings["flight_reference_status"].eq("accepted")
    assert bookings.loc[~accepted, "airline_key"].eq(0).all()
    assert bookings.loc[~accepted, "route_key"].eq(0).all()
    assert bookings["flight_date_key"].notna().eq(accepted).all()
    assert bookings["known_payment_amount"].isna().eq(bookings["usable_amount_count"].eq(0)).all()
    assert payments["usable_amount"].notna().eq(payments["amount_usable"]).all()
    assert payments["matched_booking_id"].notna().eq(payments["booking_reference_status"].eq("uniquely_matched")).all()
    matched = payments.loc[payments["matched_booking_id"].notna()]
    grouped = matched.groupby("matched_booking_id").agg(
        records=("payment_id", "size"), usable=("usable_amount", "count"),
        amount=("usable_amount", sum_known_amounts),
    )
    for column, group_column in [("payment_record_count", "records"), ("usable_amount_count", "usable")]:
        expected = bookings["booking_id"].map(grouped[group_column]).fillna(0).astype("int64")
        assert bookings[column].eq(expected).all(), "Booking payment counts do not reconcile"
    assert bookings["unusable_amount_count"].eq(bookings["payment_record_count"] - bookings["usable_amount_count"]).all()
    expected_amount = bookings["booking_id"].map(grouped["amount"])
    assert [minor_units(v) for v in bookings["known_payment_amount"]] == [minor_units(v) for v in expected_amount]
    durations = (flights["arrival_ts"] - flights["departure_ts"]).dt.total_seconds() / 60
    assert flights["duration_minutes"].equals(durations.rename("duration_minutes"))
    assert flights["duration_hours"].eq(flights["duration_minutes"] / 60).all()
    overnight = flights["arrival_ts"].dt.normalize().gt(flights["departure_ts"].dt.normalize())
    assert flights["is_overnight"].eq(overnight).all()
    for name, timestamps in [("DimFlightDate", flights["departure_ts"]), ("DimBookingDate", bookings["booking_ts"])]:
        valid_dates = timestamps.dropna().dt.normalize()
        assert not valid_dates.empty, "No observed dates for calendar validation"
        expected = pd.date_range(valid_dates.min(), valid_dates.max()).strftime("%Y-%m-%d").tolist()
        dimension = model_tables[name]
        assert dimension["date"].tolist() == expected, "Calendar bounds or continuity changed"
        assert dimension["date_key"].tolist() == [int(day.replace("-", "")) for day in expected]
    total = sum_known_amounts(payments["usable_amount"])
    matched_total = sum_known_amounts(matched["usable_amount"])
    unresolved_payments = payments.loc[payments["matched_booking_id"].isna()]
    unresolved_total = sum_known_amounts(unresolved_payments["usable_amount"])
    return {
        "overnight_flights": int(flights["is_overnight"].sum()),
        "mean_duration_minutes": float(flights["duration_minutes"].mean()),
        "cancelled_bookings": int(bookings["status"].eq("CANCELLED").sum()),
        "unknown_status_bookings": int(bookings["status"].eq("UNKNOWN").sum()),
        "usable_payment_amounts": int(payments["amount_usable"].sum()),
        "unusable_payment_amounts": int(payments["usable_amount"].isna().sum()),
        "null_booking_payment_subtotals": int(bookings["known_payment_amount"].isna().sum()),
        "known_payment_amount": None if pd.isna(total) else str(total),
        "known_matched_payment_amount": None if pd.isna(matched_total) else str(matched_total),
        "known_unresolved_payment_amount": None if pd.isna(unresolved_total) else str(unresolved_total),
        "matched_payment_records": len(matched),
        "unresolved_payment_records": len(unresolved_payments),
        "unresolved_booking_flights": int((~accepted).sum()),
        "reconciliation_passed": True,
    }


def sha256_file(path):
    """File hashes are for sources/outputs only; never call this on a secret."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, summary):
    path = Path(path)
    with path.open("w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(summary, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def write_candidate(model_tables, directory):
    """Write only into an isolated staging directory; do not touch published files."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    csv_tables, sql_rows = prepare_model_tables(model_tables)
    for name, table in csv_tables.items():
        target = directory / f"{name}.csv"
        table.to_csv(target, index=False, na_rep="", encoding="utf-8", lineterminator="\n")
        target.chmod(0o600)
        assert same_export_values(name, read_model_csv(name, target), csv_tables)
    database = directory / "asg_airlines.db"
    connection = sqlite3.connect(database)
    database.chmod(0o600)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("BEGIN IMMEDIATE")
        for name in reversed(model_schemas):
            connection.execute(f'DROP TABLE IF EXISTS "{name}"')
        for name, schema in model_schemas.items():
            definitions = [f'"{sql_column(name, column)}" {definition}' for column, definition in schema.items()]
            definitions += [
                f'FOREIGN KEY ("{column}") REFERENCES "{parent}" ("{parent_column}")'
                for column, parent, parent_column in model_foreign_keys.get(name, [])
            ]
            connection.execute(f'CREATE TABLE "{name}" ({", ".join(definitions)}) STRICT')
            names = ", ".join(f'"{sql_column(name, column)}"' for column in schema)
            placeholders = ", ".join("?" for _ in schema)
            connection.executemany(f'INSERT INTO "{name}" ({names}) VALUES ({placeholders})', sql_rows[name])
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return database


def publish_model(model_tables, output_directory, summary):
    """Validate a complete candidate before publication; restore files on ordinary errors."""
    started = time.perf_counter()
    validate_model(model_tables)
    output_directory = Path(output_directory)
    if output_directory.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    output_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    output_directory.chmod(0o700)
    filenames = [f"{name}.csv" for name in model_schemas] + ["asg_airlines.db", "run_summary.json"]
    for name in filenames:
        if (output_directory / name).is_symlink():
            raise ValueError("Model output targets must not be symlinks")
    with tempfile.TemporaryDirectory(prefix=".pipeline-", dir=output_directory) as temporary:
        stage = Path(temporary)
        database = write_candidate(model_tables, stage)
        verify_persisted_model(database, model_tables)
        summary["output_rows"] = {name: len(frame) for name, frame in model_tables.items()}
        summary["outputs"] = [
            {"filename": name, "sha256": sha256_file(stage / name)}
            for name in filenames if name != "run_summary.json"
        ]
        # The summary is published last. Readers must wait for the run to finish.
        backup = stage / "previous"
        backup.mkdir(mode=0o700)
        for name in filenames:
            if (output_directory / name).exists():
                shutil.copy2(output_directory / name, backup / name)
        published = []
        try:
            for name in filenames[:-1]:
                os.replace(stage / name, output_directory / name)
                published.append(name)
            summary["status"] = "success"
            summary["ended_utc"] = datetime.now(timezone.utc).isoformat()
            summary["error"] = None
            summary["stages"].append({"name": "persistence", "seconds": round(time.perf_counter() - started, 4)})
            write_json(stage / "run_summary.json", summary)
            os.replace(stage / "run_summary.json", output_directory / "run_summary.json")
            published.append("run_summary.json")
        except Exception:
            for name in reversed(published):
                if (backup / name).exists():
                    os.replace(backup / name, output_directory / name)
                else:
                    (output_directory / name).unlink(missing_ok=True)
            raise
    return output_directory / "asg_airlines.db"
