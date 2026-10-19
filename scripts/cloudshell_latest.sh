#!/usr/bin/env bash
# Interactive, explicitly initiated Zarr point export.
# No cron, no unattended token, no external data upload or billing activation.
set -euo pipefail

if [[ "${1:-}" == "--help" || "$#" -lt 2 || "$#" -gt 4 ]]; then
  echo "Usage: bash cloudshell_latest.sh LATITUDE LONGITUDE [HOURS=72] [EXACT_RUN_PREFIX]" >&2
  echo "Run interactively in an authorized Google Cloud Shell session." >&2
  echo "No override: newest Zarr v3 run at least 9h old. Exact prefix: historical replay." >&2
  exit 2
fi

LAT="$1"
LON="$2"
HOURS="${3:-72}"
if ! [[ "$HOURS" =~ ^[0-9]+$ ]] || (( ${#HOURS} > 3 )); then
  echo "HOURS must be a number from 1 to 360" >&2; exit 2
fi
HOURS=$((10#$HOURS))
if (( HOURS < 1 || HOURS > 360 )); then
  echo "HOURS must be from 1 to 360" >&2; exit 2
fi
for required in gcloud python3 git timeout; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Missing required Cloud Shell tool: $required" >&2; exit 2
  fi
done

# The old search was a BUG: WeatherNext 3 is Zarr v3 and uses zarr.json,
# not the legacy .zmetadata. Discovery is isolated for deterministic tests.
ROOT="$HOME/.cache/wn3-readonly-transport"
if [[ ! -d "$ROOT/.git" ]]; then
  echo "[1/4] Obtaining the public, read-only transport..."
  git clone -q --depth 1 --branch main https://github.com/IMoriana3/weathernext.git "$ROOT"
else
  echo "[1/4] Updating public, read-only transport..."
  git -C "$ROOT" pull -q --ff-only
fi
FOUND="${4:-}"
if [[ -z "$FOUND" ]]; then
  echo "[2/4] Looking for a mature 00/06/12/18 UTC Zarr v3 run (3 UTC days; 9h buffer)..."
  if ! FOUND="$(python3 "$ROOT/scripts/discover_run.py" --lookback-days 3 --min-age-hours 9)"; then
    echo "No data was exported. Check the diagnostic above; no run was guessed." >&2
    exit 1
  fi
else
  echo "[2/4] Using explicitly selected run (no automatic discovery)."
fi
if ! [[ "$FOUND" =~ ^weathernext_3_0_0_statistics/zarr/2026_to_present/[0-9]{8}_[0-9]{2}hr_[0-9]{2}_preds/predictions\.zarr$ ]]; then
  echo "Invalid run prefix format; refusing to read an unknown path." >&2
  exit 2
fi
echo "Selected run: ${FOUND##*/2026_to_present/}"
VENV="$HOME/.cache/wn3-readonly-venv"
if [[ ! -x "$VENV/bin/python" ]]; then python3 -m venv "$VENV"; fi
echo "[3/4] Installing/verifying pinned Zarr dependencies..."
"$VENV/bin/python" -m pip --disable-pip-version-check install -q -r "$ROOT/requirements.txt"

READ_TIMEOUT_SECONDS="${WN3_READ_TIMEOUT_SECONDS:-300}"
if ! [[ "$READ_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || (( ${#READ_TIMEOUT_SECONDS} > 4 )); then
  echo "WN3_READ_TIMEOUT_SECONDS must be an integer from 30 to 900" >&2; exit 2
fi
READ_TIMEOUT_SECONDS=$((10#$READ_TIMEOUT_SECONDS))
if (( READ_TIMEOUT_SECONDS < 30 || READ_TIMEOUT_SECONDS > 900 )); then
  echo "WN3_READ_TIMEOUT_SECONDS must be from 30 to 900" >&2; exit 2
fi

# Explicitly opt-in to a low-I/O diagnostic. The normal export stays
# identical unless these bounded environment switches are set by the operator.
EXTRA_READER_ARGS=()
case "${WN3_FETCH_MODE:-parallel}" in
  parallel) ;;
  serial) EXTRA_READER_ARGS+=(--fetch-mode serial) ;;
  hourly) EXTRA_READER_ARGS+=(--fetch-mode hourly) ;;
  *) echo "WN3_FETCH_MODE must be parallel, serial or hourly" >&2; exit 2 ;;
esac
case "${WN3_P50_ONLY:-0}" in
  0) ;;
  1) EXTRA_READER_ARGS+=(--p50-only) ;;
  *) echo "WN3_P50_ONLY must be 0 or 1" >&2; exit 2 ;;
esac

OUTPUT="$HOME/weathernext3-$(date -u +%Y%m%dT%H%M%SZ).json"
echo "[4/4] Reading one grid cell with $HOURS hours (at most 12 native fields; complete P50 required)..."
echo "Diagnostic: metadata/chunk stages; bounded to ${READ_TIMEOUT_SECONDS}s without publishing partial data."
if timeout --signal=TERM --kill-after=10s "${READ_TIMEOUT_SECONDS}s" \
    "$VENV/bin/python" "$ROOT/point_reader.py" \
      --lat "$LAT" --lon "$LON" --run-prefix "$FOUND" --hours "$HOURS" \
      "${EXTRA_READER_ARGS[@]}" --output "$OUTPUT"; then
  :
else
  STATUS=$?
  if (( STATUS == 124 || STATUS == 137 )); then
    echo "READ_TIMEOUT: remote GCS metadata or chunks did not complete; no forecast was exported." >&2
    echo "Retry with an older verified run or diagnose the stage shown above." >&2
  fi
  exit "$STATUS"
fi
echo "FILE READY: $OUTPUT"
echo "Use Cloud Shell\'s Download file command to save it to your computer."
echo "Do not commit the generated file to a public repository."
