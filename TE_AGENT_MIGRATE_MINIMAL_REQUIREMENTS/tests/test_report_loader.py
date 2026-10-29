import json
import os
import shutil
import tempfile
import unittest

try:
    from tests.test_report_fixtures import SECRET_MARKER, build_discovery, write_json, write_selection
except ImportError:  # discovery started inside tests/
    from test_report_fixtures import SECRET_MARKER, build_discovery, write_json, write_selection

from te_report.loader import is_secret_name, load_discovery, load_selection


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "discovery_output")
        build_discovery(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_loads_groups_agents_and_pages(self):
        data = load_discovery(self.root)
        self.assertEqual([g["aid"] for g in data["account_groups"]], ["100", "200"])
        g100 = data["groups"]["100"]
        self.assertEqual(len(g100["agents"]), 6)  # two pages flattened
        self.assertEqual(len(g100["tests"]), 5)
        self.assertEqual(len(g100["dashboards"]), 2)  # page is a plain list
        self.assertEqual(len(g100["dashboard_filters"]), 2)
        self.assertEqual(len(g100["suppression"]), 1)
        self.assertIn("d1", g100["dashboard_details"])
        self.assertIn("s1", g100["suppression_details"])
        self.assertEqual(sorted(data["agent_details"]), ["1", "2"])
        self.assertEqual(data["agent_details"]["1"][0]["_aid"], "100")

    def test_tags_merge_assignments(self):
        data = load_discovery(self.root)
        tags = dict((t["id"], t) for t in data["groups"]["100"]["tags"])
        self.assertEqual(len(tags), 3)
        self.assertEqual(len(tags["tag-agent-1"]["assignments"]), 2)

    def test_missing_optional_files_are_recorded(self):
        data = load_discovery(self.root)
        self.assertIn("aid_200/tests.json", data["missing"])
        self.assertIn("aid_200/tags_assignments.json", data["missing"])
        self.assertNotIn("aid_100/tests.json", data["missing"])
        self.assertEqual(data["groups"]["200"]["tests"], [])

    def test_tolerates_minimal_tree_and_missing_agent_details(self):
        root = os.path.join(self.tmp, "min")
        build_discovery(root, full=False)
        data = load_discovery(root)
        self.assertIn("agent_details/", data["missing"])
        self.assertEqual(len(data["groups"]["100"]["agents"]), 6)

    def test_missing_group_directory(self):
        shutil.rmtree(os.path.join(self.root, "aid_200"))
        data = load_discovery(self.root)
        self.assertIn("aid_200/", data["missing"])
        self.assertEqual(data["groups"]["200"]["agents"], [])

    def test_unreadable_json_is_recorded_not_fatal(self):
        with open(os.path.join(self.root, "aid_100", "tags.json"), "w") as handle:
            handle.write("{not json")
        data = load_discovery(self.root)
        self.assertIn("aid_100/tags.json", data["errors"])

    def test_bare_account_groups_dir_without_listing(self):
        os.remove(os.path.join(self.root, "account_groups.json"))
        data = load_discovery(self.root)
        self.assertIn("account_groups.json", data["missing"])
        self.assertEqual(sorted(g["aid"] for g in data["account_groups"]), ["100", "200"])

    def test_secret_fields_are_dropped(self):
        data = load_discovery(self.root)
        text = json.dumps(data)
        self.assertNotIn(SECRET_MARKER, text)
        self.assertNotIn("transactionScript", text)
        tests = dict((t["testId"], t) for t in data["groups"]["100"]["tests"])
        self.assertNotIn("transactionScript", tests["5005"])
        self.assertNotIn("credentials", tests["5005"])
        self.assertEqual(tests["5005"]["testName"], "script-test")
        agent = [a for a in data["groups"]["100"]["agents"] if a["agentId"] == "1"][0]
        self.assertNotIn("agentToken", agent)

    def test_secret_name_detection(self):
        for name in ("transactionScript", "userPassword", "clientSecret", "apiToken",
                     "credentials", "Authorization"):
            self.assertTrue(is_secret_name(name), name)
        for name in ("testName", "agentId", "title"):
            self.assertFalse(is_secret_name(name), name)

    def test_load_selection_ignores_comments_and_blanks(self):
        path = write_selection(
            os.path.join(self.tmp, "sel.txt"),
            ["# header", "", "  agent-a  ", "10.0.0.2  # trailing", "   ", "123"],
        )
        self.assertEqual(load_selection(path), ["agent-a", "10.0.0.2", "123"])


if __name__ == "__main__":
    unittest.main()
