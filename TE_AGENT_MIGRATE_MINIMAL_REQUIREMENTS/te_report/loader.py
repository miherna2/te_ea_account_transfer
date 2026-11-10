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

REQUIRED_GROUP_FILES = {"agents": "agentId", "tests": "testId"}


class DiscoveryInputError(ValueError):
    """The discovery directory has no trustworthy account-group inventory."""


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


def _issue(result, kind, rel, required=False):
    if rel not in result[kind]:
        result[kind].append(rel)
    if required and rel not in result["required_" + kind]:
        result["required_" + kind].append(rel)


def _valid_collection(data, keys, id_key):
    """Validate supported page shapes, including an explicitly empty list."""
    if not isinstance(data, (dict, list)):
        return False
    pages = [data] if isinstance(data, dict) else data
    for page in pages:
        if isinstance(page, dict):
            for key in keys:
                if key in page:
                    values = page[key]
                    break
            else:
                if not _valid_identifier(page.get(id_key)):
                    return False
                continue
        elif isinstance(page, list):
            values = page
        else:
            return False
        if not isinstance(values, list) or not all(
                isinstance(item, dict) and _valid_identifier(item.get(id_key))
                for item in values):
            return False
    return True


def _valid_identifier(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool) and bool(str(value).strip())


def _load_optional(path, rel, result, required=False, keys=(), id_key=None):
    try:
        data = scrub(_read_json(path))
    except FileNotFoundError:
        _issue(result, "missing", rel, required)
        return None
    except (ValueError, UnicodeError, OSError):
        _issue(result, "errors", rel, required)
        return None
    if required and not _valid_collection(data, keys, id_key):
        _issue(result, "errors", rel, True)
        return None
    return data


def _agent_detail_name(name):
    match = re.match(r"^(.+)_aid(.+)\.json$", name)
    if not match:
        return None
    return match.group(1), match.group(2)


def load_discovery(path):
    """Load a discovery_output directory into a plain dict.

    Invalid input directories or account-group listings raise DiscoveryInputError.
    Result keys: ``path``, ``missing`` (relative paths of absent files),
    ``errors`` (unreadable or malformed files), ``required_missing`` and
    ``required_errors`` (critical agents/tests gaps), ``complete`` (no critical
    gaps), ``usable`` (at least one valid agents/tests dataset), ``account_groups`` (list of
    ``{aid, accountGroupName}``), ``groups`` (aid -> per-group data) and
    ``agent_details`` (agentId -> list of detail dicts, each with ``_aid``).
    """
    if not os.path.isdir(path):
        raise DiscoveryInputError("discovery input must be an existing directory")
    result = {
        "path": path,
        "missing": [],
        "errors": [],
        "required_missing": [],
        "required_errors": [],
        "complete": False,
        "usable": False,
        "account_groups": [],
        "groups": {},
        "agent_details": {},
    }

    groups_data = _load_optional(
        os.path.join(path, "account_groups.json"), "account_groups.json", result,
        required=True, keys=("accountGroups",), id_key="aid",
    )
    known = []
    if groups_data is not None:
        for item in extract_items(groups_data, ("accountGroups",)):
            aid = str(item["aid"]).strip()
            if not re.match(r"^[A-Za-z0-9_-]+$", aid):
                raise DiscoveryInputError("account_groups.json contains an invalid account-group id")
            if any(group["aid"] == aid for group in known):
                continue
            known.append(
                {
                    "aid": aid,
                    "accountGroupName": item.get("accountGroupName"),
                }
            )
    if not known:
        raise DiscoveryInputError("account_groups.json is missing, unreadable, or has no usable account groups")
    result["account_groups"] = known
    names = dict((g["aid"], g["accountGroupName"]) for g in known)

    aids = [g["aid"] for g in known]
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        raise DiscoveryInputError("discovery input directory could not be read") from None
    for entry in entries:
        if entry.startswith("aid_") and os.path.isdir(os.path.join(path, entry)):
            aid = entry[4:]
            if re.match(r"^[A-Za-z0-9_-]+$", aid) and aid not in aids:
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
            for fname in REQUIRED_GROUP_FILES:
                _issue(result, "missing", "aid_%s/%s.json" % (aid, fname), True)
            continue

        raw = {}
        for fname in GROUP_FILES:
            rel = "aid_%s/%s.json" % (aid, fname)
            required = fname in REQUIRED_GROUP_FILES
            data = _load_optional(os.path.join(gdir, fname + ".json"), rel, result,
                                  required=required, keys=PAGE_KEYS[fname],
                                  id_key=REQUIRED_GROUP_FILES.get(fname))
            if required and data is not None:
                result["usable"] = True
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

        try:
            entries = sorted(os.listdir(gdir))
        except OSError:
            _issue(result, "errors", "aid_%s/" % aid)
            entries = []
        for entry in entries:
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
        try:
            entries = sorted(os.listdir(detail_dir))
        except OSError:
            _issue(result, "errors", "agent_details/")
            entries = []
        for entry in entries:
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
    result["complete"] = not (result["required_missing"] or result["required_errors"])
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
