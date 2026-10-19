# Native point transport · version 1

This workspace reads upstream WeatherNext 3 Zarr statistics within strict spatial and temporal limits. It **does not calculate a weather forecast**, apply physical conversions or infer field measurements.

## Input and output

- Input: an explicit authorized Zarr run path, WGS84 latitude and longitude, and 1–6 hourly steps.
- Read: a single nearest 0.1° grid point, the requested lead times and four existing P50 fields.
- Output: native values, selected grid location, initialization and forecast times, upstream field names and optional unit metadata.
- No geospatial interpolation, control decisions, private destinations, scheduled polling or automatic uploads.
- The receiving application's domain layer remains responsible for unit conversion, interpretation, validation, and any published forecast contract.

## Security and cost

The upstream storage requires an authorized Google identity; anonymous access is denied. The reader uses ambient credentials only. It never embeds an account, token, key or downstream URL.

The offline CI workflow uses synthetic arrays and **does not contact Google**. It runs in a public repository with a read-only GitHub token. The remote reader may be run interactively by an already authorized user. No billing account, scheduled ingestion or paid cloud service is configured.

Native output files should be written only to a private local directory outside version control. CI must not print their contents, publish artifacts, or commit data.

## Offline validation

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Reader invocation (authorized interactive runtime)

```sh
python point_reader.py --lat 0 --lon 0 \
  --run-prefix 'weathernext_3_0_0_statistics/zarr/2026_to_present/YYYYMMDD_HHhr_XX_preds/predictions.zarr' \
  --hours 3 --output ./output/point.json
```

Replace the run path and coordinates with legitimately authorized inputs. The shown path is a **placeholder**, not a valid run. The `output/` directory is ignored by Git; do not put confidential points, outputs or account identifiers into a public commit.

## Known limits

- Initial pilot is deliberately capped to 6 h and four P50 statistics.
- Upstream datasets and forecast data have their own access and usage terms.
- No automated ingestion or cross-system integration is asserted until a separate authorized end-to-end test passes.
