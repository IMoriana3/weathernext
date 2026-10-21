"""Guardrails for the opt-in GitHub OIDC smoke test.

These offline tests do not establish real cloud permissions; they prevent
publication settings from leaking into the identity-only manual path.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ingest.yml"


class IngestWorkflowSafetyTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_safe_manual_mode_is_default_and_not_scheduled(self):
        self.assertIn('options: ["auth-check", "publish"]', self.text)
        self.assertIn('default: "auth-check"', self.text)
        self.assertIn('vars.WN3_INGESTION_APPROVED == \'true\'', self.text)
        self.assertIn('github.event_name == \'workflow_dispatch\'', self.text)

    def test_auth_only_checks_cloud_identity_without_forecast_or_db_write(self):
        before_publish = self.text.split("      - name: Install pinned transport dependencies")[0]
        self.assertIn("WN3_GCP_AUTH_AND_LIST_OK", before_publish)
        self.assertIn("gcloud auth print-access-token >/dev/null", before_publish)
        self.assertIn("gcloud storage ls", before_publish)
        self.assertIn("20261019_06hr_01_preds/predictions.zarr/zarr.json", before_publish)
        self.assertNotIn("2026_to_present/ >/dev/null", before_publish)
        self.assertNotIn("scheduled_ingest.py", before_publish)
        self.assertNotIn("WN3_SUPABASE_SECRET_KEY", before_publish)
        self.assertNotIn("secrets.", before_publish)

    def test_publication_is_gated_and_secret_scoped_to_publish_step(self):
        marker = "      - name: Discover, validate and conditionally publish native point forecast"
        part = self.text.split(marker)[1]
        self.assertIn("vars.WN3_INGESTION_APPROVED == \'true\'", part)
        self.assertIn("inputs.mode == \'publish\'", part)
        self.assertIn("secrets.WN3_SUPABASE_SECRET_KEY", part)
        self.assertIn("scheduled_ingest.py", part)
        self.assertIn("--publish", part)

    def test_wif_is_bound_to_same_workflow_and_checkout_without_credentials(self):
        self.assertIn("google-github-actions/auth@v2", self.text)
        self.assertIn("vars.WN3_GCP_WIF_PROVIDER", self.text)
        self.assertIn("vars.WN3_GCP_SERVICE_ACCOUNT", self.text)
        self.assertIn("persist-credentials: false", self.text)
        self.assertIn("id-token: write", self.text)


if __name__ == "__main__":
    unittest.main()
