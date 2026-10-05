"""Human pause persistence and honest status; no GUI needed for these checks."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workbench"))
from status_panel import request_pause, status_text


class PanelTests(unittest.TestCase):
    def test_pause_survives_missing_holder_without_claiming_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            record, response = request_pause(root)
            saved = json.loads(next(root.glob("human-request-*.json")).read_text())
            self.assertEqual(record, saved)
            self.assertFalse(saved["safe_to_use"])
            self.assertEqual(response["state"], "RECOVERY_REQUIRED")

    def test_cached_safe_label_never_grants_human_control(self):
        title, detail = status_text(dict(ok=True, state="HUMAN_SAFE", active_steps=0))
        self.assertEqual(title, "Await handback confirmation")
        self.assertIn("Supervisor", detail)
        self.assertEqual(status_text(dict(ok=False, state="HUMAN_SAFE"))[0], "Control unverified")

    def test_drain_reports_unknown_eta(self):
        title, detail = status_text(dict(ok=True, state="DRAINING", active_steps=2))
        self.assertEqual(title, "Pause requested")
        self.assertIn("2", detail)
        self.assertIn("unknown", detail)


if __name__ == "__main__":
    unittest.main()
