"""Dashboard first view: will a dashboard or a widget break after the move?

Reads the graph and the findings. Writes one row per dashboard and one row per
widget. Plain verdicts:

  BREAKS    a filter uses a selected agent id. The widget shows no data after the move.
  CHANGES   the widget works but its content changes (check by hand).
  OK        the widget uses selected agents or tests, and nothing changes.
  UNAFFECTED  no link to any selected agent or to a test they serve.
  NOT CHECKED the dashboard detail was not downloaded (or it is a built-in dashboard).
"""
import csv
import io
import os

from te_report.outputs import safe_cell

VERDICT_ORDER = ["BREAKS", "CHANGES", "OK", "UNAFFECTED", "NOT CHECKED"]
_LEVEL_TO_VERDICT = {"BREAKS": "BREAKS", "REVIEW": "CHANGES", "OK": "OK"}
_RANK = dict((v, i) for i, v in enumerate(VERDICT_ORDER))

DASHBOARD_FIELDS = [
    "verdict", "group", "group_name", "dashboard", "dashboard_id", "widgets",
    "breaks", "changes", "ok", "unaffected", "global_filter", "why",
]
WIDGET_FIELDS = [
    "verdict", "group", "group_name", "dashboard", "widget", "widget_type",
    "rules", "why", "fix",
]


def _worse(a, b):
    return a if _RANK[a] <= _RANK[b] else b


def _group_name(graph, aid):
    node = graph.nodes.get("group:%s" % aid)
    return node["name"] if node else ""


def _by_element(findings):
    index = {}
    for item in findings or []:
        index.setdefault(item["element_id"], []).append(item)
    return index


def _widget_verdict(items):
    if not items:
        return "UNAFFECTED"
    verdict = "OK"
    for item in items:
        verdict = _worse(verdict, _LEVEL_TO_VERDICT.get(item["level"], "OK"))
    return verdict


def _join(values, limit=3, sep=" | "):
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    text = sep.join(seen[:limit])
    if len(seen) > limit:
        text += " (+%d more)" % (len(seen) - limit)
    return text


def _short(text, size=300):
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[: size - 3] + "..."


def build_views(graph, findings):
    """Return (dashboard_rows, widget_rows). Rows are dicts."""
    index = _by_element(findings)
    dashboards = []
    widgets = []
    for dnode in graph.nodes_of("dashboard"):
        attrs = dnode["attrs"]
        aid = str(attrs.get("aid", ""))
        gname = _group_name(graph, aid)
        dname = dnode["name"]
        wnodes = [graph.nodes[i] for i in graph.children(dnode["id"], "has_widget") if i in graph.nodes]

        gf_items = []
        for gf_id in graph.children(dnode["id"], "uses_global_filter"):
            gf_items.extend(index.get(gf_id, []))
        gf_verdict = _widget_verdict(gf_items) if gf_items else ""

        counts = dict((v, 0) for v in VERDICT_ORDER)
        dash_rows = []
        for wnode in wnodes:
            w_items = list(index.get(wnode["id"], []))
            w_items.extend(gf_items if gf_items else [])
            verdict = _widget_verdict(w_items)
            counts[verdict] += 1
            row = {
                "verdict": verdict,
                "group": aid,
                "group_name": gname,
                "dashboard": dname,
                "widget": wnode["name"],
                "widget_type": wnode["attrs"].get("widget_type") or "",
                "rules": _join((i["rule"] for i in w_items), 5, ", "),
                "why": _short(_join((i["reason"] for i in w_items if i["level"] != "OK") or
                                    (i["reason"] for i in w_items), 2)),
                "fix": _short(_join((i["fix"] for i in w_items if i["level"] != "OK"), 2)),
            }
            dash_rows.append(row)
        widgets.extend(dash_rows)

        built_in = bool(attrs.get("built_in"))
        has_detail = bool(attrs.get("has_detail"))
        if not wnodes and (built_in or not has_detail):
            verdict = "NOT CHECKED"
            why = "built-in dashboard (not analyzed)" if built_in else "dashboard detail was not downloaded"
        else:
            verdict = "UNAFFECTED"
            for v in VERDICT_ORDER[:3]:
                if counts[v]:
                    verdict = v
                    break
            if gf_verdict and gf_verdict != "UNAFFECTED":
                verdict = _worse(verdict, gf_verdict)
            bad = [r for r in dash_rows if r["verdict"] in ("BREAKS", "CHANGES")]
            bad.sort(key=lambda r: _RANK[r["verdict"]])
            why = _join(("%s: %s" % (r["widget"], r["rules"]) for r in bad), 3)
            if not why and verdict == "OK":
                why = "uses selected agents or tests; nothing changes"
            if not why and verdict == "UNAFFECTED":
                why = "no link to selected agents"
        dashboards.append(
            {
                "verdict": verdict,
                "group": aid,
                "group_name": gname,
                "dashboard": dname,
                "dashboard_id": attrs.get("dashboard_id") or "",
                "widgets": len(wnodes),
                "breaks": counts["BREAKS"],
                "changes": counts["CHANGES"],
                "ok": counts["OK"],
                "unaffected": counts["UNAFFECTED"],
                "global_filter": gf_verdict,
                "why": _short(why),
            }
        )
    dashboards.sort(key=lambda r: (_RANK[r["verdict"]], -r["breaks"], -r["changes"], r["group"], r["dashboard"]))
    widgets.sort(key=lambda r: (_RANK[r["verdict"]], r["group"], r["dashboard"], r["widget"]))
    return dashboards, widgets


def summarize(dashboards, widgets):
    d = dict((v, 0) for v in VERDICT_ORDER)
    w = dict((v, 0) for v in VERDICT_ORDER)
    for row in dashboards:
        d[row["verdict"]] += 1
    for row in widgets:
        w[row["verdict"]] += 1
    return {"dashboards": d, "widgets": w}


def _write_csv(path, fields, rows):
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([safe_cell(row.get(f, "")) for f in fields])


def write_csv_files(output_dir, dashboards, widgets):
    paths = {}
    for name, fields, rows in (
        ("dashboards.csv", DASHBOARD_FIELDS, dashboards),
        ("widgets.csv", WIDGET_FIELDS, widgets),
    ):
        path = os.path.join(output_dir, name)
        _write_csv(path, fields, rows)
        paths[name] = path
    return paths
