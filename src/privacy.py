"""Passenger agreement, explicit key setup, and pseudonymized allowlist views."""

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path

import pandas as pd

from .cleaning import normalize_identifier, identifier_normalization_collisions


MIN_REPORTED_AGE = 0
MAX_REPORTED_AGE = 120
gender_labels = {"M": "MALE", "MALE": "MALE", "F": "FEMALE", "FEMALE": "FEMALE"}


passenger_age_flags = [
    "age_missing", "age_blank", "age_nonnumeric", "age_nonfinite",
    "age_non_integer", "age_negative", "age_above_max",
]

passenger_gender_flags = [
    "gender_missing", "gender_blank", "gender_explicit_unknown", "gender_unrecognized",
]

age_band_edges = [MIN_REPORTED_AGE, 18, 30, 45, 60, MAX_REPORTED_AGE + 1]

age_band_labels = [f"{MIN_REPORTED_AGE}–17", "18–29", "30–44", "45–59", f"60–{MAX_REPORTED_AGE}"]


def assess_passenger_attributes(ages, genders):
    checks = pd.DataFrame(index=ages.index)
    age_text = ages.astype("string").str.strip()
    numeric_age = pd.to_numeric(ages, errors="coerce").astype("Float64")
    checks["age_missing"] = ages.isna()
    checks["age_blank"] = age_text.eq("").fillna(False)
    checks["age_nonfinite"] = (
        numeric_age.isin([float("inf"), float("-inf")])
        | age_text.str.upper().isin(["NAN", "+NAN", "-NAN"])
    )
    checks["age_nonnumeric"] = (
        numeric_age.isna() & ~checks["age_missing"]
        & ~checks["age_blank"] & ~checks["age_nonfinite"]
    )
    finite_age = numeric_age.where(~checks["age_nonfinite"])
    checks["age_non_integer"] = finite_age.mod(1).ne(0).fillna(False)
    checks["age_negative"] = finite_age.lt(0).fillna(False)
    checks["age_above_max"] = finite_age.gt(MAX_REPORTED_AGE).fillna(False)
    checks["age_valid"] = (
        finite_age.between(MIN_REPORTED_AGE, MAX_REPORTED_AGE)
        & ~checks["age_non_integer"]
    ).fillna(False)
    checks["reported_age"] = numeric_age.where(checks["age_valid"]).astype("Int64")
    checks["gender_raw"] = genders.copy()
    gender_key = genders.astype("string").str.strip().str.upper()
    checks["gender_missing"] = genders.isna()
    checks["gender_blank"] = gender_key.eq("").fillna(False)
    checks["gender_explicit_unknown"] = gender_key.eq("UNKNOWN").fillna(False)
    checks["gender_unrecognized"] = (
        gender_key.notna() & ~checks["gender_blank"]
        & ~checks["gender_explicit_unknown"] & ~gender_key.isin(gender_labels)
    )
    checks["gender_valid"] = gender_key.isin(gender_labels)
    checks["gender"] = gender_key.map(gender_labels).astype("string").fillna("UNKNOWN")
    return checks


def agreed_attribute(values):
    # A minimum represents the single agreed value only after this full-group check.
    if values.isna().any() or values.nunique() != 1:
        return pd.NA
    return values.min()


def assess_passengers(passengers_raw):
    """Compare restricted source fields; only valid agreed analytical values survive."""
    passengers_assessed = passengers_raw.copy()
    assert passengers_raw.index.is_unique, "Passenger source index must identify original rows"
    passengers_assessed["passenger_source_row"] = passengers_raw.index + 2
    passengers_assessed["passenger_id_raw"] = passengers_raw["passenger_id"].copy()
    passengers_assessed["passenger_id"] = normalize_identifier(passengers_raw["passenger_id"])
    passengers_assessed["passenger_id_missing"] = passengers_raw["passenger_id"].isna()
    passengers_assessed["passenger_id_blank"] = (
        passengers_raw["passenger_id"].astype("string").str.strip().eq("").fillna(False)
    )
    passengers_assessed["passenger_id_normalization_collision"] = identifier_normalization_collisions(
        passengers_raw["passenger_id"]
    )
    passengers_assessed["passenger_exact_duplicate"] = passengers_raw.duplicated(keep=False)
    passengers_assessed["passenger_exact_duplicate_excess"] = passengers_raw.duplicated(keep="first")
    passenger_source_id_counts = passengers_assessed["passenger_id"].value_counts()
    repeated_passenger_ids = passenger_source_id_counts.index[passenger_source_id_counts.gt(1)]
    passenger_field_conflicts = passengers_raw.groupby(
        passengers_assessed["passenger_id"]
    )[list(passengers_raw.columns)].nunique(dropna=False).gt(1)
    passenger_field_conflict_counts = passenger_field_conflicts.loc[repeated_passenger_ids].sum().astype(int)
    passenger_identity_fields = [
        "first_name", "last_name", "email", "phone", "aadhaar_id", "date_of_birth",
    ]
    passenger_attribute_checks = assess_passenger_attributes(passengers_raw["age"], passengers_raw["gender"])
    passengers_assessed[passenger_attribute_checks.columns] = passenger_attribute_checks
    passenger_groups = passengers_assessed.groupby("passenger_id")
    passenger_dimension = passenger_groups.agg(
        source_record_count=("passenger_source_row", "size"),
        reported_age=("reported_age", agreed_attribute),
        gender=("gender", agreed_attribute),
        passenger_id_normalization_collision=("passenger_id_normalization_collision", "any"),
    )
    passenger_dimension["reported_age"] = passenger_dimension["reported_age"].astype("Int64")
    passenger_dimension["gender"] = passenger_dimension["gender"].astype("string").fillna("UNKNOWN")
    passenger_dimension["record_conflict"] = passenger_field_conflicts.any(axis=1)
    passenger_dimension["identity_contact_conflict"] = passenger_field_conflicts[passenger_identity_fields].any(axis=1)
    passenger_dimension["age_conflict"] = passenger_field_conflicts["age"]
    passenger_dimension["gender_conflict"] = passenger_field_conflicts["gender"]
    passenger_dimension["age_unresolved"] = passenger_dimension["reported_age"].isna()
    passenger_dimension["gender_unresolved"] = passenger_dimension["gender"].eq("UNKNOWN")
    passenger_dimension[passenger_age_flags + passenger_gender_flags] = passenger_groups[
        passenger_age_flags + passenger_gender_flags
    ].any()
    assert all(a < b for a, b in zip(age_band_edges, age_band_edges[1:])), "Review age bands after changing age limits"
    passenger_dimension["age_band"] = pd.cut(
        passenger_dimension["reported_age"],
        bins=age_band_edges, labels=age_band_labels, right=False,
    ).astype("string").fillna("UNKNOWN")
    return passengers_assessed, passenger_dimension, passenger_field_conflicts


passenger_reporting_columns = [
    "passenger_token", "reported_age", "age_band", "gender", "source_record_count",
    "record_conflict", "identity_contact_conflict", "passenger_id_normalization_collision",
    "age_conflict", "gender_conflict", "age_unresolved", "gender_unresolved",
    "age_missing", "age_blank", "age_nonnumeric", "age_nonfinite",
    "age_non_integer", "age_negative", "age_above_max",
    "gender_missing", "gender_blank", "gender_explicit_unknown", "gender_unrecognized",
]
booking_reporting_source_columns = [
    "booking_id", "flight_id", "booking_ts", "status",
    "departure_ts", "airline", "source", "destination", "route",
    "booking_exact_duplicate", "booking_id_repeated",
    "booking_id_missing", "booking_id_blank", "booking_id_normalization_collision",
    "passenger_id_missing", "passenger_id_blank", "passenger_id_normalization_collision",
    "flight_id_missing", "flight_id_blank", "flight_id_normalization_collision",
    "status_missing", "status_blank", "status_unrecognized",
    "booking_date_parse_issue", "booking_after_departure",
    "flight_reference_status", "passenger_reference_status",
]
booking_passenger_columns = [
    "passenger_token", "reported_age", "age_band", "gender",
    "source_record_count", "record_conflict", "identity_contact_conflict",
    "age_unresolved", "gender_unresolved",
]
booking_passenger_renames = {
    "source_record_count": "passenger_source_record_count",
    "record_conflict": "passenger_record_conflict",
    "identity_contact_conflict": "passenger_identity_contact_conflict",
}
booking_reporting_columns = [
    *booking_reporting_source_columns, "passenger_token", "reported_age", "age_band", "gender",
    "passenger_source_record_count", "passenger_record_conflict", "passenger_identity_contact_conflict",
    "age_unresolved", "gender_unresolved",
]


def build_reporting_views(passenger_dimension, bookings_standardized, key):
    """Use one token derivation and explicit schemas, keeping booking inputs intact."""
    passenger_dimension = passenger_dimension.copy()
    passenger_dimension["passenger_token"] = passenger_dimension.index.to_series().map(lambda value: passenger_token(value, key))
    passengers_reporting = passenger_dimension[passenger_reporting_columns].reset_index(drop=True).copy()
    bookings_reporting = bookings_standardized[booking_reporting_source_columns].copy()
    bookings_reporting['passenger_token'] = normalize_identifier(bookings_standardized['passenger_id']).map(lambda value: passenger_token(value, key))
    bookings_reporting = bookings_reporting.merge(
        passengers_reporting[booking_passenger_columns].rename(columns=booking_passenger_renames).set_index("passenger_token"),
        how="left", left_on="passenger_token", right_index=True,
        validate="many_to_one", sort=False,
    )
    bookings_reporting["passenger_source_record_count"] = bookings_reporting["passenger_source_record_count"].astype("Int64")
    bookings_reporting["age_band"] = bookings_reporting["age_band"].fillna("UNKNOWN")
    bookings_reporting["gender"] = bookings_reporting["gender"].fillna("UNKNOWN")
    for column in ["passenger_record_conflict", "passenger_identity_contact_conflict", "age_unresolved", "gender_unresolved"]:
        bookings_reporting[column] = bookings_reporting[column].astype("boolean")
    bookings_reporting = bookings_reporting[booking_reporting_columns]
    return passengers_reporting, bookings_reporting


class KeySetupError(ValueError):
    """Safe setup messages may be shown by the CLI without exposing key material."""


def _key_path(key_file):
    path = Path(key_file).absolute()
    directory = path.parent
    if directory.name != ".secrets" or directory.is_symlink() or path.is_symlink():
        raise KeySetupError("Use a regular key file inside a project-local .secrets directory; symlinks are not allowed.")
    ignore_file = directory.parent / ".gitignore"
    if not ignore_file.is_file():
        raise KeySetupError("Create .gitignore with .secrets/ as its last active rule before key setup.")
    rules = [line.strip() for line in ignore_file.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not rules or rules[-1] != ".secrets/":
        raise KeySetupError("Keep .secrets/ as the last active .gitignore rule.")
    return path


def initialize_hmac_key(key_file):
    """Explicit, exclusive creation: an existing key is never overwritten."""
    path = _key_path(key_file)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise KeySetupError("Key already exists; initialization refused to overwrite it. Use a normal run.") from None
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(secrets.token_bytes(32))


def load_passenger_hmac_key(key_file):
    """Normal execution reads an existing private key and never creates one."""
    path = _key_path(key_file)
    if not path.is_file():
        raise KeySetupError("HMAC key is missing. Run .venv/bin/python -m src.pipeline --init-key --key-file .secrets/passenger_hmac.key first.")
    directory_stat = path.parent.stat()
    if directory_stat.st_uid != os.getuid() or stat.S_IMODE(directory_stat.st_mode) & 0o077:
        raise KeySetupError("Restrict the secret directory to its owner (chmod 700 .secrets).")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise KeySetupError("Key must be an owner-only regular file (chmod 600 on the key file).")
        key = stream.read(33)
    if len(key) != 32:
        raise KeySetupError("Existing HMAC key must contain exactly 32 bytes; it has not been replaced.")
    return key


def passenger_token(normalized_id, key):
    """Tokenize a normalized source identifier, not an inferred person."""
    if pd.isna(normalized_id):
            return pd.NA
    return hmac.new(key, normalized_id.encode("utf-8"), hashlib.sha256).hexdigest()
