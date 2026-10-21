# WeatherNext 3

Minimal, read-only transport for authorized probabilistic forecast statistics stored as Zarr arrays in Google Cloud Storage.

## Hourly forecast horizons

| Run initialization (UTC) | Default request | Maximum available |
|---|---:|---:|
| Synoptic: 00, 06, 12, 18 | **72 h** | **360 h (15 days)** |
| Interim: all other hours | Request up to 48 h explicitly | **48 h** |

The reader enforces the correct horizon **before accessing storage**. It rejects truncated or non-hourly forecasts instead of silently labeling them complete.

For one geographic grid cell, it reads up to **12 native source fields**: temperature, 10 m wind speed, 1-hour downwelling shortwave solar radiation and total cloud cover, each at P10/P50/P90. Optional percentiles absent upstream are reported as missing, never invented. Use \`--p50-only\` to read only the four medians.

## Contents

- \`point_reader.py\` — bounded GCS Zarr transport; selects variables, grid cell and lead times before loading data.
- \`tests/\` — isolated regression tests for 48/72/360 h, percentiles, units, longitude seam, missing data and guarded ingestion.
- \`scheduled_ingest.py\` — opt-in authorized current-run ingestion and readback; disabled unless explicitly approved.
- \`.github/workflows/ingest.yml\` — optional twice-daily schedule, disabled by default.
- \`.github/workflows/verify.yml\` — offline tests on standard public GitHub Actions runners.
- \`docs/transport.md\` — schema, safety and runtime contract.

No weather physics, unit conversion, control decisions or downstream system dependencies are implemented.

## Authentication and privacy

Google's statistics bucket requires an approved identity. Anonymous requests return \`403 AccessDenied\`; this repository does not contain tokens, cloud account details, private site coordinates or destinations. CI uses synthetic arrays only, never reads upstream or publishes forecast artifacts.

## Cost policy

**Default extra expenditure: zero.** The optional scheduled workflow is disabled until an operator explicitly configures approval, Google Workload Identity Federation and a backend-only Supabase secret. No new billing, identities, scheduled cloud resources or automatic uploads are activated by merging this repository.

Google lists the **statistics** bucket as *Requester Pays OFF*. This does not prove every network or processing operation has no cost; use only explicitly approved interactive access for remote tests.

## Optional interactive export

The `scripts/cloudshell_latest.sh` helper searches dated GCS run folders for the newest 00/06/12/18 UTC initialization and verifies its **Zarr v3 `zarr.json` metadata** before requesting one point. Specify 360 to request 15 days. It uses an already authorized Google Cloud Shell identity and writes a local JSON only; it cannot enable scheduling, upload data or configure billing. Remote availability and access still require an interactive check. The discovery logic has offline tests with mocked Google responses.

Example for an arbitrary location: `bash scripts/cloudshell_latest.sh 40 -3 72`. Do not commit generated outputs.

## Existing Supabase forecast-cache adapter

The optional supabase_rows.py prepares offline, point-validated JSON rows for
the already existing public.meteo_pronosticos cache. It does **not** read Google,
connect to Supabase or store secrets. Select the verified plant id and
coordinates explicitly and write into the Git-ignored output/ directory.
It keeps raw upstream units and percentile values; it does not calculate DNI,
DHI, operational control commands or a second WeatherContract.

See docs/transport.md for the exact command, validation rules, database
primary key, authorized-write boundary and current limitations.

## Current status

A historical 360-hour WeatherNext 3 cycle for El Burgo I was extracted with authorized Cloud Shell and verified end-to-end in Supabase: 360 hourly rows, 4,320 native P10/P50/P90 values, zero readback differences. This **does not validate operational forecast accuracy or automatic updates**.

A new opt-in worker, `scheduled_ingest.py`, and the gated `.github/workflows/ingest.yml` prepare the next phase: 24-hour current-run ingestion through approved GitHub OIDC/GCP identity and Supabase backend-only credentials. The automated end-to-end run, permissions, cost and long-horizon persistent staging are **not yet validated or activated**. See [automated ingestion](docs/automated-ingestion.md).

See [transport contract](docs/transport.md) for full usage details and limits.
