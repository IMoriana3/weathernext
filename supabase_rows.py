"""Validate WeatherNext point JSON and prepare rows for the EXISTING Supabase cache.

Pure, local transport adapter. No Google/Supabase requests, credentials,
physical conversions, DNI/DHI synthesis, or WeatherContract implementation.
The canonical consumer WeatherContract remains in SolarGPTfull.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from point_reader import BUCKET, FIELDS, P50_FIELDS, VARIABLES, run_limits

SOURCE_ID = "weathernext3"
TRANSPORT_SCHEMA = "weathernext3-zarr-point-v2"
FORECAST_KIND = "raw_model_forecast_not_field_measurement"
SUPABASE_TABLE = "public.meteo_pronosticos"
UPSERT_KEY = ("planta_id", "source_id", "init_time", "valid_time")


def _finite(value, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a numeric value")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside its permitted range")
    return float(value)


def _utc(value, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be an explicit UTC ISO timestamp ending Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid UTC timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return parsed.astimezone(timezone.utc)


def _lon_error(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _str_list(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError(f"{label} must be an array of strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} has duplicate fields")
    return value


def build_rows(payload: dict, *, planta_id: int, site_lat: float,
               site_lon: float) -> list[dict]:
    """Map one bounded native point to database rows; never convert physics."""
    if type(planta_id) is not int or planta_id <= 0:
        raise ValueError("planta_id must be a positive Supabase plantas.id")
    lat = _finite(site_lat, "site_lat", -90, 90)
    lon = _finite(site_lon, "site_lon", -180, 180)
    if not isinstance(payload, dict):
        raise ValueError("input must be a WeatherNext point JSON object")
    for key, expected in (
        ("transport_schema", TRANSPORT_SCHEMA),
        ("type", FORECAST_KIND),
        ("source", "Google WeatherNext 3 Statistics"),
        ("bucket", BUCKET),
        ("unit_conversions_applied", False),
    ):
        if payload.get(key) != expected or (
            key == "unit_conversions_applied"
            and payload.get(key) is not False
        ):
            raise ValueError(f"unexpected {key}; refusing unknown transport")

    prefix = payload.get("run_prefix")
    initialized, max_hours, run_class = run_limits(prefix)
    if payload.get("run_class") != run_class or payload.get("run_max_hours") != max_hours:
        raise ValueError("declared run class/horizon does not match prefix")
    if _utc(payload.get("init_time_utc"), "init_time_utc") != initialized:
        raise ValueError("init_time_utc does not match declared GCS run")

    hours = payload.get("requested_hours")
    if type(hours) is not int or not 1 <= hours <= max_hours:
        raise ValueError("requested_hours exceeds declared run horizon")
    if type(payload.get("returned_hours")) is not int or payload["returned_hours"] != hours:
        raise ValueError("returned_hours must equal requested_hours")
    samples = payload.get("samples")
    if not isinstance(samples, list) or len(samples) != hours:
        raise ValueError("incomplete sample count")

    request = payload.get("requested_grid_point")
    selected = payload.get("selected_grid_point")
    if not isinstance(request, dict) or not isinstance(selected, dict):
        raise ValueError("requested and selected coordinates must be present")
    request_lat = _finite(request.get("lat"), "requested latitude", -90, 90)
    request_lon = _finite(request.get("lon"), "requested longitude", -180, 180)
    if abs(request_lat - lat) > 0.00001 or _lon_error(request_lon, lon) > 0.00001:
        raise ValueError("FORECAST_SITE_MISMATCH: point is not for the specified plant")
    selected_lat = _finite(selected.get("lat"), "selected latitude", -90, 90)
    selected_lon = _finite(selected.get("lon_360"), "selected longitude", 0, 360)
    if abs(selected_lat - lat) > 0.11 or _lon_error(selected_lon, lon) > 0.11:
        raise ValueError("FORECAST_GRID_MISMATCH: selected grid is too far from plant")

    requested = _str_list(payload.get("requested_fields"), "requested_fields")
    available = _str_list(payload.get("available_fields"), "available_fields")
    missing = _str_list(payload.get("missing_upstream_fields"), "missing_upstream_fields")
    if requested not in (list(FIELDS), list(P50_FIELDS)):
        raise ValueError("unsupported field set; cannot infer other weather variables")
    if set(available) | set(missing) != set(requested) or set(available) & set(missing):
        raise ValueError("available/missing field inventory is inconsistent")
    if not set(P50_FIELDS).issubset(available):
        raise ValueError("required P50 fields unavailable")
    raw_units = payload.get("raw_units")
    if not isinstance(raw_units, dict) or set(raw_units) != set(available):
        raise ValueError("raw_units do not match upstream available fields")
    if any(not isinstance(unit, str) or not unit for unit in raw_units.values()):
        raise ValueError("raw_units must contain unchanged source units")

    provenance = {
        "kind": FORECAST_KIND,
        "transport_schema": TRANSPORT_SCHEMA,
        "bucket": BUCKET,
        "run_prefix": prefix,
        "run_class": run_class,
        "requested_grid_point": request,
        "selected_grid_point": selected,
        "requested_fields": requested,
        "available_fields": available,
        "missing_upstream_fields": missing,
        "raw_units": raw_units,
        "unit_conversions_applied": False,
        "weather_contract_built": False,
        "operational_control_permitted": False,
    }
    rows = []
    has_temp = False
    has_wind = False
    for expected_lead, sample in enumerate(samples, start=1):
        if not isinstance(sample, dict) or type(sample.get("lead_hour")) is not int:
            raise ValueError("invalid hourly sample")
        if sample["lead_hour"] != expected_lead:
            raise ValueError("missing, duplicate or out-of-order lead_hour")
        valid = _utc(sample.get("valid_time_utc"), "valid_time_utc")
        if valid != initialized + timedelta(hours=expected_lead):
            raise ValueError("valid_time does not equal init_time + lead_hour")
        values = sample.get("native_values")
        if not isinstance(values, dict) or set(values) != set(requested):
            raise ValueError("native_values do not match the source field inventory")
        for name, value in values.items():
            if name in missing and value is not None:
                raise ValueError("unavailable upstream field cannot contain a value")
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"invalid native numeric value in {name}")
        has_temp |= values["temperature_2m_p50"] is not None
        has_wind |= values["wind_speed_10m_p50"] is not None
        quantiles = {
            percentile: {
                variable: values.get(f"{variable}_{percentile}")
                for variable in VARIABLES
            }
            for percentile in ("p10", "p50", "p90")
        }
        rows.append({
            "planta_id": planta_id,
            "source_id": SOURCE_ID,
            "init_time": payload["init_time_utc"],
            "valid_time": sample["valid_time_utc"],
            "lead_hour": expected_lead,
            **quantiles,
            "provenance": provenance,
        })
    if not has_temp or not has_wind:
        raise ValueError("no finite temperature or wind median over requested hours")
    return rows


def _reject_json_constant(value):
    raise ValueError(f"non-standard JSON numeric literal {value}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--planta-id", type=int, required=True)
    parser.add_argument("--site-lat", type=float, required=True)
    parser.add_argument("--site-lon", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    temp_path = None
    try:
        input_path = args.input.expanduser().resolve()
        output = args.output.expanduser().resolve()
        if input_path == output or output.exists():
            raise ValueError("output must be a new file, not an overwrite")
        with input_path.open(encoding="utf-8") as fh:
            payload = json.load(fh, parse_constant=_reject_json_constant)
        rows = build_rows(payload, planta_id=args.planta_id,
                          site_lat=args.site_lat, site_lon=args.site_lon)
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            dir=output.parent, prefix=".wn3-", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2, allow_nan=False)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, output)
        temp_path = None
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(f"Supabase row preparation blocked: {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    print(f"Prepared {len(rows)} forecast-only rows for {SUPABASE_TABLE}; no upload performed.")
    print(f"Local output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
