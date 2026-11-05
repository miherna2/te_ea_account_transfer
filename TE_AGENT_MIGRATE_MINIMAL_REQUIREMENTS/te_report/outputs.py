"""CSV writers for the report (csv module only)."""

import csv
import io
import os
import re

from te_report.impact import is_offline

FINDING_COLUMNS = [
    "level", "element_kind", "element_id", "element_name",
    "reason", "fix", "fix_owner", "rule", "group",
]
TREE_COLUMNS = ["depth", "kind", "id", "name", "group", "detail", "selected"]
AGENT_COLUMNS = [
    "agent_id", "kind", "name", "state", "ips", "member_ips",
    "groups", "shared_count", "matched_by", "reason",
]
SHARE_COLUMNS = [
    "agent_id", "agent_name", "kind", "state", "group_aid", "group_name",
    "listed_in_group", "shared_count", "owner_unknown", "selected",
]

_NUMBER = re.compile(r"^-?[0-9][0-9.,]*$")


def safe_cell(value):
    """Stringify a value and neutralise spreadsheet formula prefixes."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    if text and text[0] in "=+-@\t\r" and not _NUMBER.match(text):
        return "'" + text
    return text


def write_csv(path, columns, rows):
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([safe_cell(row.get(c)) for c in columns])
    return path


# ---- tree -----------------------------------------------------------------
def tree_rows(graph, selected=None):
    """One row per element, indented by depth: group > agent/cluster > ..."""
    by_group = {}
    for node in graph.nodes.values():
        aid = node["attrs"].get("aid")
        if node["kind"] in ("test", "dashboard", "global_filter", "suppression", "tag", "alert_rule"):
            for a in (node["attrs"].get("aids") or [aid]):
                by_group.setdefault((node["kind"], a), []).append(node["id"])
    rows = []

    def sel_flag(node_id):
        if selected is None:
            return ""
        return "yes" if node_id in selected else "no"

    def add(depth, node, group, detail=""):
        rows.append(
            {
                "depth": depth,
                "kind": node["kind"],
                "id": node["id"],
                "name": "  " * depth + (node["name"] or ""),
                "group": group,
                "detail": detail,
                "selected": sel_flag(node["id"]) if node["kind"] in ("agent", "cluster") else "",
            }
        )

    def sort_nodes(ids):
        return sorted(ids, key=lambda i: ((graph.nodes[i]["name"] or "").lower(), i))

    def add_test(depth, test_id, aid, detail=""):
        add(depth, graph.nodes[test_id], aid, detail)
        for rule in sort_nodes(graph.children(test_id, "alert_rule")):
            add(depth + 1, graph.nodes[rule], aid)
        for tag in sort_nodes(graph.children(test_id, "tagged")):
            if graph.nodes[tag]["attrs"].get("aid") == aid:
                add(depth + 1, graph.nodes[tag], aid)

    groups = sorted(graph.nodes_of("group"), key=lambda n: ((n["name"] or "").lower(), n["id"]))
    for gnode in groups:
        aid = gnode["attrs"].get("aid")
        add(0, gnode, aid)
        assigned_here = set()
        for agent_id in sort_nodes(graph.parents(gnode["id"], "visible_in")):
            agent = graph.nodes[agent_id]
            detail = "state=%s; ips=%s" % (
                agent["attrs"].get("state") or "", " ".join(agent["attrs"].get("ips") or [])
            )
            add(1, agent, aid, detail)
            for member in sort_nodes(graph.children(agent_id, "member_of")):
                m = graph.nodes[member]
                add(2, m, aid, "state=%s; ips=%s" % (m["attrs"].get("state") or "", " ".join(m["attrs"].get("ips") or [])))
            for tag in sort_nodes(graph.children(agent_id, "tagged")):
                if graph.nodes[tag]["attrs"].get("aid") == aid:
                    add(2, graph.nodes[tag], aid)
            tests = [
                e["child_id"] for e in graph.out_edges(agent_id, "assigned_test")
                if e["attrs"].get("group") == aid
            ]
            for test_id in sort_nodes(set(tests)):
                assigned_here.add(test_id)
                add_test(2, test_id, aid)
        for test_id in sort_nodes(by_group.get(("test", aid), [])):
            if test_id not in assigned_here:
                add_test(1, test_id, aid, "not assigned to any agent in this group")
        for dash in sort_nodes(by_group.get(("dashboard", aid), [])):
            add(1, graph.nodes[dash], aid)
            for tag in sort_nodes(graph.children(dash, "tagged")):
                add(2, graph.nodes[tag], aid)
            for widget in graph.children(dash, "has_widget"):
                add(2, graph.nodes[widget], aid, graph.nodes[widget]["attrs"].get("widget_type") or "")
        for kind in ("global_filter", "suppression"):
            for nid in sort_nodes(by_group.get((kind, aid), [])):
                add(1, graph.nodes[nid], aid)
    return rows


# ---- agents ---------------------------------------------------------------
def _agent_row(graph, node, matched_by=None, reason=""):
    attrs = node["attrs"]
    return {
        "agent_id": attrs.get("agent_id"),
        "kind": node["kind"],
        "name": node["name"],
        "state": attrs.get("state") or "",
        "ips": " ".join(attrs.get("ips") or []),
        "member_ips": " ".join(attrs.get("member_ips") or []),
        "groups": " ".join(attrs.get("shared_with") or attrs.get("groups") or []),
        "shared_count": attrs.get("shared_count"),
        "matched_by": "; ".join(matched_by or []),
        "reason": reason,
    }


def _matched_by(resolution):
    by_node = {}
    for token, ids in resolution["matches"].items():
        for node_id in ids:
            by_node.setdefault(node_id, []).append(token)
    return by_node


def selected_rows(graph, resolution):
    by_node = _matched_by(resolution)
    return [
        _agent_row(graph, graph.nodes[n], by_node.get(n))
        for n in sorted(resolution["selected"], key=lambda i: ((graph.nodes[i]["name"] or "").lower(), i))
    ]


def offline_rows(graph, node_ids, matched=None):
    rows = []
    for node_id in sorted(node_ids, key=lambda i: ((graph.nodes[i]["name"] or "").lower(), i)):
        node = graph.nodes[node_id]
        if node["kind"] == "cluster":
            states = [s or "unknown" for s in node["attrs"].get("member_states") or []]
            reason = "cluster member(s) not online: " + ", ".join(
                sorted(set(s for s in states if s != "online"))) if states else "cluster state %s" % node["attrs"].get("state")
        else:
            reason = "agent state %s" % node["attrs"].get("state")
        rows.append(_agent_row(graph, node, (matched or {}).get(node_id), reason))
    return rows


def all_offline_ids(graph):
    out = []
    for kind in ("agent", "cluster"):
        for node in graph.nodes_of(kind):
            if is_offline(graph, node):
                out.append(node["id"])
    return out


def share_rows(graph, selected=None):
    rows = []
    for kind in ("agent", "cluster"):
        for node in graph.nodes_of(kind):
            attrs = node["attrs"]
            listed = attrs.get("groups") or []
            for aid in attrs.get("shared_with") or listed:
                gnode = graph.nodes.get("group:" + aid)
                edges = [e for e in graph.out_edges(node["id"], "visible_in") if e["child_id"] == "group:" + aid]
                rows.append(
                    {
                        "agent_id": attrs.get("agent_id"),
                        "agent_name": node["name"],
                        "kind": kind,
                        "state": attrs.get("state") or "",
                        "group_aid": aid,
                        "group_name": gnode["name"] if gnode else "",
                        "listed_in_group": aid in listed,
                        "shared_count": attrs.get("shared_count"),
                        "owner_unknown": edges[0]["attrs"].get("owner_unknown", True) if edges else True,
                        "selected": "" if selected is None else ("yes" if node["id"] in selected else "no"),
                    }
                )
    rows.sort(key=lambda r: ((r["agent_name"] or "").lower(), str(r["agent_id"]), str(r["group_aid"])))
    return rows


def write_all(graph, output_dir, resolution=None, findings=None):
    """Write graph.json and the CSV files; return ``{name: path}``."""
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    paths = {}
    selected = resolution["selected"] if resolution else None
    paths["graph.json"] = graph.write_json(os.path.join(output_dir, "graph.json"))
    paths["tree.csv"] = write_csv(os.path.join(output_dir, "tree.csv"), TREE_COLUMNS, tree_rows(graph, selected))
    paths["shares.csv"] = write_csv(os.path.join(output_dir, "shares.csv"), SHARE_COLUMNS, share_rows(graph, selected))
    if resolution is None:
        rows = offline_rows(graph, all_offline_ids(graph))
        paths["offline.csv"] = write_csv(os.path.join(output_dir, "offline.csv"), AGENT_COLUMNS, rows)
    else:
        by_node = _matched_by(resolution)
        paths["selected.csv"] = write_csv(
            os.path.join(output_dir, "selected.csv"), AGENT_COLUMNS, selected_rows(graph, resolution)
        )
        paths["offline.csv"] = write_csv(
            os.path.join(output_dir, "offline.csv"), AGENT_COLUMNS,
            offline_rows(graph, resolution["excluded_offline"], by_node),
        )
        paths["findings.csv"] = write_csv(
            os.path.join(output_dir, "findings.csv"), FINDING_COLUMNS, findings or []
        )
    return paths
