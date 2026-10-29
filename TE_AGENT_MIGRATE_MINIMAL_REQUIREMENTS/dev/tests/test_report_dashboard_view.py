import csv
import io
import os
import shutil
import tempfile
import unittest

try:
    from dev.tests.test_report_fixtures import build_discovery, SECRET_MARKER
except ImportError:
    from test_report_fixtures import build_discovery, SECRET_MARKER

from te_report import dashboard_view
from te_report.graph import build_graph
from te_report.impact import flag_impact, resolve_selection
from te_report.loader import load_discovery
from te_report.render_dashboards import render


class DashboardViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.join(cls.tmp, "discovery_output")
        build_discovery(cls.root)
        cls.graph = build_graph(load_discovery(cls.root))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def views(self, selection):
        res = resolve_selection(self.graph, selection)
        findings = flag_impact(self.graph, res["selected"])
        return dashboard_view.build_views(self.graph, findings)

    def widget(self, widgets, title):
        hit = [w for w in widgets if w["widget"] == title]
        self.assertEqual(len(hit), 1, title)
        return hit[0]

    def test_agent_id_filter_widget_breaks_and_dashboard_breaks(self):
        dashboards, widgets = self.views(["agent-a"])
        w1 = self.widget(widgets, "agent-id widget")
        self.assertEqual(w1["verdict"], "BREAKS")
        self.assertIn("filter_agent_id", w1["rules"])
        self.assertIn("new agent ID", w1["fix"])
        d1 = [d for d in dashboards if d["dashboard"] == "Dash One"][0]
        self.assertEqual(d1["verdict"], "BREAKS")
        self.assertGreaterEqual(d1["breaks"], 1)
        # worst first
        self.assertEqual(dashboards[0]["verdict"], "BREAKS")

    def test_tag_filter_widget_changes(self):
        _, widgets = self.views(["agent-a"])
        hits = [w for w in widgets if "filter_agent_tag" in w["rules"]]
        self.assertTrue(hits)
        for w in hits:
            self.assertIn(w["verdict"], ("CHANGES", "BREAKS"))

    def test_test_only_widget_is_ok(self):
        _, widgets = self.views(["agent-a"])
        ok = [w for w in widgets if w["verdict"] == "OK"]
        self.assertTrue(ok)

    def test_no_selection_means_everything_unaffected(self):
        dashboards, widgets = self.views([])
        self.assertTrue(widgets)
        self.assertTrue(all(w["verdict"] == "UNAFFECTED" for w in widgets))
        self.assertFalse([d for d in dashboards if d["verdict"] in ("BREAKS", "CHANGES")])

    def test_dashboard_without_detail_is_not_checked(self):
        dashboards, _ = self.views(["agent-a"])
        verdicts = set(d["verdict"] for d in dashboards)
        self.assertTrue(verdicts.issubset(set(dashboard_view.VERDICT_ORDER)))
        for d in dashboards:
            if d["widgets"] == 0:
                self.assertEqual(d["verdict"], "NOT CHECKED")

    def test_summary_counts_add_up(self):
        dashboards, widgets = self.views(["agent-a"])
        summary = dashboard_view.summarize(dashboards, widgets)
        self.assertEqual(sum(summary["dashboards"].values()), len(dashboards))
        self.assertEqual(sum(summary["widgets"].values()), len(widgets))

    def test_csv_files_and_formula_injection(self):
        dashboards, widgets = self.views(["agent-a"])
        widgets = list(widgets)
        widgets[0] = dict(widgets[0], widget="=HYPERLINK(\"x\")")
        out = os.path.join(self.tmp, "csv_out")
        os.makedirs(out)
        paths = dashboard_view.write_csv_files(out, dashboards, widgets)
        self.assertEqual(sorted(paths), ["dashboards.csv", "widgets.csv"])
        with io.open(paths["widgets.csv"], encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["widget"][0], "'")
        self.assertEqual(list(rows[0].keys()), dashboard_view.WIDGET_FIELDS)

    def test_html_is_escaped_self_contained_and_has_no_secret(self):
        dashboards, widgets = self.views(["agent-a"])
        dashboards = [dict(dashboards[0], dashboard="<script>alert(1)</script>")] + dashboards[1:]
        summary = dashboard_view.summarize(dashboards, widgets)
        page = render(dashboards, widgets, summary, {"generated": "now", "input": "x", "selected": 1})
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn(SECRET_MARKER, page)
        self.assertIn("BREAKS", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("eval(", page)


if __name__ == "__main__":
    unittest.main()
