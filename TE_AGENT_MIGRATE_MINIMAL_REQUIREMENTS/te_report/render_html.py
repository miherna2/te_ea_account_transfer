"""Self-contained HTML relationship report (standard library only).

One file: inline CSS and a small inline script. No external CSS, scripts,
fonts or images; a Content-Security-Policy meta tag blocks any network load.
Every value that comes from the data goes through ``html.escape``. The script
only reads and writes ``textContent`` / ``dataset`` / ``hidden`` / ``open``;
it never builds markup from data and never uses ``eval``.

Size control: a node is drawn in full only once (first place it is met). Later
places show a one-line reference with a link that opens the full node.
"""

import html

from te_report.impact import LEVELS, is_offline
from te_report.loader import is_secret_name

# caps that keep the page small on big accounts
MAX_TEST_DASHBOARDS = 3  # dashboards listed under a test
MAX_TEST_WIDGETS = 3  # widgets listed for each of those dashboards
MAX_SHARE_ROWS = 2000

_ID_KEYS = {
    "group": "aid",
    "agent": "agent_id",
    "cluster": "agent_id",
    "member": "member_id",
    "test": "test_id",
    "dashboard": "dashboard_id",
    "widget": "widget_id",
    "global_filter": "filter_id",
    "suppression": "suppression_id",
    "alert_rule": "rule_id",
    "tag": "tag_id",
}


def esc(value):
    """HTML-escape any value (None becomes an empty string)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def clean_attrs(attrs):
    """Defensive filter: drop secret-like keys and non-scalar values."""
    out = {}
    for key, value in (attrs or {}).items():
        if is_secret_name(key):
            continue
        if isinstance(value, (dict, set)):
            continue
        if isinstance(value, (list, tuple)):
            value = [v for v in value if not isinstance(v, (dict, list, tuple, set))]
        out[key] = value
    return out


def worst_levels(findings):
    """``element_id -> worst level`` over a findings list."""
    order = dict((lvl, i) for i, lvl in enumerate(LEVELS))
    out = {}
    for item in findings:
        current = out.get(item["element_id"])
        if current is None or order.get(item["level"], 99) < order.get(current, 99):
            out[item["element_id"]] = item["level"]
    return out


def _lname(node):
    return ((node["name"] or "") if node else "").lower()


CSS = """
body{font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.4;color:#202124;
margin:0 auto;padding:16px 24px;max-width:1200px;background:#fff}
h1{font-size:24px;color:#0b4a6f;margin:0 0 4px}
h2{font-size:18px;color:#0b4a6f;border-bottom:1px solid #c9d6df;padding-bottom:4px;margin:28px 0 10px}
h3{font-size:15px;margin:16px 0 6px}
p,li{margin:4px 0}
.muted{color:#5f6368}
.small{font-size:12px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #dadce0;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#eef3f7;color:#0b4a6f}
tr[hidden]{display:none}
td.num{text-align:right}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.card{border:1px solid #c9d6df;border-radius:4px;padding:6px 12px;min-width:110px}
.card b{display:block;font-size:20px}
.b{display:inline-block;font-size:11px;font-weight:bold;padding:0 6px;border-radius:3px;
border:2px solid #444;letter-spacing:.3px;white-space:nowrap}
.b-BREAKS{background:#b3261e;color:#fff;border:2px solid #7a1712}
.b-REVIEW{background:#fff3cd;color:#5c3d00;border:2px dashed #b07a00}
.b-OK{background:#fff;color:#1b5e20;border:2px dotted #2e7d32}
.b-SEL{background:#0b4a6f;color:#fff;border-color:#0b4a6f}
.b-OFF{background:#eee;color:#444;border:2px solid #888}
.roll{font-size:12px;color:#5f6368}
button{font:inherit;padding:3px 10px;margin:0 4px 4px 0;border:1px solid #9aa8b3;background:#f6f9fb;
border-radius:3px;cursor:pointer}
button.on{background:#0b4a6f;color:#fff;border-color:#0b4a6f}
input[type=text]{font:inherit;padding:3px 6px;border:1px solid #9aa8b3;border-radius:3px;width:260px}
#tree details{margin:2px 0 2px 18px}
#tree>details{margin-left:0}
#tree summary{cursor:pointer;padding:1px 0}
#tree .leaf,#tree .ref{margin:2px 0 2px 18px;padding:1px 0}
#tree .ref{color:#5f6368}
.kind{display:inline-block;min-width:60px;font-size:11px;text-transform:uppercase;color:#0b4a6f;
background:#eef3f7;padding:0 4px;border-radius:2px;text-align:center}
.name{font-weight:bold}
.id{font-family:Consolas,Menlo,monospace;font-size:12px;color:#5f6368}
.meta{font-size:12px;color:#5f6368}
.chip{font-size:11px;background:#e8f0f6;color:#0b4a6f;border-radius:8px;padding:0 6px;margin-left:3px}
.hit{outline:3px solid #f9ab00;background:#fff8e1}
a{color:#0b5cad}
.scroll{overflow-x:auto}
td.c{text-align:center}
@media print{
body{max-width:none;padding:0;font-size:11px}
button,input,.noprint{display:none}
.scroll{overflow:visible}
a{color:inherit;text-decoration:none}
details>summary{list-style:none}
h2{page-break-after:avoid}
tr{page-break-inside:avoid}
}
"""

SCRIPT = """
(function () {
  'use strict';
  var table = document.getElementById('ftable');
  var rows = table ? Array.prototype.slice.call(table.tBodies[0].rows) : [];
  var texts = null;
  var level = 'ALL';
  var query = '';
  var box = document.getElementById('fbox');
  var count = document.getElementById('fcount');
  var buttons = Array.prototype.slice.call(document.querySelectorAll('button[data-level]'));

  function apply() {
    if (query !== '' && texts === null) {
      texts = rows.map(function (r) { return r.textContent.toLowerCase(); });
    }
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var ok = (level === 'ALL' || rows[i].dataset.level === level) &&
               (query === '' || texts[i].indexOf(query) !== -1);
      rows[i].hidden = !ok;
      if (ok) { shown++; }
    }
    if (count) { count.textContent = 'Showing ' + shown + ' of ' + rows.length; }
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      level = b.dataset.level;
      buttons.forEach(function (o) { o.classList.toggle('on', o === b); });
      apply();
    });
  });
  if (box) {
    box.addEventListener('input', function () {
      query = box.value.trim().toLowerCase();
      apply();
    });
  }

  function openTo(el) {
    var p = el;
    while (p) {
      if (p.tagName === 'DETAILS') { p.open = true; }
      p = p.parentElement;
    }
  }
  function go(id) {
    var el = document.getElementById(id);
    if (!el) { return; }
    openTo(el);
    el.scrollIntoView();
    el.classList.add('hit');
    window.setTimeout(function () { el.classList.remove('hit'); }, 2500);
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a.goto') : null;
    if (!a) { return; }
    e.preventDefault();
    go(a.getAttribute('href').slice(1));
  });
  if (window.location.hash.length > 1) { go(window.location.hash.slice(1)); }

  var all = document.getElementById('tree-expand');
  var none = document.getElementById('tree-collapse');
  function setAll(state) {
    var ds = document.querySelectorAll('#tree details');
    for (var i = 0; i < ds.length; i++) {
      ds[i].open = state || ds[i].classList.contains('top');
    }
  }
  if (all) { all.addEventListener('click', function () { setAll(true); }); }
  if (none) { none.addEventListener('click', function () { setAll(false); }); }
  apply();
})();
"""


class _Page(object):
    def __init__(self, graph, findings, selection, meta):
        self.g = graph
        self.findings = list(findings or [])
        self.sel = selection
        self.meta = meta or {}
        self.levels = worst_levels(self.findings)
        self.selected = set(selection["selected"]) if selection else set()
        self.dom = {}
        self._group_index = None
        self.rendered = set()
        self.assigned_anywhere = set()
        for edge in graph.edges:
            if edge["relation"] == "assigned_test":
                self.assigned_anywhere.add(edge["child_id"])
        self.test_dash = self._test_dashboards()

    # ---- small helpers -------------------------------------------------
    def dom_id(self, node_id):
        key = self.dom.get(node_id)
        if key is None:
            key = "n%d" % (len(self.dom) + 1)
            self.dom[node_id] = key
        return key

    def sort_ids(self, ids):
        nodes = self.g.nodes
        return sorted(ids, key=lambda i: (_lname(nodes.get(i)), i))

    def native_id(self, node):
        key = _ID_KEYS.get(node["kind"])
        value = clean_attrs(node["attrs"]).get(key) if key else None
        return value if value is not None else node["id"]

    def group_name(self, aid):
        node = self.g.nodes.get("group:%s" % aid)
        return (node["name"] if node else None) or aid

    def badge(self, level):
        return '<span class="b b-%s">%s</span>' % (esc(level), esc(level))

    def flags(self, node):
        out = []
        level = self.levels.get(node["id"])
        if level:
            out.append(self.badge(level))
        if node["kind"] in ("agent", "cluster"):
            if node["id"] in self.selected:
                out.append('<span class="b b-SEL">SELECTED</span>')
            elif is_offline(self.g, node):
                out.append('<span class="b b-OFF">OFFLINE</span>')
        return " ".join(out)

    def head(self, node, extra="", flags=None):
        return (
            '<span class="kind">%s</span> <span class="name">%s</span> '
            '<span class="id">%s</span> %s%s'
            % (
                esc(node["kind"]), esc(node["name"] or node["id"]), esc(self.native_id(node)),
                flags if flags is not None else self.flags(node),
                (' <span class="meta">%s</span>' % extra) if extra else "",
            )
        )

    def ref(self, node, note=""):
        return (
            '<div class="ref"><span class="kind">%s</span> '
            '<a class="goto" href="#%s">%s</a> <span class="id">%s</span> %s'
            ' <span class="meta">%s</span></div>'
            % (
                esc(node["kind"]), self.dom_id(node["id"]), esc(node["name"] or node["id"]),
                esc(self.native_id(node)), self.flags(node), esc(note or "(shown elsewhere)"),
            )
        )

    def link(self, node):
        return '<a class="goto" href="#%s">%s</a>' % (
            self.dom_id(node["id"]), esc(node["name"] or node["id"]))

    def chips(self, tag_ids):
        return "".join(
            '<span class="chip">%s</span>' % esc(self.g.nodes[t]["name"]) for t in tag_ids
        )

    def tags_of(self, node_id, aid=None):
        out = []
        for tag_id in self.sort_ids(self.g.children(node_id, "tagged")):
            tag = self.g.nodes[tag_id]
            if aid is None or tag["attrs"].get("aid") == aid:
                out.append(tag_id)
        return out

    # ---- test <-> dashboards ---------------------------------------------
    def _test_dashboards(self):
        """test id -> ordered {dashboard id: [widget ids]} from filters_on_test."""
        g = self.g
        found = {}
        seen = set()
        for edge in g.edges:
            if edge["relation"] != "filters_on_test":
                continue
            owner = edge["parent_id"]
            test_id = edge["child_id"]
            key = (owner, test_id)
            if key in seen:
                continue
            seen.add(key)
            owner_node = g.nodes.get(owner)
            if owner_node is None:
                continue
            bucket = found.setdefault(test_id, {})
            if owner_node["kind"] == "widget":
                for dash in g.parents(owner, "has_widget"):
                    bucket.setdefault(dash, []).append(owner)
            else:
                for dash in g.parents(owner, "uses_global_filter"):
                    bucket.setdefault(dash, [])
        return found

    # ---- tree nodes --------------------------------------------------------
    def leaf(self, node, extra="", key=None):
        self.rendered.add(node["id"])
        return '<div class="leaf" id="%s">%s</div>' % (
            self.dom_id(key or node["id"]), self.head(node, extra))

    def container(self, node, extra, children, top=False, flags=None, key=None):
        self.rendered.add(node["id"])
        return '<details%s id="%s"><summary>%s</summary>%s</details>' % (
            ' class="top" open' if top else "", self.dom_id(key or node["id"]),
            self.head(node, extra, flags), "".join(children),
        )

    def node_html(self, node_id, aid):
        node = self.g.nodes[node_id]
        if node_id in self.rendered:
            return self.ref(node)
        kind = node["kind"]
        if kind in ("agent", "cluster"):
            return self.agent_html(node, aid)
        if kind == "test":
            return self.test_html(node, aid)
        if kind == "dashboard":
            return self.dashboard_html(node)
        if kind == "alert_rule":
            return self.rule_html(node)
        if kind == "widget":
            return self.widget_html(node)
        return self.leaf(node, self.simple_meta(node))

    def simple_meta(self, node):
        attrs = clean_attrs(node["attrs"])
        parts = []
        kind = node["kind"]
        if kind == "suppression":
            parts = ["enabled=%s" % attrs.get("enabled"), "status=%s" % (attrs.get("status") or "")]
            scope = len(self.g.children(node["id"], "applies_to_agent"))
            tests = len(self.g.children(node["id"], "applies_to_test"))
            parts.append("%d agent(s), %d test(s) in scope" % (scope, tests))
        elif kind == "global_filter":
            parts = ["filters: " + (", ".join(attrs.get("filter_keys") or []) or "none")]
        elif kind == "member":
            parts = ["state=%s" % (attrs.get("state") or ""), " ".join((attrs.get("ips") or [])[:3])]
        return esc("; ".join(p for p in parts if p))

    def rule_html(self, node):
        attrs = clean_attrs(node["attrs"])
        extra = esc("%s; severity=%s" % (attrs.get("flavour") or "", attrs.get("severity") or ""))
        return self.leaf(node, extra)

    def widget_html(self, node):
        attrs = clean_attrs(node["attrs"])
        parts = [attrs.get("widget_type") or ""]
        keys = attrs.get("filter_keys") or []
        if keys:
            parts.append("filters: " + ", ".join(keys))
        return self.leaf(node, esc("; ".join(p for p in parts if p)))

    def test_html(self, node, aid):
        g = self.g
        attrs = clean_attrs(node["attrs"])
        extra = esc("%s%s" % (attrs.get("test_type") or "", "" if attrs.get("enabled") in (None, True) else "; disabled"))
        tags = self.tags_of(node["id"])
        if tags:
            extra += " " + self.chips(tags)
        children = []
        for rule in self.sort_ids(g.children(node["id"], "alert_rule")):
            children.append(self.node_html(rule, aid))
        for target in self.sort_ids(g.children(node["id"], "targets_agent")):
            children.append(self.ref(g.nodes[target], "agent-to-agent target"))
        dashes = self.test_dash.get(node["id"])
        if dashes:
            dlist = self.sort_ids(dashes)
            lines = []
            for dash_id in dlist[:MAX_TEST_DASHBOARDS]:
                dash = g.nodes[dash_id]
                widgets = self.sort_ids(dashes[dash_id])
                line = '<div class="ref"><span class="kind">dashboard</span> %s <span class="id">%s</span>' % (
                    self.link(dash), esc(self.native_id(dash)))
                if widgets:
                    line += ' <span class="meta">widgets:</span> ' + ", ".join(
                        self.link(g.nodes[w]) for w in widgets[:MAX_TEST_WIDGETS])
                    if len(widgets) > MAX_TEST_WIDGETS:
                        line += ' <span class="meta">(+%d more)</span>' % (len(widgets) - MAX_TEST_WIDGETS)
                lines.append(line + "</div>")
            if len(dlist) > MAX_TEST_DASHBOARDS:
                lines.append('<div class="ref meta">+%d more dashboard(s)</div>' % (len(dlist) - MAX_TEST_DASHBOARDS))
            children.append(
                "<details><summary><span class=\"meta\">used by %d dashboard(s)</span></summary>%s</details>"
                % (len(dlist), "".join(lines))
            )
        if children:
            return self.container(node, extra, children)
        return self.leaf(node, extra)

    def agent_html(self, node, aid):
        g = self.g
        attrs = clean_attrs(node["attrs"])
        ips = " ".join((attrs.get("ips") or [])[:3])
        tests = sorted(set(
            e["child_id"] for e in g.out_edges(node["id"], "assigned_test")
            if e["attrs"].get("group") == aid
        ))
        shared = attrs.get("shared_count") or len(attrs.get("groups") or []) or 1
        extra = esc("state=%s; %s; shared with %d group(s); %d test(s) here" % (
            attrs.get("state") or "?", ips, shared, len(tests)))
        tags = self.tags_of(node["id"], aid)
        if tags:
            extra += " " + self.chips(tags)
        first = node["id"] not in self.rendered
        key = node["id"] if first else "%s|%s" % (node["id"], aid)
        children = []
        if first:
            for member in self.sort_ids(g.children(node["id"], "member_of")):
                children.append(self.node_html(member, aid))
            for rule in self.sort_ids(g.children(node["id"], "agent_notif_rule")):
                children.append(self.node_html(rule, aid))
        else:
            extra += " (also listed in another group; members and rules are shown there)"
        for test_id in self.sort_ids(tests):
            children.append(self.node_html(test_id, aid))
        if children:
            return self.container(node, extra, children, key=key)
        return self.leaf(node, extra, key=key)

    def dashboard_html(self, node):
        g = self.g
        children = []
        for gf in g.children(node["id"], "uses_global_filter"):
            children.append(self.node_html(gf, None))
        widgets = [self.g.nodes[w] for w in self.sort_ids(g.children(node["id"], "has_widget"))]
        worst = None
        counts = {}
        for w in widgets:
            level = self.levels.get(w["id"])
            if level:
                counts[level] = counts.get(level, 0) + 1
        for level in LEVELS:
            if counts.get(level) and level != "OK":
                worst = level
                break
        for w in widgets:
            children.append(self.node_html(w["id"], None))
        attrs = clean_attrs(node["attrs"])
        extra = esc("%d widget(s)%s" % (len(widgets), "; built-in" if attrs.get("built_in") else ""))
        flags = self.flags(node)
        if worst:
            flags += ' <span class="roll">widgets: %s &times;%d</span> %s' % (
                esc(worst), counts[worst], self.badge(worst))
        return self.container(node, extra, children, flags=flags)

    # ---- sections ----------------------------------------------------------
    def group_index(self):
        if self._group_index is None:
            index = {}
            for node in self.g.nodes.values():
                if node["kind"] in ("test", "dashboard", "global_filter", "suppression"):
                    for a in (node["attrs"].get("aids") or [node["attrs"].get("aid")]):
                        index.setdefault(a, {}).setdefault(node["kind"], []).append(node["id"])
            self._group_index = index
        return self._group_index

    def section_group(self, gnode):
        g = self.g
        aid = gnode["attrs"].get("aid")
        agents = self.sort_ids(g.parents(gnode["id"], "visible_in"))
        by_group = self.group_index().get(aid, {})
        parts = []

        def block(title, items):
            if items:
                parts.append(
                    "<details><summary><b>%s</b> <span class=\"meta\">(%d)</span></summary>%s</details>"
                    % (esc(title), len(items), "".join(items))
                )

        block("Agents and clusters", [self.agent_html(g.nodes[a], aid) for a in agents])
        loose = [t for t in self.sort_ids(by_group.get("test", [])) if t not in self.assigned_anywhere]
        block("Tests not assigned to any agent", [self.node_html(t, aid) for t in loose])
        block("Dashboards", [self.node_html(d, aid) for d in self.sort_ids(by_group.get("dashboard", []))])
        block("Global filters", [self.node_html(x, aid) for x in self.sort_ids(by_group.get("global_filter", []))])
        block("Suppression windows", [self.node_html(x, aid) for x in self.sort_ids(by_group.get("suppression", []))])
        extra = esc("%d agent(s)/cluster(s), %d test(s), %d dashboard(s)" % (
            len(agents), len(by_group.get("test", [])), len(by_group.get("dashboard", []))))
        return self.container(gnode, extra, parts, top=True)

    def section_tree(self):
        g = self.g
        groups = sorted(g.nodes_of("group"), key=lambda n: (_lname(n), n["id"]))
        out = []
        for gnode in groups:
            out.append(self.section_group(gnode))
        rest = []
        for kind in ("agent", "cluster", "test", "alert_rule", "dashboard", "global_filter", "suppression"):
            for node_id in self.sort_ids([n["id"] for n in g.nodes_of(kind)]):
                if node_id not in self.rendered:
                    rest.append(self.node_html(node_id, None))
        if rest:
            out.append(
                '<details class="top" open><summary><span class="kind">other</span> '
                '<span class="name">Elements not placed under a group</span> '
                '<span class="meta">(%d)</span></summary>%s</details>' % (len(rest), "".join(rest))
            )
        return "".join(out)

    def section_summary(self):
        g = self.g
        stats = g.stats()
        counts = dict((lvl, 0) for lvl in LEVELS)
        for item in self.findings:
            counts[item["level"]] = counts.get(item["level"], 0) + 1
        cards = []

        def card(label, value):
            cards.append('<div class="card"><b>%s</b>%s</div>' % (esc(value), esc(label)))

        for label, kind in (
            ("account groups", "group"), ("agents", "agent"), ("clusters", "cluster"),
            ("tests", "test"), ("dashboards", "dashboard"), ("widgets", "widget"),
            ("alert rules", "alert_rule"), ("suppression windows", "suppression"),
        ):
            card(label, stats.get(kind, 0))
        card("relationships", len(g.edges))
        out = ['<h2 id="summary">1. Summary</h2><div class="cards">%s</div>' % "".join(cards)]
        if self.sel is not None:
            sel = self.sel
            tokens = self.meta.get("selection_count")
            out.append(
                "<p>Selection%s: <b>%d</b> agent(s)/cluster(s) to migrate, <b>%d</b> excluded because offline, "
                "<b>%d</b> identifier(s) not matched.</p>" % (
                    (" (%s identifiers)" % esc(tokens)) if tokens is not None else "",
                    len(sel["selected"]), len(sel["excluded_offline"]), len(sel["unmatched"]))
            )
            if sel["unmatched"]:
                out.append('<p class="small muted">Unmatched: %s</p>' % esc(", ".join(sel["unmatched"][:50])))
            out.append(
                '<div class="cards">%s</div>' % "".join(
                    '<div class="card">%s <b>%d</b></div>' % (self.badge(l), counts[l]) for l in LEVELS)
            )
        else:
            out.append("<p>No selection was given: inventory only, no impact flags.</p>")
        out.append(
            "<p class=\"small muted\">Flags: <b>OK</b> no change expected; <b>REVIEW</b> check by hand; "
            "<b>BREAKS</b> stops working unless a fix runs. SELECTED marks agents/clusters in the move.</p>"
        )
        missing = len(self.meta.get("missing") or [])
        errors = len(self.meta.get("errors") or [])
        if missing or errors:
            out.append('<p class="small muted">Discovery gaps: %d optional file(s) missing, %d unreadable.</p>'
                       % (missing, errors))
        return "".join(out)

    def section_tree_wrapper(self):
        return (
            '<h2 id="inventory">2. Inventory tree</h2>'
            '<p class="small muted">Group &gt; agent/cluster &gt; members, tests &gt; rules, dashboards. '
            "Each element is drawn in full once; other places show a link.</p>"
            '<p class="noprint"><button type="button" id="tree-expand">Expand all</button>'
            '<button type="button" id="tree-collapse">Collapse all</button></p>'
            '<div id="tree">%s</div>' % self.section_tree()
        )

    def section_findings(self):
        order = dict((lvl, i) for i, lvl in enumerate(LEVELS))
        items = sorted(self.findings, key=lambda f: order.get(f["level"], 99))
        out = ['<h2 id="findings">3. Findings</h2>']
        if self.sel is None:
            out.append("<p>No selection given; impact flags were not computed.</p>")
            return "".join(out)
        counts = dict((lvl, 0) for lvl in LEVELS)
        for item in items:
            counts[item["level"]] = counts.get(item["level"], 0) + 1
        out.append(
            '<p class="noprint"><input type="text" id="fbox" placeholder="Filter text" '
            'aria-label="Filter findings"> <button type="button" data-level="ALL" class="on">All (%d)</button>%s '
            '<span id="fcount" class="muted small"></span></p>' % (
                len(items),
                "".join('<button type="button" data-level="%s">%s (%d)</button>' % (l, l, counts[l]) for l in LEVELS),
            )
        )
        rows = []
        for item in items:
            node = self.g.nodes.get(item["element_id"])
            name = esc(item["element_name"])
            if node is not None and item["element_id"] in self.rendered:
                name = '<a class="goto" href="#%s">%s</a>' % (self.dom_id(item["element_id"]), name)
            group = item.get("group") or ""
            gtext = ", ".join(self.group_name(a) for a in str(group).split(",") if a)
            rows.append(
                '<tr data-level="%s"><td>%s</td><td>%s</td><td>%s <span class="id">%s</span></td>'
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    esc(item["level"]), self.badge(item["level"]), esc(item["element_kind"]), name,
                    esc(self.native_id(node) if node else item["element_id"]), esc(item["reason"]),
                    esc(item["fix"]), esc(item["fix_owner"]), esc(gtext),
                )
            )
        out.append(
            '<div class="scroll"><table id="ftable"><thead><tr><th>Level</th><th>Kind</th><th>Element</th>'
            "<th>Reason</th><th>Fix</th><th>Who</th><th>Group</th></tr></thead><tbody>%s</tbody></table></div>"
            % "".join(rows)
        )
        return "".join(out)

    def agent_row(self, node):
        attrs = clean_attrs(node["attrs"])
        groups = attrs.get("shared_with") or attrs.get("groups") or []
        tests = len(self.g.children(node["id"], "assigned_test"))
        if node["kind"] == "cluster":
            states = [s or "unknown" for s in (attrs.get("member_states") or [])]
            bad = sorted(set(s for s in states if s != "online"))
            reason = "member(s) not online: " + ", ".join(bad) if bad else "cluster state %s" % attrs.get("state")
        else:
            reason = "agent state %s" % attrs.get("state")
        return (
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=\"num\">%d</td><td>%s</td></tr>"
            % (
                esc(node["name"]), esc(node["kind"]), esc(attrs.get("agent_id")), esc(attrs.get("state") or ""),
                esc(" ".join((attrs.get("ips") or [])[:3])),
                esc(", ".join(self.group_name(a) for a in groups)), tests, esc(reason),
            )
        )

    def agent_table(self, ids):
        head = ("<tr><th>Name</th><th>Kind</th><th>ID</th><th>State</th><th>IPs</th><th>Groups</th>"
                "<th>Tests</th><th>Why offline</th></tr>")
        rows = "".join(self.agent_row(self.g.nodes[i]) for i in self.sort_ids(ids))
        return '<div class="scroll"><table><thead>%s</thead><tbody>%s</tbody></table></div>' % (head, rows)

    def section_offline(self):
        g = self.g
        every = [n["id"] for k in ("agent", "cluster") for n in g.nodes_of(k) if is_offline(g, n)]
        out = ['<h2 id="offline">4. Offline agents</h2>']
        if self.sel is not None:
            ids = list(self.sel["excluded_offline"])
            out.append("<p>Selected but offline, not migrated: <b>%d</b>.</p>" % len(ids))
            if ids:
                out.append(self.agent_table(ids))
            out.append(
                "<details><summary>All offline agents and clusters in the discovery (%d)</summary>%s</details>"
                % (len(every), self.agent_table(every) if every else "")
            )
        else:
            out.append("<p>Offline agents and clusters in the discovery: <b>%d</b>.</p>" % len(every))
            if every:
                out.append(self.agent_table(every))
        return "".join(out)

    def section_shares(self):
        g = self.g
        out = ['<h2 id="shares">5. Shares: selected agents by account group</h2>']
        if self.sel is None or not self.selected:
            out.append("<p>No selected agents; nothing to show.</p>")
            return "".join(out)
        ids = self.sort_ids(self.selected)
        truncated = len(ids) > MAX_SHARE_ROWS
        ids = ids[:MAX_SHARE_ROWS]
        aids = []
        for node_id in ids:
            attrs = g.nodes[node_id]["attrs"]
            for aid in (attrs.get("shared_with") or attrs.get("groups") or []):
                if aid not in aids:
                    aids.append(aid)
        aids.sort(key=lambda a: (self.group_name(a).lower(), a))
        head = "".join('<th>%s<br><span class="id">%s</span></th>' % (esc(self.group_name(a)), esc(a)) for a in aids)
        rows = []
        for node_id in ids:
            node = g.nodes[node_id]
            attrs = node["attrs"]
            listed = attrs.get("groups") or []
            shared = attrs.get("shared_with") or listed
            cells = []
            for aid in aids:
                if aid in listed:
                    cells.append('<td class="c">yes</td>')
                elif aid in shared:
                    cells.append('<td class="c">yes*</td>')
                else:
                    cells.append('<td class="c muted">-</td>')
            rows.append("<tr><td>%s <span class=\"id\">%s</span></td><td class=\"num\">%d</td>%s</tr>" % (
                esc(node["name"]), esc(attrs.get("agent_id")), len(shared), "".join(cells)))
        out.append(
            '<p class="small muted">yes = listed in that group; yes* = shared according to agent details only; '
            "- = not shared.%s</p>" % (" Showing the first %d agents." % MAX_SHARE_ROWS if truncated else "")
        )
        out.append(
            '<div class="scroll"><table><thead><tr><th>Agent / cluster</th><th>Groups</th>%s</tr></thead>'
            "<tbody>%s</tbody></table></div>" % (head, "".join(rows))
        )
        return "".join(out)

    def build(self):
        title = "ThousandEyes agent migration: relationship report"
        generated = self.meta.get("generated")
        sub = []
        if generated:
            sub.append("Generated %s" % generated)
        if self.meta.get("input"):
            sub.append("input: %s" % self.meta["input"])
        parts = [self.section_summary()]
        parts.append(self.section_tree_wrapper())
        parts.append(self.section_findings())
        parts.append(self.section_offline())
        parts.append(self.section_shares())
        nav = (
            '<p class="noprint small"><a href="#summary">Summary</a> | <a href="#inventory">Inventory</a> | '
            '<a href="#findings">Findings</a> | <a href="#offline">Offline</a> | <a href="#shares">Shares</a></p>'
        )
        return (
            '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "style-src 'unsafe-inline'; script-src 'unsafe-inline'\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>%s</title><style>%s</style></head><body>"
            "<h1>%s</h1><p class=\"muted small\">%s</p>%s%s<script>%s</script></body></html>\n"
            % (esc(title), CSS, esc(title), esc(" | ".join(sub)), nav, "".join(parts), SCRIPT)
        )


def render(graph, findings, selection, meta):
    """Return the report as one HTML string.

    ``selection`` is the dict from ``resolve_selection`` (or None for an
    inventory-only report); ``findings`` is the list from ``flag_impact``.
    ``meta`` may hold ``generated``, ``input``, ``selection_count``,
    ``missing`` and ``errors``.
    """
    return _Page(graph, findings, selection, meta).build()
