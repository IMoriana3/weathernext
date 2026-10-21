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


def select_point(dataset, request: PointRequest, *, progress=None,
                 fetch_mode="parallel", checkpoint_dir=None,
                 strict_quantiles=False):
    """Select variables, one location and hourly leads BEFORE .load()."""
    import numpy as np
    emit = progress or (lambda message: None)
    if fetch_mode not in ("parallel", "serial", "hourly", "direct"):
        raise ValueError("fetch_mode must be parallel, serial, hourly or direct")
    if checkpoint_dir is not None and fetch_mode != "direct":
        raise ValueError("checkpoints require direct mode")
    if strict_quantiles and request.p50_only:
        raise ValueError("full quantile validation rejects P50-only requests")

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
    emit(f"CHUNKS: fetching {len(present_fields)} fields, "
         f"{request.hours} hourly values each; mode={fetch_mode}")
    if present_fields:
        # Structural graph metadata only; never print forecast values or
        # fetch any additional global chunks for this diagnostic.
        lead_chunks = subset[present_fields[0]].chunksizes.get("lead_time", ())
        emit(f"CHUNK_LAYOUT: first field lead_time uses "
             f"{len(lead_chunks)} lazy chunk(s)")
    if fetch_mode == "direct":
        from point_checkpoint import load_direct
        subset = load_direct(subset, request, emit, checkpoint_dir=checkpoint_dir,
                             strict_quantiles=strict_quantiles)
    elif fetch_mode == "hourly":
        # Independently materialize one requested lead hour at a time. This
        # restricts each active Dask graph, exposes the first slow lead and
        # fails at the earliest unavailable mandatory median. It may cost
        # more remote requests than a single multi-hour read.
        import xarray as xr
        loaded_hours = []
        for i, lead in enumerate(lead_hours):
            emit(f"CHUNK_HOUR: {i + 1}/{request.hours} lead={lead}")
            one_hour = subset.isel(
                lead_time=slice(i, i + 1)
            ).load(scheduler="single-threaded")
            for field in P50_FIELDS:
                native = np.asarray(one_hour[field].values)
                if native.size != 1 or not np.isfinite(native.item()):
                    raise ValueError(
                        f"INCOMPLETE_HOURLY_VALUES: {field} missing "
                        f"at lead hour {lead}; refusing incomplete forecast"
                    )
            loaded_hours.append(one_hour)
            emit(f"CHUNK_HOUR_OK: {i + 1}/{request.hours} lead={lead}")
        subset = xr.concat(
            loaded_hours, dim="lead_time", join="exact",
            coords="minimal", compat="equals",
        )
    elif fetch_mode == "serial":
        # Keep one Dask graph active at a time for remote GCS diagnosis:
        # fewer concurrent object requests and a progress marker per field.
        # This does NOT make remote access instantaneous or guarantee validity.
        import xarray as xr
        loaded_fields = []
        for idx, field in enumerate(present_fields, 1):
            emit(f"CHUNK_FIELD: {idx}/{len(present_fields)} {field}")
            one = subset[[field]].load(scheduler="single-threaded")
            loaded_fields.append(one)
            emit(f"CHUNK_FIELD_OK: {idx}/{len(present_fields)} {field}")
        subset = xr.merge(loaded_fields, join="exact")
    else:
        subset = subset.load()
    emit("CHUNKS_OK: numeric values received; validating hourly completeness")

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

    # A Zarr root may appear before the forecast's numerical chunks are
    # fully disseminated. A complete coordinate axis and 12 listed variables
    # do NOT prove that hourly P50 values have arrived. Fail closed rather
    # than publishing a sparse forecast as 24/72/360 valid hours.
    incomplete = {
        field: [s["lead_hour"] for s in samples
                if s["native_values"][field] is None]
        for field in P50_FIELDS
    }
    incomplete = {field: hours for field, hours in incomplete.items() if hours}
    if incomplete:
        details = "; ".join(
            f"{field}: {len(hours)}/{request.hours} hours missing "
            f"(first lead {hours[0]})"
            for field, hours in incomplete.items()
        )
        raise ValueError(
            "INCOMPLETE_HOURLY_VALUES: required P50 series contain gaps; "
            + details + ". A newer run may still be publishing."
        )

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



def require_complete_quantiles(result: dict) -> None:
    """Optional certification gate for *all* 12 hourly native percentiles.

    The general transport still explicitly supports absent optional P10/P90
    fields; this gate is opt-in when preparing a fully probabilistic batch.
    """
    if result.get("requested_fields") != list(FIELDS):
        raise ValueError(
            "QUANTILE_REQUEST: full P10/P50/P90 certification requires "
            "all 12 native source fields, not a P50-only extraction"
        )
    if (result.get("available_fields") != list(FIELDS)
            or result.get("missing_upstream_fields")):
        raise ValueError(
            "INCOMPLETE_QUANTILES: one or more upstream percentile fields "
            "are absent, so a full probabilistic series cannot be certified"
        )
    for sample in result.get("samples", []):
        for field in FIELDS:
            if sample["native_values"].get(field) is None:
                raise ValueError(
                    "INCOMPLETE_QUANTILES: native "
                    f"{field} missing at lead hour {sample['lead_hour']}"
                )

GCS_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"


def _gcs_auth_options():
    """Bridge Workload Identity Federation ADC into obstore, read-only scope.

    obstore's native GCS client only decodes ``service_account`` and
    ``authorized_user`` ADC files; the ``external_account`` file written by
    google-github-actions/auth (OIDC/WIF) fails with "unknown variant
    external_account". Only that case is delegated to google-auth; Cloud
    Shell and other ambient identities keep obstore's unchanged lookup.
    """
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        return {}
    try:
        kind = json.loads(Path(path).read_text(encoding="utf-8")).get("type")
    except (OSError, ValueError, AttributeError):
        return {}  # obstore reports its own categorized credential error
    if kind != "external_account":
        return {}
    import google.auth
    from obstore.auth.google import GoogleCredentialProvider

    credentials, _ = google.auth.default(scopes=[GCS_READ_ONLY_SCOPE])
    return {"credential_provider": GoogleCredentialProvider(credentials)}


def probe_root_metadata(prefix: str) -> int:
    """Read only the run's root zarr.json through the production GCS client.

    Proves the same identity path the ingester uses can GET a real object.
    Returns the Zarr format; no forecast arrays or values are read.
    """
    run_limits(prefix)
    if os.environ.get("GOOGLE_SKIP_SIGNATURE", "").lower() == "true":
        raise ValueError("anonymous access is not allowed for this dataset")
    import obstore
    from obstore.store import GCSStore

    store = GCSStore(bucket=BUCKET, prefix=prefix, **_gcs_auth_options())
    raw = bytes(obstore.get(store, "zarr.json").bytes())
    try:
        meta = json.loads(raw)
    except ValueError as exc:
        raise ValueError("root zarr.json is not valid JSON") from exc
    if not isinstance(meta, dict) or meta.get("zarr_format") != 3:
        raise ValueError("root metadata is not Zarr v3")
    return 3


def read_gcs_point(request: PointRequest, *, progress=None,
                   fetch_mode="parallel", checkpoint_dir=None,
                   strict_quantiles=False):
    """GCS with authorized ambient identity; anonymous calls are refused."""
    emit = progress or (lambda message: None)
    if os.environ.get("GOOGLE_SKIP_SIGNATURE", "").lower() == "true":
        raise ValueError("anonymous access is not allowed for this dataset")
    from obstore.store import GCSStore
    import xarray as xr
    from zarr.storage import ObjectStore

    emit("METADATA: connecting to authorized GCS Zarr store")
    if checkpoint_dir is not None and fetch_mode != "direct":
        raise ValueError("checkpoints require direct mode")
    options = _gcs_auth_options()
    if fetch_mode == "direct":
        options.update({
            "client_options": {"timeout": "30s", "connect_timeout": "10s"},
            "retry_config": {"max_retries": 2, "retry_timeout": timedelta(seconds=60)},
        })
    store = GCSStore(bucket=BUCKET, prefix=request.prefix, **options)
    dataset = xr.open_zarr(ObjectStore(store),
                           chunks=None if fetch_mode == "direct" else {})
    emit("METADATA_OK: opening finished; selecting site and requested hours")
    try:
        return select_point(dataset, request, progress=progress,
                            fetch_mode=fetch_mode, checkpoint_dir=checkpoint_dir,
                            strict_quantiles=strict_quantiles)
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
    parser.add_argument("--fetch-mode", choices=("parallel", "serial", "hourly", "direct"),
                        default="parallel",
                        help=("serial: one variable at a time; hourly: one lead hour "
                              "at a time; direct: no Dask, bounded point reads and optional checkpoints"))
    parser.add_argument("--checkpoint-dir", type=Path, default=None,
                        help="Private resumable staging for direct mode only")
    parser.add_argument("--require-all-quantiles", action="store_true",
                        help="Reject output unless all P10/P50/P90 values exist at every hour")
    parser.add_argument("--output", type=Path, default=None,
                        help="Write native samples locally (never to stdout)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    try:
        request = PointRequest(args.lat, args.lon, args.run_prefix,
                               args.hours, args.p50_only)
        if args.require_all_quantiles and args.p50_only:
            raise ValueError(
                "QUANTILE_REQUEST: cannot require all quantiles with --p50-only"
            )
        result = read_gcs_point(
            request,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
            fetch_mode=args.fetch_mode, checkpoint_dir=args.checkpoint_dir,
            strict_quantiles=args.require_all_quantiles,
        )
        if args.require_all_quantiles:
            require_complete_quantiles(result)
        if args.output is not None:
            path = args.output.expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            from point_checkpoint import atomic_json
            atomic_json(path, result)
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
