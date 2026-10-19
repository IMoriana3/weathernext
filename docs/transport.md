# Native WeatherNext 3 Zarr point transport · version 2

This package moves **existing hourly probabilistic statistics** from an authorized Google Cloud Storage Zarr run into a local native-value JSON file. It does not calculate forecasts, convert physical units, infer sensor measurements, issue control commands or implement a separate meteorological model.

## Temporal contract

| Run class | Init UTC | Maximum lead time | Default |
|---|---|---:|---:|
| Synoptic | 00, 06, 12, 18 | **360 h (15 days)** | **72 h** |
| Interim | 01–05, 07–11, 13–17, 19–23 | **48 h** | Specify 48 h or fewer |

Forecast steps must be **hourly and complete**, from hour 1 to the requested horizon. The reader never extrapolates, clips or interpolates missing hours. It rejects a mismatched initialization time, missing steps, duplicate steps and requests longer than the published run class. The official [GCS Statistics documentation](https://developers.google.com/weathernext/guides/gcs) confirms the two horizons and the continuous hourly lead_time coordinate.

## Spatial and field contract

Input: WGS84 latitude (−90 to 90), longitude (−180 to 180), official run path, requested hours, optional P50-only mode.

A single nearest upstream 0.1-degree grid cell is selected **before loading the native arrays**. Longitude is mapped to the 0..360 convention and selected using circular distance, including the Greenwich 0/360 seam.

The four source variable stems are temperature_2m, wind_speed_10m, surface_solar_radiation_downwards_1hr, and total_cloud_cover.

For each, read precomputed _p10, _p50, _p90 when present, without calculating percentiles. Four P50 fields are compulsory. If an optional percentile is absent, report it in missing_upstream_fields and return null for that field: never make one up. Native numeric zero remains zero.

Upper bound: 360 hourly values × 12 scalar native fields = 4,320 numerical values for one point (not an entire global grid). P50-only mode reads 4 fields.

## Output contract

Schema ID: **weathernext3-zarr-point-v2**

- type: raw_model_forecast_not_field_measurement
- source, bucket, run_prefix, init_time_utc
- run_class, run_max_hours, requested_hours, returned_hours
- requested_fields, available_fields, missing_upstream_fields
- requested_grid_point, selected_grid_point
- raw_units (upstream attributes; unspecified if absent)
- unit_conversions_applied: false
- samples: lead_hour, valid_time_utc, native_values

The receiving software is responsible for unit conversion, engineering interpretation, validation and any WeatherContract. Do not perform a second unit conversion.

## Offline validation

Run Python 3.12 and install the pinned requirements, then use:

    python -m pip install -r requirements.txt
    python -m unittest discover -s tests -v

Public CI runs these tests against **synthetic arrays only**. It never reads private upstream data, authenticates to Google or publishes artifacts.

## One-step authorized Cloud Shell export (manual only)

An interactive Google Cloud Shell user with existing GCS access can invoke
`scripts/cloudshell_latest.sh LAT LON [HOURS]` to locate the most recent
00/06/12/18 UTC run from the previous three days. It inspects only day-scoped
metadata paths and saves the Zarr transport v2 JSON into the user's HOME
directory, then prints the filename for Cloud Shell's **Download file** tool.

This helper does not create credentials, activate billing, schedule jobs,
perform backend uploads, or put data in Git. The first extraction against a
recent production run still needs manual validation of the user's permissions
and whether Google has published complete arrays.

## Authorized interactive usage

Synoptic default 72 hours (substitute a real authorized date for YYYYMMDD):

    python point_reader.py --lat 0 --lon 0 --run-prefix weathernext_3_0_0_statistics/zarr/2026_to_present/YYYYMMDD_00hr_01_preds/predictions.zarr --output ./output/forecast-native.json

Synoptic, 360 hourly steps:

    python point_reader.py --lat 0 --lon 0 --run-prefix weathernext_3_0_0_statistics/zarr/2026_to_present/YYYYMMDD_06hr_01_preds/predictions.zarr --hours 360 --output ./output/forecast-native.json

Interim 48 hours, P50-only mode:

    python point_reader.py --lat 0 --lon 0 --run-prefix weathernext_3_0_0_statistics/zarr/2026_to_present/YYYYMMDD_01hr_01_preds/predictions.zarr --hours 48 --p50-only --output ./output/forecast-native.json

These are **placeholders**, not valid dataset paths. Replace YYYYMMDD and the sample geographic coordinates with legitimately authorized values. The output directory is excluded from version control.

## Authentication, usage terms and costs

The statistics bucket requires Google-approved identity; anonymous requests fail. The statistics bucket is Requester Pays OFF, but this does not imply zero network or processing costs in all circumstances. No cloud billing is activated in this project.

Do not commit credentials, account details, operational locations or private data. Historical forecasts and recent/future forecasts have different provider licences. This package contains **no scheduler, unattended extraction, external upload or downstream endpoint**.

An offline green test is **not** an authenticated 360-hour upstream read, and does not establish production integration.
