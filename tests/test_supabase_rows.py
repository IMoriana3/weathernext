"""Offline checks for the existing Supabase forecast cache row contract."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from point_reader import BASE, BUCKET, FIELDS, P50_FIELDS, VARIABLES
from supabase_rows import build_rows, main, UPSERT_KEY

INITIAL = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
RUN = BASE + "20261019_12hr_01_preds/predictions.zarr"
SITE_LAT = 0.0
SITE_LON = 0.0


def utc(value):
    return value.isoformat().replace("+00:00", "Z")


def native_value(field):
    if field.startswith("temperature_2m"):
        return 280.15
    if field.startswith("wind_speed_10m"):
        return 4.0
    if field.startswith("surface_solar_radiation_downwards_1hr"):
        return 3600.0
    return 0.5


def fixture(hours=3):
    fields = list(FIELDS)
    units = {}
    for field in fields:
        if field.startswith("temperature_2m"):
            units[field] = "K"
        elif field.startswith("wind_speed_10m"):
            units[field] = "m/s"
        elif field.startswith("surface_solar_radiation_downwards_1hr"):
            units[field] = "J/m2"
        else:
            units[field] = "fraction"
    return {
        "transport_schema": "weathernext3-zarr-point-v2",
        "type": "raw_model_forecast_not_field_measurement",
        "source": "Google WeatherNext 3 Statistics",
        "bucket": BUCKET,
        "run_prefix": RUN,
        "init_time_utc": utc(INITIAL),
        "run_class": "synoptic",
        "run_max_hours": 360,
        "requested_hours": hours,
        "returned_hours": hours,
        "requested_fields": fields,
        "available_fields": fields.copy(),
        "missing_upstream_fields": [],
        "requested_grid_point": {"lat": SITE_LAT, "lon": SITE_LON},
        "selected_grid_point": {"lat": SITE_LAT, "lon_360": 0.0},
        "raw_units": units,
        "unit_conversions_applied": False,
        "samples": [
            {
                "lead_hour": hour,
                "valid_time_utc": utc(INITIAL + timedelta(hours=hour)),
                "native_values": {field: native_value(field) for field in fields},
            }
            for hour in range(1, hours + 1)
        ],
    }


def convert(doc):
    return build_rows(
        doc, planta_id=15, site_lat=SITE_LAT, site_lon=SITE_LON
    )


class SupabaseRowsTests(unittest.TestCase):
    def test_maps_native_quantiles_to_existing_table_without_conversions(self):
        rows = convert(fixture())
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.assertEqual([first[k] for k in UPSERT_KEY],
                         [15, "weathernext3", utc(INITIAL),
                          utc(INITIAL + timedelta(hours=1))])
        self.assertEqual(first["p10"]["temperature_2m"], 280.15)
        self.assertEqual(first["p50"]["surface_solar_radiation_downwards_1hr"], 3600.0)
        self.assertEqual(first["p90"]["total_cloud_cover"], 0.5)
        self.assertEqual(first["provenance"]["raw_units"]
                         ["surface_solar_radiation_downwards_1hr_p50"], "J/m2")
        self.assertFalse(first["provenance"]["unit_conversions_applied"])
        self.assertFalse(first["provenance"]["operational_control_permitted"])
        self.assertFalse(first["provenance"]["weather_contract_built"])
        self.assertNotIn("dni", first["p50"])
        self.assertNotIn("dhi", first["p50"])

    def test_360_hour_run_preserves_all_leads(self):
        rows = convert(fixture(hours=360))
        self.assertEqual(len(rows), 360)
        self.assertEqual(rows[-1]["lead_hour"], 360)
        self.assertEqual(rows[-1]["valid_time"],
                         utc(INITIAL + timedelta(hours=360)))
        self.assertEqual(len(set(tuple(row[k] for k in UPSERT_KEY)
                                 for row in rows)), 360)

    def test_missing_optional_upstream_is_null_not_estimated(self):
        doc = fixture()
        absent = "wind_speed_10m_p90"
        doc["available_fields"].remove(absent)
        doc["missing_upstream_fields"].append(absent)
        doc["raw_units"].pop(absent)
        for sample in doc["samples"]:
            sample["native_values"][absent] = None
        rows = convert(doc)
        self.assertIsNone(rows[0]["p90"]["wind_speed_10m"])
        self.assertIn(absent, rows[0]["provenance"]["missing_upstream_fields"])

    def test_p50_only_does_not_fabricate_p10_p90(self):
        doc = fixture()
        doc["requested_fields"] = list(P50_FIELDS)
        doc["available_fields"] = list(P50_FIELDS)
        doc["raw_units"] = {k: v for k, v in doc["raw_units"].items()
                            if k in P50_FIELDS}
        for sample in doc["samples"]:
            sample["native_values"] = {
                k: v for k, v in sample["native_values"].items()
                if k in P50_FIELDS
            }
        rows = convert(doc)
        self.assertTrue(all(value is None for value in rows[0]["p10"].values()))
        self.assertEqual(len(rows[0]["p50"]), len(VARIABLES))

    def test_rejects_wrong_site_even_when_planta_id_is_valid(self):
        doc = fixture()
        doc["requested_grid_point"]["lat"] += 0.3
        with self.assertRaisesRegex(ValueError, "FORECAST_SITE_MISMATCH"):
            convert(doc)

    def test_rejects_distant_selected_grid(self):
        doc = fixture()
        doc["selected_grid_point"]["lon_360"] = 50
        with self.assertRaisesRegex(ValueError, "FORECAST_GRID_MISMATCH"):
            convert(doc)

    def test_rejects_missing_median_or_invalid_field_inventory(self):
        doc = fixture()
        doc["available_fields"].remove("temperature_2m_p50")
        doc["missing_upstream_fields"].append("temperature_2m_p50")
        doc["raw_units"].pop("temperature_2m_p50")
        for item in doc["samples"]:
            item["native_values"]["temperature_2m_p50"] = None
        with self.assertRaisesRegex(ValueError, "required P50"):
            convert(doc)

    def test_refuses_wrong_timestamp_or_skipped_hour(self):
        doc = fixture()
        doc["samples"][1]["valid_time_utc"] = "2026-10-19T20:00:00Z"
        with self.assertRaisesRegex(ValueError, "valid_time"):
            convert(doc)
        doc = fixture()
        doc["samples"][1]["lead_hour"] = 3
        with self.assertRaisesRegex(ValueError, "lead_hour"):
            convert(doc)

    def test_no_physical_conversion_or_malformed_values(self):
        doc = fixture()
        doc["unit_conversions_applied"] = True
        with self.assertRaisesRegex(ValueError, "unit_conversions_applied"):
            convert(doc)
        doc = fixture()
        doc["samples"][0]["native_values"]["wind_speed_10m_p50"] = float("nan")
        with self.assertRaisesRegex(ValueError, "invalid native numeric"):
            convert(doc)
        doc = fixture()
        doc["samples"][0]["native_values"]["wind_speed_10m_p50"] = True
        with self.assertRaisesRegex(ValueError, "invalid native numeric"):
            convert(doc)

    def test_rejects_unknown_extra_fields_and_wrong_horizon(self):
        doc = fixture()
        doc["samples"][0]["native_values"]["invented_dni"] = 50
        with self.assertRaisesRegex(ValueError, "field inventory"):
            convert(doc)
        doc = fixture()
        doc["requested_hours"] = 4
        with self.assertRaisesRegex(ValueError, "returned_hours"):
            convert(doc)

    def test_cli_writes_private_local_file_and_never_uploads(self):
        with tempfile.TemporaryDirectory() as root:
            raw = Path(root) / "raw.json"
            out = Path(root) / "output" / "rows.json"
            raw.write_text(json.dumps(fixture()), encoding="utf-8")
            args = [
                "--input", str(raw), "--planta-id", "15",
                "--site-lat", "0", "--site-lon", "0",
                "--output", str(out),
            ]
            self.assertEqual(main(args), 0)
            self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))), 3)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertEqual(main(args), 1)  # no accidental overwrite


if __name__ == "__main__":
    unittest.main()
