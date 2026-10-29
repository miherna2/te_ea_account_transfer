"""Selection matching and migration impact flags (offline, graph based)."""

from te_report.graph import AGENT_ID_FILTERS, classify_filter

OFFLINE_STATES = ("offline", "disabled")
LEVELS = ("BREAKS", "REVIEW", "OK")

FIX_OPERATOR = "operator"
FIX_TOOL = "tool"


def _agent_kinds():
    return ("agent", "cluster")


def _norm(text):
    return str(text).strip().lower()


def _identifiers(graph, node):
    """(ids, names, ips) strings, lowercase, that identify an agent/cluster.

    Public IPs are not identifiers: many agents share one NAT address.
    """
    attrs = node["attrs"]
    ids = set([_norm(attrs.get("agent_id", ""))])
    names = set([_norm(node["name"] or "")])
    ips = set(_norm(ip) for ip in (attrs.get("ips") or []))
    if node["kind"] == "cluster":
        for member_id in graph.children(node["id"], "member_of"):
            member = graph.nodes[member_id]
            names.add(_norm(member["name"] or ""))
            ids.add(_norm(member["attrs"].get("member_id", "")))
            for ip in member["attrs"].get("ips") or []:
                ips.add(_norm(ip))
    ids.discard("")
    names.discard("")
    ips.discard("")
    return ids, names, ips


def is_offline(graph, node):
    """Standalone: state offline/disabled. Cluster: any member not online."""
    state = node["attrs"].get("state")
    if node["kind"] == "cluster":
        members = graph.children(node["id"], "member_of")
        if members:
            for member_id in members:
                if _norm(graph.nodes[member_id]["attrs"].get("state")) != "online":
                    return True
            return False
    return _norm(state) in OFFLINE_STATES


def resolve_selection(graph, selection):
    """Match identifiers (agentId, name, private IP) to agent/cluster nodes.

    Returns a dict:
    ``selected`` (set of node ids, online only), ``excluded_offline`` (set),
    ``unmatched`` (list of identifiers), ``matches`` (identifier -> list of ids)
    and ``ambiguous`` (identifier -> list of ids, for identifiers that match
    more than one device; none of those devices is selected).
    """
    index = {}
    for kind in _agent_kinds():
        for node in graph.nodes_of(kind):
            ids, names, ips = _identifiers(graph, node)
            for value in ids | names | ips:
                index.setdefault(value, set()).add(node["id"])
    selected = set()
    excluded = set()
    unmatched = []
    matches = {}
    ambiguous = {}
    for token in selection:
        found = sorted(index.get(_norm(token), []))
        if not found:
            unmatched.append(token)
            continue
        if len(found) > 1:
            ambiguous[token] = found
            continue
        matches[token] = found
        for node_id in found:
            if is_offline(graph, graph.nodes[node_id]):
                excluded.add(node_id)
            else:
                selected.add(node_id)
    return {
        "selected": selected,
        "excluded_offline": excluded,
        "unmatched": unmatched,
        "matches": matches,
        "ambiguous": ambiguous,
    }


# ----------------------------------------------------------------------
class _Findings(object):
    def __init__(self, graph):
        self.graph = graph
        self.items = []

    def add(self, level, node, reason, fix, fix_owner, rule, name=None):
        attrs = node["attrs"]
        group = attrs.get("aid")
        if group is None and attrs.get("groups"):
            group = ",".join(attrs["groups"])
        self.items.append(
            {
                "level": level,
                "element_kind": node["kind"],
                "element_id": node["id"],
                "element_name": name if name is not None else node["name"],
                "reason": reason,
                "fix": fix,
                "fix_owner": fix_owner,
                "rule": rule,
                "group": group or "",
            }
        )

    def finish(self):
        """One finding per element and rule (worst level wins), sorted worst first."""
        best = {}
        for item in self.items:
            key = (item["element_id"], item["rule"])
            current = best.get(key)
            if current is None or _level_rank(item["level"]) < _level_rank(current["level"]):
                best[key] = item
        order = dict((lvl, i) for i, lvl in enumerate(LEVELS))
        return sorted(
            best.values(),
            key=lambda f: (order[f["level"]], f["element_kind"], f["element_name"] or "", f["element_id"], f["rule"]),
        )


def _names(graph, node_ids, limit=5):
    names = [graph.nodes[n]["name"] or n for n in sorted(node_ids)]
    text = ", ".join(names[:limit])
    if len(names) > limit:
        text += " (+%d more)" % (len(names) - limit)
    return text


def _element_name(graph, node):
    if node["kind"] == "widget":
        dashboards = graph.parents(node["id"], "has_widget")
        if dashboards:
            return "%s / %s" % (graph.nodes[dashboards[0]]["name"], node["name"])
    return node["name"]


def _level_rank(level):
    return LEVELS.index(level)


def _positive(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _summary_node(kind, name, key):
    """A stand-in element for findings that cover a whole data set."""
    return {"id": "summary:" + key, "kind": kind, "name": name, "attrs": {}}


class _Scope(object):
    """Which tests/groups the selected agents serve (used for widget rules)."""

    def __init__(self, graph, selected):
        self.graph = graph
        self.selected = selected
        self.test_groups = {}  # test id -> set of groups where it is served
        self.groups = set()  # groups where a selected agent serves any test
        for node_id in selected:
            for edge in graph.out_edges(node_id, "assigned_test"):
                test_id = edge["child_id"]
                group = edge["attrs"].get("group")
                groups = self.test_groups.setdefault(test_id, set())
                if group is not None:
                    groups.add(group)
                    self.groups.add(group)
        for test_id, groups in self.test_groups.items():
            groups.update(graph.nodes[test_id]["attrs"].get("aids") or [])

    def _filter_owners(self, widget):
        owners = [widget]
        for dash_id in self.graph.parents(widget["id"], "has_widget"):
            for gf_id in self.graph.children(dash_id, "uses_global_filter"):
                owners.append(self.graph.nodes[gf_id])
        return owners

    def reads_selected(self, widget):
        """True when the widget would show data of a selected agent."""
        graph = self.graph
        aid = widget["attrs"].get("aid")
        owners = self._filter_owners(widget)
        agent_keys = False
        test_keys = False
        agent_targets = set()
        test_targets = set()
        for owner in owners:
            for key in owner["attrs"].get("filter_keys") or []:
                cls = classify_filter(key)
                if cls and cls[0] == "agent":
                    agent_keys = True
                elif cls and cls[0] == "test":
                    test_keys = True
            agent_targets.update(graph.children(owner["id"], "filters_on_agent"))
            test_targets.update(graph.children(owner["id"], "filters_on_test"))
        if agent_targets & self.selected:
            return True
        if agent_keys:
            return False
        if test_keys:
            return any(aid in self.test_groups.get(t, ()) for t in test_targets)
        return aid in self.groups


def _widget_findings(graph, selected, out):
    scope = _Scope(graph, selected)
    for node in graph.nodes_of("widget"):
        attrs = node["attrs"]
        name = _element_name(graph, node)
        reads = None
        if attrs.get("group_by_agent"):
            reads = scope.reads_selected(node)
            if reads:
                out.add(
                    "REVIEW", node,
                    "history splits: the new agent has a new id (widget groups by AGENT).",
                    "Compare old and new agent in the widget after the move; no tool fix.",
                    FIX_OPERATOR, "widget_group_by_agent", name=name,
                )
        if str(attrs.get("widget_type") or "").strip().lower() == "alert list":
            if reads is None:
                reads = scope.reads_selected(node)
            if reads:
                out.add(
                    "REVIEW", node,
                    "active alerts for the old agent clear, new ones start (Alert List widget).",
                    "Check the widget after the move.",
                    FIX_OPERATOR, "widget_alert_list", name=name,
                )


def _owned_agents_findings(graph, selected, out):
    for node in graph.nodes_of("widget"):
        attrs = node["attrs"]
        if not (
            str(attrs.get("agents") or "").lower() == "enterprise agents"
            and str(attrs.get("show") or "").lower() == "owned agents"
        ):
            continue
        aid = attrs.get("aid")
        known = []
        unknown = []
        for sel in sorted(selected):
            agent = graph.nodes[sel]
            owner = agent["attrs"].get("owner_aid")
            if owner:
                if owner == aid:
                    known.append(sel)
            elif aid in (agent["attrs"].get("groups") or []):
                unknown.append(sel)
        if not known and not unknown:
            continue
        parts = []
        if known:
            parts.append("this group owns %s" % _names(graph, known))
        if unknown:
            parts.append("owner unknown for %s" % _names(graph, unknown))
        out.add(
            "REVIEW", node,
            "Agent-status widget shows 'Owned Agents'; ownership may change (%s)." % "; ".join(parts),
            "Check the widget after the move; switch to 'All Assigned Agents' if the agents disappear.",
            FIX_OPERATOR, "widget_owned_agents", name=_element_name(graph, node),
        )


def _test_findings(graph, selected, out):
    tests = set()
    for node_id in selected:
        tests.update(graph.children(node_id, "assigned_test"))
        tests.update(graph.parents(node_id, "targets_agent"))
    for test_id in sorted(tests):
        node = graph.nodes[test_id]
        flagged = False
        targets = [c for c in graph.children(test_id, "targets_agent") if c in selected]
        if targets:
            flagged = True
            out.add(
                "BREAKS", node,
                "Agent-to-agent test targets selected agent(s): %s." % _names(graph, targets, 3),
                "Update the test target to the new agent ID after the move.",
                FIX_OPERATOR, "test_target_agent",
            )
        assigned = graph.parents(test_id, "assigned_test")
        if assigned and all(a in selected for a in assigned):
            flagged = True
            if any(graph.nodes[a]["kind"] == "cluster" for a in assigned):
                out.add(
                    "REVIEW", node,
                    "cluster tests have no agent during the maintenance window (%s)."
                    % _names(graph, assigned, 3),
                    "Plan the maintenance window; the tool re-assigns and re-enables the tests after the rebuild.",
                    FIX_OPERATOR, "test_all_agents_selected",
                )
            else:
                out.add(
                    "OK", node,
                    "agent is replaced and the tests are assigned to the new agent before the old agent is deleted (%s)."
                    % _names(graph, assigned, 3),
                    "Tool assigns the tests to the new agent and verifies them before deleting the old agent.",
                    FIX_TOOL, "test_all_agents_selected",
                )
        if not flagged:
            out.add(
                "OK", node,
                "Test keeps its ID and stays in its account group.",
                "", FIX_TOOL, "test_keeps_id",
            )
    return tests


def _rule_findings(graph, selected, out):
    # agent notification rules (cannot be set by API)
    for node_id in sorted(selected):
        if graph.children(node_id, "agent_notif_rule"):
            out.add(
                "REVIEW", graph.nodes[node_id],
                "agent notification rules cannot be set by API; restore by hand.",
                "Re-create the agent notification rules in the UI after the move.",
                FIX_OPERATOR, "agent_notification_rule",
            )
    # alert rules with minimum sources on tests served by a selected cluster
    clusters = [s for s in selected if graph.nodes[s]["kind"] == "cluster"]
    served = set()
    for cluster_id in clusters:
        served.update(graph.children(cluster_id, "assigned_test"))
    for node in graph.nodes_of("alert_rule"):
        attrs = node["attrs"]
        if attrs.get("flavour") != "alert":
            continue
        if not (_positive(attrs.get("min_sources")) or _positive(attrs.get("min_sources_pct"))):
            continue
        hit = [t for t in graph.parents(node["id"], "alert_rule") if t in served]
        if hit:
            out.add(
                "REVIEW", node,
                "alert may fire during the maintenance window (minimum sources on %d test(s) served by a selected cluster)."
                % len(hit),
                "Check or pause the alert rule during the maintenance window.",
                FIX_OPERATOR, "alert_min_sources",
            )


def _data_gap_findings(graph, out):
    counts = graph.meta.get("rule_counts") or {}
    suppressions = graph.nodes_of("suppression")
    if suppressions and not any(
        graph.children(s["id"], "applies_to_agent") or graph.children(s["id"], "applies_to_test")
        for s in suppressions
    ):
        out.add(
            "REVIEW", _summary_node("suppression", "Alert suppression windows", "suppression"),
            "suppression windows could not be linked to agents; check them by hand (%d window(s))."
            % len(suppressions),
            "Open each suppression window and check its agents and tests after the move.",
            FIX_OPERATOR, "data_gap_suppression",
        )
    if counts.get("alert_rules_total") and not counts.get("alert_rules_linked"):
        out.add(
            "REVIEW", _summary_node("alert_rule", "Alert rules", "alert_rules"),
            "alert rules could not be linked to tests or agents; check them by hand (%d rule(s))."
            % counts["alert_rules_total"],
            "Check alert rules (especially minimum sources) of the affected tests.",
            FIX_OPERATOR, "data_gap_alert_rules",
        )
    if counts.get("agent_notif_rules_total") and not counts.get("agent_notif_rules_linked"):
        out.add(
            "REVIEW", _summary_node("alert_rule", "Agent notification rules", "agent_notif_rules"),
            "agent notification rules could not be linked to agents; restore them by hand (%d rule(s))."
            % counts["agent_notif_rules_total"],
            "Re-create the agent notification rules of each moved agent in the UI.",
            FIX_OPERATOR, "data_gap_agent_notif_rules",
        )


def flag_impact(graph, selected_ids):
    """Return findings (list of dicts) for the selected agents/clusters.

    Keys: level (OK/REVIEW/BREAKS), element_kind, element_id, element_name,
    reason, fix, fix_owner, plus ``rule`` (short code) and ``group``.
    There is at most one finding per element and rule code (worst level wins).
    """
    selected = set(
        s for s in selected_ids
        if s in graph.nodes and graph.nodes[s]["kind"] in _agent_kinds()
    )
    out = _Findings(graph)
    if not selected:
        return []

    # 1. selected agents / clusters themselves
    for node_id in sorted(selected):
        node = graph.nodes[node_id]
        count = node["attrs"].get("shared_count") or len(node["attrs"].get("groups", [])) or 1
        if node["kind"] == "cluster":
            out.add(
                "REVIEW", node,
                "Cluster ID changes after rebuild; anything keyed on the old cluster ID must be updated.",
                "Record old cluster ID and re-point references to the new ID after rebuild.",
                FIX_OPERATOR, "cluster_id_changes",
            )
        if count > 1:
            out.add(
                "REVIEW", node,
                "Shared with %d account groups; sharing is lost on reset; tool restores it." % count,
                "Tool re-shares the agent with the same account groups after the move.",
                FIX_TOOL, "sharing_lost",
            )
        else:
            out.add(
                "OK", node,
                "Visible in a single account group; no sharing to restore.",
                "", FIX_TOOL, "single_group",
            )

    # 2. widgets and global filters (agent id / tag filters)
    for kind in ("widget", "global_filter"):
        for node in graph.nodes_of(kind):
            hits_id = {}
            hits_tag = {}
            for edge in graph.out_edges(node["id"], "filters_on_agent"):
                if edge["child_id"] not in selected:
                    continue
                via = edge["attrs"].get("via", "")
                bucket = hits_id if via.upper() in AGENT_ID_FILTERS else hits_tag
                bucket.setdefault(via, set()).add(edge["child_id"])
            name = _element_name(graph, node)
            if hits_id:
                agents = set().union(*hits_id.values())
                out.add(
                    "BREAKS", node,
                    "Filters on agent ID (%s) of selected agent(s): %s."
                    % (", ".join(sorted(hits_id)), _names(graph, agents)),
                    "Update the filter to the new agent ID after the move.",
                    FIX_OPERATOR, "filter_agent_id", name=name,
                )
            if hits_tag:
                agents = set().union(*hits_tag.values())
                out.add(
                    "REVIEW", node,
                    "Filters on agent tag/label (%s) covering selected agent(s): %s."
                    % (", ".join(sorted(hits_tag)), _names(graph, agents)),
                    "Tool restores agent tags after the move; verify the filter result.",
                    FIX_TOOL, "filter_agent_tag", name=name,
                )
            if not hits_id and not hits_tag:
                keys = node["attrs"].get("filter_keys") or []
                classes = [classify_filter(k) for k in keys]
                if any(c and c[0] == "test" for c in classes) and not any(
                    c and c[0] == "agent" for c in classes
                ):
                    out.add(
                        "OK", node,
                        "Filters only on tests/test tags; tests keep their IDs.",
                        "", FIX_TOOL, "filter_test_only", name=name,
                    )

    # 3. widgets: owned agents, group by agent, alert lists
    _owned_agents_findings(graph, selected, out)
    _widget_findings(graph, selected, out)

    # 4. tests
    _test_findings(graph, selected, out)

    # 5. suppression windows with agent links
    for node in graph.nodes_of("suppression"):
        hit = [c for c in graph.children(node["id"], "applies_to_agent") if c in selected]
        if hit:
            out.add(
                "REVIEW", node,
                "Suppression window references selected agent(s): %s." % _names(graph, hit),
                "Verify the window after the move; update the agent reference if needed.",
                FIX_OPERATOR, "suppression_agent",
            )

    # 6. alert rules, agent notification rules, data gaps
    _rule_findings(graph, selected, out)
    _data_gap_findings(graph, out)

    return out.finish()


def summarize(findings):
    """Counter by level: ``{"BREAKS": n, "REVIEW": n, "OK": n, "total": n}``."""
    counts = dict((level, 0) for level in LEVELS)
    for item in findings:
        counts[item["level"]] = counts.get(item["level"], 0) + 1
    counts["total"] = len(findings)
    return counts
