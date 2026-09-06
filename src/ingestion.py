"""Read the workbook without changing source values or displaying records."""

from pathlib import Path

import pandas as pd


class InputError(ValueError):
    """Fatal input/schema errors with messages safe for the run summary."""


REQUIRED_COLUMNS = {
    "flights": {"flight_id", "airline", "source", "destination", "departure_time", "arrival_time", "duration"},
    "bookings": {"booking_id", "passenger_id", "flight_id", "booking_date", "status"},
    "payments": {"payment_id", "booking_id", "amount", "payment_method"},
    "passengers": {"passenger_id", "first_name", "last_name", "age", "gender", "email", "phone", "aadhaar_id", "date_of_birth"},
}
IDENTIFIER_TYPES = {column: "string" for column in [
    "flight_id", "booking_id", "payment_id", "passenger_id", "aadhaar_id",
    "passport_number", "phone", "emergency_contact_phone",
]}


def validate_source_schema(tables):
    missing = set(REQUIRED_COLUMNS) - set(tables)
    if missing:
        raise InputError("Missing required sheets: " + ", ".join(sorted(missing)))
    for name, required in REQUIRED_COLUMNS.items():
        missing_columns = required - set(tables[name].columns)
        if missing_columns:
            raise InputError(name + ": missing required columns: " + ", ".join(sorted(missing_columns)))


def load_workbook(input_file):
    source = Path(input_file)
    if not source.is_file():
        raise InputError("Input workbook does not exist or is not a regular file; check --input.")
    try:
        tables = pd.read_excel(source, sheet_name=None, engine="openpyxl", dtype=IDENTIFIER_TYPES)
    except Exception:
        raise InputError("Workbook could not be read; check permissions and provide a valid .xlsx file.") from None
    validate_source_schema(tables)
    return tables
