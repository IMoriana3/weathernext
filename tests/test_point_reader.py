"""Offline regression tests. Synthetic Zarr-like arrays; no Google access."""
import math
import os
import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr

from point_reader import (
    BASE, DEFAULT_HOURS, FIELDS, INTERIM_MAX_HOURS, MAX_HOURS,
    MAX_VALUES_PER_POINT, P50_FIELDS, PointRequest, QUANTILES,
    SYNOPTIC_MAX_HOURS, read_gcs_point, run_limits, select_point,
)

SYNOPTIC = BASE + "20260101_00hr_01_preds/predictions.zarr"
INTERIM = BASE + "20260101_01hr_01_preds/predictions.zarr"


def fake_dataset(*, hours=360, init="2026-01-01T00:00:00",
                 missing=(), omit_hour=None):
    """Small spatial grid, arbitrary temporal horizon, actual native field names."""
    lead = np.arange(1, hours + 1)
    if omit_hour is not None:
        lead = lead[lead != omit_hour]
    lead = lead.astype("timedelta64[h]")
    shape = (len(lead), 2, 2)
    coords = {
        "lead_time": lead,
        "lat_0p1": np.array([0.0, 0.1]),
        "lon_0p1": np.array([0.0, 359.9]),
        "init_time": np.datetime64(init),
    }
    data_vars = {}
    for base in ("temperature_2m", "wind_speed_10m",
                 "surface_solar_radiation_downwards_1hr", "total_cloud_cover"):
        for percentile, offset in (("p10", -1.0), ("p50", 0.0), ("p90", 1.0)):
            field = base + "_" + percentile
            if field in missing:
                continue
            if base == "temperature_2m":
                value, units = 280.15 + offset, "K"
            elif base == "wind_speed_10m":
                value, units = 4.0 + offset, "m/s"
            elif base == "surface_solar_radiation_downwards_1hr":
                value, units = 3000.0 + offset, "J/m2"
            else:
                value, units = 0.5 + offset / 10, "fraction"
            data_vars[field] = xr.DataArray(
                np.full(shape, value, dtype=np.float32),
                dims=("lead_time", "lat_0p1", "lon_0p1"),
                coords={k: coords[k] for k in ("lead_time", "lat_0p1", "lon_0p1")},
                attrs={"units": units},
            )
    return xr.Dataset(data_vars, coords=coords)


class HorizonTests(unittest.TestCase):
    def test_default_72_hours_is_synoptic_only(self):
        p = PointRequest(lat=0, lon=0, prefix=SYNOPTIC)
        self.assertEqual(p.hours, 72)
        self.assertEqual(DEFAULT_HOURS, 72)
        self.assertEqual(SYNOPTIC_MAX_HOURS, 360)
        self.assertEqual(INTERIM_MAX_HOURS, 48)
        self.assertEqual(MAX_HOURS, 360)
        self.assertEqual(MAX_VALUES_PER_POINT, 12 * 360)

    def test_interim_cannot_request_72_or_360_hours(self):
        for hours in (49, 72, 360):
            with self.subTest(hours=hours), self.assertRaisesRegex(ValueError, "48"):
                PointRequest(lat=0, lon=0, prefix=INTERIM, hours=hours)
        with self.assertRaisesRegex(ValueError, "48"):
            PointRequest(lat=0, lon=0, prefix=INTERIM)

    def test_all_four_synoptic_cycles_support_360(self):
        for hour in ("00", "06", "12", "18"):
            prefix = BASE + f"20260101_{hour}hr_01_preds/predictions.zarr"
            self.assertEqual(run_limits(prefix)[1], 360)
            self.assertEqual(PointRequest(0, 0, prefix, 360).hours, 360)

    def test_interim_cycles_restricted_to_48(self):
        for hour in ("01", "05", "07", "11", "13", "17", "19", "23"):
            prefix = BASE + f"20260101_{hour}hr_01_preds/predictions.zarr"
            self.assertEqual(run_limits(prefix)[1], 48)
            self.assertEqual(PointRequest(0, 0, prefix, 48).hours, 48)

    def test_synoptic_360_hour_output_is_complete(self):
        out = select_point(fake_dataset(hours=360), PointRequest(
            lat=0.1, lon=-0.1, prefix=SYNOPTIC, hours=360,
        ))
        self.assertEqual(out["transport_schema"], "weathernext3-zarr-point-v2")
        self.assertEqual(out["run_class"], "synoptic")
        self.assertEqual(out["run_max_hours"], 360)
        self.assertEqual(out["returned_hours"], 360)
        self.assertEqual(out["requested_hours"], 360)
        self.assertEqual(len(out["available_fields"]), 12)
        self.assertEqual(out["samples"][0]["lead_hour"], 1)
        self.assertEqual(out["samples"][-1]["lead_hour"], 360)
        self.assertEqual(out["samples"][-1]["valid_time_utc"],
                         "2026-01-16T00:00:00Z")
        self.assertEqual(out["missing_upstream_fields"], [])
        self.assertEqual(out["raw_units"]["temperature_2m_p50"], "K")
        self.assertFalse(out["unit_conversions_applied"])

    def test_interim_48_hour_output(self):
        out = select_point(fake_dataset(hours=48, init="2026-01-01T01:00:00"),
                           PointRequest(0.1, -0.1, INTERIM, 48))
        self.assertEqual(out["run_class"], "interim")
        self.assertEqual(out["run_max_hours"], 48)
        self.assertEqual(out["returned_hours"], 48)
        self.assertEqual(out["samples"][-1]["valid_time_utc"],
                         "2026-01-03T01:00:00Z")

    def test_synoptic_default_72_hour_output(self):
        out = select_point(fake_dataset(hours=360), PointRequest(0, 0, SYNOPTIC))
        self.assertEqual(len(out["samples"]), 72)
        self.assertEqual(out["samples"][-1]["lead_hour"], 72)

    def test_missing_hourly_step_is_error_not_silent_shorter_forecast(self):
        with self.assertRaisesRegex(ValueError, "incomplete hourly forecast"):
            select_point(fake_dataset(hours=360, omit_hour=150),
                         PointRequest(0, 0, SYNOPTIC, 360))

    def test_synthetic_zarr_metadata_does_not_mask_sparse_p50_values(self):
        ds = fake_dataset(hours=24)
        # Coordinates and advertised variables exist, but one hourly
        # temperature median was not materialized in the upstream run.
        ds["temperature_2m_p50"].values[3, 1, 1] = np.nan
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_HOURLY_VALUES"):
            select_point(ds, PointRequest(0.1, -0.1, SYNOPTIC, 24))

    def test_missing_optional_percentile_hour_does_not_fake_a_median(self):
        ds = fake_dataset(hours=24)
        ds["wind_speed_10m_p90"].values[4, 1, 1] = np.nan
        out = select_point(ds, PointRequest(0.1, -0.1, SYNOPTIC, 24))
        self.assertIsNone(out["samples"][4]["native_values"]["wind_speed_10m_p90"])
        self.assertEqual(out["returned_hours"], 24)

    def test_request_longer_than_available_is_not_mislabeled(self):
        with self.assertRaisesRegex(ValueError, "incomplete hourly forecast"):
            select_point(fake_dataset(hours=71),
                         PointRequest(0, 0, SYNOPTIC, 72))

    def test_date_and_input_validation(self):
        bad = (
            {"prefix": "../secrets.json"},
            {"prefix": BASE + "20261342_00hr_01_preds/predictions.zarr"},
            {"lat": 91.0},
            {"lat": math.nan},
            {"lon": -181.0},
            {"hours": 0},
            {"hours": 361},
            {"hours": 1.5},
            {"p50_only": "yes"},
        )
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                PointRequest(**{**{"lat": 0, "lon": 0, "prefix": SYNOPTIC},
                                **overrides})


class PercentileTests(unittest.TestCase):
    def test_all_precomputed_percentiles_are_preserved_as_native_values(self):
        out = select_point(fake_dataset(), PointRequest(0, 0, SYNOPTIC, 1))
        v = out["samples"][0]["native_values"]
        self.assertEqual(len(out["requested_fields"]), 12)
        self.assertEqual(tuple(QUANTILES), ("p10", "p50", "p90"))
        self.assertAlmostEqual(v["temperature_2m_p10"], 279.15, places=4)
        self.assertAlmostEqual(v["temperature_2m_p50"], 280.15, places=4)
        self.assertAlmostEqual(v["temperature_2m_p90"], 281.15, places=4)
        self.assertAlmostEqual(v["wind_speed_10m_p50"], 4.0)
        self.assertAlmostEqual(v["surface_solar_radiation_downwards_1hr_p50"], 3000)
        self.assertEqual(out["missing_upstream_fields"], [])

    def test_p50_only_reads_four_fields(self):
        p = PointRequest(0, 0, SYNOPTIC, 360, p50_only=True)
        out = select_point(fake_dataset(hours=360), p)
        self.assertEqual(p.fields, P50_FIELDS)
        self.assertEqual(len(out["available_fields"]), 4)
        self.assertEqual(out["returned_hours"], 360)

    def test_missing_optional_quantile_is_explicit_not_made_up(self):
        field = "total_cloud_cover_p90"
        out = select_point(fake_dataset(missing=(field,)),
                           PointRequest(0, 0, SYNOPTIC, 1))
        self.assertEqual(out["missing_upstream_fields"], [field])
        self.assertNotIn(field, out["available_fields"])
        self.assertIsNone(out["samples"][0]["native_values"][field])

    def test_missing_p50_is_fatal(self):
        with self.assertRaisesRegex(ValueError, "missing required upstream P50"):
            select_point(fake_dataset(missing=("wind_speed_10m_p50",)),
                         PointRequest(0, 0, SYNOPTIC, 1))

    def test_nan_is_null_and_actual_zero_is_still_zero(self):
        ds = fake_dataset(hours=3)
        ds["total_cloud_cover_p90"].values[0, 1, 1] = np.nan
        ds["surface_solar_radiation_downwards_1hr_p50"].values[0, 1, 1] = 0.0
        out = select_point(ds, PointRequest(0.1, -0.1, SYNOPTIC, 3))
        r = out["samples"][0]["native_values"]
        self.assertIsNone(r["total_cloud_cover_p90"])
        self.assertEqual(r["surface_solar_radiation_downwards_1hr_p50"], 0)

    def test_seam_selects_nearest_longitude_circularly(self):
        out = select_point(fake_dataset(hours=3),
                           PointRequest(0.1, -0.01, SYNOPTIC, 3))
        self.assertEqual(out["selected_grid_point"]["lon_360"], 0.0)

    def test_init_mismatch_rejected_before_loading_data(self):
        with self.assertRaisesRegex(ValueError, "init_time does not match"):
            select_point(fake_dataset(init="2026-01-01T06:00:00"),
                         PointRequest(0, 0, SYNOPTIC, 3))


class AuthorizationTests(unittest.TestCase):
    def test_anonymous_access_is_disabled_without_network(self):
        with patch.dict(os.environ, {"GOOGLE_SKIP_SIGNATURE": "true"}):
            with self.assertRaisesRegex(ValueError, "anonymous access"):
                read_gcs_point(PointRequest(0, 0, SYNOPTIC, 72))

    def test_no_private_identifiers_appear_in_reader_source(self):
        from pathlib import Path
        source = Path(__file__).resolve().parents[1].joinpath(
            "point_reader.py"
        ).read_text(encoding="utf-8").lower()
        # The public package must not contain operational infrastructure URLs.
        self.assertNotIn("downstream_endpoint", source)
        self.assertNotIn("service_role", source)


if __name__ == "__main__":
    unittest.main()
