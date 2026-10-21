"""Offline-only tests of scheduled ingestion, protocol and safety gates."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from point_reader import FIELDS, VARIABLES
from scheduled_ingest import (
    EXPECTED_UNITS, IngestionBlocked, NoRedirect, ProtectedSupabase,
    _checked_run, _compare_existing, _strict_native_rows, main,
    publish_rows, run_once,
)
from test_supabase_rows import fixture, RUN, INITIAL, SITE_LAT, SITE_LON


def valid_payload(hours=24):
    doc = fixture(hours=hours)
    doc["raw_units"] = {
        field: EXPECTED_UNITS[field[:-4]] for field in FIELDS
    }
    return doc


def rows_for(doc):
    return _strict_native_rows(
        doc, planta_id=1, lat=SITE_LAT, lon=SITE_LON,
        hours=len(doc["samples"]),
    )


class FakeClient:
    def __init__(self, existing=(), *, mismatch_plant=False, skip_write=False):
        self.saved = deepcopy(list(existing))
        self.written = []
        self.read_calls = 0
        self.mismatch_plant = mismatch_plant
        self.skip_write = skip_write

    def verify_plant(self, planta_id, code):
        if self.mismatch_plant or (planta_id, code) != (1, "24002"):
            raise IngestionBlocked("SUPABASE_PLANT_IDENTITY_MISMATCH")

    def existing_run(self, planta_id, init):
        assert planta_id == 1
        assert init == self.expected_init
        self.read_calls += 1
        return deepcopy(self.saved)

    def insert_missing(self, rows):
        self.written += deepcopy(rows)
        if not self.skip_write:
            seen = {r["lead_hour"] for r in self.saved}
            self.saved += deepcopy(
                [r for r in rows if r["lead_hour"] not in seen]
            )


class IngestTests(TestCase):
    def client(self, doc, existing=(), **kwargs):
        client = FakeClient(existing, **kwargs)
        client.expected_init = doc["init_time_utc"]
        return client

    def test_strict_source_has_no_new_physics_and_preserves_units(self):
        rows = rows_for(valid_payload())
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0]["p50"]["temperature_2m"], 280.15)
        self.assertFalse(rows[0]["provenance"]["weather_contract_built"])
        self.assertFalse(rows[0]["provenance"]["operational_control_permitted"])
        self.assertFalse(rows[0]["provenance"]["unit_conversions_applied"])
        self.assertNotIn("dni", rows[0]["p50"])
        self.assertNotIn("dhi", rows[0]["p50"])

    def test_missing_any_optional_percentile_blocks_all_rows(self):
        doc = valid_payload()
        doc["samples"][4]["native_values"]["wind_speed_10m_p90"] = None
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_QUANTILES"):
            rows_for(doc)

    def test_monotonicity_and_native_units_fail_closed(self):
        doc = valid_payload()
        doc["samples"][0]["native_values"]["temperature_2m_p10"] = 300
        with self.assertRaisesRegex(IngestionBlocked, "PERCENTILE_ORDER"):
            rows_for(doc)
        doc = valid_payload()
        doc["raw_units"]["wind_speed_10m_p90"] = "knots"
        with self.assertRaisesRegex(IngestionBlocked, "SOURCE_UNITS"):
            rows_for(doc)

    def test_nonnegative_and_cloud_domain_fail_closed(self):
        doc = valid_payload()
        for p in ("p10", "p50", "p90"):
            doc["samples"][0]["native_values"]["total_cloud_cover_" + p] = 1.1
        with self.assertRaisesRegex(IngestionBlocked, "CLOUD_FRACTION"):
            rows_for(doc)
        doc = valid_payload()
        for p in ("p10", "p50", "p90"):
            doc["samples"][0]["native_values"]["wind_speed_10m_" + p] = -0.1
        with self.assertRaisesRegex(IngestionBlocked, "NONNEGATIVE"):
            rows_for(doc)

    def test_preflight_then_missing_only_then_complete_readback(self):
        doc = valid_payload()
        rows = rows_for(doc)
        client = self.client(doc, rows[:8])
        saved = deepcopy(client.saved)
        inserted = publish_rows(client, rows, init_time=doc["init_time_utc"])
        self.assertEqual(inserted, 16)
        self.assertEqual(len(client.written), 16)
        self.assertEqual([r["lead_hour"] for r in client.written],
                         list(range(9, 25)))
        self.assertEqual(client.saved[:8], saved)
        self.assertEqual(client.read_calls, 2)

    def test_duplicate_delivery_is_idempotent(self):
        doc = valid_payload()
        rows = rows_for(doc)
        client = self.client(doc, rows)
        self.assertEqual(publish_rows(
            client, rows, init_time=doc["init_time_utc"]), 0)
        self.assertEqual(client.written, [])
        self.assertEqual(client.read_calls, 2)

    def test_value_and_provenance_conflicts_abort_before_write(self):
        doc = valid_payload()
        rows = rows_for(doc)
        for kind in ("value", "provenance", "time"):
            with self.subTest(kind=kind):
                existing = deepcopy(rows[:3])
                if kind == "value":
                    existing[1]["p50"]["temperature_2m"] += 1
                elif kind == "provenance":
                    existing[1]["provenance"]["operational_control_permitted"] = True
                else:
                    existing[1]["valid_time"] = "2026-10-19T18:00:00Z"
                client = self.client(doc, existing)
                with self.assertRaisesRegex(
                    IngestionBlocked, "FORECAST_CONFLICT_EXISTING_RUN"
                ):
                    publish_rows(client, rows, init_time=doc["init_time_utc"])
                self.assertEqual(client.written, [])

    def test_missing_result_after_write_is_not_success(self):
        doc = valid_payload()
        rows = rows_for(doc)
        client = self.client(doc, skip_write=True)
        with self.assertRaisesRegex(IngestionBlocked,
                                    "SUPABASE_READBACK_INCOMPLETE"):
            publish_rows(client, rows, init_time=doc["init_time_utc"])

    def test_duplicate_or_unexpected_lead_in_db_blocks(self):
        doc = valid_payload()
        rows = rows_for(doc)
        existing = [rows[0], deepcopy(rows[0])]
        with self.assertRaisesRegex(IngestionBlocked, "DUPLICATE"):
            _compare_existing(existing, rows)

    def test_stale_or_immature_cycles_never_publish(self):
        for age in (2, 43):
            with self.subTest(age=age):
                with self.assertRaisesRegex(IngestionBlocked,
                                            "MATURE_OR_STALE"):
                    _checked_run(RUN, now=INITIAL + timedelta(hours=age))
        self.assertEqual(
            _checked_run(RUN, now=INITIAL + timedelta(hours=12))[1], 360
        )

    def test_invalid_plant_blocks_source_access(self):
        doc = valid_payload()
        client = self.client(doc, mismatch_plant=True)
        reads = []
        def read(req, **kwargs):
            reads.append(req)
            return doc
        with self.assertRaisesRegex(IngestionBlocked, "PLANT_IDENTITY"):
            run_once(planta_id=1, site_code="24002",
                     lat=SITE_LAT, lon=SITE_LON, hours=24, prefix=RUN,
                     now=INITIAL + timedelta(hours=12), publish=True,
                     client=client, read_point=read)
        self.assertEqual(reads, [])

    def test_run_once_publish_uses_direct_with_full_quantiles(self):
        doc = valid_payload()
        client = self.client(doc)
        received = []
        def point(req, **kwargs):
            received.append((req, kwargs))
            return doc
        actual = run_once(
            planta_id=1, site_code="24002",
            lat=SITE_LAT, lon=SITE_LON, hours=24, prefix=RUN,
            now=INITIAL + timedelta(hours=12),
            publish=True, client=client, read_point=point,
            checkpoint_dir="/private/checkpoints"
        )
        self.assertEqual(actual, 24)
        self.assertEqual(len(client.saved), 24)
        self.assertEqual(received[0][1]["fetch_mode"], "direct")
        self.assertTrue(received[0][1]["strict_quantiles"])
        self.assertEqual(received[0][1]["checkpoint_dir"], "/private/checkpoints")

    def test_dry_run_does_not_use_supabase_even_with_valid_data(self):
        doc = valid_payload()
        class NoNetwork:
            def __getattribute__(self, name):
                raise AssertionError("must not invoke network")
        self.assertEqual(run_once(
            planta_id=1, site_code="24002",
            lat=SITE_LAT, lon=SITE_LON, hours=24, prefix=RUN,
            now=INITIAL + timedelta(hours=12), publish=False,
            client=NoNetwork(), read_point=lambda req, **kwargs: doc,
        ), 0)

    def test_privileged_write_requires_explicit_approval_before_discovery(self):
        argv = [
            "--planta-id", "1", "--site-code", "24002",
            "--lat", "0", "--lon", "0", "--publish"
        ]
        with patch.dict("os.environ", {"WN3_INGESTION_APPROVED": ""}):
            self.assertEqual(main(argv), 1)

    def test_restrict_endpoint_and_key_and_disallow_redirects(self):
        for url in (
            "http://valid.supabase.co", "https://valid.supabase.co.attacker.tld",
            "https://valid.supabase.co@attacker.tld",
            "https://valid.supabase.co/path",
            "https://localhost:3000",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                IngestionBlocked, "SUPABASE_URL_NOT_APPROVED"
            ):
                ProtectedSupabase(url, "sb_secret_abcdefghijklmnopqrst")
        with self.assertRaisesRegex(IngestionBlocked, "BACKEND_SECRET"):
            ProtectedSupabase("https://example.supabase.co", "sb_publishable_wrong")
        with self.assertRaisesRegex(IngestionBlocked, "REDIRECT_REFUSED"):
            NoRedirect().redirect_request(None, None, 302, "Moved",
                                           None, "https://elsewhere.tld")

    def test_http_uses_apikey_only_and_upsert_ignore_duplicates(self):
        events = []
        def mock_transport(method, url, headers, body):
            events.append((method, url, headers, body))
            if method == "GET":
                return (200, b"[]")
            return (201, b"")
        client = ProtectedSupabase(
            "https://example.supabase.co", "sb_secret_abcdefghijklmnopqrst",
            transport=mock_transport
        )
        self.assertEqual(client.existing_run(1, "2026-10-19T12:00:00Z"), [])
        doc = valid_payload()
        client.insert_missing(rows_for(doc)[:2])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][2]["apikey"], "sb_secret_abcdefghijklmnopqrst")
        self.assertNotIn("Authorization", events[0][2])
        self.assertIn("on_conflict", parse_qs(urlparse(events[1][1]).query))
        self.assertEqual(events[1][2]["Prefer"],
                         "resolution=ignore-duplicates,return=minimal")
        self.assertEqual(json.loads(events[1][3])[0]["source_id"], "weathernext3")

    def test_http_rejects_bad_status_without_server_text(self):
        def fake_transport(*args):
            return (503, b'{"sensitive": "SECRET_VALUE"}')
        client = ProtectedSupabase(
            "https://example.supabase.co", "sb_secret_abcdefghijklmnopqrst",
            transport=fake_transport,
        )
        with self.assertRaisesRegex(IngestionBlocked, "SUPABASE_UNEXPECTED_HTTP_503"):
            client.existing_run(1, "2026-10-19T12:00:00Z")


if __name__ == "__main__":
    import unittest
    unittest.main()
