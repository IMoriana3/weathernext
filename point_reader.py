"""Bounded transport-only reader for authorized WeatherNext 3 statistics Zarr.

Only selects existing upstream fields at one geographic grid point.  Does not
implement a forecast algorithm, transform physical units, or transmit output.
Authentication is provided by the ambient Google identity at execution time.
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
FIELDS = (
    "temperature_2m_p50",
    "wind_speed_10m_p50",
    "surface_solar_radiation_downwards_1hr_p50",
    "total_cloud_cover_p50",
)
MAX_HOURS = 6  # explicit bound for the initial pilot, not a model limitation


@dataclass(frozen=True)
class PointRequest:
    lat: float
    lon: float
    prefix: str
    hours: int = 3

    def __post_init__(self):
        if not (math.isfinite(self.lat) and -90 <= self.lat <= 90):
            raise ValueError("latitude must be finite and within [-90,90]")
        if not (math.isfinite(self.lon) and -180 <= self.lon <= 180):
            raise ValueError("longitude must be finite and within [-180,180]")
        if type(self.hours) is not int or not 1 <= self.hours <= MAX_HOURS:
            raise ValueError(f"hours must be an integer from 1 to {MAX_HOURS}")
        matched = RUN_PATTERN.fullmatch(self.prefix)
        if not matched:
            raise ValueError("prefix must identify an approved-format statistics Zarr run")
        try:
            datetime.strptime(matched.group("day") + matched.group("hour"), "%Y%m%d%H")
        except ValueError as exc:
            raise ValueError("prefix contains an invalid date") from exc


def select_point(dataset, request: PointRequest):
    """Select field/space/time BEFORE reading Zarr chunks into memory."""
    import numpy as np

    missing_axes = [axis for axis in ("lat_0p1", "lon_0p1", "lead_time", "init_time")
                    if axis not in dataset]
    if missing_axes:
        raise ValueError("missing upstream coordinate(s): " + ", ".join(missing_axes))
    missing_fields = [name for name in FIELDS if name not in dataset.data_vars]
    if missing_fields:
        raise ValueError("missing upstream data field(s): " + ", ".join(missing_fields))

    lon_360 = request.lon % 360.0
    subset = dataset[list(FIELDS)].sel(
        lat_0p1=request.lat, lon_0p1=lon_360, method="nearest"
    ).sel(lead_time=slice(np.timedelta64(1, "h"), np.timedelta64(request.hours, "h")))
    if subset.sizes.get("lead_time", 0) < 1:
        raise ValueError("no forecast lead times in the requested range")

    # The only step that materializes Google data. A single 0.1-degree grid
    # point, at most six lead times, and four allowlisted variables.
    subset = subset.load()
    init_value = np.asarray(dataset["init_time"].values).reshape(-1)[0]
    init_time = str(np.datetime64(init_value, "ns"))
    initial = datetime.fromisoformat(init_time).replace(tzinfo=timezone.utc)
    matched = RUN_PATTERN.fullmatch(request.prefix)
    declared = datetime.strptime(matched.group("day") + matched.group("hour"), "%Y%m%d%H")
    declared = declared.replace(tzinfo=timezone.utc)
    if initial != declared:
        raise ValueError("Zarr init_time does not match the declared run")

    samples = []
    for idx, delta in enumerate(subset["lead_time"].values):
        seconds = int(np.asarray(delta).astype("timedelta64[s]").astype(int))
        if seconds % 3600 != 0 or not 3600 <= seconds <= request.hours * 3600:
            raise ValueError("unexpected or sub-hourly lead time")
        lead_hour = seconds // 3600
        readings = {}
        for field in FIELDS:
            array = np.asarray(subset[field].isel(lead_time=idx).values)
            if array.size != 1:
                raise ValueError("unexpected extra data dimensions")
            v = float(array.item())
            readings[field] = v if math.isfinite(v) else None
        samples.append({
            "lead_hour": lead_hour,
            "valid_time_utc": (initial + timedelta(hours=lead_hour))
                              .isoformat().replace("+00:00", "Z"),
            "native_values": readings,
        })
    hours = [r["lead_hour"] for r in samples]
    if hours != sorted(set(hours)):
        raise ValueError("unsorted or repeated lead times")
    if not any(r["native_values"]["temperature_2m_p50"] is not None for r in samples):
        raise ValueError("no finite temperature values at the selected point")
    if not any(r["native_values"]["wind_speed_10m_p50"] is not None for r in samples):
        raise ValueError("no finite wind values at the selected point")

    return {
        "transport_schema": "weathernext3-zarr-point-v1",
        "type": "raw_model_forecast_not_field_measurement",
        "source": "Google WeatherNext 3 Statistics",
        "bucket": BUCKET,
        "run_prefix": request.prefix,
        "init_time_utc": initial.isoformat().replace("+00:00", "Z"),
        "requested_grid_point": {"lat": request.lat, "lon": request.lon},
        "selected_grid_point": {
            "lat": float(subset["lat_0p1"].values),
            "lon_360": float(subset["lon_0p1"].values),
        },
        "raw_units": {field: str(dataset[field].attrs.get("units", "unspecified"))
                      for field in FIELDS},
        "unit_conversions_applied": False,
        "samples": samples,
    }


def read_gcs_point(request: PointRequest):
    """Google ADC-only access. No anonymous fallback and no embedded keys."""
    if os.environ.get("GOOGLE_SKIP_SIGNATURE", "").lower() == "true":
        raise ValueError("anonymous access is not allowed for this dataset")
    from obstore.store import GCSStore
    import xarray as xr
    from zarr.storage import ObjectStore

    store = GCSStore(bucket=BUCKET, prefix=request.prefix)
    ds = xr.open_zarr(ObjectStore(store), chunks={})
    try:
        return select_point(ds, request)
    finally:
        ds.close()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--run-prefix", required=True)
    parser.add_argument("--hours", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None,
                        help="Write native samples locally (never to stdout)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    try:
        req = PointRequest(args.lat, args.lon, args.run_prefix, args.hours)
        result = read_gcs_point(req)
        if args.output is not None:
            path = args.output.expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fp:
                json.dump(result, fp, ensure_ascii=False, indent=2, allow_nan=False)
        print("OK: authorized, bounded point read")
        print(f"Lead times: {len(result['samples'])}; fields: {len(FIELDS)}")
        print("Native values are not printed to public logs.")
        if args.output:
            print("Output saved to locally selected destination.")
        return 0
    except Exception as exc:
        # Some Google SDK errors embed URLs, account identifiers and tokens.
        # Never print raw exception strings from network or auth failures.
        if isinstance(exc, ValueError):
            print(f"Input or schema error: {exc}", file=sys.stderr)
        else:
            print(f"Read blocked: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
