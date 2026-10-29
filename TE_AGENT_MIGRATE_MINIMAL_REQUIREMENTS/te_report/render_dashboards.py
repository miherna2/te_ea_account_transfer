"""Self-contained HTML page: dashboards first. One answer per dashboard and per widget."""
import html

from te_report.dashboard_view import VERDICT_ORDER

_LABEL = {
    "BREAKS": "BREAKS - shows no data after the move",
    "CHANGES": "CHANGES - check by hand",
    "OK": "OK - nothing changes",
    "UNAFFECTED": "UNAFFECTED - no link to selected agents",
    "NOT CHECKED": "NOT CHECKED - no widget detail",
}
_CSS = """
body{font-family:Arial,Helvetica,sans-serif;margin:24px;color:#202124;max-width:1200px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:26px 0 8px}
.small{color:#5f6368;font-size:12px}
.badge{display:inline-block;padding:1px 7px;border-radius:3px;font-size:12px;font-weight:bold;border:2px solid #444;white-space:nowrap}
.BREAKS{background:#fde7e9;border-color:#b3261e;color:#7a1410;border-style:solid}
.CHANGES{background:#fff4d6;border-color:#a56a00;color:#6b4500;border-style:dashed}
.OK{background:#e6f4ea;border-color:#1e7e34;color:#14532d;border-style:dotted}
.UNAFFECTED{background:#f1f3f4;border-color:#9aa0a6;color:#3c4043;border-style:solid}
.NOTCHECKED{background:#e8eaf6;border-color:#5c6bc0;color:#283593;border-style:double}
table{border-collapse:collapse;width:100%;font-size:13px} th,td{border:1px solid #dadce0;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#f8f9fa} details{margin:4px 0;border:1px solid #dadce0;border-radius:4px;padding:4px 8px}
summary{cursor:pointer} .bar button{margin:0 4px 4px 0;padding:3px 9px} input{padding:4px;width:260px}
[hidden]{display:none !important}
"""
_JS = """
(function(){
 var level='ALL';
 function apply(){
  var q=(document.getElementById('q').value||'').toLowerCase();
  var items=document.querySelectorAll('[data-v]');
  for(var i=0;i<items.length;i++){
   var el=items[i];
   var ok=(level==='ALL'||el.dataset.v===level)&&(q===''||el.dataset.t.indexOf(q)>=0);
   el.hidden=!ok;
  }
 }
 var btns=document.querySelectorAll('button[data-l]');
 for(var b=0;b<btns.length;b++){btns[b].addEventListener('click',function(){level=this.dataset.l;apply();});}
 document.getElementById('q').addEventListener('input',apply);
})();
"""


def _e(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _badge(verdict):
    css = verdict.replace(" ", "")
    return '<span class="badge %s">%s</span>' % (css, _e(verdict))


def _text_key(*parts):
    return _e(" ".join(str(p) for p in parts).lower())


def render(dashboards, widgets, summary, meta=None):
    meta = meta or {}
    by_dash = {}
    for w in widgets:
        by_dash.setdefault((w["group"], w["dashboard"]), []).append(w)

    out = []
    out.append("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    out.append("<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">")
    out.append("<title>Dashboard impact</title><style>%s</style></head><body>" % _CSS)
    out.append("<h1>Dashboard impact: will a widget break after the move?</h1>")
    out.append("<div class='small'>Generated %s. Input: %s. Selected agents: %s.</div>" % (
        _e(meta.get("generated", "")), _e(meta.get("input", "")), _e(meta.get("selected", ""))))

    out.append("<h2>Summary</h2><table><tr><th>Verdict</th><th>Dashboards</th><th>Widgets</th></tr>")
    for v in VERDICT_ORDER:
        out.append("<tr><td>%s<div class='small'>%s</div></td><td>%d</td><td>%d</td></tr>" % (
            _badge(v), _e(_LABEL[v]), summary["dashboards"][v], summary["widgets"][v]))
    out.append("</table>")
    out.append("<p class='small'>Rule: a filter on an agent id breaks the widget, because the new agent has a new id. "
               "Group by agent, alert lists and agent tags change content. Test based filters keep working: tests keep their ids. "
               "Built-in dashboards and dashboards without detail are not checked.</p>")

    out.append("<div class='bar'><b>Filter:</b> ")
    out.append("<button data-l='ALL'>All</button>")
    for v in VERDICT_ORDER:
        out.append("<button data-l='%s'>%s</button>" % (_e(v), _e(v)))
    out.append("<input id='q' placeholder='search dashboard, widget, group'></div>")

    out.append("<h2>Dashboards (worst first)</h2>")
    for d in dashboards:
        ws = [w for w in by_dash.get((d["group"], d["dashboard"]), []) if w["verdict"] != "UNAFFECTED"]
        key = _text_key(d["dashboard"], d["group_name"], d["group"], d["why"], *[w["widget"] for w in ws])
        head = "%s <b>%s</b> <span class='small'>group %s (%s) - %d widgets: %d break, %d change, %d ok, %d unaffected</span>" % (
            _badge(d["verdict"]), _e(d["dashboard"]), _e(d["group_name"]), _e(d["group"]),
            d["widgets"], d["breaks"], d["changes"], d["ok"], d["unaffected"])
        if not ws:
            out.append("<div data-v='%s' data-t='%s' style='padding:3px 8px'>%s <span class='small'>%s</span></div>" % (
                _e(d["verdict"]), key, head, _e(d["why"])))
            continue
        out.append("<details data-v='%s' data-t='%s'><summary>%s</summary>" % (_e(d["verdict"]), key, head))
        if d["global_filter"] and d["global_filter"] not in ("UNAFFECTED", "OK"):
            out.append("<p class='small'>Dashboard global filter: %s</p>" % _badge(d["global_filter"]))
        out.append("<table><tr><th>Verdict</th><th>Widget</th><th>Type</th><th>Why</th><th>What to do</th></tr>")
        for w in ws:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                _badge(w["verdict"]), _e(w["widget"]), _e(w["widget_type"]), _e(w["why"]), _e(w["fix"])))
        out.append("</table></details>")
    out.append("<script>%s</script></body></html>" % _JS)
    return "".join(out)
