"""Offline loader for the ThousandEyes discovery output (API v7 JSON).

Only the standard library is used and nothing is sent over the network.
Secret-like fields (``transactionScript`` and any field whose name contains
password/secret/token/credential/authorization) are dropped while loading, so
they never reach the rest of the tool.
"""

import io
import json
import os
import re

SECRET_NAME_PARTS = ("password", "secret", "token", "credential", "authorization")
SECRET_EXACT_NAMES = ("transactionscript",)
NOISE_KEYS = ("_links", "apilink")

GROUP_FILES = (
    "agents",
    "tests",
    "alert_rules",
    "agent_notif_rules",
    "suppression",
    "tags",
    "tags_assignments",
    "dashboards",
    "dashboard_filters",
)

# list key of each paged file ("" = pages are plain lists)
PAGE_KEYS = {
    "agents": ("agents",),
    "tests": ("tests",),
    "alert_rules": ("alertRules",),
    "agent_notif_rules": ("agentAlertRules",),
    "suppression": ("alertSuppressionWindows",),
    "tags": ("tags",),
    "tags_assignments": ("tags",),
    "dashboards": ("dashboards",),
    "dashboard_filters": ("dashboardFilters",),
}


def is_secret_name(name):
    """True when a field name must never be loaded."""
    low = str(name).lower()
    if low in SECRET_EXACT_NAMES:
        return True
    for part in SECRET_NAME_PARTS:
        if part in low:
            return True
    return False


def scrub(value):
    """Return a copy of ``value`` without secret-like or noisy fields."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if is_secret_name(key) or str(key).lower() in NOISE_KEYS:
                continue
            out[key] = scrub(item)
        return out
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def _read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def extract_items(data, keys):
    """Flatten the page shapes seen on disk into one list of dict items.

    Accepted shapes: array of pages (page = object with a list key, or a plain
    list of items), a single page object, or a plain list of items.
    """
    items = []
    if isinstance(data, dict):
        pages = [data]
    elif isinstance(data, list):
        pages = data
    else:
        return items
    for page in pages:
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
        elif isinstance(page, dict):
            found = False
            for key in keys:
                if isinstance(page.get(key), list):
                    items.extend(i for i in page[key] if isinstance(i, dict))
                    found = True
            if not found and not any(k in page for k in ("_links", "pages")):
                # a bare item (e.g. a list file holding objects directly)
                items.append(page)
    return items


def _load_optional(path, rel, result):
    if not os.path.isfile(path):
        result["missing"].append(rel)
        return None
    try:
        return scrub(_read_json(path))
    except (ValueError, OSError):
        result["errors"].append(rel)
        return None


def _agent_detail_name(name):
    match = re.match(r"^(.+)_aid(.+)\.json$", name)
    if not match:
        return None
    return match.group(1), match.group(2)


def load_discovery(path):
    """Load a discovery_output directory into a plain dict.

    Result keys: ``path``, ``missing`` (relative paths of absent optional
    files), ``errors`` (unreadable files), ``account_groups`` (list of
    ``{aid, accountGroupName}``), ``groups`` (aid -> per-group data) and
    ``agent_details`` (agentId -> list of detail dicts, each with ``_aid``).
    """
    result = {
        "path": path,
        "missing": [],
        "errors": [],
        "account_groups": [],
        "groups": {},
        "agent_details": {},
    }

    groups_data = _load_optional(
        os.path.join(path, "account_groups.json"), "account_groups.json", result
    )
    known = []
    if groups_data is not None:
        for item in extract_items(groups_data, ("accountGroups",)):
            if item.get("aid") is None:
                continue
            known.append(
                {
                    "aid": str(item.get("aid")),
                    "accountGroupName": item.get("accountGroupName"),
                }
            )
    result["account_groups"] = known
    names = dict((g["aid"], g["accountGroupName"]) for g in known)

    aids = [g["aid"] for g in known]
    if os.path.isdir(path):
        for entry in sorted(os.listdir(path)):
            if entry.startswith("aid_") and os.path.isdir(os.path.join(path, entry)):
                aid = entry[4:]
                if aid not in aids:
                    aids.append(aid)
                    result["account_groups"].append({"aid": aid, "accountGroupName": None})
    for aid in aids:
        if not os.path.isdir(os.path.join(path, "aid_" + aid)):
            result["missing"].append("aid_%s/" % aid)

    for aid in aids:
        gdir = os.path.join(path, "aid_" + aid)
        group = {
            "aid": aid,
            "name": names.get(aid),
            "agents": [],
            "tests": [],
            "alert_rules": [],
            "agent_notif_rules": [],
            "suppression": [],
            "suppression_details": {},
            "tags": [],
            "dashboards": [],
            "dashboard_details": {},
            "dashboard_filters": [],
        }
        result["groups"][aid] = group
        if not os.path.isdir(gdir):
            continue

        raw = {}
        for fname in GROUP_FILES:
            rel = "aid_%s/%s.json" % (aid, fname)
            data = _load_optional(os.path.join(gdir, fname + ".json"), rel, result)
            raw[fname] = extract_items(data, PAGE_KEYS[fname]) if data is not None else []

        group["agents"] = raw["agents"]
        group["tests"] = raw["tests"]
        group["alert_rules"] = raw["alert_rules"]
        group["agent_notif_rules"] = raw["agent_notif_rules"]
        group["suppression"] = raw["suppression"]
        group["dashboards"] = raw["dashboards"]
        group["dashboard_filters"] = raw["dashboard_filters"]

        # tags_assignments is the same list with an ``assignments`` list per tag
        tags = {}
        order = []
        for item in raw["tags"] + raw["tags_assignments"]:
            tid = str(item.get("id"))
            if tid not in tags:
                tags[tid] = dict(item)
                order.append(tid)
            else:
                tags[tid].update(item)
        group["tags"] = [tags[t] for t in order]

        for entry in sorted(os.listdir(gdir)):
            if not entry.endswith(".json"):
                continue
            full = os.path.join(gdir, entry)
            rel = "aid_%s/%s" % (aid, entry)
            if entry.startswith("dashboard_") and entry not in (
                "dashboard_filters.json",
            ):
                did = entry[len("dashboard_"):-len(".json")]
                data = _load_optional(full, rel, result)
                if isinstance(data, list):
                    data = data[0] if data and isinstance(data[0], dict) else None
                if isinstance(data, dict):
                    group["dashboard_details"][str(data.get("dashboardId", did))] = data
            elif entry.startswith("suppression_"):
                sid = entry[len("suppression_"):-len(".json")]
                data = _load_optional(full, rel, result)
                if isinstance(data, dict):
                    group["suppression_details"][str(
                        data.get("alertSuppressionWindowId", sid))] = data

    detail_dir = os.path.join(path, "agent_details")
    if os.path.isdir(detail_dir):
        for entry in sorted(os.listdir(detail_dir)):
            parsed = _agent_detail_name(entry)
            if not parsed:
                continue
            agent_id, aid = parsed
            data = _load_optional(
                os.path.join(detail_dir, entry), "agent_details/" + entry, result
            )
            if isinstance(data, list):
                data = data[0] if data and isinstance(data[0], dict) else None
            if isinstance(data, dict):
                data["_aid"] = aid
                result["agent_details"].setdefault(agent_id, []).append(data)
    else:
        result["missing"].append("agent_details/")
    return result


def load_selection(path):
    """Read one agent/cluster identifier per line ('#' comments, blanks ignored)."""
    out = []
    with io.open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text.startswith("#"):
                continue
            text = re.sub(r"\s+#.*$", "", text).strip()
            if text:
                out.append(text)
    return out
