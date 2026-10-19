"""Safe offline checks of the optional interactive Cloud Shell helper."""
from pathlib import Path
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloudshell_latest.sh"


class CloudShellHelperTests(unittest.TestCase):
    def test_shell_syntax_without_gcs_access(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)

    def test_cloudshell_read_is_timed_and_reports_diagnostics(self):
        source = SCRIPT.read_text(encoding='utf-8')
        self.assertIn('timeout --signal=TERM --kill-after=10s', source)
        self.assertIn('READ_TIMEOUT:', source)
        self.assertIn('WN3_READ_TIMEOUT_SECONDS', source)
        self.assertIn('METADATA:', (SCRIPT.parents[1] / 'point_reader.py').read_text())

    def test_horizon_gate_precedes_any_google_call(self):
        run = subprocess.run(
            ["bash", str(SCRIPT), "0", "0", "361"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(run.returncode, 2)
        self.assertIn("1 to 360", run.stderr)
        self.assertNotIn("gs://", run.stdout + run.stderr)

    def test_usage_only_without_credential_access(self):
        run = subprocess.run(
            ["bash", str(SCRIPT), "--help"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(run.returncode, 2)
        self.assertIn("interactive", run.stderr)


if __name__ == "__main__":
    unittest.main()
