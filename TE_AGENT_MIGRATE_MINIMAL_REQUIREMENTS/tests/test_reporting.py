import json
import tempfile
import unittest
from pathlib import Path

from te_agent_migrate.reporting import RunReport


class ReportingTests(unittest.TestCase):
    def test_final_report_is_json_and_csv_and_redacts_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            report = RunReport(Path(directory), "dry-run")
            report.add_agent({"agent": "TE1", "token": "a" * 32})
            json_path, csv_dir = report.write_final()
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["agents"][0]["token"], "[REDACTED]")
            self.assertTrue((csv_dir / "agents.csv").is_file())
            self.assertTrue((csv_dir / "summary.csv").is_file())


if __name__ == "__main__":
    unittest.main()
