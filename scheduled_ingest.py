"""Opt-in, single-run WeatherNext 3 ingestion into the existing Supabase cache.

No scheduler, weather model, field measurements, conversions or operational
control logic lives here. The authorized runner owns its schedule, ADC,
approved coordinates and secret. Never log raw forecast values or credentials.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib import error, parse, request

from point_reader import (
    FIELDS, VARIABLES, PointRequest, read_gcs_point, require_complete_quantiles,
    run_limits,
)
from scripts.discover_run import discover_recent_synoptic
from supabase_rows import build_rows

SOURCE = "weathernext3"
MAX_RUN_AGE_HOURS = 42
MAX_RESPONSE_BYTES = 3_000_000
EXPECTED_UNITS = {
    "temperature_2m": "K",
    "wind_speed_10m": "m s**-1",
    "surface_solar_radiation_downwards_1hr": "J m**-2",
    "total_cloud_cover": "(0 - 1)",
}


class IngestionBlocked(RuntimeError):
    """Failure category with no tokens, URLs or raw server responses."""


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _utc(value):
    if not isinstance(value, str):
        raise IngestionBlocked("INVALID_TIME")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestionBlocked("INVALID_TIME") from exc
    if stamp.tzinfo is None:
        raise IngestionBlocked("MISSING_TIMEZONE")
    return stamp.astimezone(timezone.utc)


def _strict_native_rows(payload, *, planta_id, lat, lon, hours):
    """Canonical pure adapter followed by production-only percentile gate."""
    require_complete_quantiles(payload)
    if payload.get("returned_hours") != hours:
        raise IngestionBlocked("INCOMPLETE_HORIZON")
    for variable in VARIABLES:
        for p in ("p10", "p50", "p90"):
            key = f"{variable}_{p}"
            if payload.get("raw_units", {}).get(key) != EXPECTED_UNITS[variable]:
                raise IngestionBlocked("UNRECOGNIZED_SOURCE_UNITS")
    rows = build_rows(payload, planta_id=planta_id, site_lat=lat, site_lon=lon)
    if len(rows) != hours:
        raise IngestionBlocked("INCOMPLETE_HORIZON")
    for row in rows:
        for variable in VARIABLES:
            low = row["p10"][variable]
            mid = row["p50"][variable]
            high = row["p90"][variable]
            if low is None or mid is None or high is None or not low <= mid <= high:
                raise IngestionBlocked("INVALID_NATIVE_PERCENTILE_ORDER")
            if variable == "total_cloud_cover" and not 0 <= low <= high <= 1:
                raise IngestionBlocked("INVALID_NATIVE_CLOUD_FRACTION")
            if variable in ("wind_speed_10m", "surface_solar_radiation_downwards_1hr") and low < 0:
                raise IngestionBlocked("INVALID_NATIVE_NONNEGATIVE_FIELD")
    return rows


def _checked_run(prefix, *, now, allow_age_hours=MAX_RUN_AGE_HOURS):
    initialized, max_hours, run_class = run_limits(prefix)
    if run_class != "synoptic":
        raise IngestionBlocked("AUTOMATION_REQUIRES_SYNOPTIC_RUN")
    age = now - initialized
    if age < timedelta(hours=9) or age > timedelta(hours=allow_age_hours):
        raise IngestionBlocked("RUN_NOT_MATURE_OR_STALE")
    return initialized, max_hours


def _same_stored_row(actual, wanted):
    return (
        actual.get("lead_hour") == wanted["lead_hour"]
        and _utc(actual.get("valid_time")) == _utc(wanted["valid_time"])
        and actual.get("p10") == wanted["p10"]
        and actual.get("p50") == wanted["p50"]
        and actual.get("p90") == wanted["p90"]
        and actual.get("provenance") == wanted["provenance"]
    )


def _compare_existing(saved, expected):
    """Read/check every existing lead; never silently overwrite a divergence."""
    if not isinstance(saved, list) or len(saved) > 360:
        raise IngestionBlocked("INVALID_EXISTING_SNAPSHOT")
    by_lead = {}
    for row in saved:
        lead = row.get("lead_hour")
        if type(lead) is not int or not 1 <= lead <= 360 or lead in by_lead:
            raise IngestionBlocked("DUPLICATE_OR_INVALID_EXISTING_LEAD")
        by_lead[lead] = row
    missing = []
    for row in expected:
        seen = by_lead.get(row["lead_hour"])
        if seen is None:
            missing.append(row)
        elif not _same_stored_row(seen, row):
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
    return missing


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IngestionBlocked("SUPABASE_REDIRECT_REFUSED")


class ProtectedSupabase:
    """Limited PostgREST surface, new backend-only sb_secret API key."""

    def __init__(self, url, key, *, transport=None):
        if not isinstance(url, str) or not re.fullmatch(
                r"https://[a-z0-9-]+\.supabase\.co/?", url):
            raise IngestionBlocked("SUPABASE_URL_NOT_APPROVED")
        if not isinstance(key, str) or not key.startswith("sb_secret_") or len(key) < 20:
            raise IngestionBlocked("SUPABASE_REQUIRES_NEW_BACKEND_SECRET_KEY")
        self.base = url.rstrip("/") + "/rest/v1/"
        self.key = key
        self.transport = transport or self._network

    @staticmethod
    def _network(method, url, headers, body):
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.build_opener(NoRedirect()).open(req, timeout=60) as src:
                status = src.status
                response = src.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            raise IngestionBlocked(f"SUPABASE_HTTP_{exc.code}") from None
        except (error.URLError, TimeoutError, OSError):
            raise IngestionBlocked("SUPABASE_NETWORK_FAILURE") from None
        if len(response) > MAX_RESPONSE_BYTES:
            raise IngestionBlocked("SUPABASE_RESPONSE_TOO_LARGE")
        return status, response

    def _call(self, method, table, params, *, body=None, prefer=None):
        if table not in ("plantas", "meteo_pronosticos"):
            raise IngestionBlocked("UNAPPROVED_TABLE")
        suffix = parse.urlencode(params, safe=",")
        url = self.base + table + ("?" + suffix if suffix else "")
        headers = {
            "apikey": self.key,
            "Accept": "application/json",
            "User-Agent": "weathernext3-ingest/1.0",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
            headers["Prefer"] = prefer or "resolution=ignore-duplicates,return=minimal"
            headers["Content-Profile"] = "public"
        else:
            headers["Accept-Profile"] = "public"
        status, response = self.transport(method, url, headers, body)
        if status not in ((200,) if method == "GET" else (200, 201, 204)):
            raise IngestionBlocked(f"SUPABASE_UNEXPECTED_HTTP_{status}")
        if method == "POST":
            return None
        if len(response) > MAX_RESPONSE_BYTES:
            raise IngestionBlocked("SUPABASE_RESPONSE_TOO_LARGE")
        try:
            data = json.loads(response)
        except (UnicodeDecodeError, ValueError) as exc:
            raise IngestionBlocked("SUPABASE_INVALID_JSON") from exc
        if not isinstance(data, list):
            raise IngestionBlocked("SUPABASE_UNEXPECTED_RESULT")
        return data

    def verify_plant(self, planta_id, site_code):
        items = self._call("GET", "plantas", [
            ("select", "id,code,proyecto"),
            ("id", f"eq.{planta_id}"),
            ("limit", "2"),
        ])
        if (len(items) != 1 or items[0].get("id") != planta_id
                or items[0].get("code") != site_code):
            raise IngestionBlocked("SUPABASE_PLANT_IDENTITY_MISMATCH")

    def existing_run(self, planta_id, init_time):
        return self._call("GET", "meteo_pronosticos", [
            ("select", "lead_hour,valid_time,p10,p50,p90,provenance"),
            ("planta_id", f"eq.{planta_id}"),
            ("source_id", f"eq.{SOURCE}"),
            ("init_time", f"eq.{init_time}"),
            ("order", "lead_hour.asc"),
            ("limit", "361"),
        ])

    def insert_missing(self, rows):
        if not rows:
            return
        if len(rows) > 360 or any(
                row.get("source_id") != SOURCE for row in rows):
            raise IngestionBlocked("UNAPPROVED_BATCH")
        self._call("POST", "meteo_pronosticos", [
            ("on_conflict", "planta_id,source_id,init_time,valid_time"),
        ], body=_json(rows))



def _verified_complete_run(saved, *, hours, prefix, initialized, lat, lon):
    """Avoid re-downloading an already-complete source run on repeat schedule.

    This is an optimization, not a substitute for source verification during
    first publication. Only verified raw transport rows of THIS run/cell and
    the exact expected percentile/units envelope may be reused.
    """
    import math

    if not isinstance(saved, list) or len(saved) > 360:
        raise IngestionBlocked("INVALID_EXISTING_SNAPSHOT")
    by_lead = {}
    for row in saved:
        lead = row.get("lead_hour")
        if type(lead) is not int or not 1 <= lead <= 360 or lead in by_lead:
            raise IngestionBlocked("DUPLICATE_OR_INVALID_EXISTING_LEAD")
        by_lead[lead] = row

    if any(i not in by_lead for i in range(1, hours + 1)):
        return False

    for lead in range(1, hours + 1):
        row = by_lead[lead]
        if _utc(row.get("valid_time")) != initialized + timedelta(hours=lead):
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
        p = row.get("provenance")
        if not isinstance(p, dict):
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
        point = p.get("requested_grid_point")
        cell = p.get("selected_grid_point")
        if not isinstance(point, dict) or not isinstance(cell, dict):
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
        try:
            site_matches = (
                abs(point["lat"] - lat) <= 0.00001
                and abs((point["lon"] - lon + 180) % 360 - 180) <= 0.00001
                and abs(cell["lat"] - lat) <= 0.11
                and abs((cell["lon_360"] - lon + 180) % 360 - 180) <= 0.11
            )
        except (TypeError, KeyError):
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN") from None
        if not site_matches:
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
        if (
            p.get("run_prefix") != prefix
            or p.get("kind") != "raw_model_forecast_not_field_measurement"
            or p.get("transport_schema") != "weathernext3-zarr-point-v2"
            or p.get("bucket") != "weathernext3_statistics_spatial"
            or p.get("unit_conversions_applied") is not False
            or p.get("weather_contract_built") is not False
            or p.get("operational_control_permitted") is not False
            or p.get("available_fields") != list(FIELDS)
            or p.get("requested_fields") != list(FIELDS)
            or p.get("missing_upstream_fields") != []
        ):
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
        units = p.get("raw_units")
        if not isinstance(units, dict) or len(units) != len(FIELDS) or any(
                units.get(f"{variable}_{quantile}") != expected
                for variable, expected in EXPECTED_UNITS.items()
                for quantile in ("p10", "p50", "p90")
        ):
            raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
        for quantile in ("p10", "p50", "p90"):
            values = row.get(quantile)
            if not isinstance(values, dict) or set(values) != set(VARIABLES):
                raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
            if any(type(value) not in (float, int) or not math.isfinite(value)
                   for value in values.values()):
                raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
        for variable in VARIABLES:
            lo, mid, hi = (row[q][variable] for q in ("p10", "p50", "p90"))
            if not lo <= mid <= hi:
                raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
            if variable == "total_cloud_cover" and not 0 <= lo <= hi <= 1:
                raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
            if variable in ("wind_speed_10m", "surface_solar_radiation_downwards_1hr") and lo < 0:
                raise IngestionBlocked("FORECAST_CONFLICT_EXISTING_RUN")
    return True

def publish_rows(client, rows, *, init_time):
    """Preflight, insert only missing rows, then read back and verify all.

    Concurrent ingesters cannot change existing rows: ignore-conflict writes
    only new keys. If another writer inserted divergent rows, readback fails.
    """
    if not rows or len(rows) > 360:
        raise IngestionBlocked("INVALID_BATCH")
    plant = rows[0]["planta_id"]
    if any(r["planta_id"] != plant or r["init_time"] != init_time
           or r["source_id"] != SOURCE for r in rows):
        raise IngestionBlocked("MIXED_BATCH")
    before = client.existing_run(plant, init_time)
    missing = _compare_existing(before, rows)
    client.insert_missing(missing)
    after = client.existing_run(plant, init_time)
    if _compare_existing(after, rows):
        raise IngestionBlocked("SUPABASE_READBACK_INCOMPLETE")
    return len(missing)


def run_once(*, planta_id, site_code, lat, lon, hours, prefix, now,
             read_point=read_gcs_point, client=None, checkpoint_dir=None,
             publish=False, progress=None):
    if type(planta_id) is not int or planta_id <= 0 or not site_code:
        raise IngestionBlocked("INVALID_PLANT_CONFIGURATION")
    if type(hours) is not int or hours not in (24, 72, 360):
        raise IngestionBlocked("UNAPPROVED_HORIZON")
    initialized, maximum = _checked_run(prefix, now=now)
    if hours > maximum:
        raise IngestionBlocked("RUN_TOO_SHORT")
    emit = progress or (lambda _: None)
    if publish:
        if client is None:
            raise IngestionBlocked("NO_AUTHORIZED_SUPABASE_CLIENT")
        client.verify_plant(planta_id, site_code)
        current = client.existing_run(
            planta_id, initialized.isoformat().replace("+00:00", "Z")
        )
        if _verified_complete_run(
            current, hours=hours, prefix=prefix,
            initialized=initialized, lat=lat, lon=lon,
        ):
            emit = progress or (lambda _: None)
            emit(f"EXISTING_RUN_VERIFIED_NO_FETCH: {hours} complete hours")
            return 0
    req = PointRequest(lat, lon, prefix, hours, False)
    emit(f"RUN_ACCEPTED: horizon={hours}h init_utc={initialized.isoformat()}")
    # Deliberately read full source before publishing; a partial checkpoint
    # is NOT an approved forecast.
    forecast = read_point(
        req, progress=emit, fetch_mode="direct", checkpoint_dir=checkpoint_dir,
        strict_quantiles=True,
    )
    rows = _strict_native_rows(
        forecast, planta_id=planta_id, lat=lat, lon=lon, hours=hours,
    )
    if not publish:
        emit(f"VALIDATED_NOT_PUBLISHED: rows={len(rows)}")
        return 0
    inserted = publish_rows(client, rows, init_time=forecast["init_time_utc"])
    emit(f"VERIFIED_AFTER_WRITE: rows={len(rows)} newly_inserted={inserted}")
    return inserted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planta-id", required=True, type=int)
    parser.add_argument("--site-code", required=True)
    parser.add_argument("--lat", required=True, type=float)
    parser.add_argument("--lon", required=True, type=float)
    parser.add_argument("--hours", type=int, choices=(24, 72, 360), default=24)
    parser.add_argument("--checkpoint-dir", type=Path, default=None)
    parser.add_argument("--publish", action="store_true",
                        help="Explicit opt-in to privileged Supabase writes")
    parser.add_argument("--run-prefix", default=None,
                        help="Exact run only for operator-initiated dry runs")
    args = parser.parse_args(argv)
    try:
        if args.publish and os.environ.get("WN3_INGESTION_APPROVED") != "true":
            raise IngestionBlocked("INGESTION_NOT_APPROVED")
        if args.publish and args.run_prefix:
            raise IngestionBlocked("PUBLISH_REJECTS_MANUAL_HISTORICAL_RUN")
        now = datetime.now(timezone.utc)
        prefix = args.run_prefix or discover_recent_synoptic(
            now=now, days=3, min_age_hours=9,
            progress=lambda message: print(message, file=sys.stderr),
        )
        client = None
        if args.publish:
            client = ProtectedSupabase(
                os.environ.get("WN3_SUPABASE_URL", ""),
                os.environ.get("WN3_SUPABASE_SECRET_KEY", ""),
            )
        run_once(
            planta_id=args.planta_id, site_code=args.site_code,
            lat=args.lat, lon=args.lon, hours=args.hours, prefix=prefix,
            now=now, client=client, publish=args.publish,
            checkpoint_dir=args.checkpoint_dir,
            progress=lambda message: print(message, flush=True),
        )
    except (IngestionBlocked, ValueError) as exc:
        # These messages are our own fixed categories or pure validation.
        print(f"INGEST_BLOCKED: {str(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Google SDK exception strings can contain sensitive identifiers.
        print(f"INGEST_FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
