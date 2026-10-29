import csv
import io
import json
import os
import shutil
import tempfile
import unittest

try:
    from dev.tests.test_report_fixtures import SECRET_MARKER, build_discovery, write_selection
except ImportError:
    from test_report_fixtures import SECRET_MARKER, build_discovery, write_selection

from te_report.__main__ import main
from te_report.outputs import safe_cell


def read_csv(path):
    with io.open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "discovery_output")
        build_discovery(self.root)
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, selection=None):
        argv = ["--input", self.root, "--output", self.out]
        if selection is not None:
            argv += ["--selection", write_selection(os.path.join(self.tmp, "sel.txt"), selection)]
        buf = io.StringIO()
        self.assertEqual(main(argv, out=buf), 0)
        return buf.getvalue()

    def all_output_text(self):
        text = ""
        for name in os.listdir(self.out):
            with io.open(os.path.join(self.out, name), "r", encoding="utf-8") as handle:
                text += handle.read()
        return text

    def test_run_with_selection_writes_all_files(self):
        text = self.run_cli(["# my agents", "agent-a", "10.0.1.2", "agent-off", "ghost"])
        self.assertEqual(
            sorted(os.listdir(self.out)),
            [
                "dashboards.csv", "dashboards.html", "findings.csv", "graph.json", "offline.csv",
                "selected.csv", "shares.csv", "tree.csv", "widgets.csv",
            ],
        )
        self.assertIn("2 agents/clusters selected", text)
        self.assertIn("1 excluded offline", text)
        self.assertIn("1 unmatched", text)
        self.assertIn("unmatched: ghost", text)
        self.assertIn("BREAKS=", text)
        self.assertIn("Dashboards: BREAKS=", text)
        self.assertIn("Widgets:    BREAKS=", text)

        selected = read_csv(os.path.join(self.out, "selected.csv"))
        self.assertEqual(sorted(r["agent_id"] for r in selected), ["1", "4"])
        self.assertEqual([r["agent_id"] for r in read_csv(os.path.join(self.out, "offline.csv"))], ["3"])

        findings = read_csv(os.path.join(self.out, "findings.csv"))
        self.assertEqual(
            list(findings[0].keys())[:7],
            ["level", "element_kind", "element_id", "element_name", "reason", "fix", "fix_owner"],
        )
        self.assertEqual(findings[0]["level"], "BREAKS")
        self.assertTrue(any(r["level"] == "OK" for r in findings))

    def test_ambiguous_identifier_reported_not_selected(self):
        path = os.path.join(self.root, "aid_100", "agents.json")
        with io.open(path, "r", encoding="utf-8") as handle:
            pages = json.load(handle)
        pages[0]["agents"].append({
            "agentId": "77", "agentName": "agent-a", "agentType": "enterprise",
            "agentState": "online", "ipAddresses": ["10.0.0.77"],
        })
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(pages))
        text = self.run_cli(["agent-a", "10.0.1.2", "10.0.0.2"])
        self.assertIn("1 ambiguous", text)
        self.assertIn("2 agents/clusters selected", text)  # cluster-c and agent-b
        self.assertIn("ambiguous (not selected): agent-a", text)
        selected = read_csv(os.path.join(self.out, "selected.csv"))
        self.assertEqual(sorted(r["agent_id"] for r in selected), ["2", "4"])

    def test_shares_csv_one_row_per_agent_and_group(self):
        self.run_cli(["agent-a"])
        rows = [r for r in read_csv(os.path.join(self.out, "shares.csv")) if r["agent_id"] == "1"]
        self.assertEqual(sorted(r["group_aid"] for r in rows), ["100", "200"])
        self.assertEqual(set(r["selected"] for r in rows), set(["yes"]))
        self.assertEqual(set(r["owner_unknown"] for r in rows), set(["yes"]))
        agent2 = [r for r in read_csv(os.path.join(self.out, "shares.csv")) if r["agent_id"] == "2"]
        self.assertEqual(sorted((r["group_aid"], r["listed_in_group"]) for r in agent2),
                         [("100", "yes"), ("300", "no")])

    def test_tree_csv_depth_and_order(self):
        self.run_cli(["agent-a"])
        rows = read_csv(os.path.join(self.out, "tree.csv"))
        self.assertEqual(rows[0]["kind"], "group")
        self.assertEqual(rows[0]["depth"], "0")
        by_id = dict((r["id"], r) for r in rows if r["id"] in (
            "agent:1", "cluster:4", "member:4:cm1", "dashboard:100:d1", "widget:100:d1:w1",
            "alert_rule:100:alert:r1", "test:5005"))
        self.assertEqual(by_id["agent:1"]["depth"], "1")
        self.assertEqual(by_id["agent:1"]["selected"], "yes")
        self.assertEqual(by_id["member:4:cm1"]["depth"], "2")
        self.assertEqual(by_id["dashboard:100:d1"]["depth"], "1")
        self.assertEqual(by_id["widget:100:d1:w1"]["depth"], "2")
        self.assertEqual(by_id["alert_rule:100:alert:r1"]["depth"], "3")  # group > agent > test > rule
        self.assertEqual(by_id["test:5005"]["depth"], "1")  # unassigned test sits under the group
        self.assertTrue(by_id["widget:100:d1:w1"]["name"].startswith("    "))
        # a child always follows its parent in file order
        ids = [r["id"] for r in rows]
        self.assertLess(ids.index("cluster:4"), ids.index("member:4:cm1"))
        self.assertLess(ids.index("dashboard:100:d1"), ids.index("widget:100:d1:w1"))

    def test_inventory_only_without_selection(self):
        text = self.run_cli()
        self.assertIn("inventory-only", text)
        self.assertEqual(sorted(os.listdir(self.out)), ["graph.json", "offline.csv", "shares.csv", "tree.csv"])
        tree = read_csv(os.path.join(self.out, "tree.csv"))
        self.assertEqual(set(r["selected"] for r in tree), set([""]))
        offline = set(r["agent_id"] for r in read_csv(os.path.join(self.out, "offline.csv")))
        self.assertEqual(offline, set(["3", "5", "6"]))
        with open(os.path.join(self.out, "graph.json")) as handle:
            self.assertEqual(json.load(handle)["schema"], "te_report.graph/1")

    def test_no_secret_values_in_outputs_or_console(self):
        text = self.run_cli(["agent-a", "10.0.1.2"])
        combined = text + self.all_output_text()
        self.assertNotIn(SECRET_MARKER, combined)
        self.assertNotIn("transactionScript", combined)
        self.assertNotIn("agentToken", combined)

    def test_creates_output_dir_and_handles_missing_files(self):
        shutil.rmtree(os.path.join(self.root, "aid_200"))
        text = self.run_cli(["agent-a"])
        self.assertIn("Missing optional files", text)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "findings.csv")))

    def test_safe_cell_neutralises_formulas(self):
        self.assertEqual(safe_cell("=SUM(A1)"), "'=SUM(A1)")
        self.assertEqual(safe_cell("@cmd"), "'@cmd")
        self.assertEqual(safe_cell("-1"), "-1")
        self.assertEqual(safe_cell(None), "")
        self.assertEqual(safe_cell(True), "yes")


if __name__ == "__main__":
    unittest.main()
