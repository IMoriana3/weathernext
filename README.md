# WeatherNext 3

Lightweight tools for reading and validating probabilistic weather forecast statistics.

## Scope

- Read authorized Google Cloud Storage Zarr datasets.
- Validate a small geographic and temporal sample before any scheduled processing.
- Keep upstream credentials, downstream destinations, and operational settings outside the repository.
- Preserve source timestamps, units, quantiles, and provenance.

This is a data transport workspace, not a separate weather model or control system.

## Access

The upstream datasets require an authorized Google identity. Anonymous requests return `403 AccessDenied`. Public CI does **not** bypass provider access controls.

## Operating policy

- No billing activation or paid cloud infrastructure.
- No credentials, API keys, refresh tokens, project identifiers, site lists, or operational datasets in public files or build logs.
- No scheduled ingestion until access, authorization, cost, and data-use conditions are verified.
- No forecasts are interpreted as field measurements or control commands.

Initial status: repository scaffold only; automated ingestion is not enabled.
