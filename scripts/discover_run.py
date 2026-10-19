"""Discover the newest authorized WeatherNext 3 synoptic Zarr v3 run.

Transport-only helper. A day-scoped GCS folder listing followed by an exact
root zarr.json metadata check; no global data-grid downloads or auth tokens.
The only stdout content on success is the selected relative Zarr prefix.

WeatherNext 3 uses Zarr v3, whose metadata file is zarr.json, NOT .zmetadata.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import re
import subprocess
import sys

BUCKET = "weathernext3_statistics_spatial"
BASE = "weathernext_3_0_0_statistics/zarr/2026_to_present"
URI = f"gs://{BUCKET}/{BASE}"
RUN_RE = re.compile(
    re.escape(URI) +
    r"/(?P<run>20\d{6}_(?:00|06|12|18)hr_\d{2}_preds)"
    r"(?:/(?:predictions\.zarr(?:/[^\s]*)?)?)?/?$"
)
MAX_DAY_LISTINGS = 7
MAX_METADATA_CHECKS = 16
DEFAULT_MIN_RUN_AGE_HOURS = 9  # buffer for delayed GCS statistics dissemination


class DiscoveryError(RuntimeError):
    """Categorized transport discovery failure; never includes credentials."""


def _call_gcloud(uri, *, command=subprocess.run):
    try:
        result = command(
            ["gcloud", "storage", "ls", uri],
            capture_output=True, text=True, check=False, timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryError("TIMEOUT: Google Storage listing exceeded 45 seconds") from exc
    except OSError as exc:
        raise DiscoveryError("GCLOUD_UNAVAILABLE: cannot execute gcloud storage") from exc
    if result.returncode == 0:
        return result.stdout
    lower = (result.stderr or "") .lower()
    if any(token in lower for token in (
        "accessdenied", "permission denied", "permission_denied",
        "403", "401", "unauthenticated", "not authorized",
    )):
        raise DiscoveryError(
            "ACCESS_DENIED: current Google Cloud Shell account cannot list/read "
            "the requested statistics object"
        )
    if any(token in lower for token in (
        "not found", "404", "no urls matched", "no matches", "matched no objects",
        "one or more urls matched no objects", "no objects matched", "no objects",
    )):
        return ""
    raise DiscoveryError(
        "GCLOUD_LIST_ERROR: storage listing failed (not proof that forecasts "
        "are missing); check gcloud auth and your project configuration"
    )


def _run_names(listing, day):
    """Extract run folders whether ls returns folders or paths below them."""
    found = set()
    for line in listing.splitlines():
        uri = line.strip().rstrip("/")
        matched = RUN_RE.fullmatch(uri)
        if matched and matched.group("run").startswith(day + "_"):
            found.add(matched.group("run"))
    return sorted(found, reverse=True)


def discover_recent_synoptic(*, now=None, days=3, max_checks=8,
                             min_age_hours=DEFAULT_MIN_RUN_AGE_HOURS,
                             list_uri=_call_gcloud, progress=None):
    """Find a run with verified Zarr v3 root metadata, newest first.

    A successful listing does not prove a forecast has all hourly chunks:
    the point reader must validate the requested fields/time steps afterwards.
    """
    if not isinstance(days, int) or not 1 <= days <= MAX_DAY_LISTINGS:
        raise ValueError(f"days must be 1..{MAX_DAY_LISTINGS}")
    if not isinstance(max_checks, int) or not 1 <= max_checks <= MAX_METADATA_CHECKS:
        raise ValueError(f"max_checks must be 1..{MAX_METADATA_CHECKS}")
    if type(min_age_hours) is not int or not 0 <= min_age_hours <= 24:
        raise ValueError("min_age_hours must be 0..24")
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must have a time zone")
    now = now.astimezone(timezone.utc)
    emit = progress or (lambda msg: None)
    checks = 0
    any_candidate = False
    any_mature_candidate = False
    for day_offset in range(days):
        day = (now - timedelta(days=day_offset)).strftime("%Y%m%d")
        # One shallow, dated prefix listing, not a recursive worldwide scan.
        listing = list_uri(f"{URI}/{day}_*")
        candidates = _run_names(listing, day)
        emit(f"{day}: {len(candidates)} synoptic run folder(s)")
        if candidates:
            any_candidate = True
        for run in candidates:
            run_time = datetime.strptime(
                run[:8] + run[9:11], "%Y%m%d%H"
            ).replace(tzinfo=timezone.utc)
            if run_time > now - timedelta(hours=min_age_hours):
                emit(f"Run {run}: skipped before {min_age_hours}h publication buffer")
                continue
            any_mature_candidate = True
            if checks >= max_checks:
                raise DiscoveryError(
                    f"METADATA_LIMIT: checked {checks} candidates without a valid "
                    "Zarr v3 root; widen only after inspecting the listing"
                )
            checks += 1
            prefix = f"{BASE}/{run}/predictions.zarr"
            metadata = f"gs://{BUCKET}/{prefix}/zarr.json"
            existing = list_uri(metadata)
            # This is an exact object check, not a prediction that it exists.
            if metadata in existing.splitlines():
                emit(f"Root metadata verified for run {run}")
                return prefix
            emit(f"Run {run}: no readable root zarr.json; trying older run")
    if any_candidate and not any_mature_candidate:
        raise DiscoveryError(
            f"NO_MATURE_RUNS: observed run folders, but none is at least "
            f"{min_age_hours}h old; this is a publication precaution"
        )
    if any_candidate:
        raise DiscoveryError(
            "NO_VERIFIED_METADATA: mature run folders exist but no readable root "
            "zarr.json was found in the bounded search"
        )
    raise DiscoveryError(
        f"NO_RECENT_RUN_FOLDERS: no 00/06/12/18 UTC run folder was found "
        f"under the dated 2026_to_present prefixes checked ({days} UTC days). "
        "This does not establish that an older run is unavailable"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--max-metadata-checks", type=int, default=8)
    parser.add_argument("--min-age-hours", type=int, default=DEFAULT_MIN_RUN_AGE_HOURS,
                        help="precautionary age buffer for statistics publication (default 9h)")
    args = parser.parse_args(argv)
    try:
        prefix = discover_recent_synoptic(
            days=args.lookback_days, max_checks=args.max_metadata_checks,
            min_age_hours=args.min_age_hours,
            progress=lambda msg: print(msg, file=sys.stderr, flush=True),
        )
    except (DiscoveryError, ValueError) as exc:
        print(f"WeatherNext discovery: {exc}", file=sys.stderr)
        return 1
    print(prefix)  # only URI prefix; never a token or a forecast data value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
