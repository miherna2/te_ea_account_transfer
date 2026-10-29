"""Seed an EMPTY ThousandEyes SANDBOX so that the Strategy D tool can be tested (MIGRATION_PLAN.md, section 20).

Run from the project root. The default layout is PLAN-DRIVEN (see lab_plan.py and lab_seed.py): the lab copies
the shape of the real scope workbook with anonymous names (SBX-G01.., SBX-D001..).

    python -m dev.sandbox.seed_sandbox --xlsx FILE.xlsx --plan-only --write-plan work/sandbox/lab_plan.json
    python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox            (dry run)
    python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --apply
    python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --verify
    python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --cleanup --apply

The old generic layout (S1..S6, clusters C1/C2, X1) stays available with ``--layout generic``.

Safety rules (all enforced in code):
- The map file must contain ``"sandbox": true`` AND the flag ``--i-know-this-is-sandbox`` must be given.
- Only the account groups listed in the map are used. SOURCE and TARGET must differ; no group id may repeat.
- Default is a dry run: reads only, zero writes (the client is wrapped in ``ReadOnlyApi``).
- Every object created here has a name that starts with ``SBX-``. Cleanup deletes only such names.
- The token is read from the environment (or a hidden prompt). It is never printed or written.

Exit codes: 0 = clean, 1 = hard error (state line printed), 2 = a safety gate refused.
"""

import argparse
import getpass
import json
import os
import secrets
import sys
import time

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.models import StepStatus
from te_agent_migrate.output import MigrationConsole, mask_secrets
from te_agent_migrate.strategy_d.context import ReadOnlyApi

PREFIX = "SBX-"
ALL_STEPS = ("clusters", "shares", "tests", "tags", "alerts", "report")
GROUP_LABELS = ("SOURCE", "TARGET", "VIEW1", "VIEW2", "OTHER")
REQUIRED_GROUPS = ("SOURCE", "TARGET", "VIEW1", "VIEW2")
AGENT_LABELS = ("S1", "S2", "S3", "S4", "S5", "S6", "X1")
REQUIRED_AGENTS = ("S1", "S2", "S3", "S4", "S5")
CLUSTER_LABELS = ("C1", "C2")
REQUIRED_CLUSTERS = ("C1",)
TEST_GROUP_ORDER = ("SOURCE", "VIEW1", "VIEW2")

WRITE_PAUSE_SECONDS = 0.2
ASSIGN_CHUNK = 100
RESULT_FILE = "sandbox_seed_result.json"
MANUAL_TASKS_FILE = "sandbox_manual_tasks.md"
DEFAULT_OUTPUT_DIR = "work/sandbox"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2

# Fake and safe targets only.
FAKE_URL = "https://example.com"
FAKE_HOST = "example.com"
FAKE_DNS_SERVER = "a.iana-servers.net"  # authoritative server of example.com
INTERVAL_DEFAULT = 300
INTERVAL_BULK = 3600  # the 120 bulk tests only prove the assign chunking, so they run less often (saves units)

# ----------------------------------------------------------------------------------------- test sets
SET_S1_HTTP = "S1_HTTP"
SET_S2_HTTP = "S2_HTTP"
SET_S3_NET = "S3_NET"
SET_A2A = "A2A_S1_S4"
SET_C1_HTTP = "C1_HTTP"
SET_C1_BULK = "C1_BULK"
SET_S2_DNS = "S2_DNS"
SET_S2_PAGE = "S2_PAGE"
SET_S1_TXN = "S1_TXN"
SET_S5_ONLY = "S5_ONLY"
SET_C1_DISABLED = "C1_DISABLED"
SET_V1_S3 = "V1_S3"
SET_V1_C1 = "V1_C1"
SET_V2_C1 = "V2_C1"

TXN_DESCRIPTION = "SBX fake credentials"


# ----------------------------------------------------------------------------------------- errors
class SafetyGate(Exception):
    """A safety gate refused the run. Exit code 2."""


class SeedError(Exception):
    """A hard error. The run stops. Exit code 1."""


# ----------------------------------------------------------------------------------------- map file
class SeedMap(object):
    """Validated map file. ``groups``: label -> aid. ``agents``: label -> agent id.
    ``clusters``: label -> list of member agent ids (first = base)."""

    def __init__(self, groups, agents, clusters):
        self.groups = groups
        self.agents = agents
        self.clusters = clusters


def _text_id(value, where):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise SafetyGate("map: %s must be a non-empty string or integer" % where)
    return str(value).strip()


def parse_map(data):
    """Validate the map (dict). Raise ``SafetyGate`` on any problem."""
    if not isinstance(data, dict):
        raise SafetyGate("map: the file must contain a JSON object")
    if data.get("sandbox") is not True:
        raise SafetyGate('map: the key "sandbox": true is missing. Refusing to run.')
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, dict):
        raise SafetyGate('map: "groups" must be an object')
    groups = {}
    for label, value in raw_groups.items():
        if label not in GROUP_LABELS:
            raise SafetyGate("map: unknown group %r. Allowed: %s" % (label, ", ".join(GROUP_LABELS)))
        groups[label] = _text_id(value, "groups.%s" % label)
    for label in REQUIRED_GROUPS:
        if label not in groups:
            raise SafetyGate("map: group %s is missing" % label)
    if groups["SOURCE"] == groups["TARGET"]:
        raise SafetyGate("map: SOURCE and TARGET are the same account group. Refusing to run.")
    seen = {}
    for label in GROUP_LABELS:
        if label in groups:
            if groups[label] in seen:
                raise SafetyGate("map: group id repeats (%s and %s). Refusing to run." % (seen[groups[label]], label))
            seen[groups[label]] = label

    raw_agents = data.get("agents")
    if not isinstance(raw_agents, dict):
        raise SafetyGate('map: "agents" must be an object')
    agents = {}
    for label, value in raw_agents.items():
        if label not in AGENT_LABELS:
            raise SafetyGate("map: unknown agent %r. Allowed: %s" % (label, ", ".join(AGENT_LABELS)))
        agents[label] = _text_id(value, "agents.%s" % label)
    for label in REQUIRED_AGENTS:
        if label not in agents:
            raise SafetyGate("map: agent %s is missing" % label)

    raw_clusters = data.get("clusters")
    if not isinstance(raw_clusters, dict):
        raise SafetyGate('map: "clusters" must be an object')
    clusters = {}
    for label, value in raw_clusters.items():
        if label not in CLUSTER_LABELS:
            raise SafetyGate("map: unknown cluster %r. Allowed: %s" % (label, ", ".join(CLUSTER_LABELS)))
        members = value.get("members") if isinstance(value, dict) else None
        if not isinstance(members, list) or len(members) < 2:
            raise SafetyGate("map: cluster %s needs a list of at least 2 members" % label)
        clusters[label] = [_text_id(m, "clusters.%s.members" % label) for m in members]
    for label in REQUIRED_CLUSTERS:
        if label not in clusters:
            raise SafetyGate("map: cluster %s is missing" % label)

    all_ids = list(agents.values())
    for label in sorted(clusters):
        all_ids.extend(clusters[label])
    if len(set(all_ids)) != len(all_ids):
        raise SafetyGate("map: an agent id repeats. Every agent and cluster member needs its own id.")
    return SeedMap(groups, agents, clusters)


def load_map(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SafetyGate("map: cannot read %s: %s" % (path, exc))
    return parse_map(data)


# ----------------------------------------------------------------------------------------- payload builders
# One builder per test type. The field names are the v7 names as far as we know them. The lab must verify
# each builder once (see README_SANDBOX.md). If the API rejects a field, change ONLY the builder.
def _agents_field(agent_ids):
    return [{"agentId": str(value)} for value in agent_ids]


def build_http_server(name, agent_ids, interval=INTERVAL_DEFAULT, enabled=True, url=FAKE_URL):
    return {
        "testName": name,
        "url": url,
        "interval": interval,
        "agents": _agents_field(agent_ids),
        "enabled": enabled,
    }


def build_agent_to_server(name, agent_ids, protocol, interval=INTERVAL_DEFAULT, enabled=True, server=FAKE_HOST):
    # protocol: "icmp" or "tcp". A TCP test needs a port.
    payload = {
        "testName": name,
        "server": server,
        "protocol": protocol,
        "interval": interval,
        "agents": _agents_field(agent_ids),
        "enabled": enabled,
    }
    if protocol == "tcp":
        payload["port"] = 443
    return payload


def build_agent_to_agent(name, source_agent_ids, target_agent_id, interval=INTERVAL_DEFAULT, enabled=True):
    # "agents" = source agents; "targetAgentId" = target agent. "direction" is left out (default: to target).
    return {
        "testName": name,
        "targetAgentId": str(target_agent_id),
        "protocol": "tcp",
        "port": 49153,
        "interval": interval,
        "agents": _agents_field(source_agent_ids),
        "enabled": enabled,
    }


def build_dns_server(name, agent_ids, interval=INTERVAL_DEFAULT, enabled=True, domain=FAKE_HOST + " A"):
    # "domain" holds the name and the record type. "dnsServers" is a list of server names.
    return {
        "testName": name,
        "domain": domain,
        "dnsServers": [{"serverName": FAKE_DNS_SERVER}],
        "interval": interval,
        "agents": _agents_field(agent_ids),
        "enabled": enabled,
    }


def build_page_load(name, agent_ids, interval=INTERVAL_DEFAULT, enabled=True, url=FAKE_URL):
    return {
        "testName": name,
        "url": url,
        "interval": interval,
        "agents": _agents_field(agent_ids),
        "enabled": enabled,
    }


def build_transaction_script(fake_user, fake_pass):
    """Script text for the web-transaction test. The two values are fake and made at run time.
    The script only checks that they are set. It does not log in anywhere."""
    return "\n".join([
        "import { driver } from 'thousandeyes';",
        "const FAKE_USER = '%s';" % fake_user,
        "const FAKE_PASS = '%s';" % fake_pass,
        "runScript();",
        "async function runScript() {",
        "  await driver.get('%s');" % FAKE_URL,
        "  if (!FAKE_USER || !FAKE_PASS) { throw new Error('fake values missing'); }",
        "}",
    ])


def build_web_transaction(name, agent_ids, script, interval=INTERVAL_DEFAULT, enabled=True):
    return {
        "testName": name,
        "url": FAKE_URL,
        "transactionScript": script,
        "description": TXN_DESCRIPTION,
        "interval": interval,
        "agents": _agents_field(agent_ids),
        "enabled": enabled,
    }


TAG_COLOR = "#00bceb"


def build_tag_payload(key, value, object_type):
    # objectType: "agent" for agent tags, "test" for test tags. Static tag, one fixed colour.
    return {"key": key, "value": value, "objectType": object_type, "type": "static", "color": TAG_COLOR}


# Alert rule fields: see ALERT_RULE_CREATE_FIELDS in te_agent_migrate/strategies.py.
ALERT_EXPRESSION_HTTP = '((error != "None"))'


def build_alert_rule(name, alert_type, minimum_sources=None):
    payload = {
        "ruleName": name,
        "alertType": alert_type,
        "expression": ALERT_EXPRESSION_HTTP,
        "roundsViolatingOutOf": 1,
        "roundsViolatingRequired": 1,
        "notifyOnClear": False,
        "isDefault": False,
        "description": "SBX sandbox rule",
    }
    if minimum_sources is not None:
        payload["minimumSources"] = int(minimum_sources)
    return payload


ALERT_DEFAULT_NAME = PREFIX + "ALERT-HTTP-DEFAULT"
ALERT_MIN_SOURCES_NAME = PREFIX + "ALERT-C1-MIN-SOURCES-2"
ALERT_DEFS = (
    # (rule name, minimum sources, test sets that get the rule)
    (ALERT_DEFAULT_NAME, None, (SET_S1_HTTP, SET_S2_HTTP)),
    (ALERT_MIN_SOURCES_NAME, 2, (SET_C1_HTTP,)),
)

TAG_DEFS = (
    # (key, value, object type, assignment type, agent labels, test sets)
    ("env", "sbx", "agent", "v-agent", ("S1", "S3", "C1"), ()),
    ("site", "a", "agent", "v-agent", ("S1", "S3", "C1"), ()),
    ("app", "web", "test", "test", (), (SET_S1_HTTP, SET_S2_HTTP)),
    ("app", "dns", "test", "test", (), (SET_S2_DNS,)),
)
TAG_PAIRS = tuple((d[0], d[1]) for d in TAG_DEFS)


# ----------------------------------------------------------------------------------------- test plan
class TestSpec(object):
    """One test to create. ``payload`` may hold the fake transaction script: never print or store it."""

    __test__ = False  # not a test class for unittest/pytest

    def __init__(self, group, set_name, name, test_type, payload, target):
        self.group = group
        self.set_name = set_name
        self.name = name
        self.test_type = test_type
        self.payload = payload
        self.target = target


def build_test_plan(cfg, cluster_ids, token_hex=None):
    """List of ``TestSpec`` for the whole lab (SOURCE, VIEW1, VIEW2). Pure function: no API call."""
    token_hex = token_hex or secrets.token_hex
    agents = cfg.agents
    s1, s2, s3, s4, s5 = [agents[label] for label in ("S1", "S2", "S3", "S4", "S5")]
    c1 = cluster_ids["C1"]
    specs = []

    def add(group, set_name, name, test_type, payload, target):
        specs.append(TestSpec(group, set_name, name, test_type, payload, target))

    for index in range(1, 6):
        add("SOURCE", SET_S1_HTTP, "SBX-S1-HTTP-%02d" % index, "http-server",
            build_http_server("SBX-S1-HTTP-%02d" % index, [s1]), "S1")
    for index in range(1, 6):
        add("SOURCE", SET_S2_HTTP, "SBX-S2-HTTP-%02d" % index, "http-server",
            build_http_server("SBX-S2-HTTP-%02d" % index, [s2]), "S2")
    for index in range(1, 11):
        protocol = "icmp" if index % 2 else "tcp"
        add("SOURCE", SET_S3_NET, "SBX-S3-NET-%02d" % index, "agent-to-server",
            build_agent_to_server("SBX-S3-NET-%02d" % index, [s3], protocol), "S3")
    for index in range(1, 3):
        add("SOURCE", SET_A2A, "SBX-A2A-S1-S4-%02d" % index, "agent-to-agent",
            build_agent_to_agent("SBX-A2A-S1-S4-%02d" % index, [s1], s4), "S1>S4")
    for index in range(1, 31):
        add("SOURCE", SET_C1_HTTP, "SBX-C1-HTTP-%02d" % index, "http-server",
            build_http_server("SBX-C1-HTTP-%02d" % index, [c1]), "C1")
    for index in range(1, 121):
        add("SOURCE", SET_C1_BULK, "SBX-C1-BULK-%03d" % index, "http-server",
            build_http_server("SBX-C1-BULK-%03d" % index, [c1], interval=INTERVAL_BULK), "C1")
    for index in range(1, 4):
        add("SOURCE", SET_S2_DNS, "SBX-S2-DNS-%02d" % index, "dns-server",
            build_dns_server("SBX-S2-DNS-%02d" % index, [s2]), "S2")
    add("SOURCE", SET_S2_PAGE, "SBX-S2-PAGE-01", "page-load", build_page_load("SBX-S2-PAGE-01", [s2]), "S2")
    # Fake user and password: made now, only inside the script text. Never printed, never stored.
    script = build_transaction_script(token_hex(8), token_hex(8))
    add("SOURCE", SET_S1_TXN, "SBX-S1-TXN-01", "web-transactions",
        build_web_transaction("SBX-S1-TXN-01", [s1], script), "S1")
    for index in range(1, 3):
        add("SOURCE", SET_S5_ONLY, "SBX-S5-HTTP-%02d" % index, "http-server",
            build_http_server("SBX-S5-HTTP-%02d" % index, [s5]), "S5")
    for index in range(1, 4):
        add("SOURCE", SET_C1_DISABLED, "SBX-C1-DISABLED-%02d" % index, "http-server",
            build_http_server("SBX-C1-DISABLED-%02d" % index, [c1], enabled=False), "C1")
    for index in range(1, 6):
        add("VIEW1", SET_V1_S3, "SBX-V1-S3-%02d" % index, "http-server",
            build_http_server("SBX-V1-S3-%02d" % index, [s3]), "S3")
    for index in range(1, 6):
        add("VIEW1", SET_V1_C1, "SBX-V1-C1-%02d" % index, "http-server",
            build_http_server("SBX-V1-C1-%02d" % index, [c1]), "C1")
    for index in range(1, 4):
        add("VIEW2", SET_V2_C1, "SBX-V2-C1-%02d" % index, "http-server",
            build_http_server("SBX-V2-C1-%02d" % index, [c1]), "C1")
    return specs


# ----------------------------------------------------------------------------------------- manual tasks
MANUAL_TASKS = (
    "Agent notification rule on S1: create it by hand in SBX-SOURCE (name SBX-AGENT-NOTIFY-S1, agent S1). "
    "The API for it is read-only or not sure, so this script does not try it.",
    "Suppression window on a tag: create it by hand in SBX-SOURCE (name SBX-SUPPRESS-APP-WEB, tag app=web; "
    "if the UI accepts only agent tags, use env=sbx). The API for it is not sure.",
)

DASHBOARDS = (
    ("D1", "SBX-D1-TEST-FILTER", "Time Series, Number, Test Table",
     "Test filter only: tests SBX-S1-HTTP-01..05 and SBX-S2-HTTP-01..05. No agent filter.", "OK"),
    ("D2", "SBX-D2-AGENT-ID", "Time Series, Color Grid",
     "Widget filter on AGENT ID: agent S1 (pick S1 by name in the agent filter).", "BREAKS (filter_agent_id)"),
    ("D3", "SBX-D3-AGENT-TAG", "Color Grid, Number",
     "Widget filter on AGENT TAG env=sbx.", "REVIEW"),
    ("D4", "SBX-D4-GROUP-BY-AGENT", "Time Series, Test Table",
     "Test filter: tests SBX-S3-NET-01..10. Widget GROUP BY: Agent.", "REVIEW"),
    ("D5", "SBX-D5-AGENT-STATUS", "Agent Status",
     'Agent Status widget with "All Assigned Agents". No other filter.', "OK"),
    ("D6", "SBX-D6-ALERT-LIST", "Alert List",
     "Alert List widget. Alert rule filter: %s. Test filter: SBX-S1-HTTP and SBX-S2-HTTP tests." % ALERT_DEFAULT_NAME,
     "REVIEW"),
    ("D7", "SBX-D7-GLOBAL-TAG", "Time Series, Map, Number",
     "GLOBAL filter on TEST TAG app=web (tests of S1 and S2).", "OK"),
    ("D8", "SBX-D8-CLUSTER", "Color Grid",
     "Test filter: tests SBX-C1-HTTP-01..30 (tests of cluster C1).", "REVIEW (cluster)"),
)


def build_manual_tasks_md(map_groups=None):
    lines = [
        "# Sandbox manual tasks",
        "",
        "Do these tasks by hand in the ThousandEyes UI, in the SBX-SOURCE account group.",
        "Use only the sandbox. Name every object SBX-...",
        "",
        "## Manual tasks",
        "",
    ]
    for index, task in enumerate(MANUAL_TASKS, 1):
        lines.append("%d. %s" % (index, task))
    lines.extend([
        "",
        "## Dashboard checklist D1 to D8 (MIGRATION_PLAN.md, section 20.6)",
        "",
        "Build each dashboard in SBX-SOURCE. The last column is the flag that the report must show.",
        "",
        "| ID | Dashboard name | Widgets | Exact filter to build | Expected flag |",
        "|---|---|---|---|---|",
    ])
    for row in DASHBOARDS:
        lines.append("| %s | %s | %s | %s | %s |" % row)
    lines.extend([
        "",
        "Tick each dashboard when it is built:",
        "",
    ])
    for row in DASHBOARDS:
        lines.append("- [ ] %s %s" % (row[0], row[1]))
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------- output helpers
class SeedConsole(MigrationConsole):
    """MigrationConsole with a compact recap: counters are kept per ``scope``, not per object name."""

    scope = "localhost"

    def _bump(self, host, key):
        MigrationConsole._bump(self, self.scope, key)


def ensure_output_dir(path):
    path = str(path)
    if not os.path.isdir(path):
        os.makedirs(path, 0o700)
    os.chmod(path, 0o700)
    return path


def write_text_file(directory, name, text):
    directory = ensure_output_dir(directory)
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path


def _id_of(item, *keys):
    if isinstance(item, dict):
        for key in keys:
            if item.get(key) is not None and str(item.get(key)):
                return str(item.get(key))
        return ""
    return str(item) if item is not None else ""


# ----------------------------------------------------------------------------------------- the seeder
class Seeder(object):
    def __init__(self, api, cfg, console, apply=False, sleep=time.sleep, token_hex=None):
        self.apply = bool(apply)
        self.api = api if (self.apply or isinstance(api, ReadOnlyApi)) else ReadOnlyApi(api)
        self.cfg = cfg
        self.console = console
        self.sleep = sleep
        self.token_hex = token_hex
        self.cluster_ids = {}
        self._plan = None
        self._tests = {}
        self._label_of_aid = dict((aid, label) for label, aid in cfg.groups.items())
        # Hooks for the plan layout (dev/sandbox/lab_seed.py). The generic layout keeps these values.
        self.source_label = "SOURCE"
        self.tag_defs = TAG_DEFS
        self.tag_pairs = TAG_PAIRS
        self.bucket = "created" if self.apply else "planned"
        self.result = {
            "schema": 1,
            "mode": "apply" if self.apply else "dry-run",
            "steps": [],
            "created": self._empty_bucket(),
            "planned": self._empty_bucket(),
            "existing": {"clusters": 0, "shares": 0, "tests": 0, "tags": 0, "tag_assignments": 0,
                         "alert_rules": 0, "alert_attachments": 0},
            "deleted": self._empty_bucket(),
            "planned_delete": self._empty_bucket(),
            "error": None,
            "exit_code": None,
        }

    @staticmethod
    def _empty_bucket():
        return {"clusters": [], "shares": [], "tests": [], "tags": [], "tag_assignments": [],
                "alert_rules": [], "alert_attachments": []}

    # ------------------------------------------------------------------ small helpers
    def _record(self, kind, item):
        self.result[self.bucket][kind].append(item)

    def _count_existing(self, kind):
        self.result["existing"][kind] += 1

    def _pace(self):
        if self.apply:
            self.sleep(WRITE_PAUSE_SECONDS)

    def _line(self, status, host, task, detail):
        self.console.status(status, host, task, detail)

    def _call(self, host, what, func, *args, **kwargs):
        """Call the API. Any failure becomes a ``SeedError`` after a failed line."""
        try:
            return func(*args, **kwargs)
        except RuntimeError as exc:
            text = "%s failed: %s" % (what, mask_secrets(str(exc)))
            self.console.status(StepStatus.FAILED, host, None, text)
            raise SeedError("%s: %s" % (host, text))

    def group_labels(self):
        """Labels of the groups of the map that this layout uses (generic: the fixed labels)."""
        return [label for label in GROUP_LABELS if label in self.cfg.groups]

    def test_groups(self):
        """Labels of the groups that get tests, in order."""
        return list(TEST_GROUP_ORDER)

    def _verb(self, past, future):
        return past if self.apply else "would " + future

    def state_text(self):
        data = self.result[self.bucket]
        return "%s so far: %s" % (
            self.bucket,
            ", ".join("%s=%d" % (kind, len(data[kind])) for kind in sorted(data)),
        )

    def plan(self):
        if self._plan is None:
            ids = dict((label, self.cluster_id(label)) for label in ("C1",))
            self._plan = build_test_plan(self.cfg, ids, token_hex=self.token_hex)
        return self._plan

    def tests_in(self, group):
        """name -> TestRecord for one group (read once, then kept up to date)."""
        if group not in self._tests:
            records = self._call(group, "list tests", self.api.list_tests, self.cfg.groups[group])
            self._tests[group] = dict((record.name, record) for record in records)
        return self._tests[group]

    def _find_cluster(self, agents, members):
        wanted = set(members)
        for agent in agents:
            if not agent.is_cluster:
                continue
            ids = set(_id_of(m, "memberId", "agentId", "id") for m in agent.cluster_members)
            if wanted <= ids:
                return agent
        return None

    def cluster_id(self, label):
        """Agent id of a cluster. Read from the API when the cluster exists, else the base member id
        (the plan says: cluster id = first member)."""
        if label in self.cluster_ids:
            return self.cluster_ids[label]
        members = self.cfg.clusters[label]
        agents = self._call(label, "list agents", self.api.list_agents_with_clusters,
                            self.cfg.groups[self.source_label])
        found = self._find_cluster(agents, members)
        self.cluster_ids[label] = found.agent_id if found is not None else members[0]
        return self.cluster_ids[label]

    # ------------------------------------------------------------------ preflight
    def preflight(self):
        self.console.scope = "preflight"
        groups = self._call("global", "list account groups", self.api.list_account_groups)
        names = dict((str(g.aid), g.name) for g in groups)
        for label in self.group_labels():
            aid = self.cfg.groups.get(label)
            if aid is None:
                continue
            if aid not in names:
                raise SafetyGate("group %s (%s) is not visible with this token. Refusing to run." % (label, aid))
            if not str(names[aid]).upper().startswith(PREFIX):
                self._line(StepStatus.SKIPPED, label, "preflight",
                           "WARNING: group name %r does not start with SBX-. Check that this is the sandbox." % names[aid])
            else:
                self._line(StepStatus.OK, label, "preflight", "group %s = %s" % (aid, names[aid]))

    # ------------------------------------------------------------------ step: clusters
    def step_clusters(self):
        self.console.scope = "clusters"
        task = "clusters"
        source = self.cfg.groups[self.source_label]
        agents = self._call("global", "list agents", self.api.list_agents_with_clusters, source)
        known = set(a.agent_id for a in agents)
        for label in sorted(self.cfg.clusters):
            members = self.cfg.clusters[label]
            base, others = members[0], members[1:]
            existing = self._find_cluster(agents, members)
            if existing is not None:
                self.cluster_ids[label] = existing.agent_id
                self._count_existing("clusters")
                self._line(StepStatus.OK, label, task, "already exists (cluster id %s)" % existing.agent_id)
                continue
            missing = [m for m in members if m not in known]
            if missing:
                text = "member agents not found in SOURCE: %s" % ", ".join(missing)
                self._line(StepStatus.FAILED, label, task, text)
                raise SeedError("%s: %s" % (label, text))
            if not self.apply:
                self.cluster_ids[label] = base
                self._record("clusters", {"cluster": label, "baseAgentId": base, "members": others})
                self._line(StepStatus.CHANGED, label, task,
                           "would build cluster (base %s, members %s)" % (base, ", ".join(others)))
                continue
            self._call(label, "cluster assign", self.api.cluster_assign, source, base, others)
            self._pace()
            self.cluster_ids.pop(label, None)
            found = self._find_cluster(
                self._call(label, "list agents", self.api.list_agents_with_clusters, source), members)
            self.cluster_ids[label] = found.agent_id if found is not None else base
            self._record("clusters", {"cluster": label, "clusterId": self.cluster_ids[label],
                                      "baseAgentId": base, "members": others})
            self._line(StepStatus.CHANGED, label, task,
                       "built cluster %s (base %s, members %s)" % (self.cluster_ids[label], base, ", ".join(others)))

    # ------------------------------------------------------------------ step: shares
    def _share_one(self, agent_label, agent_id, owner_label, wanted_labels):
        task = "shares"
        groups = self.cfg.groups
        owner_aid = groups[owner_label]
        wanted = [groups[label] for label in wanted_labels]
        existing = []
        try:
            existing = list(self.api.get_agent(owner_aid, agent_id).shared_aids)
        except RuntimeError as exc:
            if self.apply:
                self._line(StepStatus.FAILED, agent_label, task, "read agent failed: %s" % mask_secrets(str(exc)))
                raise SeedError("%s: read agent failed" % agent_label)
        if set(wanted) <= set(existing):
            self._count_existing("shares")
            self._line(StepStatus.OK, agent_label, task, "already shared to %s" % ", ".join(wanted_labels))
            return
        # Always the FULL list: wanted groups first (owner first), then every share that exists now.
        full = list(wanted) + [aid for aid in existing if aid not in wanted]
        names = ", ".join(self._label_of_aid.get(aid, aid) for aid in full)
        if not self.apply:
            self._record("shares", {"agent": agent_label, "agentId": agent_id, "ownerGroup": owner_label,
                                    "accountGroups": full})
            self._line(StepStatus.CHANGED, agent_label, task, "would share agent %s to %s" % (agent_id, names))
            return
        self._call(agent_label, "set account groups", self.api.set_agent_account_groups, owner_aid, agent_id, full)
        self._pace()
        self._record("shares", {"agent": agent_label, "agentId": agent_id, "ownerGroup": owner_label,
                                "accountGroups": full})
        self._line(StepStatus.CHANGED, agent_label, task, "shared agent %s to %s" % (agent_id, names))

    def step_shares(self):
        self.console.scope = "shares"
        self._share_one("S3", self.cfg.agents["S3"], "SOURCE", ("SOURCE", "VIEW1", "VIEW2"))
        for label in sorted(self.cfg.clusters):
            self._share_one(label, self.cluster_id(label), "SOURCE", ("SOURCE", "VIEW1", "VIEW2"))
        if "X1" in self.cfg.agents and "OTHER" in self.cfg.groups:
            # X1 is owned by OTHER and shared into SOURCE: the call uses aid=OTHER.
            self._share_one("X1", self.cfg.agents["X1"], "OTHER", ("OTHER", "SOURCE"))
        else:
            self._line(StepStatus.SKIPPED, "X1", "shares", "no X1 or no OTHER group in the map")

    # ------------------------------------------------------------------ step: tests
    def step_tests(self):
        for group in self.test_groups():
            self.console.scope = "tests/%s" % group
            task = "tests %s" % group
            aid = self.cfg.groups[group]
            existing = self.tests_in(group)
            for spec in self.plan():
                if spec.group != group:
                    continue
                if spec.name in existing:
                    self._count_existing("tests")
                    self._line(StepStatus.OK, spec.name, task, "already exists")
                    continue
                if not self.apply:
                    self._record("tests", {"group": group, "name": spec.name, "type": spec.test_type})
                    self._line(StepStatus.CHANGED, spec.name, task,
                               "would create %s on %s" % (spec.test_type, spec.target))
                    continue
                record = self._call(spec.name, "create test", self.api.create_test, aid, spec.test_type, spec.payload)
                self._pace()
                existing[spec.name] = record
                self._record("tests", {"group": group, "name": spec.name, "type": spec.test_type,
                                       "id": record.test_id})
                self._line(StepStatus.CHANGED, spec.name, task,
                           "created %s id=%s on %s" % (spec.test_type, record.test_id, spec.target))

    # ------------------------------------------------------------------ step: tags
    def _agent_id_for(self, label):
        if label in self.cfg.clusters:
            return self.cluster_id(label)
        return self.cfg.agents[label]

    def step_tags(self):
        self.console.scope = "tags"
        task = "tags"
        source = self.cfg.groups[self.source_label]
        tags = self._call("global", "list tags", self.api.list_tags, source, True)
        source_tests = self.tests_in(self.source_label) if any(d[5] for d in self.tag_defs) else {}
        for key, value, object_type, assign_type, agent_labels, sets in self.tag_defs:
            host = "%s=%s" % (key, value)
            found = None
            for tag in tags:
                if (str(tag.get("key")) == key and str(tag.get("value")) == value
                        and str(tag.get("objectType", object_type)) == object_type):
                    found = tag
                    break
            if found is not None:
                tag_id = _id_of(found, "tagId", "id")
                have = set(_id_of(a, "id", "objectId") for a in (found.get("assignments") or []))
                self._count_existing("tags")
                self._line(StepStatus.OK, host, task, "tag already exists (id %s)" % tag_id)
            else:
                tag_id = None
                have = set()
                if self.apply:
                    created = self._call(host, "create tag", self.api.create_tag, source,
                                         build_tag_payload(key, value, object_type))
                    self._pace()
                    tag_id = _id_of(created, "tagId", "id")
                    if not tag_id:
                        self._line(StepStatus.FAILED, host, task, "create tag returned no id")
                        raise SeedError("%s: create tag returned no id" % host)
                self._record("tags", {"key": key, "value": value, "objectType": object_type, "id": tag_id})
                self._line(StepStatus.CHANGED, host, task, "%s tag (%s)" % (self._verb("created", "create"), object_type))
            # Objects that must carry the tag.
            wanted = []  # (id or None, label)
            for label in agent_labels:
                wanted.append((self._agent_id_for(label), label))
            for spec in self.plan():
                if spec.group == self.source_label and spec.set_name in sets:
                    record = source_tests.get(spec.name)
                    if record is None and self.apply:
                        self._line(StepStatus.FAILED, host, task, "test %s not found (run the tests step first)" % spec.name)
                        raise SeedError("%s: test %s not found" % (host, spec.name))
                    wanted.append((record.test_id if record is not None else None, spec.name))
            missing = [(oid, label) for oid, label in wanted if oid is None or oid not in have]
            if not missing:
                self._count_existing("tag_assignments")
                self._line(StepStatus.OK, host, task, "all %d assignments already exist" % len(wanted))
                continue
            if not self.apply:
                self._record("tag_assignments", {"tag": host, "type": assign_type, "count": len(missing)})
                self._line(StepStatus.CHANGED, host, task,
                           "would assign to %d objects (type %s)" % (len(missing), assign_type))
                continue
            ids = [oid for oid, _label in missing]
            for start in range(0, len(ids), ASSIGN_CHUNK):
                chunk = ids[start:start + ASSIGN_CHUNK]
                self._call(host, "assign tag", self.api.assign_tag, source, tag_id,
                           [(oid, assign_type) for oid in chunk])
                self._pace()
            self._record("tag_assignments", {"tag": host, "type": assign_type, "count": len(ids)})
            self._line(StepStatus.CHANGED, host, task, "assigned to %d objects (type %s)" % (len(ids), assign_type))

    # ------------------------------------------------------------------ step: alerts
    def step_alerts(self):
        self.console.scope = "alerts"
        task = "alerts"
        source = self.cfg.groups["SOURCE"]
        rules = self._call("global", "list alert rules", self.api.list_alert_rules, source)
        source_tests = self.tests_in("SOURCE")
        for name, minimum_sources, sets in ALERT_DEFS:
            rule = next((r for r in rules if str(r.get("ruleName")) == name), None)
            if rule is not None:
                rule_id = _id_of(rule, "ruleId", "alertRuleId", "id")
                self._count_existing("alert_rules")
                self._line(StepStatus.OK, name, task, "alert rule already exists (id %s)" % rule_id)
            else:
                rule_id = None
                if self.apply:
                    created = self._call(name, "create alert rule", self.api.create_alert_rule, source,
                                         build_alert_rule(name, "http-server", minimum_sources))
                    self._pace()
                    rule_id = _id_of(created, "ruleId", "alertRuleId", "id")
                    if not rule_id:
                        self._line(StepStatus.FAILED, name, task, "create alert rule returned no id")
                        raise SeedError("%s: create alert rule returned no id" % name)
                self._record("alert_rules", {"name": name, "id": rule_id, "minimumSources": minimum_sources})
                self._line(StepStatus.CHANGED, name, task, "%s alert rule%s" % (
                    self._verb("created", "create"),
                    "" if minimum_sources is None else " (minimumSources %d)" % minimum_sources))
            # Attach the rule to the tests. The payload is the one we built, plus the rule id.
            attached = 0
            for spec in self.plan():
                if spec.group != "SOURCE" or spec.set_name not in sets:
                    continue
                record = source_tests.get(spec.name)
                if record is None:
                    if self.apply:
                        self._line(StepStatus.FAILED, spec.name, task, "test not found (run the tests step first)")
                        raise SeedError("%s: test not found" % spec.name)
                    attached += 1
                    continue
                if rule_id is not None:
                    current = self._call(spec.name, "get test", self.api.get_test, source, spec.test_type, record.test_id)
                    ids = [_id_of(a, "ruleId", "id") for a in current.alert_rules]
                    if rule_id in ids:
                        self._count_existing("alert_attachments")
                        continue
                else:
                    ids = []
                attached += 1
                if not self.apply:
                    continue
                payload = dict(spec.payload)
                payload["alertRules"] = [value for value in ids if value] + [rule_id]
                payload["alertsEnabled"] = True
                self._call(spec.name, "attach alert rule", self.api.update_test, source, spec.test_type,
                           record.test_id, payload)
                self._pace()
            self._record("alert_attachments", {"rule": name, "count": attached})
            if attached:
                self._line(StepStatus.CHANGED, name, task, "%s to %d tests" % (self._verb("attached", "attach"), attached))
            else:
                self._line(StepStatus.OK, name, task, "attached to all tests already")
        for text in MANUAL_TASKS:
            self.console.info("MANUAL TASK", text)

    # ------------------------------------------------------------------ step: report
    def step_report(self, output_dir):
        self.console.scope = "report"
        path = write_text_file(output_dir, MANUAL_TASKS_FILE, build_manual_tasks_md())
        self._line(StepStatus.OK, "report", "report", "wrote %s" % path)
        for text in MANUAL_TASKS:
            self.console.info("MANUAL TASK", text)

    # ------------------------------------------------------------------ cleanup
    def cleanup(self):
        """List (dry run) or delete (apply) the SBX- tests, SBX- alert rules and our tags. Never agents,
        groups or dashboards."""
        self.bucket = "deleted" if self.apply else "planned_delete"
        task = "cleanup"
        found_tests, found_rules, found_tags = [], [], []
        for group in self.group_labels():
            aid = self.cfg.groups[group]
            self.console.scope = "cleanup/%s" % group
            for record in self._call(group, "list tests", self.api.list_tests, aid):
                if not record.name.startswith(PREFIX):
                    continue
                if not record.test_type or record.test_type == "unknown":
                    self._line(StepStatus.SKIPPED, record.name, task, "test type unknown; not deleted")
                    continue
                found_tests.append((group, aid, record))
            for rule in self._call(group, "list alert rules", self.api.list_alert_rules, aid):
                if str(rule.get("ruleName", "")).startswith(PREFIX):
                    found_rules.append((group, aid, rule))
        source = self.cfg.groups[self.source_label]
        for tag in self._call(self.source_label, "list tags", self.api.list_tags, source):
            if (str(tag.get("key")), str(tag.get("value"))) in self.tag_pairs:
                found_tags.append((self.source_label, source, tag))
        self.console.scope = "cleanup"
        self.console.info("cleanup", "found %d tests, %d alert rules, %d tags (SBX- names only)" % (
            len(found_tests), len(found_rules), len(found_tags)))
        for group, _aid, record in found_tests:
            self._line(StepStatus.OK, record.name, "cleanup list", "%s test in %s (id %s)" % (record.test_type, group, record.test_id))
        for group, _aid, rule in found_rules:
            self._line(StepStatus.OK, str(rule.get("ruleName")), "cleanup list", "alert rule in %s" % group)
        for _group, _aid, tag in found_tags:
            self._line(StepStatus.OK, "%s=%s" % (tag.get("key"), tag.get("value")), "cleanup list", "tag in SOURCE")
        if not self.apply:
            self._record_cleanup(found_tests, found_rules, found_tags)
            self._line(StepStatus.CHANGED, "cleanup", task, "dry run: would delete the objects listed above")
            return
        for group, aid, record in found_tests:  # tests first, then the rules that they used, then tags
            self.console.scope = "cleanup/%s" % group
            if not record.name.startswith(PREFIX):
                continue
            self._call(record.name, "delete test", self.api.delete_test, aid, record.test_type, record.test_id)
            self._pace()
            self._line(StepStatus.CHANGED, record.name, task, "deleted %s test id=%s" % (record.test_type, record.test_id))
            self._record("tests", {"group": group, "name": record.name, "id": record.test_id})
        for group, aid, rule in found_rules:
            name = str(rule.get("ruleName"))
            if not name.startswith(PREFIX):
                continue
            rule_id = _id_of(rule, "ruleId", "alertRuleId", "id")
            self._call(name, "delete alert rule", self.api.request, "DELETE", "alerts/rules/%s" % rule_id,
                       aid=aid, expected=set((200, 204)), safe_to_retry=False)
            self._pace()
            self._line(StepStatus.CHANGED, name, task, "deleted alert rule id=%s" % rule_id)
            self._record("alert_rules", {"group": group, "name": name, "id": rule_id})
        for _group, aid, tag in found_tags:
            host = "%s=%s" % (tag.get("key"), tag.get("value"))
            tag_id = _id_of(tag, "tagId", "id")
            self._call(host, "delete tag", self.api.request, "DELETE", "tags/%s" % tag_id,
                       aid=aid, expected=set((200, 204)), safe_to_retry=False)
            self._pace()
            self._line(StepStatus.CHANGED, host, task, "deleted tag id=%s" % tag_id)
            self._record("tags", {"key": str(tag.get("key")), "value": str(tag.get("value")), "id": tag_id})

    def _record_cleanup(self, tests, rules, tags):
        for group, _aid, record in tests:
            self._record("tests", {"group": group, "name": record.name, "id": record.test_id})
        for group, _aid, rule in rules:
            self._record("alert_rules", {"group": group, "name": str(rule.get("ruleName"))})
        for _group, _aid, tag in tags:
            self._record("tags", {"key": str(tag.get("key")), "value": str(tag.get("value"))})


# ----------------------------------------------------------------------------------------- CLI
def parse_steps(text):
    if text is None or not str(text).strip():
        return list(ALL_STEPS)
    wanted = [item.strip() for item in str(text).split(",") if item.strip()]
    unknown = [item for item in wanted if item not in ALL_STEPS]
    if unknown:
        raise SafetyGate("unknown step(s): %s. Allowed: %s" % (", ".join(unknown), ", ".join(ALL_STEPS)))
    return [step for step in ALL_STEPS if step in wanted]  # always the fixed order


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m dev.sandbox.seed_sandbox",
        description="Seed an EMPTY ThousandEyes SANDBOX with SBX- resources. Dry run by default.",
    )
    parser.add_argument("--map", default=None,
                        help="path of the JSON map file (see examples/sandbox_map.example.json)")
    parser.add_argument("--layout", choices=("plan", "generic"), default="plan",
                        help="plan (default): lab from the scope workbook; generic: the old S1..S6 layout")
    parser.add_argument("--steps", default=None, help="comma list of: %s (default: all)" % ",".join(ALL_STEPS))
    parser.add_argument("--apply", action="store_true", help="send the writes (default: dry run)")
    parser.add_argument("--cleanup", action="store_true",
                        help="delete (with --apply) or list (dry run) the SBX- objects created by this script")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, help="output directory (default: %(default)s)")
    parser.add_argument("--i-know-this-is-sandbox", dest="sandbox_ack", action="store_true",
                        help="required: confirms that the token and the map belong to a SANDBOX")
    parser.add_argument("--verbose", action="store_true")
    plan_group = parser.add_argument_group("plan layout (default)")
    plan_group.add_argument("--xlsx", default=None, help="scope workbook (default: xlsx in the map)")
    plan_group.add_argument("--prefix-map", dest="prefix_map", default=None,
                            help="prefix_map.csv (default: prefix_map in the map, else derived from the workbook)")
    plan_group.add_argument("--plan", default=None, help="read a lab plan JSON file instead of the workbook")
    plan_group.add_argument("--plan-only", dest="plan_only", action="store_true",
                            help="print the lab plan summary and stop: no API use, no token, no map needed")
    plan_group.add_argument("--write-plan", dest="write_plan", default=None, metavar="FILE",
                            help="write the anonymized lab plan JSON (and the private alias map in --output)")
    plan_group.add_argument("--vms", type=int, default=None,
                        help="number of lab VMs (default: 12, or the number of vms in the map if smaller)")
    plan_group.add_argument("--waves", type=int, default=None,
                            help="split all READY devices into this many waves; wave 1 = archetype set")
    plan_group.add_argument("--wave", type=int, default=None, help="wave to seed (default 1; needs --waves)")
    plan_group.add_argument("--max-tests-per-group", dest="max_tests_per_group", type=int, default=25,
                            help="scale the tests of each group to at most this number (default: 25)")
    plan_group.add_argument("--max-calls", dest="max_calls", type=int, default=2000,
                            help="stop when the run would make more API calls than this (default: 2000; 0 = off)")
    plan_group.add_argument("--verify", action="store_true",
                            help="read only: per lab device, check shares and assigned tests against the plan")
    plan_group.add_argument("--with-clusters", dest="with_clusters", action="store_true",
                            help="also run the clusters step (OFF by default: the real scope has no clusters)")
    return parser


def _read_token(environ, console):
    token = environ.get("TE_OAUTH_TOKEN") or environ.get("THOUSANDEYES_OAUTH_TOKEN")
    if token:
        console.info("input", "API OAuth token loaded from environment")
        return token
    return getpass.getpass("Sandbox API v7 OAuth bearer token (hidden): ")


def _write_result(output_dir, result):
    return write_text_file(output_dir, RESULT_FILE, json.dumps(result, indent=2, sort_keys=True) + "\n")


def main(argv=None, api_factory=None, sleep=time.sleep, stream=None, environ=None, token_hex=None):
    """Run the CLI. Returns the exit code (0 clean, 1 hard error, 2 gate refused)."""
    stream = stream or sys.stdout
    environ = os.environ if environ is None else environ
    args = build_parser().parse_args(argv)
    console = SeedConsole(verbose=args.verbose, stream=stream, use_color=False)
    if args.layout == "plan":
        from dev.sandbox import lab_seed  # late import: lab_seed imports this module
        return lab_seed.main_plan(args, console, api_factory, sleep, environ, token_hex)
    seeder = None
    api = None
    try:
        if not args.sandbox_ack:
            raise SafetyGate("the flag --i-know-this-is-sandbox is missing. Refusing to run.")
        if not args.map:
            raise SafetyGate("--map is required. Refusing to run.")
        cfg = load_map(args.map)
        steps = parse_steps(args.steps)
        token = _read_token(environ, console)
        if not token:
            raise SafetyGate("no API token. Set TE_OAUTH_TOKEN. Refusing to run.")
        factory = api_factory or ThousandEyesApiClient
        api = factory(token)
        mode = "apply" if args.apply else "dry-run"
        console.play("SANDBOX SEED%s" % (" CLEANUP" if args.cleanup else ""), mode.upper(), "sandbox")
        console.info("safety", "sandbox flag set; groups: %s" % ", ".join(
            "%s=%s" % (label, cfg.groups[label]) for label in GROUP_LABELS if label in cfg.groups))
        seeder = Seeder(api, cfg, console, apply=args.apply, sleep=sleep, token_hex=token_hex)
        seeder.preflight()
        if args.cleanup:
            seeder.result["steps"] = ["cleanup"]
            if args.steps:
                console.info("cleanup", "--steps is ignored with --cleanup")
            seeder.cleanup()
        else:
            seeder.result["steps"] = steps
            for step in steps:
                if step == "report":
                    seeder.step_report(args.output)
                else:
                    getattr(seeder, "step_" + step)()
        seeder.result["exit_code"] = EXIT_OK
        path = _write_result(args.output, seeder.result)
        console.info("result", "result file: %s" % path)
        console.scope = "recap"
        console.recap()
        return EXIT_OK
    except SafetyGate as exc:
        console.status(StepStatus.FAILED, "safety", "safety gate", "REFUSED: %s" % exc)
        return EXIT_REFUSED
    except SeedError as exc:
        console.info("STATE", "stopped at the first error. %s" % seeder.state_text())
        seeder.result["error"] = mask_secrets(str(exc))
        seeder.result["exit_code"] = EXIT_ERROR
        try:
            path = _write_result(args.output, seeder.result)
            console.info("result", "result file: %s" % path)
        except OSError as io_exc:
            console.info("result", "result file not written: %s" % mask_secrets(str(io_exc)))
        console.recap()
        return EXIT_ERROR
    except KeyboardInterrupt:
        console.info("STATE", "interrupted. %s" % (seeder.state_text() if seeder else "nothing was created"))
        return 130
    finally:
        closer = getattr(api, "close", None)
        if callable(closer):
            closer()


if __name__ == "__main__":
    sys.exit(main())
