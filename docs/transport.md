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

## Discovery of the current Zarr v3 run

The first interactive export helper searched specifically for
`.zmetadata`, which is a **legacy Zarr v2 metadata filename**. Google
documents WeatherNext 3 as **Zarr v3**, with root `zarr.json` metadata.
That mistake could produce a false "no recent run" even when runs exist.

The discovery helper in `scripts/discover_run.py` now:

1. Lists only dated prefixes (3 UTC days by default; maximum 7) using
   `gcloud storage ls gs://.../YYYYMMDD_*` with the Google identity
   already logged into Cloud Shell.
2. Extracts **synoptic runs only** from the returned folder/object paths
   (00/06/12/18 UTC). It does not enumerate or download global grids.
3. Sorts runs newest first and checks each candidate's *exact* root
   `predictions.zarr/zarr.json` object, with a maximum of 8 metadata checks.
4. Distinguishes `ACCESS_DENIED`, `GCLOUD_LIST_ERROR`,
   `NO_RECENT_RUN_FOLDERS` and `NO_VERIFIED_METADATA`. It never
   assumes a date or proceeds to download a guessed run.
5. Gives the chosen existing prefix to the unchanged point reader,
   which independently checks that all requested hours/fields exist.

The archive may contain older runs even if no recent run is visible.
For an approved, manually selected historical run, supply the exact
relative run prefix as an optional fourth argument:

    bash scripts/cloudshell_latest.sh 0 0 3 \
      'weathernext_3_0_0_statistics/zarr/2026_to_present/20260101_01hr_01_preds/predictions.zarr'

That example is **historical/interim (48h maximum)** and must not be
presented as a current forecast. It requires the same authorized
identity as the live reader. All data files remain local and ignored
by Git. Offline tests mock all Google listings and never issue network
requests.

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

## Existing Supabase forecast cache: strictly local row preparation

The optional supabase_rows.py adapter targets the **existing**
public.meteo_pronosticos table, not a new weather model or a new database.
Its verified target contract is:

- planta_id (explicit public.plantas.id foreign key), source_id='weathernext3';
- UTC init_time, valid_time, integer lead_hour;
- p10, p50, p90 JSON objects of **unchanged upstream native variables**;
- provenance containing bucket/run, requested/selected grid cell, raw units,
  available/missing native fields, and explicit no-conversion/no-control flags.

Database uniqueness is (planta_id, source_id, init_time, valid_time).
An authenticated web user may read according to RLS, but ordinary authenticated
sessions are **not** a forecast-ingestion credential. Privileged database writes
require a separately authorized protected server-side channel. This transport
adapter neither contains nor requests a Supabase key, performs network calls,
or writes to the database.

After **an actual authorized Zarr point extraction**, select the exact verified
Supabase plant id and coordinates, and produce local rows:

    python supabase_rows.py --input "$HOME/weathernext3-EXAMPLE.json" --planta-id 123 --site-lat 0 --site-lon 0 --output output/example-supabase-rows.json

The numbers and filename above are **placeholders**, not an actual plant or
existing forecast. The command refuses mismatched site coordinates, selected
grid cells farther than 0.11 degrees, invalid run metadata, missing hourly steps,
missing required P50 fields, bad units and non-finite values. It refuses to
overwrite an existing output file; outputs are local with file mode 0600.
The output/ folder is ignored by Git. Do not publish raw exports publicly.

An authorized ingestion implementation can upsert these validated rows against
the **existing** primary key, with a verification read of row counts, provenance
and time range afterwards. Until that authenticated step succeeds,
public.meteo_pronosticos remains empty; no imported data is claimed.

**Contract boundary:** This is a transport/cache adapter, not
df_meteo_canonical, MeteoReport, or WeatherContract v2. Those are owned by
IMoriana3/SolarGPTfull (solargpt/CONTRACTS.md). WeatherNext point output
currently supplies temperature, 10 m wind, hourly downwelling solar radiation
and cloud fraction, **not** a complete GHI/DNI/DHI triplet suitable for
canonical production physics. Never fill missing DNI or DHI with synthetic
zeros or derive bankable uncertainty without evidence. WEATHER informs;
CONTROL decides.

## Live data-quality regression: early run metadata is not a complete forecast

An authorized WeatherNext 3 statistics point read on 19 October 2026 selected
the 12:00 UTC run while the Google publication schedule listed approximately
19:45 UTC as the expected GCS statistics availability. The local extraction
occurred at 19:14 UTC: before the expected complete release, though whether
publication timing caused the gaps is **unverified**.

The exported 24 forecast steps and all 12 advertised variables existed,
but many native numerical values were null: only 5 of 24 hours had all four
mandatory P50 variables. The old reader's "fields: 12/12" referred to
variable metadata, **not** 12 complete hourly time series. The older check
only required any finite temperature and wind over the whole horizon. This is
classified as a **BUG** in quality gating, with the underlying nulls' origin
still **UNKNOWN** pending replay after publication.

Regression safeguards now:
- Default auto-discovery considers synoptic runs at least 9 hours old.
  This is an explicit **APPROXIMATION**/safety buffer informed by Google's
  publication schedule; it is not a guarantee of availability or quality.
  Explicit exact-run historical replay still bypasses that age heuristic.
- The reader **fails** with INCOMPLETE_HOURLY_VALUES if any of the four
  mandatory hourly P50 arrays has a null/NaN for a requested hour, even if
  root zarr.json, coordinates and field names are present.
- The Supabase adapter independently rejects any such input before
  preparing or storing database rows. No partial series is represented as a
  usable continuous 24/72/360-hour forecast.
- Optional P10/P90 point-level nulls remain explicit nulls; they are not
  fabricated from P50 and must not be presented as a complete interval.
- No automatic backfilling or synthesis is performed. Corrected extraction
  must be revalidated from a sufficiently mature cycle.

Official schedule: https://developers.google.com/weathernext/guides/dissemination

## Step 4 Cloud Shell diagnostics and bounded remote reads

The GCS reader now logs coarse progress markers to stderr only (no raw
forecast values or credentials): METADATA, METADATA_OK, CHUNKS, CHUNKS_OK.
If a manual run stops at METADATA, the remote Zarr metadata open is pending.
If it stops at CHUNKS, selected grid-cell arrays are still being retrieved.
This does not by itself prove a network or application defect.

The optional Cloud Shell script runs the point reader via GNU `timeout`,
with a 300-second default and a 10-second force-kill grace period.
The interactive user may choose an explicitly bounded alternative
using `WN3_READ_TIMEOUT_SECONDS` (30–900). On timeout the script emits
`READ_TIMEOUT`, exits nonzero and never reports a successfully exported
forecast. No stored database rows, remote writes or credentials are involved.
Actual cause of a timeout (slow remote data, permissions, incomplete upstream
publication or a client-library issue) requires further evidence.

## Opt-in small point I/O probe (GCS read diagnostic)

If a multi-variable Dask read is stuck in CHUNKS and reaches READ_TIMEOUT,
do **not** rerun the same 24h/12-field operation. This script can request
just one lead hour and only four source P50 fields, and can fetch the
four median variables **serially** with the single-threaded Dask scheduler:

    WN3_P50_ONLY=1 WN3_FETCH_MODE=serial WN3_READ_TIMEOUT_SECONDS=120 \
      bash ~/wn3.sh 41.575945 -0.798424 1

The latitude and longitude above are an example; the output is a local
single-hour point, **not** a full forecast for production. It still requires
the operator's existing Google identity. The shell helper uses the same run
discovery, trusted Zarr metadata, native fields and strict P50 validation
as the full export. It uses no additional cloud services or database writes.

Each variable emits CHUNK_FIELD and CHUNK_FIELD_OK without reporting values
or credentials. Failure before the first CHUNK_FIELD_OK suggests even one
native variable/block is slow or unavailable. Successful 1h/4-field output
does **not** certify a complete 24h/12-field run, but can isolate
a Dask-concurrency/total-work bottleneck from a more basic GCS read issue.

The two opt-in environment switches are WN3_P50_ONLY=1 (else 0) and
WN3_FETCH_MODE=serial (else parallel). Serial mode reduces concurrent
remote requests and identifies exactly which variable is pending; it is
not guaranteed to be faster. Stop after the bounded timeout if it still
does not read. No synthetic values or fallback meteorology are generated.

## 24-hour P50 reading diagnostic: one-hour windows (opt in)

One authorized single-lead P50 extraction completed with all four medians;
however, a subsequent 24-hour serial field load stopped at
CHUNK_FIELD 1/4 temperature_2m_p50 without completing. The origin of
the delay remains UNKNOWN: Dask scheduling, remote chunk layout, missing
objects or GCS latency are not yet distinguished.

To isolate the earliest slow or unavailable lead, run an explicit 24-hour
diagnostic with P50-only and per-hour (not per-field) Dask loads:

    WN3_P50_ONLY=1 WN3_FETCH_MODE=hourly WN3_READ_TIMEOUT_SECONDS=300 bash ~/wn3.sh 41.575945 -0.798424 24

The script prints CHUNK_LAYOUT (lazy lead-time chunk count) and
CHUNK_HOUR: i/24 lead=h / CHUNK_HOUR_OK: i/24 lead=h. A null in any
mandatory P50 stops extraction on that lead with INCOMPLETE_HOURLY_VALUES.
No incomplete 24-hour JSON is reported as a complete forecast.

The hourly-window mode may perform more remote requests and may not be
faster than the per-field mode. It is a diagnostic, not a performance
guarantee. It keeps source variables, units, coordinate identity, hourly
timestamps, forecast-only provenance and upstream authorization unchanged.

Until all 24 hours are complete and validated, Supabase ingestion stays
blocked; no partial output is mislabeled as a full production forecast.
