# WeatherNext point recovery — 2026-10-20

## Evidence and classification

**HECHO:** the operator's 19 October logs completed 11 of 24 hourly P50
reads before a global 300-second timeout during lead 12. This does not
prove that lead 12 is invalid, or that Dask is deadlocked.

**BUG:** the old program retained successful hours only in RAM and wrote
JSON only after the entire horizon. Termination discarded all progress.
The earlier public statement that Zarr must be abandoned was premature.

**HECHO:** parallel, serial and hourly modes all opened Xarray with
`chunks={}`, thus all used Dask. The word serial did not disable Dask.

**UNKNOWN:** the dominant cause of GCS latency. No authenticated benchmark
in the assistant's local environment is available. No speedup is claimed.

**INTENCIONAL:** add an opt-in, resumable `direct` transport, keeping the
existing canonical point selection, units, temporal checks and output
schema. No new weather model, no DNI/DHI synthesis, no control decisions.
BigQuery remains an unvalidated alternative, not a prerequisite.

## Changes

- Direct mode uses `xr.open_zarr(..., chunks=None)` and selects each point
  scalar before backend materialization, with at most four scalar reads
  in flight. It rejects Dask-backed arrays rather than silently using them.
- Every completed scalar is written to private staging with atomic replace
  and fsync. A normal exception, Ctrl+C or SIGTERM cannot invalidate the
  last acknowledged checkpoint. Data not acknowledged before termination
  may need to be fetched again.
- A checkpoint is keyed by run, requested and selected coordinates, horizon,
  field set, source units and array layout. Identity, checksum, numeric
  values and file permissions are verified before reuse. The checksum
  detects accidental corruption; it is not a provider signature.
- One writer per checkpoint, no symlink reads; files use 0600. Checkpoints
  are ignored by Git, use a distinct schema and are NOT valid forecasts
  for the Supabase adapter. No production database changes are made.
- Required P50 gaps still fail closed. Optional percentile nulls stay null;
  real numeric zero stays zero. Only the canonical reader emits a complete
  transport-v2 forecast after validating the whole requested horizon.
- Obstore requests have explicit connection/request timeouts and bounded
  retry policy. The existing outer execution budget remains configurable
  from 30 to 900 seconds. A budget is not a completion-time promise.

## Offline verification

The new tests assert scalar spatial/time indexes at a lazy Xarray backend,
24h and 360h parity with the canonical reader, a forced failure at lead 12
followed by resume without reading leads 1–11 again, zero meteorological
reads on completed replay, preservation of null/zero semantics, changed
site isolation, checksum and cache-value validation, private permissions,
writer exclusion and a real SIGTERM subprocess retaining eleven hours.

A separate integration test creates a compressed, sharded Zarr v3 store,
compares Dask and direct native results and inspects actual byte-range reads.
It is skipped locally only if Zarr/Dask are absent. CI installs the existing
pinned requirements and must execute it. Synthetic data is clearly marked;
none of these tests constitutes an authenticated Google forecast download.

## Opt-in usage after integration

Use the same explicit run prefix to resume. Automatic discovery can choose
a different run on a later day; distinct runs deliberately do not share
cached values. A changed horizon or field set also creates separate staging.

    WN3_P50_ONLY=1 WN3_FETCH_MODE=direct WN3_READ_TIMEOUT_SECONDS=900 \
      bash scripts/cloudshell_latest.sh LAT LON 24 EXACT_RUN_PREFIX

LAT, LON and EXACT_RUN_PREFIX are placeholders, not credentials. The existing
Google-approved ambient identity is required; no new billing, subscriptions,
credentials or server upload is enabled. The default checkpoint directory is
`$HOME/.cache/wn3-point-checkpoints`; `WN3_CHECKPOINT_DIR` overrides it.
Remove the relevant local checkpoint explicitly to force a fresh snapshot.
The previous eleven hours cannot be recovered retroactively: they were
never persisted by the old program.

## Open acceptance gate

Still required: an authenticated complete 24h export, payload inspection and
an explicitly authorized Supabase write/read verification. Do not label
this work production-validated based on offline or CI success alone.

## Primary documentation

- Xarray chunks=None: https://docs.xarray.dev/en/stable/generated/xarray.open_zarr.html
- Google statistics GCS: https://developers.google.com/weathernext/guides/gcs
- Obstore timeouts and retry policy: https://developmentseed.org/obstore/latest/api/store/config/

## Reutilizar P50 ya descargado al completar P10/P90

El primer pronóstico real de El Burgo I del ciclo 20261019_06 UTC
se descargó con 24 horas y cuatro P50; esos cuatro campos están conservados
en su checkpoint privado de Cloud Shell. Una petición con los doce
campos, en modo direct y **exactamente el mismo ciclo, sitio y horizonte**,
reutiliza los valores P50 que superen el control de identidad.

El origen compatible debe coincidir en: prefijo del run, coordenadas
solicitadas y celda seleccionada, 24 horas, init_time, unidades y
dtype/dimensiones de cada campo común. Se bloquean discrepancias entre
dos snapshots de la misma ejecución; no se reescriben valores divergentes.
Este proceso NO reutiliza automáticamente datos entre fechas o horizontes
diferentes. Si no existe checkpoint P50 compatible se descargan de origen.

Comando manual (Cloud Shell autenticado; mismo ciclo histórico):

    WN3_FETCH_MODE=direct WN3_REQUIRE_ALL_QUANTILES=1 WN3_READ_TIMEOUT_SECONDS=900 \
      bash ~/wn3.sh 41.575945 -0.798424 24 \
      weathernext_3_0_0_statistics/zarr/2026_to_present/20261019_06hr_01_preds/predictions.zarr

La opción WN3_REQUIRE_ALL_QUANTILES=1 exige que los doce valores
P10/P50/P90 sean finitos en cada hora. No permite declarar completo un
pronóstico en el que solo haya P50, ni rellena valores opcionales ausentes.
Ante timeout, los nuevos campos ya descargados quedan retenidos para
reanudar el mismo ciclo. Checkpoints no se suben a GitHub ni se importan a
Supabase; solo un JSON final completo validado puede pasar al adaptador.

El objetivo posterior de 72/360 horas requiere una extracción/certificación
independiente y no queda verificado por esta prueba de 24 horas.
