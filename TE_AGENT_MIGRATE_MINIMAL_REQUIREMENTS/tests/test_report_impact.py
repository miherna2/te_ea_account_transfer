import os
import shutil
import tempfile
import unittest

try:
    from tests.test_report_fixtures import build_discovery
except ImportError:
    from test_report_fixtures import build_discovery

from te_report.graph import build_graph
from te_report.impact import flag_impact, resolve_selection, summarize
from te_report.loader import load_discovery


class ImpactBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.join(cls.tmp, "discovery_output")
        build_discovery(cls.root)
        cls.graph = build_graph(load_discovery(cls.root))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def flags(self, selection):
        res = resolve_selection(self.graph, selection)
        return flag_impact(self.graph, res["selected"])

    def pick(self, findings, element_id, level=None, rule=None):
        return [
            f for f in findings
            if f["element_id"] == element_id
            and (level is None or f["level"] == level)
            and (rule is None or f["rule"] == rule)
        ]


class SelectionTests(ImpactBase):
    def test_match_by_id_name_and_ip(self):
        res = resolve_selection(self.graph, ["1", "AGENT-B", "10.0.0.2"])
        self.assertEqual(res["selected"], set(["agent:1", "agent:2"]))
        self.assertEqual(res["unmatched"], [])
        self.assertEqual(res["matches"]["AGENT-B"], ["agent:2"])

    def test_cluster_matches_by_member_ip_name_and_id(self):
        for token in ("10.0.1.2", "node1", "cm2", "cluster-c", "4"):
            res = resolve_selection(self.graph, [token])
            self.assertEqual(res["selected"], set(["cluster:4"]), token)

    def test_unmatched_reported(self):
        res = resolve_selection(self.graph, ["ghost", "agent-a", "10.9.9.9"])
        self.assertEqual(res["unmatched"], ["ghost", "10.9.9.9"])
        self.assertEqual(res["selected"], set(["agent:1"]))

    def test_offline_and_disabled_excluded(self):
        res = resolve_selection(self.graph, ["agent-off", "agent-disabled", "agent-a"])
        self.assertEqual(res["selected"], set(["agent:1"]))
        self.assertEqual(res["excluded_offline"], set(["agent:3", "agent:6"]))

    def test_public_ip_is_not_an_identifier(self):
        data = load_discovery(self.root)
        data["groups"]["100"]["agents"][0]["publicIpAddresses"] = ["203.0.113.5"]
        data["groups"]["100"]["agents"][1]["publicIpAddresses"] = ["203.0.113.5"]
        data["groups"]["100"]["agents"][3]["clusterMembers"][0]["publicIpAddresses"] = ["203.0.113.5"]
        graph = build_graph(data)
        res = resolve_selection(graph, ["203.0.113.5", "10.0.0.1"])
        self.assertEqual(res["unmatched"], ["203.0.113.5"])
        self.assertEqual(res["ambiguous"], {})
        self.assertEqual(res["selected"], set(["agent:1"]))

    def test_ambiguous_identifier_selects_nothing(self):
        data = load_discovery(self.root)
        dup = dict(data["groups"]["100"]["agents"][1])  # agent-b data, new id, same name
        dup["agentId"] = "77"
        dup["agentName"] = "agent-a"
        dup["ipAddresses"] = ["10.0.0.77"]
        data["groups"]["100"]["agents"].append(dup)
        graph = build_graph(data)
        res = resolve_selection(graph, ["agent-a", "AGENT-A", "10.0.0.77", "1", "10.0.0.2"])
        self.assertEqual(sorted(res["ambiguous"]), ["AGENT-A", "agent-a"])
        self.assertEqual(res["ambiguous"]["agent-a"], ["agent:1", "agent:77"])
        self.assertEqual(res["selected"], set(["agent:77", "agent:1", "agent:2"]))
        self.assertNotIn("agent-a", res["matches"])
        # only the unambiguous identifiers select something
        only = resolve_selection(graph, ["agent-a"])
        self.assertEqual(only["selected"], set())
        self.assertEqual(only["unmatched"], [])
        self.assertEqual(flag_impact(graph, only["selected"]), [])

    def test_no_ambiguity_in_plain_fixture(self):
        res = resolve_selection(self.graph, ["agent-a", "10.0.1.1"])
        self.assertEqual(res["ambiguous"], {})

    def test_cluster_with_offline_member_excluded(self):
        res = resolve_selection(self.graph, ["10.0.2.1", "cluster-c"])  # member of cluster-bad is online, other is not
        self.assertEqual(res["excluded_offline"], set(["cluster:5"]))
        self.assertEqual(res["selected"], set(["cluster:4"]))


class RuleTests(ImpactBase):
    def test_agent_id_filters_break(self):
        f = self.flags(["agent-a"])
        w1 = self.pick(f, "widget:100:d1:w1", "BREAKS", "filter_agent_id")
        self.assertEqual(len(w1), 1)
        self.assertEqual(w1[0]["fix_owner"], "operator")
        self.assertIn("new agent ID", w1[0]["fix"])
        self.assertEqual(w1[0]["element_name"], "Dash One / agent-id widget")
        f2 = self.flags(["agent-b"])
        self.assertTrue(self.pick(f2, "widget:100:d1:w6", "BREAKS"))  # nested TARGET_AGENT
        self.assertTrue(self.pick(f2, "global_filter:100:gf1", "BREAKS"))  # SOURCE_AND_TARGET_AGENT

    def test_tag_and_label_filters_review(self):
        f = self.flags(["agent-a"])
        for element in ("widget:100:d1:w2", "widget:100:d1:w5", "global_filter:100:gf2"):
            hit = self.pick(f, element, "REVIEW", "filter_agent_tag")
            self.assertEqual(len(hit), 1, element)
            self.assertEqual(hit[0]["fix_owner"], "tool")
        self.assertFalse(self.pick(f, "widget:100:d1:w2", "BREAKS"))

    def test_owned_agents_status_widget_review(self):
        f = self.flags(["agent-a"])
        self.assertEqual(len(self.pick(f, "widget:100:d1:w3", "REVIEW", "widget_owned_agents")), 1)
        self.assertFalse(self.pick(f, "widget:100:d1:w7"))  # 'All Assigned Agents'

    def test_test_only_widgets_are_ok(self):
        f = self.flags(["agent-a"])
        self.assertEqual(len(self.pick(f, "widget:100:d1:w4", "OK")), 1)
        self.assertFalse(self.pick(f, "widget:100:d1:w4", "BREAKS"))
        self.assertFalse(self.pick(f, "widget:100:d1:w4", "REVIEW"))

    def test_agent_to_agent_target_breaks(self):
        f = self.flags(["agent-a"])
        hit = self.pick(f, "test:5003", "BREAKS", "test_target_agent")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["fix_owner"], "operator")

    def test_standalone_agent_replaced_is_ok(self):
        f = self.flags(["agent-a"])
        hit = self.pick(f, "test:5001", None, "test_all_agents_selected")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["level"], "OK")
        self.assertIn(
            "agent is replaced and the tests are assigned to the new agent before the old agent is deleted",
            hit[0]["reason"],
        )
        self.assertIn("agent-a", hit[0]["reason"])
        self.assertEqual(hit[0]["fix_owner"], "tool")
        self.assertFalse(self.pick(f, "test:5001", None, "test_keeps_id"))
        self.assertFalse([x for x in f if x["rule"] == "test_all_agents_selected" and x["level"] == "BREAKS"])
        # test 5002 also has agent-b, so it keeps working and its ID is unchanged
        self.assertEqual(len(self.pick(f, "test:5002", "OK", "test_keeps_id")), 1)
        self.assertFalse(self.pick(f, "test:5002", None, "test_all_agents_selected"))
        # selecting both agents covers 5002 as well
        both = self.flags(["agent-a", "agent-b"])
        self.assertTrue(self.pick(both, "test:5002", "OK", "test_all_agents_selected"))

    def test_cluster_tests_have_no_agent_in_window(self):
        f = self.flags(["cluster-c"])
        hit = self.pick(f, "test:5004", None, "test_all_agents_selected")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["level"], "REVIEW")
        self.assertIn("cluster tests have no agent during the maintenance window", hit[0]["reason"])
        self.assertEqual(hit[0]["fix_owner"], "operator")
        self.assertFalse(self.pick(f, "test:5004", "BREAKS"))

    def test_mixed_standalone_and_cluster_is_review(self):
        data = load_discovery(self.root)
        data["groups"]["100"]["agents"][0]["testIds"].append(5004)  # agent-a also serves the cluster test
        graph = build_graph(data)
        res = resolve_selection(graph, ["agent-a", "cluster-c"])
        f = flag_impact(graph, res["selected"])
        hit = [x for x in f if x["element_id"] == "test:5004" and x["rule"] == "test_all_agents_selected"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["level"], "REVIEW")
        self.assertEqual(hit[0]["fix_owner"], "operator")
        # only the standalone agent selected: the cluster still serves the test
        only = flag_impact(graph, resolve_selection(graph, ["agent-a"])["selected"])
        self.assertFalse([x for x in only if x["element_id"] == "test:5004" and x["rule"] == "test_all_agents_selected"])

    def test_one_finding_per_test_and_rule_with_agent_list(self):
        both = self.flags(["agent-a", "agent-b"])
        hit = self.pick(both, "test:5002", None, "test_all_agents_selected")
        self.assertEqual(len(hit), 1)
        self.assertIn("agent-a", hit[0]["reason"])
        self.assertIn("agent-b", hit[0]["reason"])
        keys = [(x["element_id"], x["rule"]) for x in both]
        self.assertEqual(len(keys), len(set(keys)))
        # a test with more than three agents lists three and counts the rest
        data = load_discovery(self.root)
        names = []
        for n in range(5):
            extra = dict(data["groups"]["100"]["agents"][1])
            extra["agentId"] = str(80 + n)
            extra["agentName"] = "extra-%d" % n
            extra["ipAddresses"] = ["10.0.9.%d" % n]
            extra["testIds"] = [5002]
            data["groups"]["100"]["agents"].append(extra)
            names.append(extra["agentName"])
        graph = build_graph(data)
        res = resolve_selection(graph, names + ["agent-a", "agent-b"])
        f = flag_impact(graph, res["selected"])
        hit = [x for x in f if x["element_id"] == "test:5002" and x["rule"] == "test_all_agents_selected"]
        self.assertEqual(len(hit), 1)
        self.assertIn("(+4 more)", hit[0]["reason"])
        keys = [(x["element_id"], x["rule"]) for x in f]
        self.assertEqual(len(keys), len(set(keys)))

    def test_shared_agent_review(self):
        f = self.flags(["agent-a", "agent-b"])
        for node in ("agent:1", "agent:2"):
            hit = self.pick(f, node, "REVIEW", "sharing_lost")
            self.assertEqual(len(hit), 1)
            self.assertIn("sharing is lost on reset", hit[0]["reason"])
            self.assertEqual(hit[0]["fix_owner"], "tool")

    def test_unshared_agent_has_no_sharing_review(self):
        data = load_discovery(self.root)
        data["agent_details"] = {}
        data["groups"]["200"]["agents"] = []
        graph = build_graph(data)
        res = resolve_selection(graph, ["agent-a"])
        f = flag_impact(graph, res["selected"])
        self.assertFalse([x for x in f if x["rule"] == "sharing_lost"])
        self.assertTrue([x for x in f if x["rule"] == "single_group"])

    def test_cluster_review(self):
        f = self.flags(["10.0.1.1"])
        hit = self.pick(f, "cluster:4", "REVIEW", "cluster_id_changes")
        self.assertEqual(len(hit), 1)
        self.assertIn("Cluster ID changes after rebuild", hit[0]["reason"])
        self.assertTrue(self.pick(f, "test:5004", "REVIEW", "test_all_agents_selected"))

    def test_suppression_window_review(self):
        f = self.flags(["agent-b"])
        self.assertEqual(len(self.pick(f, "suppression:100:s1", "REVIEW")), 1)
        f = self.flags(["agent-a"])
        self.assertFalse(self.pick(f, "suppression:100:s1"))

    def test_finding_fields_and_summary(self):
        f = self.flags(["agent-a", "10.0.1.1"])
        keys = set(["level", "element_kind", "element_id", "element_name", "reason", "fix", "fix_owner"])
        for item in f:
            self.assertTrue(keys <= set(item))
            self.assertIn(item["level"], ("OK", "REVIEW", "BREAKS"))
            self.assertIn(item["fix_owner"], ("operator", "tool"))
        counts = summarize(f)
        self.assertEqual(counts["total"], len(f))
        self.assertEqual(counts["BREAKS"] + counts["REVIEW"] + counts["OK"], len(f))
        self.assertGreater(counts["BREAKS"], 0)
        # worst first
        levels = [x["level"] for x in f]
        self.assertEqual(levels, sorted(levels, key=["BREAKS", "REVIEW", "OK"].index))

    def test_empty_selection_has_no_findings(self):
        self.assertEqual(flag_impact(self.graph, set()), [])


class DataRuleTests(ImpactBase):
    """Rules that need data the base fixture does not hold (built by mutation)."""

    def run_data(self, mutate, selection):
        data = load_discovery(self.root)
        mutate(data)
        graph = build_graph(data)
        res = resolve_selection(graph, selection)
        return flag_impact(graph, res["selected"])

    def add_dash(self, data, widgets, aid="100", dash_id="dz"):
        """A dashboard without global filter (d1 has one that filters on agent 2)."""
        data["groups"][aid].setdefault("dashboard_details", {})[dash_id] = {
            "dashboardId": dash_id, "title": "Dash " + dash_id, "widgets": widgets,
        }

    # -- owned agents ------------------------------------------------------
    def test_owned_agents_owner_unknown_text(self):
        f = self.flags(["agent-a"])
        hit = self.pick(f, "widget:100:d1:w3", "REVIEW", "widget_owned_agents")
        self.assertEqual(len(hit), 1)
        self.assertIn("owner unknown", hit[0]["reason"])

    def test_owned_agents_only_owner_group_when_known(self):
        def owner_200(data):
            data["agent_details"]["1"][0]["ownerAid"] = "200"

        def owner_100(data):
            data["agent_details"]["1"][0]["ownerAid"] = "100"

        # widget lives in group 100; the owner is 200 -> no finding
        self.assertFalse([x for x in self.run_data(owner_200, ["agent-a"]) if x["rule"] == "widget_owned_agents"])
        hit = [x for x in self.run_data(owner_100, ["agent-a"]) if x["rule"] == "widget_owned_agents"]
        self.assertEqual(len(hit), 1)
        self.assertNotIn("owner unknown", hit[0]["reason"])

    # -- group by agent ----------------------------------------------------
    def test_group_by_agent_each_field(self):
        for field, value in (
            ("groupBy", "AGENT"), ("groupCardsBy", ["TEST", "AGENT"]),
            ("columnGroupBy", "AGENT"), ("axisGroupBy", "AGENT"),
        ):
            def mutate(data, field=field, value=value):
                self.add_dash(data, [
                    {"id": "g1", "title": "grouped", "type": "Table", field: value, "filters": {"TEST": ["5001"]}}
                ])

            f = self.run_data(mutate, ["agent-a"])
            hit = [x for x in f if x["rule"] == "widget_group_by_agent"]
            self.assertEqual([x["element_id"] for x in hit], ["widget:100:dz:g1"], field)
            self.assertEqual(hit[0]["level"], "REVIEW")
            self.assertEqual(hit[0]["fix_owner"], "operator")
            self.assertIn("history splits: the new agent has a new id", hit[0]["reason"])

    def test_group_by_agent_negative_cases(self):
        def mutate(data):
            self.add_dash(data, [
                {"id": "n1", "title": "by test", "type": "Table", "groupBy": "TEST", "filters": {}},
                {"id": "n2", "title": "other tests", "type": "Table", "groupBy": "AGENT",
                 "filters": {"TEST": ["5003"]}},  # served only by agent-b
                {"id": "n3", "title": "other agent", "type": "Table", "groupBy": "AGENT",
                 "filters": {"AGENT": ["2"]}},
                {"id": "p1", "title": "no test filter", "type": "Table", "groupBy": "AGENT", "filters": {}},
            ])
            # group 200 sees agent-a but none of its tests
            data["groups"]["200"]["dashboard_details"]["dx"] = {
                "dashboardId": "dx", "title": "Dash X",
                "widgets": [{"id": "x1", "title": "x", "type": "Table", "groupBy": "AGENT", "filters": {}}],
            }

        f = self.run_data(mutate, ["agent-a"])
        hit = sorted(x["element_id"] for x in f if x["rule"] == "widget_group_by_agent")
        self.assertEqual(hit, ["widget:100:dz:p1"])
        # selecting agent-b: only the widget on test 5003 and the unfiltered widget (agent-b serves group 100)
        f = self.run_data(mutate, ["agent-b"])
        hit = sorted(x["element_id"] for x in f if x["rule"] == "widget_group_by_agent")
        self.assertEqual(hit, ["widget:100:dz:n2", "widget:100:dz:n3", "widget:100:dz:p1"])

    def test_group_by_agent_respects_global_filter_agents(self):
        def mutate(data):
            # dashboard d1 uses global filter gf1, which filters on agent 2 only
            data["groups"]["100"]["dashboard_details"]["d1"]["widgets"].append(
                {"id": "g3", "title": "g", "type": "Table", "groupBy": "AGENT", "filters": {}})

        self.assertFalse([x for x in self.run_data(mutate, ["agent-a"]) if x["rule"] == "widget_group_by_agent"])
        hit = [x for x in self.run_data(mutate, ["agent-b"]) if x["rule"] == "widget_group_by_agent"]
        self.assertEqual([x["element_id"] for x in hit], ["widget:100:d1:g3"])

    def test_group_by_agent_attr_in_graph(self):
        def mutate(data):
            self.add_dash(data, [{"id": "g2", "title": "g", "type": "Table", "groupBy": "AGENT", "filters": {}}])

        data = load_discovery(self.root)
        mutate(data)
        graph = build_graph(data)
        self.assertTrue(graph.nodes["widget:100:dz:g2"]["attrs"]["group_by_agent"])
        self.assertNotIn("group_by_agent", graph.nodes["widget:100:d1:w1"]["attrs"])

    # -- alert list --------------------------------------------------------
    def test_alert_list_widget(self):
        def mutate(data):
            self.add_dash(data, [
                {"id": "a1", "title": "alerts", "type": "Alert List", "filters": {}},
                {"id": "a2", "title": "alerts of tests", "type": "Alert List", "filters": {"TEST": ["5003"]}},
                {"id": "a3", "title": "not a list", "type": "Table", "filters": {}},
            ])
            data["groups"]["200"]["dashboard_details"]["dx"] = {
                "dashboardId": "dx", "title": "Dash X",
                "widgets": [{"id": "x1", "title": "x", "type": "Alert List", "filters": {}}],
            }

        f = self.run_data(mutate, ["agent-a"])
        hit = [x for x in f if x["rule"] == "widget_alert_list"]
        self.assertEqual([x["element_id"] for x in hit], ["widget:100:dz:a1"])
        self.assertEqual(hit[0]["level"], "REVIEW")
        self.assertEqual(hit[0]["fix_owner"], "operator")
        self.assertIn("active alerts for the old agent clear, new ones start", hit[0]["reason"])

    # -- agent notification rules -----------------------------------------
    def test_agent_notification_rule(self):
        f = self.flags(["agent-b"])
        hit = self.pick(f, "agent:2", "REVIEW", "agent_notification_rule")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["fix_owner"], "operator")
        self.assertIn("agent notification rules cannot be set by API; restore by hand", hit[0]["reason"])
        self.assertFalse(self.pick(self.flags(["agent-a"]), "agent:1", None, "agent_notification_rule"))

    # -- alert rules with minimum sources ---------------------------------
    def test_alert_min_sources_on_cluster_tests(self):
        def mutate(data):
            data["groups"]["100"]["alert_rules"].extend([
                {"ruleId": "m1", "ruleName": "min", "minimumSources": 2, "tests": [{"testId": "5004"}]},
                {"ruleId": "m2", "ruleName": "pct", "minimumSourcesPct": 50.0, "testIds": ["5004"]},
                {"ruleId": "m3", "ruleName": "no min", "tests": [{"testId": "5004"}]},
                {"ruleId": "m4", "ruleName": "std test", "minimumSources": 2, "tests": [{"testId": "5001"}]},
            ])

        f = self.run_data(mutate, ["cluster-c"])
        hit = sorted(x["element_id"] for x in f if x["rule"] == "alert_min_sources")
        self.assertEqual(hit, ["alert_rule:100:alert:m1", "alert_rule:100:alert:m2"])
        one = [x for x in f if x["rule"] == "alert_min_sources"][0]
        self.assertEqual(one["level"], "REVIEW")
        self.assertEqual(one["fix_owner"], "operator")
        self.assertIn("alert may fire during the maintenance window", one["reason"])
        # a standalone agent never triggers the rule
        f = self.run_data(mutate, ["agent-a"])
        self.assertFalse([x for x in f if x["rule"] == "alert_min_sources"])

    def test_alert_min_sources_skipped_without_such_rules(self):
        f = self.flags(["cluster-c", "agent-a"])
        self.assertFalse([x for x in f if x["rule"] == "alert_min_sources"])

    # -- data gaps ---------------------------------------------------------
    def test_data_gap_suppression(self):
        def strip(data):
            detail = data["groups"]["100"]["suppression_details"]["s1"]
            detail.pop("agents")

        f = self.run_data(strip, ["agent-a"])
        hit = [x for x in f if x["rule"] == "data_gap_suppression"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["level"], "REVIEW")
        self.assertEqual(hit[0]["element_kind"], "suppression")
        self.assertIn("suppression windows could not be linked to agents; check them by hand", hit[0]["reason"])
        # linked windows (the base fixture) or nothing selected: no gap finding
        self.assertFalse([x for x in self.flags(["agent-a"]) if x["rule"] == "data_gap_suppression"])
        self.assertEqual(self.run_data(strip, []), [])

    def test_data_gap_alert_rules_and_agent_notif_rules(self):
        def strip(data):
            for rule in data["groups"]["100"]["alert_rules"]:
                rule.pop("tests", None)
            for rule in data["groups"]["100"]["agent_notif_rules"]:
                rule.pop("agents", None)

        f = self.run_data(strip, ["agent-a"])
        alert = [x for x in f if x["rule"] == "data_gap_alert_rules"]
        notif = [x for x in f if x["rule"] == "data_gap_agent_notif_rules"]
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert[0]["level"], "REVIEW")
        self.assertEqual(alert[0]["fix_owner"], "operator")
        self.assertEqual(len(notif), 1)
        base = self.flags(["agent-a"])
        self.assertFalse([x for x in base if x["rule"] in ("data_gap_alert_rules", "data_gap_agent_notif_rules")])


if __name__ == "__main__":
    unittest.main()
