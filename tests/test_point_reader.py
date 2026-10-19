"""Synthetic metadata and small arrays; never connect to Google in CI."""
import math
import unittest

import numpy as np
import xarray as xr

from point_reader import BASE, FIELDS, MAX_HOURS, PointRequest, select_point, read_gcs_point

RUN = BASE + "20260101_01hr_01_preds/predictions.zarr"


def fake_dataset(*, missing=(), init="2026-01-01T01:00:00"):
    time = np.array([1, 2, 3], dtype="timedelta64[h]")
    data = np.array([[[273.15, 275.15], [276.15, 280.15]],
                     [[273.95, 274.15], [276.95, 281.15]],
                     [[274.95, 274.25], [277.95, 282.15]]])
    coords = {
        "lat_0p1": np.array([0.0, 0.1]),
        "lon_0p1": np.array([359.8, 359.9]),
        "lead_time": time,
        "init_time": np.datetime64(init),
    }
    vars_ = {}
    for idx, key in enumerate(FIELDS):
        if key in missing:
            continue
        values = data if idx == 0 else np.full((3, 2, 2), float(idx + 1))
        vars_[key] = xr.DataArray(
            values, dims=("lead_time", "lat_0p1", "lon_0p1"),
            coords={x: coords[x] for x in ("lead_time", "lat_0p1", "lon_0p1")},
            attrs={"units": "K" if idx == 0 else "native"},
        )
    return xr.Dataset(vars_, coords=coords)


class PointReaderTests(unittest.TestCase):
    def test_one_cell_three_hours_and_no_units_conversion(self):
        result = select_point(fake_dataset(), PointRequest(lat=0.11, lon=-0.11,
                                                           prefix=RUN, hours=3))
        self.assertEqual(result["transport_schema"], "weathernext3-zarr-point-v1")
        self.assertFalse(result["unit_conversions_applied"])
        self.assertEqual(result["selected_grid_point"], {"lat": 0.1, "lon_360": 359.9})
        self.assertEqual(result["init_time_utc"], "2026-01-01T01:00:00Z")
        self.assertEqual([r["lead_hour"] for r in result["samples"]], [1, 2, 3])
        self.assertEqual(result["samples"][0]["native_values"]["temperature_2m_p50"], 280.15)
        self.assertEqual(result["samples"][2]["valid_time_utc"], "2026-01-01T04:00:00Z")

    def test_null_is_not_invented_zero(self):
        ds = fake_dataset()
        ds["total_cloud_cover_p50"].values[0, 1, 1] = np.nan
        result = select_point(ds, PointRequest(lat=0.1, lon=-0.1, prefix=RUN))
        self.assertIsNone(result["samples"][0]["native_values"]["total_cloud_cover_p50"])

    def test_missing_variable_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing upstream data field"):
            select_point(fake_dataset(missing=("wind_speed_10m_p50",)),
                         PointRequest(lat=0, lon=0, prefix=RUN))

    def test_different_initialization_rejected(self):
        with self.assertRaisesRegex(ValueError, "init_time does not match"):
            select_point(fake_dataset(init="2026-01-01T00:00:00"),
                         PointRequest(lat=0, lon=0, prefix=RUN))

    def test_rejects_invalid_prefix_and_coords(self):
        for kwargs in (
            {"prefix": "../secrets.json"},
            {"prefix": BASE + "20261342_01hr_01_preds/predictions.zarr"},
            {"lat": 91.0}, {"lon": -181.0}, {"lat": math.nan},
            {"hours": 0}, {"hours": MAX_HOURS + 1}, {"hours": 1.5}
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PointRequest(**{**{"lat": 0, "lon": 0, "prefix": RUN}, **kwargs})

    def test_anonymous_access_disabled_even_without_network(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"GOOGLE_SKIP_SIGNATURE": "true"}):
            with self.assertRaisesRegex(ValueError, "anonymous access"):
                read_gcs_point(PointRequest(lat=0, lon=0, prefix=RUN))


if __name__ == "__main__":
    unittest.main()
