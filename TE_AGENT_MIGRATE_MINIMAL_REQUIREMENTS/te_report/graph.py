"""Relationship graph of what belongs to what in a discovery export.

Nodes   : ``{"id": "agent:123", "kind": "agent", "name": "...", "attrs": {...}}``
Edges   : ``{"parent_id", "child_id", "relation", "attrs": {...}}``

Node kinds: group, agent, cluster, member, test, alert_rule (only when the
discovery has rule details), tag, dashboard, widget, global_filter,
suppression.

Edge relations (parent -> child):

* ``visible_in``        agent/cluster -> group (attrs: owner_unknown, owner)
* ``member_of``         cluster -> member
* ``assigned_test``     agent/cluster -> test (attrs: group)
* ``tagged``            test/agent/dashboard -> tag
* ``has_widget``        dashboard -> widget
* ``uses_global_filter``dashboard -> global_filter
* ``filters_on_agent``  widget/global_filter -> agent/cluster (attrs: via, mode, tag)
* ``filters_on_test``   widget/global_filter -> test (attrs: via, mode, tag)
* ``targets_agent``     test -> agent (agent-to-agent tests)
* ``alert_rule``        test -> alert_rule, ``agent_notif_rule`` agent -> alert_rule
* ``applies_to_agent`` / ``applies_to_test``  suppression -> agent / test
"""

import io
import json

SCHEMA = "te_report.graph/1"

AGENT_ID_FILTERS = ("AGENT", "TARGET_AGENT", "SOURCE_AND_TARGET_AGENT")
AGENT_TAG_FILTERS = ("AGENT_TAG", "AGENT_TAG_ID", "AGENT_LABEL")
TEST_ID_FILTERS = ("TEST",)
TEST_TAG_FILTERS = ("TEST_TAG", "TEST_TAG_ID", "TEST_LABEL", "TEST_TAG_KEY")


def classify_filter(name):
    """Return ``(target, mode)`` for a filter name, or None when irrelevant.

    ``target`` is ``agent`` or ``test``; ``mode`` is ``id`` or ``tag``.
    Endpoint-agent filters (ENDPOINT_*, EYEBROW_*) are not enterprise agents.
    """
    key = str(name).upper()
    if key in AGENT_ID_FILTERS:
        return ("agent", "id")
    if key in AGENT_TAG_FILTERS:
        return ("agent", "tag")
    if key in TEST_ID_FILTERS:
        return ("test", "id")
    if key in TEST_TAG_FILTERS:
        return ("test", "tag")
    return None


def _s(value):
    return None if value is None else str(value)


def _id_list(values):
    """Extract string ids from a list of ids or of objects holding an id."""
    out = []
    if not isinstance(values, list):
        return out
    for item in values:
        if isinstance(item, dict):
            for key in ("agentId", "testId", "id"):
                if item.get(key) is not None:
                    out.append(str(item[key]))
                    break
        elif item is not None:
            out.append(str(item))
    return out


class Graph(object):
    def __init__(self):
        self.nodes = {}
        self.edges = []
        self.meta = {}
        self._out = {}
        self._in = {}
        self._edge_keys = set()

    # ---- construction -------------------------------------------------
    def add_node(self, node_id, kind, name, **attrs):
        node = self.nodes.get(node_id)
        if node is None:
            node = {"id": node_id, "kind": kind, "name": name, "attrs": {}}
            self.nodes[node_id] = node
        elif name and not node["name"]:
            node["name"] = name
        for key, value in attrs.items():
            if value is not None:
                node["attrs"][key] = value
        return node

    def add_edge(self, parent_id, child_id, relation, **attrs):
        attrs = dict((k, v) for k, v in attrs.items() if v is not None)
        key = (parent_id, child_id, relation, json.dumps(attrs, sort_keys=True))
        if key in self._edge_keys:
            return None
        self._edge_keys.add(key)
        edge = {
            "parent_id": parent_id,
            "child_id": child_id,
            "relation": relation,
            "attrs": attrs,
        }
        self.edges.append(edge)
        self._out.setdefault(parent_id, []).append(edge)
        self._in.setdefault(child_id, []).append(edge)
        return edge

    # ---- lookups ------------------------------------------------------
    def node(self, node_id):
        return self.nodes.get(node_id)

    def nodes_of(self, kind):
        return [n for n in self.nodes.values() if n["kind"] == kind]

    def out_edges(self, node_id, relation=None):
        return [e for e in self._out.get(node_id, []) if relation in (None, e["relation"])]

    def in_edges(self, node_id, relation=None):
        return [e for e in self._in.get(node_id, []) if relation in (None, e["relation"])]

    def children(self, node_id, relation=None):
        """Node ids this node points to (unique, in edge order)."""
        seen = []
        for edge in self.out_edges(node_id, relation):
            if edge["child_id"] not in seen:
                seen.append(edge["child_id"])
        return seen

    def parents(self, node_id, relation=None):
        seen = []
        for edge in self.in_edges(node_id, relation):
            if edge["parent_id"] not in seen:
                seen.append(edge["parent_id"])
        return seen

    def descendants(self, node_id):
        """All node ids reachable through child edges (cycle safe)."""
        seen = set([node_id])
        order = []
        stack = [node_id]
        while stack:
            current = stack.pop()
            for child in self.children(current):
                if child not in seen:
                    seen.add(child)
                    order.append(child)
                    stack.append(child)
        return order

    def find(self, kind, text):
        """Nodes of ``kind`` whose id, name or identifying attrs contain ``text``."""
        needle = str(text).lower()
        found = []
        for node in self.nodes.values():
            if kind and node["kind"] != kind:
                continue
            hay = [node["id"], node["name"] or ""]
            attrs = node["attrs"]
            for key in ("agent_id", "test_id", "aid", "ips", "public_ips", "member_ips"):
                value = attrs.get(key)
                if isinstance(value, list):
                    hay.extend(str(v) for v in value)
                elif value is not None:
                    hay.append(str(value))
            if any(needle in str(h).lower() for h in hay):
                found.append(node)
        return found

    # ---- output -------------------------------------------------------
    def stats(self):
        counts = {}
        for node in self.nodes.values():
            counts[node["kind"]] = counts.get(node["kind"], 0) + 1
        return counts

    def to_dict(self):
        return {
            "schema": SCHEMA,
            "meta": self.meta,
            "stats": dict(self.stats(), edges=len(self.edges)),
            "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]),
            "edges": self.edges,
        }

    def write_json(self, path):
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.to_dict(), indent=1, sort_keys=True, ensure_ascii=False))
        return path


# ----------------------------------------------------------------------
# builder
# ----------------------------------------------------------------------
def _tag_name(tag):
    key = tag.get("key")
    value = tag.get("value")
    if key and value:
        return "%s:%s" % (key, value)
    return key or value or str(tag.get("id"))


def _owner_from_detail(detail):
    for key in ("ownerAid", "ownerAccountGroupId", "owner_aid"):
        if detail.get(key) is not None:
            return str(detail[key])
    owner = detail.get("owner") or detail.get("ownerAccountGroup")
    if isinstance(owner, dict) and owner.get("aid") is not None:
        return str(owner["aid"])
    return None


def _merge_filters(target, filters):
    """Merge a ``{NAME: [values]}`` object into ``target`` (name -> list)."""
    if not isinstance(filters, dict):
        return
    for name, values in filters.items():
        if not isinstance(values, list):
            continue
        bucket = target.setdefault(str(name).upper(), [])
        for value in values:
            if value is not None and str(value) not in bucket:
                bucket.append(str(value))


def _widget_filters(widget):
    merged = {}
    _merge_filters(merged, widget.get("filters"))
    for sub in ("numberCards", "multiMetricColumns"):
        for card in widget.get(sub) or []:
            if isinstance(card, dict):
                _merge_filters(merged, card.get("filters"))
    return merged


GROUP_BY_FIELDS = ("groupBy", "groupCardsBy", "columnGroupBy", "axisGroupBy")


def _group_by_agent(widget):
    """True when the widget (or one of its cards/columns) groups by AGENT."""
    items = [widget]
    for sub in ("numberCards", "multiMetricColumns", "cards", "columns"):
        value = widget.get(sub)
        if isinstance(value, list):
            items.extend(i for i in value if isinstance(i, dict))
    for item in items:
        for field in GROUP_BY_FIELDS:
            value = item.get(field)
            values = value if isinstance(value, list) else [value]
            for entry in values:
                if isinstance(entry, str) and entry.strip().upper() == "AGENT":
                    return True
    return False


class _Builder(object):
    def __init__(self, data):
        self.data = data
        self.g = Graph()
        self.agent_node = {}  # agentId -> node id
        self.test_node = {}  # testId -> node id
        self.tag_by_id = {}  # (aid, id) -> node id
        self.tag_by_legacy = {}  # (aid, objectType, legacy) -> node id
        self.tag_by_key = {}  # (aid, objectType, key) -> [node ids]
        self.tag_members = {}  # tag node id -> {"agent": set, "test": set}
        self._pending_dash_tags = []

    def build(self):
        data = self.data
        self.g.meta = {
            "missing": list(data.get("missing", [])),
            "errors": list(data.get("errors", [])),
        }
        for group in data.get("account_groups", []):
            aid = str(group["aid"])
            self.g.add_node("group:" + aid, "group", group.get("accountGroupName") or aid, aid=aid)
        groups = data.get("groups", {})
        for aid in sorted(groups):
            if "group:" + aid not in self.g.nodes:
                self.g.add_node("group:" + aid, "group", groups[aid].get("name") or aid, aid=aid)
        details = self._agent_details()
        for aid in sorted(groups):
            self._agents(aid, groups[aid], details)
        for aid in sorted(groups):
            self._tests(aid, groups[aid])
        for aid in sorted(groups):
            self._agent_tests(aid, groups[aid])
        self._finalize_agents(details)
        for aid in sorted(groups):
            self._tags(aid, groups[aid])
        for aid in sorted(groups):
            self._rules(aid, groups[aid])
            self._suppression(aid, groups[aid])
        for aid in sorted(groups):
            self._global_filters(aid, groups[aid])
        for aid in sorted(groups):
            self._dashboards(aid, groups[aid])
        return self.g

    # -- agents ----------------------------------------------------------
    def _agent_details(self):
        out = {}
        for agent_id, items in self.data.get("agent_details", {}).items():
            out[str(agent_id)] = items
        return out

    def _agents(self, aid, group, details):
        g = self.g
        for agent in group.get("agents", []):
            agent_id = _s(agent.get("agentId"))
            if agent_id is None:
                continue
            is_cluster = (agent.get("agentType") == "enterprise-cluster") or bool(
                agent.get("clusterMembers")
            )
            kind = "cluster" if is_cluster else "agent"
            node_id = "%s:%s" % (kind, agent_id)
            self.agent_node[agent_id] = node_id
            node = g.add_node(
                node_id,
                kind,
                agent.get("agentName") or agent_id,
                agent_id=agent_id,
                agent_type=agent.get("agentType"),
                state=agent.get("agentState"),
                ips=list(agent.get("ipAddresses") or []),
                public_ips=list(agent.get("publicIpAddresses") or []),
            )
            attrs = node["attrs"]
            attrs.setdefault("groups", [])
            if aid not in attrs["groups"]:
                attrs["groups"].append(aid)
            if attrs.get("state") is None and agent.get("agentState"):
                attrs["state"] = agent.get("agentState")
            if is_cluster:
                self._members(node_id, agent_id, agent.get("clusterMembers") or [])

    def _members(self, cluster_id, agent_id, members):
        g = self.g
        for member in members:
            if not isinstance(member, dict):
                continue
            member_id = _s(member.get("memberId") or member.get("agentId") or member.get("name"))
            if member_id is None:
                continue
            mid = "member:%s:%s" % (agent_id, member_id)
            g.add_node(
                mid,
                "member",
                member.get("name") or member_id,
                member_id=member_id,
                cluster_id=agent_id,
                state=member.get("agentState"),
                ips=list(member.get("ipAddresses") or []),
                public_ips=list(member.get("publicIpAddresses") or []),
            )
            g.add_edge(cluster_id, mid, "member_of")
        node = g.nodes[cluster_id]
        node["attrs"]["member_ips"] = sorted(
            set(ip for m in g.children(cluster_id, "member_of") for ip in g.nodes[m]["attrs"].get("ips", []))
        )
        node["attrs"]["member_states"] = [
            g.nodes[m]["attrs"].get("state") for m in g.children(cluster_id, "member_of")
        ]

    def _finalize_agents(self, details):
        g = self.g
        for agent_id, node_id in sorted(self.agent_node.items()):
            node = g.nodes[node_id]
            attrs = node["attrs"]
            shared = list(attrs.get("groups", []))
            owner = None
            for detail in details.get(agent_id, []):
                for ag in detail.get("accountGroups") or []:
                    if isinstance(ag, dict) and ag.get("aid") is not None:
                        if str(ag["aid"]) not in shared:
                            shared.append(str(ag["aid"]))
                owner = owner or _owner_from_detail(detail)
                if node["kind"] == "cluster" and not g.children(node_id, "member_of"):
                    self._members(node_id, agent_id, detail.get("clusterMembers") or [])
            attrs["shared_with"] = shared
            attrs["shared_count"] = len(shared)
            if owner:
                attrs["owner_aid"] = owner
            for aid in attrs.get("groups", []):
                g.add_edge(
                    node_id,
                    "group:" + aid,
                    "visible_in",
                    owner_unknown=(not owner),
                    owner=((aid == owner) if owner else None),
                )

    # -- tests -----------------------------------------------------------
    def _tests(self, aid, group):
        g = self.g
        for test in group.get("tests", []):
            test_id = _s(test.get("testId"))
            if test_id is None:
                continue
            node_id = "test:" + test_id
            self.test_node[test_id] = node_id
            node = g.add_node(
                node_id,
                "test",
                test.get("testName") or test_id,
                test_id=test_id,
                test_type=test.get("type"),
                enabled=test.get("enabled"),
                target_agent_id=_s(test.get("targetAgentId")),
            )
            aids = node["attrs"].setdefault("aids", [])
            if aid not in aids:
                aids.append(aid)
            node["attrs"]["aid"] = aids[0]
        for test in group.get("tests", []):
            test_id = _s(test.get("testId"))
            target = _s(test.get("targetAgentId"))
            if test_id and target and target in self.agent_node:
                g.add_edge("test:" + test_id, self.agent_node[target], "targets_agent")

    def _agent_tests(self, aid, group):
        g = self.g
        for agent in group.get("agents", []):
            agent_id = _s(agent.get("agentId"))
            if agent_id not in self.agent_node:
                continue
            for test_id in _id_list(agent.get("testIds")):
                if test_id not in self.test_node:
                    node_id = "test:" + test_id
                    g.add_node(
                        node_id, "test", "(unlisted test %s)" % test_id,
                        test_id=test_id, stub=True, aid=aid, aids=[aid],
                    )
                    self.test_node[test_id] = node_id
                g.add_edge(self.agent_node[agent_id], self.test_node[test_id], "assigned_test", group=aid)

    # -- tags ------------------------------------------------------------
    def _tags(self, aid, group):
        g = self.g
        for tag in group.get("tags", []):
            tag_id = _s(tag.get("id"))
            if tag_id is None:
                continue
            node_id = "tag:%s:%s" % (aid, tag_id)
            otype = tag.get("objectType")
            g.add_node(
                node_id,
                "tag",
                _tag_name(tag),
                tag_id=tag_id,
                aid=aid,
                key=tag.get("key"),
                value=tag.get("value"),
                object_type=otype,
                tag_type=tag.get("type"),
                legacy_id=_s(tag.get("legacyId")),
            )
            self.tag_by_id[(aid, tag_id)] = node_id
            if tag.get("legacyId") is not None:
                self.tag_by_legacy[(aid, otype, str(tag["legacyId"]))] = node_id
            if tag.get("key") is not None:
                self.tag_by_key.setdefault((aid, otype, str(tag["key"])), []).append(node_id)
            members = self.tag_members.setdefault(node_id, {"agent": set(), "test": set()})
            for item in tag.get("assignments") or []:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                atype = item.get("type")
                aid_item = str(item["id"])
                if atype == "test" and aid_item in self.test_node:
                    members["test"].add(self.test_node[aid_item])
                    g.add_edge(self.test_node[aid_item], node_id, "tagged")
                elif atype == "v-agent" and aid_item in self.agent_node:
                    members["agent"].add(self.agent_node[aid_item])
                    g.add_edge(self.agent_node[aid_item], node_id, "tagged")
            g.nodes[node_id]["attrs"]["assignment_count"] = len(tag.get("assignments") or [])
        # dashboards tagged: resolved after dashboards exist
        for tag in group.get("tags", []):
            for item in tag.get("assignments") or []:
                if isinstance(item, dict) and item.get("type") == "dashboard" and item.get("id") is not None:
                    self._pending_dash_tags.append(
                        (aid, str(item["id"]), "tag:%s:%s" % (aid, tag.get("id")))
                    )

    def _resolve_tags(self, aid, target, name, value):
        """Tag node ids matched by a tag/label filter value (id, legacy id or key)."""
        otype = "v-agent" if target == "agent" else "test"
        value = str(value)
        if name.upper() == "TEST_TAG_KEY":
            found = list(self.tag_by_key.get((aid, otype, value), []))
            if not found:
                for (a, t, k), nodes in self.tag_by_key.items():
                    if t == otype and k == value:
                        found.extend(nodes)
            return found
        node_id = self.tag_by_id.get((aid, value)) or self.tag_by_legacy.get((aid, otype, value))
        if node_id:
            return [node_id]
        for (a, tid), node_id in self.tag_by_id.items():
            if tid == value:
                return [node_id]
        for (a, t, legacy), node_id in self.tag_by_legacy.items():
            if t == otype and legacy == value:
                return [node_id]
        return []

    # -- filters ---------------------------------------------------------
    def _link_filters(self, owner_id, aid, filters):
        g = self.g
        unresolved = {"agents": 0, "tests": 0, "tags": 0}
        for name in sorted(filters):
            cls = classify_filter(name)
            if cls is None:
                continue
            target, mode = cls
            for value in filters[name]:
                if mode == "id":
                    lookup = self.agent_node if target == "agent" else self.test_node
                    node_id = lookup.get(value)
                    if node_id:
                        g.add_edge(owner_id, node_id, "filters_on_" + target, via=name, mode="id")
                    else:
                        unresolved[target + "s"] += 1
                else:
                    tags = self._resolve_tags(aid, target, name, value)
                    if not tags:
                        unresolved["tags"] += 1
                    for tag_id in tags:
                        members = self.tag_members.get(tag_id, {}).get(target, set())
                        for node_id in sorted(members):
                            g.add_edge(
                                owner_id, node_id, "filters_on_" + target,
                                via=name, mode="tag", tag=g.nodes[tag_id]["name"],
                            )
        owner = g.nodes[owner_id]
        if any(unresolved.values()):
            owner["attrs"]["unresolved"] = unresolved

    def _global_filters(self, aid, group):
        g = self.g
        for gf in group.get("dashboard_filters", []):
            gf_id = _s(gf.get("id"))
            if gf_id is None:
                continue
            node_id = "global_filter:%s:%s" % (aid, gf_id)
            filters = {}
            for ctx in gf.get("context") or []:
                if not isinstance(ctx, dict):
                    continue
                if str(ctx.get("dataSourceId", "")).upper().startswith("ENDPOINT"):
                    continue
                for flt in ctx.get("filters") or []:
                    if isinstance(flt, dict) and isinstance(flt.get("values"), list):
                        _merge_filters(filters, {flt.get("filterId"): flt["values"]})
            g.add_node(
                node_id, "global_filter", gf.get("name") or gf_id,
                filter_id=gf_id, aid=aid, filter_keys=sorted(filters),
            )
            self._link_filters(node_id, aid, filters)

    # -- dashboards ------------------------------------------------------
    def _dashboards(self, aid, group):
        g = self.g
        listed = {}
        for dash in group.get("dashboards", []):
            if dash.get("dashboardId") is not None:
                listed[str(dash["dashboardId"])] = dash
        details = group.get("dashboard_details", {})
        for dash_id in sorted(set(listed) | set(details)):
            summary = listed.get(dash_id, {})
            detail = details.get(dash_id, {})
            merged = dict(summary)
            merged.update(detail)
            node_id = "dashboard:%s:%s" % (aid, dash_id)
            gf_id = _s(merged.get("globalFilterId"))
            g.add_node(
                node_id, "dashboard", merged.get("title") or dash_id,
                dashboard_id=dash_id, aid=aid,
                built_in=merged.get("isBuiltIn"),
                global_filter_id=gf_id,
                has_detail=bool(detail),
            )
            if gf_id:
                gf_node = "global_filter:%s:%s" % (aid, gf_id)
                if gf_node in g.nodes:
                    g.add_edge(node_id, gf_node, "uses_global_filter")
                else:
                    g.nodes[node_id]["attrs"]["global_filter_missing"] = True
            for index, widget in enumerate(detail.get("widgets") or []):
                if not isinstance(widget, dict):
                    continue
                wid = _s(widget.get("id")) or "w%d" % index
                wnode = "widget:%s:%s:%s" % (aid, dash_id, wid)
                filters = _widget_filters(widget)
                g.add_node(
                    wnode, "widget", widget.get("title") or wid,
                    widget_id=wid, dashboard_id=dash_id, aid=aid,
                    widget_type=widget.get("type"),
                    metric_group=widget.get("metricGroup"),
                    metric=widget.get("metric"),
                    data_source=widget.get("dataSource"),
                    agents=widget.get("agents"),
                    show=widget.get("show"),
                    filter_keys=sorted(filters),
                    group_by_agent=True if _group_by_agent(widget) else None,
                )
                g.add_edge(node_id, wnode, "has_widget")
                self._link_filters(wnode, aid, filters)
        for aid2, dash_id, tag_node in self._pending_dash_tags:
            dnode = "dashboard:%s:%s" % (aid2, dash_id)
            if aid2 == aid and dnode in g.nodes:
                g.add_edge(dnode, tag_node, "tagged")

    # -- rules / suppression ---------------------------------------------
    def _count(self, key, linked):
        counts = self.g.meta.setdefault("rule_counts", {})
        counts[key + "_total"] = counts.get(key + "_total", 0) + 1
        counts.setdefault(key + "_linked", 0)
        if linked:
            counts[key + "_linked"] += 1

    def _rules(self, aid, group):
        g = self.g
        for rule in group.get("alert_rules", []):
            tests = _id_list(rule.get("tests") or rule.get("testIds"))
            agents = _id_list(rule.get("agents") or rule.get("agentIds"))
            self._count("alert_rules", tests or agents)
            if tests:
                node_id = self._rule_node(aid, rule, "alert")
                for test_id in tests:
                    if test_id in self.test_node:
                        g.add_edge(self.test_node[test_id], node_id, "alert_rule")
        for rule in group.get("agent_notif_rules", []):
            agents = _id_list(rule.get("agents") or rule.get("agentIds"))
            self._count("agent_notif_rules", agents)
            if agents:
                node_id = self._rule_node(aid, rule, "agent")
                for agent_id in agents:
                    if agent_id in self.agent_node:
                        g.add_edge(self.agent_node[agent_id], node_id, "agent_notif_rule")

    def _rule_node(self, aid, rule, flavour):
        rid = _s(rule.get("ruleId")) or _s(rule.get("ruleName"))
        node_id = "alert_rule:%s:%s:%s" % (aid, flavour, rid)
        self.g.add_node(
            node_id, "alert_rule", rule.get("ruleName") or rid,
            rule_id=rid, aid=aid, flavour=flavour,
            severity=rule.get("severity"), alert_type=rule.get("alertType"),
            min_sources=rule.get("minimumSources"),
            min_sources_pct=rule.get("minimumSourcesPct"),
        )
        return node_id

    def _suppression(self, aid, group):
        g = self.g
        details = group.get("suppression_details", {})
        listed = {}
        for item in group.get("suppression", []):
            if item.get("alertSuppressionWindowId") is not None:
                listed[str(item["alertSuppressionWindowId"])] = item
        for sid in sorted(set(listed) | set(details)):
            merged = dict(listed.get(sid, {}))
            merged.update(details.get(sid, {}))
            node_id = "suppression:%s:%s" % (aid, sid)
            g.add_node(
                node_id, "suppression", merged.get("name") or sid,
                suppression_id=sid, aid=aid,
                enabled=merged.get("isEnabled"), status=merged.get("status"),
            )
            for agent_id in _id_list(merged.get("agents") or merged.get("agentIds")):
                if agent_id in self.agent_node:
                    g.add_edge(node_id, self.agent_node[agent_id], "applies_to_agent")
            for test_id in _id_list(merged.get("tests") or merged.get("testIds")):
                if test_id in self.test_node:
                    g.add_edge(node_id, self.test_node[test_id], "applies_to_test")


def build_graph(data):
    """Build a :class:`Graph` from the dict returned by ``load_discovery``."""
    return _Builder(data).build()
