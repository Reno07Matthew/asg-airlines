"""Small synthetic examples; never read or print the real workbook or key."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from src import cleaning, privacy, model, persistence
from src.ingestion import InputError, REQUIRED_COLUMNS, load_workbook, validate_source_schema
from src.pipeline import new_run_summary, run_pipeline


def synthetic_model():
    """Two bookings: one partially covered by two payments, one with no record."""
    flights = pd.DataFrame({
        "flight_id": ["AI001"], "airline": ["Air India"],
        "source": ["BLR"], "destination": ["DEL"],
        "departure_time": [pd.Timestamp("2026-01-02 23:00")],
        "arrival_time": [pd.Timestamp("2026-01-03 01:00")], "duration": [120],
    })
    bookings = pd.DataFrame({
        "booking_id": ["B001", "B002"], "passenger_id": [" P001 ", "p001"],
        "flight_id": ["AI001", "AI001"],
        "booking_date": [pd.Timestamp("2026-01-01")] * 2,
        "status": ["CONFIRMED", "CANCELLED"],
    })
    payments = pd.DataFrame({
        "payment_id": ["T001", "T002"], "booking_id": ["B001", "B001"],
        "amount": ["0.10", None], "payment_method": ["card", "UPI"],
    })
    passengers = pd.DataFrame({
        "passenger_id": ["P001"], "age": [25], "gender": ["F"],
        **{name: [None] for name in ["first_name", "last_name", "email", "phone", "aadhaar_id", "date_of_birth"]},
    })
    _, unique = cleaning.split_exact_duplicates(flights)
    checks = cleaning.check_flights(unique)
    _, accepted = cleaning.partition_flights(checks)
    flights = cleaning.standardize_flights(accepted)
    bookings, _ = cleaning.standardize_bookings(bookings)
    bookings, *_ = cleaning.attach_flight_references(bookings, checks, flights)
    bookings = cleaning.attach_passenger_references(bookings, passengers["passenger_id"])
    payments, _ = cleaning.standardize_payments(payments)
    payments = cleaning.attach_booking_references(payments, bookings)
    coverage, _, _ = cleaning.summarize_booking_payments(payments, bookings)
    _, passenger_dimension, _ = privacy.assess_passengers(passengers)
    passenger_view, booking_view = privacy.build_reporting_views(passenger_dimension, bookings, bytes(range(32)))
    dimensions = model.build_dimensions(flights, booking_view, passenger_view)
    return model.build_model(flights, booking_view, payments, coverage, dimensions)


class BusinessBehaviorTests(unittest.TestCase):
    def test_full_dated_duration_and_duplicate_identity(self):
        raw = pd.DataFrame({
            "flight_id": ["AI001", "AI001", "SJ002", "UK003", "UK003"],
            "departure_time": ["2026-01-01 23:00", "2026-01-01 23:00", "2026-01-02 20:00", "2026-01-01 10:00", "2026-01-01 12:00"],
            "arrival_time": ["2026-01-02 01:00", "2026-01-02 01:00", "2026-01-02 01:00", "2026-01-01 11:00", "2026-01-01 11:00"],
        })
        removed, unique = cleaning.split_exact_duplicates(raw)
        checks = cleaning.check_flights(unique)
        quarantined, accepted = cleaning.partition_flights(checks)
        self.assertEqual(len(removed), 1)
        self.assertEqual(accepted["duration_minutes"].tolist(), [120])
        self.assertTrue(accepted["is_overnight"].all())
        self.assertEqual(checks.loc[2, "duration_minutes"], -1140)
        self.assertEqual(int(checks["flight_id_conflict"].sum()), 2)
        self.assertEqual(len(quarantined.loc[4, "issue_reasons"]), 2)
        self.assertEqual(len(raw), len(removed) + len(quarantined) + len(accepted))

    def test_multiple_payments_partial_all_unknown_and_absent(self):
        bookings = pd.DataFrame({
            "booking_id": ["B1", "B2", "B3", "B4"], "booking_source_row": [2, 3, 4, 5],
            "status": ["UNKNOWN"] * 4, "flight_reference_status": ["accepted"] * 4,
        })
        bookings["booking_id"] = cleaning.normalize_identifier(bookings["booking_id"])
        raw = pd.DataFrame({
            "payment_id": ["T1", "T2", "T3", "T4", "T5", "T6"],
            "booking_id": ["B1", "B1", "B2", "B3", "B3", "ABSENT"],
            "amount": ["0.10", "INVALID", None, "0.20", "0.30", "1.00"],
            "payment_method": ["UPI"] * 6,
        })
        payments, _ = cleaning.standardize_payments(raw)
        payments = cleaning.attach_booking_references(payments, bookings)
        summary, reconciliation, _ = cleaning.summarize_booking_payments(payments, bookings)
        self.assertEqual(len(payments), 6)
        self.assertEqual(len(summary), 4)
        self.assertEqual(summary["payment_record_count"].tolist(), [2, 1, 2, 0])
        self.assertEqual(summary["amount_coverage"].tolist(), [
            "partial amount coverage", "payment records but no usable amounts",
            "complete amount coverage", "no payment record in this extract",
        ])
        self.assertEqual(summary.loc[0, "known_payment_amount"], Decimal("0.10"))
        self.assertTrue(pd.isna(summary.loc[1, "known_payment_amount"]))
        self.assertTrue(pd.isna(summary.loc[3, "known_payment_amount"]))
        self.assertEqual(cleaning.sum_known_amounts(summary["known_payment_amount"]), Decimal("0.60"))
        self.assertEqual(reconciliation.loc["unmatched", "known_payment_amount"], Decimal("1.00"))

    def test_amount_edge_cases_never_impute_or_round(self):
        flags = cleaning.validate_payment_amounts(pd.Series([None, " ", "INVALID", "Infinity", "-2.50", "0", "1.234"], dtype=object))
        self.assertEqual(flags["amount_usable"].tolist(), [False] * 5 + [True, True])
        self.assertTrue(flags.loc[5, "amount_zero"])
        self.assertTrue(flags.loc[6, "amount_extra_precision"])
        self.assertEqual(flags.loc[6, "usable_amount"], Decimal("1.234"))
        with self.assertRaises(AssertionError):
            persistence.minor_units(flags.loc[6, "usable_amount"])

    def test_key_setup_is_explicit_stable_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitignore").write_text(".secrets/\n")
            key_file = root / ".secrets/passenger_hmac.key"
            with self.assertRaises(privacy.KeySetupError):
                privacy.load_passenger_hmac_key(key_file)
            privacy.initialize_hmac_key(key_file)
            key = privacy.load_passenger_hmac_key(key_file)
            normalized = cleaning.normalize_identifier(pd.Series([" p001 ", "P001", None]))
            tokens = normalized.map(lambda value: privacy.passenger_token(value, key))
            self.assertTrue(tokens.iloc[0] == tokens.iloc[1])
            self.assertTrue(pd.isna(tokens.iloc[2]))
            with self.assertRaises(privacy.KeySetupError):
                privacy.initialize_hmac_key(key_file)
            self.assertTrue(key == privacy.load_passenger_hmac_key(key_file))
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(key_file.parent.stat().st_mode & 0o777, 0o700)

    def test_reporting_linkage_and_schemas(self):
        tables = synthetic_model()
        token = tables["DimPassenger"]["passenger_token"].iloc[0]
        self.assertTrue(tables["FactBookings"]["passenger_token"].eq(token).all())
        forbidden = {"passenger_id", "first_name", "last_name", "name", "email", "phone", "aadhaar_id", "date_of_birth", "passport", "passport_number", "seat_number", "emergency_contact_name", "emergency_contact_phone"}
        for frame in tables.values():
            self.assertFalse(set(frame.columns) & forbidden)
            self.assertFalse(any(column.endswith("_raw") for column in frame.columns))
        for columns in [privacy.passenger_reporting_columns, privacy.booking_reporting_columns]:
            self.assertFalse(set(columns) & forbidden)
        tables["FactBookings"]["passport_number"] = "synthetic prohibited field"
        with self.assertRaises(AssertionError):
            persistence.prepare_model_tables(tables)

    def test_schema_failures_are_explicit(self):
        with self.assertRaises(InputError):
            load_workbook(Path("/nonexistent-synthetic-input.xlsx"))
        with self.assertRaisesRegex(InputError, "Missing required sheets"):
            validate_source_schema({})
        tables = {name: pd.DataFrame(columns=sorted(columns)) for name, columns in REQUIRED_COLUMNS.items()}
        del tables["payments"]["amount"]
        with self.assertRaisesRegex(InputError, "payments: missing required columns: amount"):
            validate_source_schema(tables)

    def test_staging_rerun_and_failed_candidate_preserve_snapshot(self):
        tables = synthetic_model()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "model"
            for _ in range(2):
                persistence.publish_model(tables, output, new_run_summary("synthetic.xlsx"))
                persistence.verify_persisted_model(output / "asg_airlines.db", tables)
            before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
            with patch("src.persistence.verify_persisted_model", side_effect=ValueError("synthetic failure")):
                with self.assertRaises(ValueError):
                    persistence.publish_model(tables, output, new_run_summary("synthetic.xlsx"))
            after = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
            self.assertTrue(before == after, "Failed candidate changed the published snapshot")

            # A failure after the first replacement restores the entire previous set.
            original_replace = persistence.os.replace
            def interrupted_replace(source, target):
                if Path(source).name == "DimRoute.csv" and Path(source).parent.name != "previous":
                    raise OSError("synthetic publication interruption")
                return original_replace(source, target)
            with patch("src.persistence.os.replace", side_effect=interrupted_replace):
                with self.assertRaises(OSError):
                    persistence.publish_model(tables, output, new_run_summary("synthetic.xlsx"))
            after = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
            self.assertTrue(before == after, "Interrupted publication did not restore the snapshot")

    def test_cli_failure_summary_and_no_secret_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitignore").write_text(".secrets/\n")
            key_file = root / ".secrets/passenger_hmac.key"
            privacy.initialize_hmac_key(key_file)
            output = root / "output"
            with self.assertLogs(level="ERROR"):
                code, summary = run_pipeline(root / "missing.xlsx", output, key_file)
            self.assertEqual(code, 1)
            self.assertEqual(summary["error"]["stage"], "ingestion")
            report = json.loads((output / "runs" / (summary["run_id"] + ".json")).read_text())
            self.assertEqual(report["status"], "failure")
            self.assertEqual(report["outputs"], [])
            self.assertIsNone(report["input"]["sha256"])
            self.assertFalse((output / "run_summary.json").exists())
            with patch("src.pipeline.sha256_file") as hasher, self.assertLogs(level="ERROR"):
                code, summary = run_pipeline(key_file, output, key_file)
            self.assertEqual(code, 1)
            hasher.assert_not_called()

    def test_passenger_agreement_does_not_resolve_identity_conflicts(self):
        raw = pd.DataFrame({
            "passenger_id": ["P001", "P001", "P002", "P002"],
            "age": [25, 25, 20, 21], "gender": ["F", "F", "M", "F"],
            "first_name": ["synthetic-a", "synthetic-b", None, None],
            **{name: [None] * 4 for name in ["last_name", "email", "phone", "aadhaar_id", "date_of_birth"]},
        })
        _, dimension, _ = privacy.assess_passengers(raw)
        self.assertEqual(dimension.loc["P001", "reported_age"], 25)
        self.assertTrue(dimension.loc["P001", "identity_contact_conflict"])
        self.assertTrue(pd.isna(dimension.loc["P002", "reported_age"]))
        self.assertEqual(dimension.loc["P002", "gender"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
