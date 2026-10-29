import json
import os
import shutil
import tempfile
import unittest

try:
    from dev.tests.test_report_fixtures import SECRET_MARKER, build_discovery
except ImportError:
    from test_report_fixtures import SECRET_MARKER, build_discovery

from te_report.graph import build_graph, classify_filter
from te_report.loader import load_discovery


def edge_set(graph, relation):
    return set((e["parent_id"], e["child_id"]) for e in graph.edges if e["relation"] == relation)


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.join(cls.tmp, "discovery_output")
        build_discovery(cls.root)
        cls.graph = build_graph(load_discovery(cls.root))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_node_kinds_and_ids(self):
        g = self.graph
        for node_id, kind in (
            ("group:100", "group"), ("agent:1", "agent"), ("cluster:4", "cluster"),
            ("member:4:cm1", "member"), ("test:5001", "test"), ("tag:100:tag-agent-1", "tag"),
            ("dashboard:100:d1", "dashboard"), ("widget:100:d1:w1", "widget"),
            ("global_filter:100:gf1", "global_filter"), ("suppression:100:s1", "suppression"),
            ("alert_rule:100:alert:r1", "alert_rule"),
        ):
            self.assertEqual(g.nodes[node_id]["kind"], kind, node_id)
            self.assertIn("attrs", g.nodes[node_id])
        # alert rule without details is not a node
        self.assertNotIn("alert_rule:100:alert:r2", g.nodes)

    def test_agent_visible_in_every_group_with_owner_unknown(self):
        g = self.graph
        self.assertEqual(sorted(g.children("agent:1", "visible_in")), ["group:100", "group:200"])
        edges = g.out_edges("agent:1", "visible_in")
        self.assertTrue(all(e["attrs"]["owner_unknown"] for e in edges))
        self.assertEqual(g.nodes["agent:1"]["attrs"]["shared_count"], 2)
        # group 300 is only known from the agent detail
        self.assertEqual(g.nodes["agent:2"]["attrs"]["shared_with"], ["100", "300"])
        self.assertEqual(g.parents("group:200"), ["agent:1"])

    def test_owner_known_when_detail_has_owner(self):
        data = load_discovery(self.root)
        data["agent_details"]["1"][0]["ownerAid"] = "100"
        g = build_graph(data)
        by_group = dict((e["child_id"], e["attrs"]) for e in g.out_edges("agent:1", "visible_in"))
        self.assertFalse(by_group["group:100"]["owner_unknown"])
        self.assertTrue(by_group["group:100"]["owner"])
        self.assertFalse(by_group["group:200"]["owner"])

    def test_cluster_members_and_tests(self):
        g = self.graph
        self.assertEqual(sorted(g.children("cluster:4", "member_of")), ["member:4:cm1", "member:4:cm2"])
        self.assertEqual(g.nodes["cluster:4"]["attrs"]["member_ips"], ["10.0.1.1", "10.0.1.2"])
        self.assertEqual(g.children("cluster:4", "assigned_test"), ["test:5004"])

    def test_assigned_test_edges_carry_group(self):
        g = self.graph
        edges = g.out_edges("agent:1", "assigned_test")
        self.assertEqual(sorted(e["child_id"] for e in edges), ["test:5001", "test:5002"])
        self.assertTrue(all(e["attrs"]["group"] == "100" for e in edges))
        self.assertEqual(sorted(g.parents("test:5002", "assigned_test")), ["agent:1", "agent:2"])

    def test_targets_agent(self):
        self.assertEqual(edge_set(self.graph, "targets_agent"), set([("test:5003", "agent:1")]))

    def test_tag_edges_from_assignments(self):
        g = self.graph
        self.assertEqual(
            edge_set(g, "tagged"),
            set([
                ("agent:1", "tag:100:tag-agent-1"), ("agent:2", "tag:100:tag-agent-1"),
                ("test:5001", "tag:100:tag-test-1"), ("test:5002", "tag:100:tag-test-1"),
                ("dashboard:100:d1", "tag:100:tag-dash-1"),
            ]),
        )

    def test_dashboard_widgets_and_global_filter(self):
        g = self.graph
        self.assertEqual(len(g.children("dashboard:100:d1", "has_widget")), 7)
        self.assertEqual(g.children("dashboard:100:d1", "uses_global_filter"), ["global_filter:100:gf1"])
        self.assertEqual(g.children("dashboard:100:d2", "uses_global_filter"), ["global_filter:100:gf2"])
        self.assertEqual(g.children("dashboard:100:d2", "has_widget"), [])

    def test_widget_filter_edges(self):
        g = self.graph
        self.assertEqual(g.children("widget:100:d1:w1", "filters_on_agent"), ["agent:1"])
        self.assertEqual(g.children("widget:100:d1:w1", "filters_on_test"), ["test:5001"])
        # tag filter resolved through assignments (tag id) and legacy label id
        self.assertEqual(sorted(g.children("widget:100:d1:w2", "filters_on_agent")), ["agent:1", "agent:2"])
        self.assertEqual(sorted(g.children("widget:100:d1:w5", "filters_on_agent")), ["agent:1", "agent:2"])
        via = set(e["attrs"]["via"] for e in g.out_edges("widget:100:d1:w5", "filters_on_agent"))
        self.assertEqual(via, set(["AGENT_LABEL"]))
        # test tags resolved to tests; endpoint filters ignored
        self.assertEqual(sorted(g.children("widget:100:d1:w4", "filters_on_test")), ["test:5001", "test:5002"])
        # nested numberCards filters are picked up
        self.assertEqual(g.children("widget:100:d1:w6", "filters_on_agent"), ["agent:2"])
        self.assertEqual(g.nodes["widget:100:d1:w3"]["attrs"]["show"], "Owned Agents")

    def test_global_filter_edges(self):
        g = self.graph
        self.assertEqual(g.children("global_filter:100:gf1", "filters_on_agent"), ["agent:2"])
        self.assertEqual(sorted(g.children("global_filter:100:gf1", "filters_on_test")), ["test:5001", "test:5002"])
        self.assertEqual(sorted(g.children("global_filter:100:gf2", "filters_on_agent")), ["agent:1", "agent:2"])

    def test_alert_rule_and_suppression_edges(self):
        g = self.graph
        self.assertEqual(g.children("test:5001", "alert_rule"), ["alert_rule:100:alert:r1"])
        self.assertEqual(g.children("agent:2", "agent_notif_rule"), ["alert_rule:100:agent:n1"])
        self.assertEqual(g.children("suppression:100:s1", "applies_to_agent"), ["agent:2"])

    def test_helpers(self):
        g = self.graph
        desc = g.descendants("widget:100:d1:w1")
        self.assertIn("agent:1", desc)
        self.assertIn("test:5002", desc)  # via agent -> assigned_test
        self.assertIn("tag:100:tag-test-1", desc)  # via test -> tag
        self.assertNotIn("widget:100:d1:w1", desc)  # cycle safe, excludes self
        self.assertIn("widget:100:d1:w1", g.descendants("dashboard:100:d1"))
        self.assertEqual([n["id"] for n in g.find("agent", "AGENT-B")], ["agent:2"])
        self.assertEqual([n["id"] for n in g.find("cluster", "10.0.1.2")], ["cluster:4"])
        self.assertEqual(g.find("test", "no-such-thing"), [])

    def test_to_dict_and_write_json(self):
        g = self.graph
        data = g.to_dict()
        self.assertEqual(data["schema"], "te_report.graph/1")
        for node in data["nodes"]:
            self.assertEqual(sorted(node), ["attrs", "id", "kind", "name"])
        for edge in data["edges"]:
            self.assertEqual(sorted(edge), ["attrs", "child_id", "parent_id", "relation"])
        path = g.write_json(os.path.join(self.tmp, "graph.json"))
        with open(path) as handle:
            text = handle.read()
        self.assertEqual(json.loads(text)["stats"]["agent"], 4)
        self.assertNotIn(SECRET_MARKER, text)
        self.assertNotIn("transactionScript", text)

    def test_classify_filter(self):
        self.assertEqual(classify_filter("TARGET_AGENT"), ("agent", "id"))
        self.assertEqual(classify_filter("agent_tag"), ("agent", "tag"))
        self.assertEqual(classify_filter("TEST_LABEL"), ("test", "tag"))
        self.assertIsNone(classify_filter("ENDPOINT_AGENT_TAG_ID"))


if __name__ == "__main__":
    unittest.main()
