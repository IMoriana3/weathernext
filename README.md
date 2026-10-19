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
- \`tests/\` — isolated regression tests for 48/72/360 h, percentiles, units, longitude seam and missing data.
- \`.github/workflows/verify.yml\` — offline tests on standard public GitHub Actions runners.
- \`docs/transport.md\` — schema, safety and runtime contract.

No weather physics, unit conversion, control decisions or downstream system dependencies are implemented.

## Authentication and privacy

Google's statistics bucket requires an approved identity. Anonymous requests return \`403 AccessDenied\`; this repository does not contain tokens, cloud account details, private site coordinates or destinations. CI uses synthetic arrays only, never reads upstream or publishes forecast artifacts.

## Cost policy

**Additional expenditure: zero.** No billing activation, paid services, scheduled extraction or automatic uploads.

Google lists the **statistics** bucket as *Requester Pays OFF*. This does not prove every network or processing operation has no cost; use only explicitly approved interactive access for remote tests.

## Optional interactive export

The `scripts/cloudshell_latest.sh` helper can find a recent upstream synoptic run for **an already authorized Google Cloud Shell identity** and save one point's 72-hour JSON locally. Specify 360 to request 15 days. It does not enable scheduling, upload data or configure cloud billing. Real upstream discovery is not part of offline CI.

Example for an arbitrary location: `bash scripts/cloudshell_latest.sh 40 -3 72`. Do not commit generated outputs.

## Current status

Source and offline tests are integrated. An authenticated 360-hour live read and an unattended access method are **not yet validated**. The repository does not fetch data automatically.

See [transport contract](docs/transport.md) for full usage details and limits.
