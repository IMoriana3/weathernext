"""Read-only WeatherNext 3 statistics Zarr transport, one bounded point.

No forecast physics, conversions, business rules, automated publishing or
credentials are implemented here. Input must be an authorized GCS run.
Official hourly horizons: 360 h for 00/06/12/18 UTC; 48 h for interim runs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys

BUCKET = "weathernext3_statistics_spatial"
BASE = "weathernext_3_0_0_statistics/zarr/2026_to_present/"
RUN_PATTERN = re.compile(
    r"^" + re.escape(BASE) +
    r"(?P<day>20\d{6})_(?P<hour>0\d|1\d|2[0-3])hr_"
    r"(?P<part>\d{2})_preds/predictions\.zarr$"
)

# This is an allowlist of unchanged upstream names, not another model schema.
VARIABLES = (
    "temperature_2m",
    "wind_speed_10m",
    "surface_solar_radiation_downwards_1hr",
    "total_cloud_cover",
)
QUANTILES = ("p10", "p50", "p90")
P50_FIELDS = tuple(base + "_p50" for base in VARIABLES)
FIELDS = tuple(base + "_" + p for base in VARIABLES for p in QUANTILES)

DEFAULT_HOURS = 72
SYNOPTIC_MAX_HOURS = 360
INTERIM_MAX_HOURS = 48
MAX_HOURS = SYNOPTIC_MAX_HOURS
MAX_VALUES_PER_POINT = len(FIELDS) * MAX_HOURS


def run_limits(prefix: str):
    """Return (UTC initialization, max forecast hours, cycle class)."""
    matched = RUN_PATTERN.fullmatch(str(prefix))
    if not matched:
        raise ValueError("prefix must identify an approved-format statistics Zarr run")
    try:
        initialized = datetime.strptime(
            matched.group("day") + matched.group("hour"), "%Y%m%d%H"
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("prefix contains an invalid date") from exc
    synoptic = initialized.hour % 6 == 0
    return (initialized, SYNOPTIC_MAX_HOURS if synoptic else INTERIM_MAX_HOURS,
            "synoptic" if synoptic else "interim")


@dataclass(frozen=True)
class PointRequest:
    lat: float
    lon: float
    prefix: str
    hours: int = DEFAULT_HOURS
    p50_only: bool = False

    def __post_init__(self):
        if not (math.isfinite(self.lat) and -90 <= self.lat <= 90):
            raise ValueError("latitude must be finite and within [-90,90]")
        if not (math.isfinite(self.lon) and -180 <= self.lon <= 180):
            raise ValueError("longitude must be finite and within [-180,180]")
        initial, maximum, cycle = run_limits(self.prefix)
        if type(self.hours) is not int or not 1 <= self.hours <= maximum:
            raise ValueError(
                f"{cycle} run initialized at {initial.isoformat()}: "
                f"hours must be an integer from 1 to {maximum}"
            )
        if type(self.p50_only) is not bool:
            raise ValueError("p50_only must be a boolean")

    @property
    def fields(self):
        return P50_FIELDS if self.p50_only else FIELDS


def _lead_hours(lead_array):
    """Read only the small time coordinate; require integral hourly offsets."""
    import numpy as np
    steps = np.asarray(lead_array)
    if not np.issubdtype(steps.dtype, np.timedelta64):
        raise ValueError("upstream lead_time must contain timedelta offsets")
    seconds = steps.astype("timedelta64[s]").astype("int64")
    if any(int(sec) % 3600 != 0 for sec in seconds):
        raise ValueError("upstream lead_time is not hourly")
    return [int(sec // 3600) for sec in seconds]


def _nearest_circular_longitude(dataset, longitude):
    """Select around the 0/360 seam without reading data chunks."""
    import numpy as np
    coords = np.asarray(dataset["lon_0p1"].values, dtype=float)
    if coords.ndim != 1 or coords.size < 1 or not np.isfinite(coords).all():
        raise ValueError("invalid longitude coordinate axis")
    target = longitude % 360.0
    distances = np.abs((coords - target + 180.0) % 360.0 - 180.0)
    return int(np.argmin(distances))


def select_point(dataset, request: PointRequest):
    """Select variables, one location and hourly leads BEFORE .load()."""
    import numpy as np

    axes = ("lat_0p1", "lon_0p1", "lead_time", "init_time")
    missing_axes = [name for name in axes if name not in dataset]
    if missing_axes:
        raise ValueError("missing upstream coordinate(s): " + ", ".join(missing_axes))

    required = [name for name in P50_FIELDS if name not in dataset.data_vars]
    if required:
        raise ValueError("missing required upstream P50 field(s): " + ", ".join(required))

    selected_fields = request.fields
    present_fields = tuple(field for field in selected_fields if field in dataset.data_vars)
    missing_optional = tuple(field for field in selected_fields if field not in dataset.data_vars)
    if len(present_fields) * request.hours > MAX_VALUES_PER_POINT:
        raise ValueError("request exceeds the per-point numeric-value budget")

    declared_init, max_hours, cycle = run_limits(request.prefix)
    init_raw = np.asarray(dataset["init_time"].values).reshape(-1)
    if len(init_raw) != 1 or np.isnat(np.datetime64(init_raw[0])):
        raise ValueError("upstream initialization is missing or ambiguous")
    initial = datetime.fromisoformat(
        str(np.datetime64(init_raw[0], "ns"))
    ).replace(tzinfo=timezone.utc)
    if initial != declared_init:
        raise ValueError("Zarr init_time does not match the declared run")

    lon_index = _nearest_circular_longitude(dataset, request.lon)
    subset = dataset[list(present_fields)].sel(
        lat_0p1=request.lat, method="nearest"
    ).isel(lon_0p1=lon_index).sel(
        lead_time=slice(np.timedelta64(1, "h"),
                        np.timedelta64(request.hours, "h"))
    )
    lead_hours = _lead_hours(subset["lead_time"].values)
    if lead_hours != list(range(1, request.hours + 1)):
        raise ValueError(
            f"incomplete hourly forecast: {len(lead_hours)} of "
            f"{request.hours} requested lead times"
        )

    # Materialization is bounded to one grid cell, <=360 time steps,
    # and <=12 existing P10/P50/P90 variables. Never load a global field.
    subset = subset.load()

    samples = []
    for idx, lead_hour in enumerate(lead_hours):
        values = {}
        for field in selected_fields:
            if field not in present_fields:
                values[field] = None  # missing variable explicitly noted below
                continue
            array = np.asarray(subset[field].isel(lead_time=idx).values)
            if array.size != 1:
                raise ValueError("unexpected extra data dimensions")
            val = float(array.item())
            values[field] = val if math.isfinite(val) else None
        samples.append({
            "lead_hour": lead_hour,
            "valid_time_utc": (initial + timedelta(hours=lead_hour))
                              .isoformat().replace("+00:00", "Z"),
            "native_values": values,
        })

    if not any(s["native_values"]["temperature_2m_p50"] is not None for s in samples):
        raise ValueError("no finite temperature values at the selected point")
    if not any(s["native_values"]["wind_speed_10m_p50"] is not None for s in samples):
        raise ValueError("no finite wind values at the selected point")

    return {
        "transport_schema": "weathernext3-zarr-point-v2",
        "type": "raw_model_forecast_not_field_measurement",
        "source": "Google WeatherNext 3 Statistics",
        "bucket": BUCKET,
        "run_prefix": request.prefix,
        "init_time_utc": initial.isoformat().replace("+00:00", "Z"),
        "run_class": cycle,
        "run_max_hours": max_hours,
        "requested_hours": request.hours,
        "returned_hours": len(samples),
        "requested_fields": list(selected_fields),
        "available_fields": list(present_fields),
        "missing_upstream_fields": list(missing_optional),
        "requested_grid_point": {"lat": request.lat, "lon": request.lon},
        "selected_grid_point": {
            "lat": float(subset["lat_0p1"].values),
            "lon_360": float(subset["lon_0p1"].values),
        },
        "raw_units": {name: str(dataset[name].attrs.get("units", "unspecified"))
                      for name in present_fields},
        "unit_conversions_applied": False,
        "samples": samples,
    }


def read_gcs_point(request: PointRequest):
    """GCS with authorized ambient identity; anonymous calls are refused."""
    if os.environ.get("GOOGLE_SKIP_SIGNATURE", "").lower() == "true":
        raise ValueError("anonymous access is not allowed for this dataset")
    from obstore.store import GCSStore
    import xarray as xr
    from zarr.storage import ObjectStore

    store = GCSStore(bucket=BUCKET, prefix=request.prefix)
    dataset = xr.open_zarr(ObjectStore(store), chunks={})
    try:
        return select_point(dataset, request)
    finally:
        dataset.close()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--run-prefix", required=True)
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help="72h default; max 360h synoptic or 48h interim")
    parser.add_argument("--p50-only", action="store_true",
                        help="Read only four P50 variables instead of P10/P50/P90")
    parser.add_argument("--output", type=Path, default=None,
                        help="Write native samples locally (never to stdout)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    try:
        request = PointRequest(args.lat, args.lon, args.run_prefix,
                               args.hours, args.p50_only)
        result = read_gcs_point(request)
        if args.output is not None:
            path = args.output.expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fp:
                json.dump(result, fp, ensure_ascii=False, indent=2, allow_nan=False)
        print("OK: authorized, bounded point read")
        print(f"Horizon: {result['returned_hours']}h; "
              f"fields: {len(result['available_fields'])}/"
              f"{len(result['requested_fields'])}")
        if result["missing_upstream_fields"]:
            print("Some optional upstream percentile fields are unavailable.")
        print("Native values are not printed to public logs.")
        if args.output is not None:
            print("Output saved to locally selected destination.")
        return 0
    except Exception as exc:
        # Remote Google SDK messages can include account/project identifiers.
        # The public CI has no access to credentials or restricted data.
        if isinstance(exc, ValueError):
            print(f"Request or data validation failed: {exc}", file=sys.stderr)
        else:
            print(f"Read blocked: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
