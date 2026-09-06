"""Shared flight, booking, and payment rules; functions never display records."""

from decimal import Decimal, InvalidOperation, Inexact, Rounded, localcontext

import pandas as pd


def normalize_identifier(values):
    return values.astype("string").str.strip().str.upper().replace("", pd.NA)


def identifier_normalization_collisions(values):
    # Count distinct original strings per normalized key, excluding missing keys.
    keys = normalize_identifier(values)
    variants = values.astype("string").groupby(keys).transform("nunique")
    return keys.notna() & variants.gt(1)


standardized_fields = ["flight_id", "source", "destination", "airline"]
canonical_airlines = {
    "air india": "Air India",
    "indigo": "IndiGo",
    "spicejet": "SpiceJet",
    "vistara": "Vistara",
}
airport_allowlist = {"BLR", "BOM", "CCU", "DEL", "HYD", "MAA"}
issue_labels = {
    "timestamp_parse_issue": "Missing or unparseable departure/arrival timestamp",
    "non_positive_duration": "Duration is zero or negative",
    "flight_id_conflict": "Ambiguous flight ID shared by different records",
}
recognized_booking_statuses = ["CONFIRMED", "CANCELLED", "PENDING"]
expected_payment_methods = ["CARD", "UPI", "NETBANKING"]


def split_exact_duplicates(flights_raw):
    """Separate excess exact copies without resolving repeated flight identifiers."""
    exact_duplicate_mask = flights_raw.duplicated(keep="first")
    flights_removed_duplicates = flights_raw.loc[exact_duplicate_mask].copy()
    flights_unique_rows = flights_raw.loc[
        ~exact_duplicate_mask
    ].copy()
    return flights_removed_duplicates, flights_unique_rows


def check_flights(flights_unique_rows):
    """Preserve signed, fully dated durations and all applicable quarantine reasons."""
    flight_checks = flights_unique_rows.copy()
    flight_checks["flight_id_conflict"] = (
        flight_checks.duplicated(
            subset=["flight_id"],
            keep=False,
        )
    )
    flight_checks["departure_ts"] = pd.to_datetime(
        flight_checks["departure_time"],
        errors="coerce",
    )
    flight_checks["arrival_ts"] = pd.to_datetime(
        flight_checks["arrival_time"],
        errors="coerce",
    )
    flight_checks["timestamp_parse_issue"] = (
        flight_checks[
            ["departure_ts", "arrival_ts"]
        ].isna().any(axis=1)
    )
    flight_checks["duration_minutes"] = (
        flight_checks["arrival_ts"]
        - flight_checks["departure_ts"]
    ).dt.total_seconds() / 60
    flight_checks["non_positive_duration"] = (
        flight_checks["duration_minutes"].le(0)
    )
    flight_checks["is_overnight"] = (
        ~flight_checks["timestamp_parse_issue"]
        & (
            flight_checks["arrival_ts"].dt.normalize()
            > flight_checks["departure_ts"].dt.normalize()
        )
    )
    issue_flags = flight_checks[list(issue_labels)]
    flight_checks["issue_reasons"] = issue_flags.apply(
        lambda flags: [issue_labels[flag] for flag in flags.index[flags]],
        axis=1,
    )
    return flight_checks


def partition_flights(flight_checks):
    """Hold all ambiguous or invalid timestamp records; never choose a winner."""
    quarantine_mask = flight_checks[list(issue_labels)].any(axis=1)
    flights_quarantined = flight_checks.loc[quarantine_mask].copy()
    flights_accepted = flight_checks.loc[~quarantine_mask].copy()
    return flights_quarantined, flights_accepted


def standardize_flights(flights_accepted):
    """Normalize reporting labels without overwriting their preserved source copies."""
    flights_standardized = flights_accepted.copy()
    for column in standardized_fields:
        flights_standardized[f"{column}_raw"] = flights_accepted[column].copy()
    for column in ["flight_id", "source", "destination"]:
        flights_standardized[column] = (
            flights_standardized[column].astype("string")
            .str.strip().str.upper().replace("", pd.NA)
        )
    airline_trimmed = flights_standardized["airline_raw"].astype("string").str.strip()
    airline_key = airline_trimmed.str.casefold()
    flights_standardized["airline_missing"] = airline_trimmed.isna()
    flights_standardized["airline_blank"] = airline_trimmed.eq("").fillna(False)
    flights_standardized["airline_explicit_unknown"] = airline_key.eq("unknown").fillna(False)
    unknown_airline_mask = flights_standardized[[
        "airline_missing", "airline_blank", "airline_explicit_unknown",
    ]].any(axis=1)
    flights_standardized["airline_unrecognized"] = (
        ~unknown_airline_mask & ~airline_key.isin(canonical_airlines)
    )
    flights_standardized["airline"] = (
        airline_key.map(canonical_airlines).astype("string")
        .fillna(airline_trimmed).mask(unknown_airline_mask, "UNKNOWN")
    )
    for column in ["flight_id", "source", "destination"]:
        flights_standardized[f"{column}_missing"] = flights_standardized[column].isna()
    flights_standardized["flight_id_format_issue"] = (
        ~flights_standardized["flight_id_missing"]
        & ~flights_standardized["flight_id"].str.fullmatch(r"(AI|SJ|UK|6F)[0-9]{3}", na=False)
    )
    flights_standardized["flight_id_normalization_conflict"] = (
        ~flights_standardized["flight_id_missing"]
        & flights_standardized["flight_id"].duplicated(keep=False)
    )
    flights_standardized["source_equals_destination"] = (
        flights_standardized["source"].eq(flights_standardized["destination"]).fillna(False)
    )
    for column in ["source", "destination"]:
        flights_standardized[f"{column}_outside_allowlist"] = (
            flights_standardized[column].notna()
            & ~flights_standardized[column].isin(airport_allowlist)
        )
    flights_standardized["route"] = (
        flights_standardized["source"] + " → " + flights_standardized["destination"]
    )
    flights_standardized["departure_date"] = flights_standardized["departure_ts"].dt.date
    flights_standardized["departure_hour"] = flights_standardized["departure_ts"].dt.hour
    flights_standardized["duration_hours"] = flights_standardized["duration_minutes"] / 60
    return flights_standardized


def standardize_bookings(bookings_raw):
    """Normalize booking fields while retaining every source row and identifier issue."""
    bookings_standardized = bookings_raw.copy()
    assert bookings_raw.index.is_unique, "Source index must uniquely identify booking rows"
    bookings_standardized["booking_source_row"] = bookings_raw.index + 2
    bookings_standardized["booking_exact_duplicate"] = bookings_raw.duplicated(keep=False)
    bookings_standardized["booking_exact_duplicate_excess"] = bookings_raw.duplicated(keep="first")
    booking_identifier_columns = ["booking_id", "passenger_id", "flight_id"]
    booking_identifier_profile_rows = []
    for column in booking_identifier_columns:
        original = bookings_raw[column]
        trimmed = original.astype("string").str.strip()
        normalized = normalize_identifier(original)
        collisions = identifier_normalization_collisions(original)
        bookings_standardized[f"{column}_raw"] = original.copy()
        bookings_standardized[column] = normalized
        bookings_standardized[f"{column}_missing"] = original.isna()
        bookings_standardized[f"{column}_blank"] = trimmed.eq("").fillna(False)
        bookings_standardized[f"{column}_normalization_collision"] = collisions
        booking_identifier_profile_rows.append({
            "identifier": column,
            "raw_distinct_nonmissing": original.nunique(),
            "standardized_distinct_nonmissing": normalized.nunique(),
            "missing": int(original.isna().sum()),
            "blank": int(trimmed.eq("").sum()),
            "normalization_collision_rows": int(collisions.sum()),
            "normalization_collision_keys": normalized[collisions].nunique(),
        })
    bookings_standardized["booking_id_repeated_raw"] = (
        bookings_standardized["booking_id"].notna()
        & bookings_raw["booking_id"].duplicated(keep=False)
    )
    bookings_standardized["booking_id_repeated"] = (
        bookings_standardized["booking_id"].notna()
        & bookings_standardized["booking_id"].duplicated(keep=False)
    )
    booking_identifier_profile = pd.DataFrame(booking_identifier_profile_rows).set_index("identifier")
    bookings_standardized["status_raw"] = bookings_raw["status"].copy()
    booking_status_key = bookings_raw["status"].astype("string").str.strip().str.upper()
    bookings_standardized["status_missing"] = bookings_raw["status"].isna()
    bookings_standardized["status_blank"] = booking_status_key.eq("").fillna(False)
    bookings_standardized["status_unrecognized"] = (
        booking_status_key.notna()
        & ~bookings_standardized["status_blank"]
        & ~booking_status_key.isin(recognized_booking_statuses)
    )
    bookings_standardized["status"] = booking_status_key.where(
        booking_status_key.isin(recognized_booking_statuses), "UNKNOWN"
    )
    bookings_standardized["booking_ts"] = pd.to_datetime(
        bookings_raw["booking_date"], errors="coerce"
    )
    bookings_standardized["booking_date_missing"] = bookings_raw["booking_date"].isna()
    bookings_standardized["booking_date_blank"] = (
        bookings_raw["booking_date"].astype("string").str.strip().eq("").fillna(False)
    )
    bookings_standardized["booking_date_parse_issue"] = bookings_standardized["booking_ts"].isna()
    bookings_standardized["booking_date_unparseable"] = (
        bookings_standardized["booking_date_parse_issue"]
        & ~bookings_standardized["booking_date_missing"]
        & ~bookings_standardized["booking_date_blank"]
    )
    return bookings_standardized, booking_identifier_profile


def attach_flight_references(bookings_standardized, flight_checks, flights_standardized):
    """Classify against deduplicated source flights before the many-to-one left merge."""
    source_index = bookings_standardized.index.copy()
    bookings_standardized = bookings_standardized.copy()
    source_flight_references = flight_checks[["flight_id"]].rename(
        columns={"flight_id": "flight_id_raw"}
    ).copy()
    source_flight_references["flight_id"] = normalize_identifier(
        source_flight_references["flight_id_raw"]
    )
    source_flight_references["has_quality_issue"] = flight_checks[list(issue_labels)].any(axis=1)
    source_flight_references["in_accepted_flights"] = source_flight_references.index.isin(
        flights_standardized.index
    )
    flight_reference_lookup = source_flight_references.groupby("flight_id").agg(
        source_records=("flight_id_raw", "size"),
        raw_id_variants=("flight_id_raw", "nunique"),
        has_quality_issue=("has_quality_issue", "any"),
        accepted_records=("in_accepted_flights", "sum"),
    )
    flight_reference_lookup["normalization_collision"] = flight_reference_lookup["raw_id_variants"].gt(1)
    unique_source_flight = flight_reference_lookup["source_records"].eq(1)
    accepted_source_flight = (
        unique_source_flight
        & flight_reference_lookup["accepted_records"].eq(1)
        & ~flight_reference_lookup["has_quality_issue"]
    )
    assert (
        ~unique_source_flight
        | accepted_source_flight
        | (flight_reference_lookup["has_quality_issue"] & flight_reference_lookup["accepted_records"].eq(0))
    ).all(), "A unique source flight is neither accepted nor excluded by flight-quality rules"
    flight_reference_lookup["flight_reference_status"] = "quarantined"
    flight_reference_lookup.loc[accepted_source_flight, "flight_reference_status"] = "accepted"
    flight_reference_lookup.loc[~unique_source_flight, "flight_reference_status"] = "ambiguous"
    bookings_standardized["flight_reference_status"] = (
        bookings_standardized["flight_id"]
        .map(flight_reference_lookup["flight_reference_status"])
        .astype("string").fillna("unmatched")
        .mask(bookings_standardized["flight_id"].isna(), "missing")
    )
    flight_attribute_columns = ["departure_ts", "airline", "source", "destination", "route"]
    accepted_reference_ids = flight_reference_lookup.index[accepted_source_flight]
    accepted_flight_lookup = flights_standardized.loc[
        flights_standardized["flight_id"].isin(accepted_reference_ids),
        ["flight_id", *flight_attribute_columns],
    ].set_index("flight_id")
    bookings_standardized = bookings_standardized.merge(
        accepted_flight_lookup,
        how="left",
        left_on="flight_id",
        right_index=True,
        validate="many_to_one",
        sort=False,
    )
    assert len(bookings_standardized) == len(source_index), 'Flight merge changed booking row count'
    assert bookings_standardized.index.equals(source_index), 'Flight merge changed source row order'
    booking_date_evaluable = (
        bookings_standardized["flight_reference_status"].eq("accepted")
        & bookings_standardized["booking_ts"].notna()
        & bookings_standardized["departure_ts"].notna()
    )
    bookings_standardized["booking_after_departure"] = pd.Series(
        pd.NA, index=bookings_standardized.index, dtype="boolean"
    )
    bookings_standardized.loc[booking_date_evaluable, "booking_after_departure"] = (
        bookings_standardized.loc[booking_date_evaluable, "booking_ts"]
        > bookings_standardized.loc[booking_date_evaluable, "departure_ts"]
    )
    return bookings_standardized, flight_reference_lookup, source_flight_references, accepted_flight_lookup, booking_date_evaluable


def attach_passenger_references(bookings_standardized, passenger_reference_ids_raw):
    """Count passenger identifiers only; no passenger rows or personal data are joined."""
    bookings_standardized = bookings_standardized.copy()
    passenger_reference_ids = normalize_identifier(passenger_reference_ids_raw)
    passenger_reference_counts = passenger_reference_ids.value_counts()
    passenger_source_collisions = identifier_normalization_collisions(passenger_reference_ids_raw)
    booking_passenger_match_counts = (
        bookings_standardized["passenger_id"].map(passenger_reference_counts).fillna(0)
    )
    bookings_standardized["passenger_reference_status"] = pd.Series(
        "unmatched", index=bookings_standardized.index, dtype="string"
    )
    bookings_standardized.loc[
        booking_passenger_match_counts.eq(1), "passenger_reference_status"
    ] = "existing-unique"
    bookings_standardized.loc[
        booking_passenger_match_counts.gt(1), "passenger_reference_status"
    ] = "existing-nonunique"
    bookings_standardized.loc[
        bookings_standardized["passenger_id"].isna(), "passenger_reference_status"
    ] = "missing"
    return bookings_standardized


def parse_payment_amount(value):
    if pd.isna(value) or not str(value).strip():
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def validate_payment_amounts(values):
    checks = pd.DataFrame(index=values.index)
    checks["amount_raw"] = values.copy()
    parsed = values.map(parse_payment_amount)
    finite = parsed.map(lambda value: value is not None and value.is_finite())
    checks["amount_missing"] = values.isna()
    checks["amount_blank"] = values.astype("string").str.strip().eq("").fillna(False)
    checks["amount_nonnumeric"] = (
        parsed.map(lambda value: value is None)
        & ~checks["amount_missing"] & ~checks["amount_blank"]
    )
    checks["amount_nonfinite"] = parsed.map(
        lambda value: value is not None and not value.is_finite()
    )
    checks["amount_negative"] = parsed.map(
        lambda value: value is not None and value.is_finite() and value < 0
    )
    checks["amount_zero"] = parsed.map(
        lambda value: value is not None and value.is_finite() and value == 0
    )
    checks["amount_decimal_places"] = parsed.map(
        lambda value: max(0, -value.as_tuple().exponent)
        if value is not None and value.is_finite() else pd.NA
    ).astype("Int64")
    checks["amount_extra_precision"] = checks["amount_decimal_places"].gt(2).fillna(False)
    checks["amount"] = parsed.where(finite, pd.NA)
    checks["amount_usable"] = finite & ~checks["amount_negative"]
    checks["usable_amount"] = checks["amount"].where(checks["amount_usable"], pd.NA)
    return checks


def sum_known_amounts(values):
    known = values.dropna()
    if known.empty:
        return pd.NA
    # ponytail: default Decimal precision covers this extract; increase explicitly if a trap fires.
    with localcontext() as context:
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        return sum(known, Decimal(0))


def standardize_payments(payments_raw):
    """Preserve payment records, exact decimal amounts, and their quality flags."""
    payments_standardized = payments_raw.copy()
    assert payments_raw.index.is_unique, "Payment source index must uniquely identify rows"
    payments_standardized["payment_source_row"] = payments_raw.index + 2
    payments_standardized["payment_exact_duplicate"] = payments_raw.duplicated(keep=False)
    payments_standardized["payment_exact_duplicate_excess"] = payments_raw.duplicated(keep="first")
    payment_identifier_columns = ["payment_id", "booking_id"]
    payment_identifier_profile_rows = []
    for column in payment_identifier_columns:
        original = payments_raw[column]
        trimmed = original.astype("string").str.strip()
        normalized = normalize_identifier(original)
        collisions = identifier_normalization_collisions(original)
        payments_standardized[f"{column}_raw"] = original.copy()
        payments_standardized[column] = normalized
        payments_standardized[f"{column}_missing"] = original.isna()
        payments_standardized[f"{column}_blank"] = trimmed.eq("").fillna(False)
        payments_standardized[f"{column}_normalization_collision"] = collisions
        payment_identifier_profile_rows.append({
            "identifier": column,
            "raw_distinct_nonmissing": original.nunique(),
            "standardized_distinct_nonmissing": normalized.nunique(),
            "missing": int(original.isna().sum()),
            "blank": int(trimmed.eq("").sum()),
            "normalization_collision_rows": int(collisions.sum()),
            "normalization_collision_keys": normalized[collisions].nunique(),
        })
    payment_identifier_profile = pd.DataFrame(payment_identifier_profile_rows).set_index("identifier")
    payments_standardized["payment_id_repeated_raw"] = (
        payments_standardized["payment_id"].notna()
        & payments_raw["payment_id"].duplicated(keep=False)
    )
    payments_standardized["payment_id_repeated"] = (
        payments_standardized["payment_id"].notna()
        & payments_standardized["payment_id"].duplicated(keep=False)
    )
    payment_amount_checks = validate_payment_amounts(payments_raw["amount"])
    payments_standardized[payment_amount_checks.columns] = payment_amount_checks
    payments_standardized["payment_method_raw"] = payments_raw["payment_method"].copy()
    payment_method_key = payments_raw["payment_method"].astype("string").str.strip().str.upper()
    payments_standardized["payment_method_missing"] = payments_raw["payment_method"].isna()
    payments_standardized["payment_method_blank"] = payment_method_key.eq("").fillna(False)
    payments_standardized["payment_method_unrecognized"] = (
        payment_method_key.notna()
        & ~payments_standardized["payment_method_blank"]
        & ~payment_method_key.isin(expected_payment_methods)
    )
    payments_standardized["payment_method"] = payment_method_key.where(
        payment_method_key.isin(expected_payment_methods), "UNKNOWN"
    )
    return payments_standardized, payment_identifier_profile


def attach_booking_references(payments_standardized, bookings_standardized):
    """Attach safe booking attributes without changing amount usability."""
    source_index = payments_standardized.index.copy()
    payments_standardized = payments_standardized.copy()
    payment_booking_lookup = bookings_standardized[[
        "booking_id", "booking_source_row", "status", "flight_reference_status",
    ]].rename(columns={"status": "booking_status"}).set_index("booking_id")
    assert payment_booking_lookup.index.notna().all(), "Booking lookup contains missing IDs"
    assert payment_booking_lookup.index.is_unique, "Booking lookup IDs conflict; do not choose records"
    assert normalize_identifier(bookings_standardized["booking_id"]).equals(
        bookings_standardized["booking_id"]
    ), "Booking lookup identifiers must already be standardized"
    payments_standardized["booking_reference_status"] = pd.Series(
        "unmatched", index=payments_standardized.index, dtype="string"
    )
    payments_standardized.loc[
        payments_standardized["booking_id"].isin(payment_booking_lookup.index),
        "booking_reference_status",
    ] = "uniquely_matched"
    payments_standardized.loc[
        payments_standardized["booking_id"].isna(), "booking_reference_status"
    ] = "missing"
    payments_standardized = payments_standardized.merge(
        payment_booking_lookup,
        how="left", left_on="booking_id", right_index=True,
        validate="many_to_one", sort=False,
    )
    assert len(payments_standardized) == len(source_index), 'Booking merge changed payment row count'
    assert payments_standardized.index.equals(source_index), 'Booking merge changed source row order'
    return payments_standardized


def summarize_booking_payments(payments_standardized, bookings_standardized):
    """Aggregate transactions first; an all-unknown or absent subtotal remains null."""
    matched_payment_mask = payments_standardized["booking_reference_status"].eq("uniquely_matched")
    matched_payments = payments_standardized.loc[
        matched_payment_mask, ["booking_id", "payment_source_row", "usable_amount"]
    ]
    payments_by_booking = matched_payments.groupby("booking_id").agg(
        payment_record_count=("payment_source_row", "size"),
        usable_amount_count=("usable_amount", "count"),
        known_payment_amount=("usable_amount", sum_known_amounts),
    )
    booking_payment_summary = bookings_standardized[[
        "booking_id", "booking_source_row", "status", "flight_reference_status",
    ]].rename(columns={"status": "booking_status"}).merge(
        payments_by_booking,
        how="left", left_on="booking_id", right_index=True,
        validate="one_to_one", sort=False,
    )
    payment_count_columns = ["payment_record_count", "usable_amount_count"]
    booking_payment_summary[payment_count_columns] = (
        booking_payment_summary[payment_count_columns].fillna(0).astype("int64")
    )
    booking_payment_summary["unusable_amount_count"] = (
        booking_payment_summary["payment_record_count"] - booking_payment_summary["usable_amount_count"]
    )
    booking_payment_summary["amount_coverage"] = "complete amount coverage"
    booking_payment_summary.loc[
        booking_payment_summary["unusable_amount_count"].gt(0),
        "amount_coverage",
    ] = "partial amount coverage"
    booking_payment_summary.loc[
        booking_payment_summary["usable_amount_count"].eq(0),
        "amount_coverage",
    ] = "payment records but no usable amounts"
    booking_payment_summary.loc[
        booking_payment_summary["payment_record_count"].eq(0),
        "amount_coverage",
    ] = "no payment record in this extract"
    payment_reference_categories = ["missing", "unmatched", "uniquely_matched"]
    payment_reference_reconciliation = payments_standardized.groupby("booking_reference_status").agg(
        payment_record_count=("payment_source_row", "size"),
        usable_amount_count=("usable_amount", "count"),
        known_payment_amount=("usable_amount", sum_known_amounts),
    ).reindex(payment_reference_categories)
    payment_reference_reconciliation[payment_count_columns] = (
        payment_reference_reconciliation[payment_count_columns].fillna(0).astype("int64")
    )
    payment_reference_reconciliation["unusable_amount_count"] = (
        payment_reference_reconciliation["payment_record_count"]
        - payment_reference_reconciliation["usable_amount_count"]
    )
    known_payment_total = sum_known_amounts(payments_standardized["usable_amount"])
    known_matched_payment_total = sum_known_amounts(matched_payments["usable_amount"])
    known_booking_payment_total = sum_known_amounts(booking_payment_summary["known_payment_amount"])
    return booking_payment_summary, payment_reference_reconciliation, payments_by_booking
