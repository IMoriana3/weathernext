# WeatherNext 3 · opt-in scheduled point ingestion (v1)

## Facts versus deployment

- **HECHO:** historical El Burgo I run `20261019_06hr_01_preds` has 360 consecutive hourly leads × 12 native fields, validated after write in Supabase (360 rows, 4320 exact values, no divergences). This is **not** a current/automatically updated forecast.
- **INTENCIONAL:** `scheduled_ingest.py` uses the existing source reader (`point_reader.py`) and pure `supabase_rows.build_rows`, never a new weather engine, never recalculates P10/P50/P90, never synthesizes DNI/DHI, never authorizes operational STOW.
- **INTENCIONAL:** `.github/workflows/ingest.yml` provides an optional schedule for **24h** probabilistic point forecasts twice daily, disabled by default until an operator configures the exact activation variable and cloud credentials.
- **UNKNOWN:** unattended access to Google's authorized WeatherNext bucket through Workload Identity Federation and actual runtime/costs on GitHub-hosted runners are **not certified**. Passing offline CI does not validate external credentials, remote availability, operating cost or production scheduling.

## Canonical data and database contract

`public.meteo_pronosticos` in the existing Supabase project remains the only forecast cache. Upsert key: `(planta_id,source_id,init_time,valid_time)`; source always `weathernext3`. No table/DDL/RLS changes are introduced.

Before sending data to Supabase, the worker:

1. Discovers the most recent mature 00/06/12/18 UTC run via existing `scripts.discover_run` (3 days, ≤8 metadata probes, min 9h since initialization); rejects selections older than 42h. A Zarr root is not proof of numeric completeness.
2. Requires full requested 24/72/360 lead hours, all 12 **native** P10/P50/P90 fields, non-null finite numerical values, exactly matching unit labels (`K`, `m s**-1`, `J m**-2`, `(0 - 1)`), native quantile order and domain bounds. Reuses `PointRequest`, `read_gcs_point` in direct mode with optional private checkpoint and the sole canonical `build_rows` adapter. No field-measurement reinterpretation.
3. Uses an `sb_secret_...` backend-only key via the `apikey` header. The `SUPABASE_URL` must be an exact HTTPS `*.supabase.co` host; redirects are rejected to avoid secret forwarding. No credentials, URLs with tokens or remote exception bodies are logged.
4. Verifies the explicitly configured `planta_id` and expected plant code against `public.plantas` before reading GCS; then checks every already-stored row for this run against source P10/P50/P90, provenance, timestamp and lead. Differences block all writes (`FORECAST_CONFLICT_EXISTING_RUN`).
5. Posts **only missing** rows through PostgREST with `Prefer: resolution=ignore-duplicates,return=minimal`. It never overwrites a previously stored row; a concurrent difference will fail the next readback.
6. Reads the run back and checks every expected row and every original native value. Partial storage, a conflicting winner in a concurrent insert, or an HTTP problem returns a failure. No accepted output is issued on failure.

## Controlled deployment prerequisites — not performed by this PR

All settings below belong to the **GitHub repository's** Actions secrets and variables; do not put real site locations or credentials in commits.

| Type | Name | Requirement |
|---|---|---|
| Secret | `WN3_SUPABASE_SECRET_KEY` | Dedicated backend `sb_secret_...`, not anon/publishable or legacy role key |
| Variable | `WN3_SUPABASE_URL` | Supabase REST project URL as `https://<project-ref>.supabase.co` (no path) |
| Variable | `WN3_PLANTA_ID` | Verified `public.plantas.id` for a single approved plant |
| Variable | `WN3_SITE_CODE` | Matching verified `public.plantas.code` |
| Variable | `WN3_SITE_LAT` / `WN3_SITE_LON` | Operator-approved WGS84 coordinates for the same plant |
| Variable | `WN3_GCP_WIF_PROVIDER` | Approved Google Workload Identity Federation provider resource name |
| Variable | `WN3_GCP_SERVICE_ACCOUNT` | GCP service-account email with the minimum approved GCS object listing/reading rights |
| Variable | `WN3_INGESTION_APPROVED` | **Must remain absent/false until acceptance**; exact `true` enables scheduled runs |

Set Google Workload Identity Federation to trust **only** this repository's main-branch workflow identity. The GitHub OIDC request grants temporary credentials; **no service-account JSON key should be stored**. Ensure the account is authorized by the dataset provider for the relevant GCS Statistics resources; an unrelated GCP account is insufficient.

Review your GitHub Actions usage/billing and GCP/Supabase terms before enabling `WN3_INGESTION_APPROVED=true`. This repository does not create WIF resources, secrets, accounts, paid services or billable jobs itself. Repo variables and secrets must be created by the authorized operator using GitHub settings.

## Rollout

1. Merge only after offline CI is green; workflow remains disabled by default.
2. Configure all credentials and variables, verify provider licence and spending limits, then explicitly approve activation. Initial `workflow_dispatch` at **24 h** should be watched with logs; acceptance requires a fresh authorized GCS source and confirmed Supabase row/readback after the run.
3. Verify repeat triggers are idempotent for an unchanged run, missing-source/incomplete publication blocks insertion, and a new run becomes available without manual Cloud Shell commands. Inspect dates and monitoring; scheduled GitHub Actions can be delayed or disabled in inactive public repositories.
4. Only after measured performance, consider 72h and 360h unattended. Hosted GitHub runners have **ephemeral local checkpoints**, so 360h recovery across separate jobs is not yet durable. A private persistent state store or authorized dedicated runner needs engineering and explicit approval.

### Workflow defaults

- UTC schedule at `06:17` and `18:17` (twice daily); most recent mature synoptic run, 24h horizon.
- `timeout-minutes: 150`, no unbounded retries, at most 360×12 native values per point.
- `concurrency` serializes scheduled/manual tasks; `contents: read`, `id-token: write` only.
- Job-level condition checks the exact `WN3_INGESTION_APPROVED == 'true'` repository variable.
- No public artifacts or raw forecast values in logs.

### Distinguishing classifications

| Label | Meaning |
|---|---|
| HECHO | Historical 360h transport/import verified from Google Cloud Shell and Supabase readback |
| INTENCIONAL | New gated worker uses canonical existing point reader and Supabase row mapper |
| APROXIMACIÓN | 9h dissemination buffer and 42h max-age window are heuristics, not provider SLA |
| UNKNOWN | Google WIF + obstore ADC compatibility, scheduled runtime, operating cost and first real automated run |
| LEGACY | Manual-only Cloud Shell script remains available for approved historical diagnostics |

**WeatherContract v2 is owned by `IMoriana3/SolarGPTfull`** and still requires canonical GHI/DNI/DHI semantics. Storing native `surface_solar_radiation_downwards_1hr` does not build that contract, nor calibrate forecast uncertainty or permit STOW decisions.
