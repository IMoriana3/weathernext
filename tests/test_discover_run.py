"""Discovery regression: synthetic GCS folder listings, no cloud calls."""
import subprocess
import unittest
from datetime import datetime, timezone

from scripts.discover_run import (
    BASE, BUCKET, URI, DiscoveryError, _call_gcloud, _run_names,
    discover_recent_synoptic,
)

NOW = datetime(2026, 10, 19, 22, tzinfo=timezone.utc)


def run_uri(folder):
    return f"{URI}/{folder}/predictions.zarr"


def metadata(folder):
    return run_uri(folder) + "/zarr.json"


class DiscoveryTests(unittest.TestCase):
    def test_parse_zarr_v3_folder_paths_and_ignore_interim(self):
        listing = "\n".join([
            URI + "/20261019_18hr_01_preds/",
            URI + "/20261019_12hr_03_preds/predictions.zarr/",
            URI + "/20261019_06hr_01_preds/predictions.zarr/zarr.json",
            URI + "/20261019_13hr_01_preds/predictions.zarr/",
            URI + "/20261018_18hr_01_preds/",
            "gs://unrelated-bucket/20261019_00hr_01_preds/",
        ])
        self.assertEqual(_run_names(listing, "20261019"), [
            "20261019_18hr_01_preds",
            "20261019_12hr_03_preds",
            "20261019_06hr_01_preds",
        ])

    def test_latest_complete_zarr_json_selected_not_legacy_zmetadata(self):
        expected = "20261019_12hr_01_preds"
        calls = []
        def lookup(uri):
            calls.append(uri)
            if uri.endswith("20261019_*"):
                return "\n".join([
                    URI + "/20261019_18hr_01_preds/",
                    URI + f"/{expected}/predictions.zarr/",
                    URI + "/20261019_13hr_01_preds/",
                ])
            if uri == metadata("20261019_18hr_01_preds"):
                return ""  # newest upload not yet complete
            if uri == metadata(expected):
                return uri + "\n"
            self.fail("unexpected listing or metadata URI: " + uri)
        result = discover_recent_synoptic(now=NOW, list_uri=lookup)
        self.assertEqual(result, BASE + "/" + expected + "/predictions.zarr")
        self.assertTrue(any(x.endswith("/zarr.json") for x in calls))
        self.assertFalse(any(".zmetadata" in x for x in calls))
        self.assertEqual(len(calls), 2)  # 18Z is younger than 9h
        self.assertFalse(any(metadata("20261019_18hr_01_preds") == x for x in calls))

    def test_falls_back_one_day_but_never_guesses_a_run(self):
        folder = "20261018_18hr_01_preds"
        calls = []
        def lookup(uri):
            calls.append(uri)
            if uri.endswith("20261019_*"):
                return ""
            if uri.endswith("20261018_*"):
                return URI + "/" + folder + "/predictions.zarr/"
            if uri == metadata(folder):
                return uri
            self.fail("Unexpected request " + uri)
        got = discover_recent_synoptic(now=NOW, days=3, list_uri=lookup)
        self.assertEqual(got, BASE + "/" + folder + "/predictions.zarr")
        self.assertEqual(len(calls), 3)

    def test_empty_recent_prefixes_do_not_claim_no_history(self):
        with self.assertRaisesRegex(DiscoveryError, "NO_RECENT_RUN_FOLDERS"):
            discover_recent_synoptic(now=NOW, list_uri=lambda uri: "")

    def test_visible_folders_without_metadata_report_unverified(self):
        folder = "20261019_06hr_01_preds"
        def lookup(uri):
            if uri.endswith("_*"):
                return URI + "/" + folder + "/"
            if uri == metadata(folder):
                return ""
            self.fail("Unexpected URI " + uri)
        with self.assertRaisesRegex(DiscoveryError, "NO_VERIFIED_METADATA"):
            discover_recent_synoptic(now=NOW, list_uri=lookup)

    def test_authorization_does_not_disguise_itself_as_no_runs(self):
        def denied(*args, **kwargs):
            return subprocess.CompletedProcess(
                args[0], returncode=1, stdout="",
                stderr="403 AccessDenied: storage.objects.list permission denied",
            )
        with self.assertRaisesRegex(DiscoveryError, "ACCESS_DENIED"):
            _call_gcloud(f"{URI}/20261019_*", command=denied)

    def test_404_is_a_bounded_missing_match(self):
        def missing(*args, **kwargs):
            return subprocess.CompletedProcess(
                args[0], returncode=1, stdout="",
                stderr="Error: 404 Not Found",
            )
        self.assertEqual(_call_gcloud(f"{URI}/20261019_*", command=missing), "")

    def test_unexpected_errors_fail_closed(self):
        def failure(*args, **kwargs):
            return subprocess.CompletedProcess(
                args[0], returncode=1, stdout="",
                stderr="Active account has no project configured",
            )
        with self.assertRaisesRegex(DiscoveryError, "GCLOUD_LIST_ERROR"):
            _call_gcloud(f"{URI}/20261019_*", command=failure)

    def test_listing_limits_are_bounded(self):
        with self.assertRaises(ValueError):
            discover_recent_synoptic(now=NOW, days=10, list_uri=lambda uri: "")
        with self.assertRaises(ValueError):
            discover_recent_synoptic(now=NOW, max_checks=99, list_uri=lambda uri: "")

    def test_metadata_check_limit_prevents_many_requests(self):
        folders = [f"20261019_{hour:02d}hr_{version:02d}_preds"
                   for hour in (0, 6, 12, 18) for version in range(1, 3)]
        def lookup(uri):
            if uri.endswith("20261019_*"):
                return "\n".join(URI + "/" + x + "/" for x in folders)
            return ""
        with self.assertRaisesRegex(DiscoveryError, "METADATA_LIMIT"):
            discover_recent_synoptic(
                now=NOW, list_uri=lookup, max_checks=3,
            )


    def test_skips_12z_before_publication_and_selects_mature_06z(self):
        early = datetime(2026, 10, 19, 19, 14, tzinfo=timezone.utc)
        mature = "20261019_06hr_01_preds"
        premature = "20261019_12hr_01_preds"
        calls = []
        def listing(uri):
            calls.append(uri)
            if uri.endswith("20261019_*"):
                return ("\n".join([
                    URI + "/" + premature + "/",
                    URI + "/" + mature + "/",
                ]))
            if uri == metadata(mature):
                return uri
            self.fail("Premature or unexpected metadata listing: " + uri)
        got = discover_recent_synoptic(now=early, list_uri=listing)
        self.assertEqual(got, BASE + "/" + mature + "/predictions.zarr")
        self.assertEqual(len(calls), 2)
        self.assertNotIn(metadata(premature), calls)

    def test_only_premature_runs_report_explicit_error(self):
        early = datetime(2026, 10, 19, 19, 14, tzinfo=timezone.utc)
        def listing(uri):
            if uri.endswith("20261019_*"):
                return URI + "/20261019_12hr_01_preds/"
            return ""
        with self.assertRaisesRegex(DiscoveryError, "NO_MATURE_RUNS"):
            discover_recent_synoptic(now=early, list_uri=listing)


if __name__ == "__main__":
    unittest.main()
