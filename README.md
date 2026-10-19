# WeatherNext 3

A minimal, read-only transport for authorized WeatherNext 3 probability statistics stored as Zarr arrays in Google Cloud Storage.

## What is here?

- `point_reader.py`: bounded point reader; one grid cell, 1–6 hours and four P50 upstream variables.
- `tests/`: synthetic tests for coordinates, source timestamps, raw values and missing-data handling.
- `.github/workflows/verify.yml`: free public GitHub Actions job; runs **offline tests only**.
- `docs/transport.md`: the versioned input/output transport contract.

The program does **not** implement another meteorological model, convert physical units, publish observations or send control commands.

## Access and privacy

The Google bucket is not anonymously readable. An authorized upstream identity is required for interactive reads. No Google credentials, operational coordinates, downstream endpoints or secrets belong in this repository.

The CI workflow never accesses the restricted bucket, never retrieves private forecast data and never uploads artifacts.

## Cost policy

Additional expenditure: **zero**. No paid services, billing activation, unattended extraction or scheduled jobs are configured.

## Current status

- Bounded transport and offline tests: implemented in a review branch.
- Authenticated upstream read from a production runner: not validated.
- Unattended ingestion and downstream publication: **disabled**.

See [transport contract](docs/transport.md) for exact boundaries and limitations.
