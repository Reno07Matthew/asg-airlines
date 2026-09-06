"""Unchanged reporting allowlists, dimension keys, and model construction."""

import pandas as pd

from .cleaning import canonical_airlines


model_flight_flags = [
    "timestamp_parse_issue", "non_positive_duration", "flight_id_conflict",
    "airline_missing", "airline_blank", "airline_explicit_unknown", "airline_unrecognized",
    "flight_id_format_issue", "flight_id_normalization_conflict",
    "source_equals_destination", "source_outside_allowlist", "destination_outside_allowlist",
]
model_booking_flags = [
    "booking_exact_duplicate", "booking_id_repeated",
    "booking_id_normalization_collision", "passenger_id_normalization_collision",
    "flight_id_normalization_collision", "status_missing", "status_blank",
    "status_unrecognized", "booking_date_parse_issue",
]
model_passenger_flags = [
    "record_conflict", "identity_contact_conflict", "passenger_id_normalization_collision",
    "age_conflict", "gender_conflict", "age_unresolved", "gender_unresolved",
]
model_payment_flags = [
    "amount_missing", "amount_blank", "amount_nonnumeric", "amount_nonfinite",
    "amount_negative", "amount_zero", "amount_extra_precision", "amount_usable",
    "payment_method_missing", "payment_method_blank", "payment_method_unrecognized",
    "payment_id_repeated", "payment_exact_duplicate",
]
calendar_schema = {
    "date_key": "INTEGER NOT NULL PRIMARY KEY",
    "date": "TEXT NOT NULL UNIQUE", "year": "INTEGER NOT NULL",
    "month": "INTEGER NOT NULL", "month_name": "TEXT NOT NULL",
    "day": "INTEGER NOT NULL", "weekday": "INTEGER NOT NULL", "weekday_name": "TEXT NOT NULL",
}
model_schemas = {
    "DimAirline": {
        "airline_key": "INTEGER NOT NULL PRIMARY KEY", "airline": "TEXT NOT NULL UNIQUE",
    },
    "DimRoute": {
        "route_key": "INTEGER NOT NULL PRIMARY KEY",
        "source": "TEXT", "destination": "TEXT", "route": "TEXT NOT NULL UNIQUE",
    },
    "DimFlightDate": calendar_schema.copy(),
    "DimBookingDate": calendar_schema.copy(),
    "DimPassenger": {
        "passenger_token": "TEXT NOT NULL PRIMARY KEY",
        "age_band": "TEXT NOT NULL", "gender": "TEXT NOT NULL",
        "source_record_count": "INTEGER NOT NULL CHECK (source_record_count > 0)",
        **{flag: f'INTEGER NOT NULL CHECK ("{flag}" IN (0, 1))' for flag in model_passenger_flags},
    },
    "FactFlights": {
        "flight_id": "TEXT NOT NULL PRIMARY KEY",
        "departure_ts": "TEXT NOT NULL", "arrival_ts": "TEXT NOT NULL",
        "duration_minutes": "REAL NOT NULL CHECK (duration_minutes > 0)",
        "duration_hours": "REAL NOT NULL CHECK (duration_hours > 0)",
        "is_overnight": "INTEGER NOT NULL CHECK (is_overnight IN (0, 1))",
        "departure_hour": "INTEGER NOT NULL CHECK (departure_hour BETWEEN 0 AND 23)",
        "airline_key": "INTEGER NOT NULL", "route_key": "INTEGER NOT NULL",
        "flight_date_key": "INTEGER NOT NULL",
        **{flag: f'INTEGER NOT NULL CHECK ("{flag}" IN (0, 1))' for flag in model_flight_flags},
    },
    "FactBookings": {
        "booking_id": "TEXT NOT NULL PRIMARY KEY", "passenger_token": "TEXT",
        "flight_id": "TEXT", "booking_ts": "TEXT", "status": "TEXT NOT NULL",
        "airline_key": "INTEGER NOT NULL", "route_key": "INTEGER NOT NULL",
        "flight_date_key": "INTEGER", "booking_date_key": "INTEGER",
        "flight_reference_status": "TEXT NOT NULL", "passenger_reference_status": "TEXT NOT NULL",
        **{flag: f'INTEGER NOT NULL CHECK ("{flag}" IN (0, 1))' for flag in model_booking_flags},
        "booking_after_departure": "INTEGER CHECK (booking_after_departure IN (0, 1))",
        "payment_record_count": "INTEGER NOT NULL CHECK (payment_record_count >= 0)",
        "usable_amount_count": "INTEGER NOT NULL CHECK (usable_amount_count >= 0)",
        "unusable_amount_count": "INTEGER NOT NULL CHECK (unusable_amount_count >= 0)",
        "known_payment_amount": "INTEGER",  # SQL column is renamed to known_payment_amount_minor.
        "amount_coverage": "TEXT NOT NULL",
    },
    "FactPayments": {
        "payment_id": "TEXT NOT NULL PRIMARY KEY", "booking_id": "TEXT",
        "matched_booking_id": "TEXT", "payment_method": "TEXT NOT NULL",
        "usable_amount": "INTEGER",  # SQL column is renamed to usable_amount_minor.
        **{flag: f'INTEGER NOT NULL CHECK ("{flag}" IN (0, 1))' for flag in model_payment_flags},
        "booking_reference_status": "TEXT NOT NULL",
        "payment_source_row": "INTEGER NOT NULL UNIQUE",
    },
}
model_primary_keys = {
    "DimAirline": "airline_key", "DimRoute": "route_key",
    "DimFlightDate": "date_key", "DimBookingDate": "date_key",
    "DimPassenger": "passenger_token", "FactFlights": "flight_id",
    "FactBookings": "booking_id", "FactPayments": "payment_id",
}
model_foreign_keys = {
    "FactFlights": [
        ("airline_key", "DimAirline", "airline_key"),
        ("route_key", "DimRoute", "route_key"),
        ("flight_date_key", "DimFlightDate", "date_key"),
    ],
    "FactBookings": [
        ("airline_key", "DimAirline", "airline_key"),
        ("route_key", "DimRoute", "route_key"),
        ("flight_date_key", "DimFlightDate", "date_key"),
        ("booking_date_key", "DimBookingDate", "date_key"),
        ("passenger_token", "DimPassenger", "passenger_token"),
    ],
    # Database audit link only; not an initial Power BI relationship.
    "FactPayments": [("matched_booking_id", "FactBookings", "booking_id")],
}
model_money_columns = {"FactBookings": "known_payment_amount", "FactPayments": "usable_amount"}


def calendar_dimension(timestamps):
    dates = timestamps.dropna().dt.normalize()
    assert not dates.empty, "Cannot fabricate a date dimension without valid source dates"
    calendar = pd.Series(pd.date_range(dates.min(), dates.max(), freq="D"))
    return pd.DataFrame({
        "date_key": calendar.dt.strftime("%Y%m%d").astype("int64"),
        "date": calendar.dt.strftime("%Y-%m-%d"),
        "year": calendar.dt.year, "month": calendar.dt.month,
        "month_name": calendar.dt.month_name(), "day": calendar.dt.day,
        "weekday": calendar.dt.dayofweek + 1, "weekday_name": calendar.dt.day_name(),
    })[list(calendar_schema)]


def build_dimensions(flights_standardized, bookings_reporting, passengers_reporting):
    """Build snapshot keys, real calendars, and a minimized passenger dimension."""
    DimAirline = pd.DataFrame({
        "airline_key": range(len(canonical_airlines) + 1),
        "airline": ["UNKNOWN", *sorted(canonical_airlines.values())],
    })[list(model_schemas["DimAirline"])]
    assert flights_standardized["airline"].isin(DimAirline["airline"]).all(), "Review unexpected airline labels before export"
    route_pairs = flights_standardized[["source", "destination"]].dropna().drop_duplicates().sort_values(
        ["source", "destination"]
    ).reset_index(drop=True)
    route_pairs["route_key"] = range(1, len(route_pairs) + 1)
    route_pairs["route"] = route_pairs["source"] + " → " + route_pairs["destination"]
    DimRoute = pd.concat([
        pd.DataFrame([{"route_key": 0, "source": pd.NA, "destination": pd.NA, "route": "UNRESOLVED"}]),
        route_pairs[list(model_schemas["DimRoute"])],
    ], ignore_index=True)[list(model_schemas["DimRoute"])]
    DimFlightDate = calendar_dimension(flights_standardized["departure_ts"])
    DimBookingDate = calendar_dimension(bookings_reporting["booking_ts"])
    DimPassenger = passengers_reporting[list(model_schemas["DimPassenger"])].copy()
    airline_keys = DimAirline.set_index("airline")["airline_key"]
    route_keys = DimRoute.set_index("route")["route_key"]
    return {"DimAirline": DimAirline, "DimRoute": DimRoute, "DimFlightDate": DimFlightDate,
            "DimBookingDate": DimBookingDate, "DimPassenger": DimPassenger}


def build_model(flights_standardized, bookings_reporting, payments_standardized, booking_payment_summary, dimensions):
    """Preserve fact grains and unresolved booking attribution; select only schema columns."""
    DimAirline = dimensions["DimAirline"]
    DimRoute = dimensions["DimRoute"]
    DimFlightDate = dimensions["DimFlightDate"]
    DimBookingDate = dimensions["DimBookingDate"]
    DimPassenger = dimensions["DimPassenger"]
    airline_keys = DimAirline.set_index("airline")["airline_key"]
    route_keys = DimRoute.set_index("route")["route_key"]
    flight_model_source_columns = [
        "flight_id", "departure_ts", "arrival_ts", "duration_minutes", "duration_hours",
        "is_overnight", "departure_hour", "airline", "route", *model_flight_flags,
    ]
    flight_model = flights_standardized[flight_model_source_columns].copy()
    flight_model["airline_key"] = flight_model["airline"].map(airline_keys).astype("Int64")
    flight_model["route_key"] = flight_model["route"].map(route_keys).fillna(0).astype("Int64")
    flight_model["flight_date_key"] = flight_model["departure_ts"].dt.strftime("%Y%m%d").astype("Int64")
    FactFlights = flight_model[list(model_schemas["FactFlights"])].copy()
    booking_model_source_columns = [
        "booking_id", "passenger_token", "flight_id", "booking_ts", "status",
        "airline", "route", "departure_ts", "flight_reference_status", "passenger_reference_status",
        *model_booking_flags, "booking_after_departure",
    ]
    booking_model = bookings_reporting[booking_model_source_columns].copy()
    accepted_booking_attribution = booking_model["flight_reference_status"].eq("accepted")
    booking_model["airline_key"] = booking_model["airline"].map(airline_keys).where(
        accepted_booking_attribution, 0
    ).astype("Int64")
    booking_model["route_key"] = booking_model["route"].map(route_keys).where(
        accepted_booking_attribution, 0
    ).fillna(0).astype("Int64")
    booking_model["flight_date_key"] = booking_model["departure_ts"].where(
        accepted_booking_attribution
    ).dt.strftime("%Y%m%d").astype("Int64")
    booking_model["booking_date_key"] = booking_model["booking_ts"].dt.strftime("%Y%m%d").astype("Int64")
    booking_payment_model_columns = [
        "booking_id", "payment_record_count", "usable_amount_count",
        "unusable_amount_count", "known_payment_amount", "amount_coverage",
    ]
    booking_model = booking_model.merge(
        booking_payment_summary[booking_payment_model_columns].set_index("booking_id"),
        how="left", left_on="booking_id", right_index=True, validate="one_to_one", sort=False,
    )
    assert len(booking_model) == len(bookings_reporting), "Payment summary merge changed booking grain"
    assert booking_model["payment_record_count"].notna().all(), "Booking payment coverage row is absent"
    FactBookings = booking_model[list(model_schemas["FactBookings"])].copy()
    payment_model_source_columns = [
        "payment_id", "booking_id", "payment_method", "usable_amount",
        *model_payment_flags, "booking_reference_status", "payment_source_row",
    ]
    payment_model = payments_standardized[payment_model_source_columns].copy()
    payment_model["matched_booking_id"] = payment_model["booking_id"].where(
        payment_model["booking_reference_status"].eq("uniquely_matched")
    )
    FactPayments = payment_model[list(model_schemas["FactPayments"])].copy()
    model_tables = {
        "DimAirline": DimAirline, "DimRoute": DimRoute,
        "DimFlightDate": DimFlightDate, "DimBookingDate": DimBookingDate,
        "DimPassenger": DimPassenger, "FactFlights": FactFlights,
        "FactBookings": FactBookings, "FactPayments": FactPayments,
    }
    model_tables = {
        name: table.sort_values(model_primary_keys[name]).reset_index(drop=True)
        for name, table in model_tables.items()
    }
    return model_tables
