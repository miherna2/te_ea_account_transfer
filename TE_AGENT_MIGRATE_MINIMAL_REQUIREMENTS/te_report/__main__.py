"""CLI: python -m te_report --input DIR [--selection FILE] --output DIR [--html] [--xlsx]"""

import argparse
import datetime
import io
import os
import sys

from te_report.graph import build_graph
from te_report.impact import flag_impact, resolve_selection, summarize
from te_report.loader import DiscoveryInputError, load_discovery, load_selection
from te_report.outputs import write_all


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m te_report",
        description="Offline relationship graph and migration impact report.",
    )
    parser.add_argument("--input", required=True, help="discovery directory (work/discovery)")
    parser.add_argument("--selection", help="text file: one agent id / name / IP per line")
    parser.add_argument("--output", required=True, help="directory for graph.json and CSV files")
    parser.add_argument("--html", action="store_true", help="also write report.html (one self-contained page)")
    parser.add_argument("--xlsx", action="store_true", help="also write report.xlsx (needs openpyxl)")
    return parser


def main(argv=None, out=None):
    out = out or sys.stdout
    try:
        return _run(argv, out)
    except DiscoveryInputError as exc:
        out.write("ERROR: %s\n" % exc)
    except Exception as exc:
        # Filesystem and malformed-data exceptions may include secret payloads.
        out.write("ERROR: could not complete the report (%s)\n" % type(exc).__name__)
    return 1


def _run(argv, out):
    args = build_parser().parse_args(argv)

    def say(text):
        out.write(text + "\n")

    data = load_discovery(args.input)
    if not data["usable"]:
        say("ERROR: no usable agents.json or tests.json datasets; no report written")
        return 1
    graph = build_graph(data)
    for key in ("complete", "required_missing", "required_errors"):
        graph.meta[key] = data[key]
    stats = graph.stats()
    say("Discovery: %s" % args.input)
    say(
        "Graph: %d groups, %d agents, %d clusters, %d members, %d tests, %d tags, "
        "%d dashboards, %d widgets, %d global filters, %d suppression windows, %d edges"
        % (
            stats.get("group", 0), stats.get("agent", 0), stats.get("cluster", 0),
            stats.get("member", 0), stats.get("test", 0), stats.get("tag", 0),
            stats.get("dashboard", 0), stats.get("widget", 0),
            stats.get("global_filter", 0), stats.get("suppression", 0), len(graph.edges),
        )
    )
    optional_missing = [rel for rel in data["missing"] if rel not in data["required_missing"]]
    optional_errors = [rel for rel in data["errors"] if rel not in data["required_errors"]]
    if not data["complete"]:
        say("WARNING: INCOMPLETE discovery; impact findings cannot establish complete coverage")
        for rel in data["required_missing"]:
            say("  missing required file: %s" % rel)
        for rel in data["required_errors"]:
            say("  unreadable or invalid required file: %s" % rel)
    if optional_missing:
        say("Missing optional files: %d" % len(optional_missing))
    if optional_errors:
        say("Unreadable optional files: %d" % len(optional_errors))

    resolution = None
    findings = None
    selection = []
    if args.selection:
        selection = load_selection(args.selection)
        resolution = resolve_selection(graph, selection)
        findings = flag_impact(graph, resolution["selected"])
        counts = summarize(findings)
        say(
            "Selection: %d identifiers, %d agents/clusters selected, %d excluded offline, %d unmatched, "
            "%d ambiguous"
            % (
                len(selection), len(resolution["selected"]),
                len(resolution["excluded_offline"]), len(resolution["unmatched"]),
                len(resolution.get("ambiguous", {})),
            )
        )
        for token in resolution["unmatched"]:
            say("  unmatched: %s" % token)
        for token in sorted(resolution.get("ambiguous", {})):
            say(
                "  ambiguous (not selected): %s -> %s"
                % (token, ", ".join(resolution["ambiguous"][token]))
            )
        say(
            "Findings: BREAKS=%d REVIEW=%d OK=%d (total %d)"
            % (counts["BREAKS"], counts["REVIEW"], counts["OK"], counts["total"])
        )
    else:
        say("No --selection given: inventory-only outputs (no impact flags).")

    paths = write_all(graph, args.output, resolution, findings)
    meta = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "input": args.input,
        "selection_count": len(selection) if args.selection else None,
        "missing": optional_missing,
        "errors": optional_errors,
        "complete": data["complete"],
        "required_missing": data["required_missing"],
        "required_errors": data["required_errors"],
    }
    status = 0 if data["complete"] else 1
    if findings is not None:
        from te_report import dashboard_view
        from te_report.render_dashboards import render as render_dashboards

        dash_rows, widget_rows = dashboard_view.build_views(graph, findings)
        view = dashboard_view.summarize(dash_rows, widget_rows)
        paths.update(dashboard_view.write_csv_files(args.output, dash_rows, widget_rows))
        dash_meta = dict(meta)
        dash_meta["selected"] = len(resolution["selected"])
        dash_path = os.path.join(args.output, "dashboards.html")
        with io.open(dash_path, "w", encoding="utf-8") as handle:
            handle.write(render_dashboards(dash_rows, widget_rows, view, dash_meta))
        paths["dashboards.html"] = dash_path
        d, w = view["dashboards"], view["widgets"]
        say(
            "Dashboards: BREAKS=%d CHANGES=%d OK=%d UNAFFECTED=%d NOT CHECKED=%d"
            % (d["BREAKS"], d["CHANGES"], d["OK"], d["UNAFFECTED"], d["NOT CHECKED"])
        )
        say(
            "Widgets:    BREAKS=%d CHANGES=%d OK=%d UNAFFECTED=%d"
            % (w["BREAKS"], w["CHANGES"], w["OK"], w["UNAFFECTED"])
        )
    if args.html:
        from te_report.render_html import render

        html_path = os.path.join(args.output, "report.html")
        with io.open(html_path, "w", encoding="utf-8") as handle:
            handle.write(render(graph, findings or [], resolution, meta))
        paths["report.html"] = html_path
    if args.xlsx:
        from te_report.render_xlsx import write_workbook

        xlsx_path = os.path.join(args.output, "report.xlsx")
        try:
            write_workbook(xlsx_path, graph, findings or [], resolution, meta)
            paths["report.xlsx"] = xlsx_path
        except RuntimeError as exc:  # openpyxl missing
            say("ERROR: " + str(exc))
            status = 1
        except Exception as exc:  # never echo data from the exception text
            say("ERROR: could not write report.xlsx (%s)" % type(exc).__name__)
            status = 1
    say("Wrote: " + ", ".join(sorted(paths)) + " -> " + args.output)
    return status


if __name__ == "__main__":
    sys.exit(main())
