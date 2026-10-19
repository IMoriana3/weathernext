#!/usr/bin/env bash
# Interactive, explicitly initiated Zarr point export.
# No cron, no unattended token, no external data upload or billing activation.
set -euo pipefail

if [[ "${1:-}" == "--help" || "$#" -lt 2 || "$#" -gt 3 ]]; then
  echo "Usage: bash cloudshell_latest.sh LATITUDE LONGITUDE [HOURS=72]" >&2
  echo "Run interactively in an authorized Google Cloud Shell session." >&2
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
for required in gcloud python3 git; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Missing required Cloud Shell tool: $required" >&2; exit 2
  fi
done

BUCKET="weathernext3_statistics_spatial"
BASE="weathernext_3_0_0_statistics/zarr/2026_to_present"
FOUND=""
echo "Finding the most recent available 00/06/12/18 UTC run (up to 3 days)..."
for days_back in 0 1 2; do
  day="$(date -u -d "$days_back days ago" +%Y%m%d)"
  # Day-scoped listing avoids scanning the worldwide archive.
  listing="$(gcloud storage ls "gs://$BUCKET/$BASE/${day}_*hr_*_preds/predictions.zarr/.zmetadata" 2>/dev/null || true)"
  if [[ -z "$listing" ]]; then continue; fi
  while IFS= read -r line; do
    if [[ "$line" =~ ^gs://$BUCKET/$BASE/[0-9]{8}_(00|06|12|18)hr_[0-9]{2}_preds/predictions\.zarr/\.zmetadata$ ]]; then
      prefix="${line#gs://$BUCKET/}"
      prefix="${prefix%/.zmetadata}"
      if [[ -z "$FOUND" || "$prefix" > "$FOUND" ]]; then FOUND="$prefix"; fi
    fi
  done <<< "$listing"
  if [[ -n "$FOUND" ]]; then break; fi
done

if [[ -z "$FOUND" ]]; then
  echo "No recent synoptic Zarr metadata found with the current Google account." >&2
  echo "Check Google account permissions or whether the model has published a run." >&2
  exit 1
fi
echo "Selected an authorized-format synoptic run."

ROOT="$HOME/.cache/wn3-readonly-transport"
if [[ ! -d "$ROOT/.git" ]]; then
  echo "Obtaining the public read-only point reader..."
  git clone -q --depth 1 --branch main https://github.com/IMoriana3/weathernext.git "$ROOT"
else
  echo "Updating the point reader..."
  git -C "$ROOT" pull -q --ff-only
fi
VENV="$HOME/.cache/wn3-readonly-venv"
if [[ ! -x "$VENV/bin/python" ]]; then python3 -m venv "$VENV"; fi
echo "Installing/verifying pinned Zarr dependencies..."
"$VENV/bin/python" -m pip --disable-pip-version-check install -q -r "$ROOT/requirements.txt"

OUTPUT="$HOME/weathernext3-$(date -u +%Y%m%dT%H%M%SZ).json"
echo "Reading one grid cell with $HOURS hours (at most 12 native fields)..."
"$VENV/bin/python" "$ROOT/point_reader.py" \
  --lat "$LAT" --lon "$LON" --run-prefix "$FOUND" --hours "$HOURS" \
  --output "$OUTPUT"
echo "FILE READY: $OUTPUT"
echo "Use Cloud Shell's Download file command to save it to your computer."
echo "Do not commit the generated file to a public repository."
