"""Plan-driven sandbox seed (the default layout of ``python -m dev.sandbox.seed_sandbox``).

The layout of the lab comes from the anonymized lab plan (``dev/sandbox/lab_plan.py``), which comes from the
scope workbook. This module has:

* ``parse_lab_map``  - validate the operator map (xlsx path, VMs, alias -> sandbox group id);
* ``plan_group_tests`` - the tests of each group (pure function, no API call);
* ``PlanSeeder``     - the seeder (extends ``seed_sandbox.Seeder``: same safety rules, same helpers);
* ``main_plan``      - the CLI flow for the plan layout.

Safety rules (same as the generic layout): map has ``"sandbox": true`` and the flag
``--i-know-this-is-sandbox`` is given; dry run is the default; every object has a name that starts with
``SBX-``; the token is never printed. Extra guard: ``--max-calls`` stops the run when too many API calls are
made. Not seeded (no API shape is known in this project): dashboards and suppression windows. They are
listed in ``lab_manual_tasks.md`` and the run prints ``not seeded: <reason>``.

Python 3.6 compatible.
"""
import ipaddress
import json
import os
import re
import secrets

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import StepStatus
from te_agent_migrate.output import mask_secrets

from dev.sandbox import lab_plan
from dev.sandbox import seed_sandbox as base
from dev.sandbox.seed_sandbox import SafetyGate, SeedError

PLAN_STEPS = ("clusters", "shares", "tests", "tags", "alerts", "report")
DEFAULT_PLAN_STEPS = ("shares", "tests", "tags", "alerts", "report")  # clusters: OFF by default
DEFAULT_MAX_CALLS = 2000
LAB_INTERVAL = base.INTERVAL_BULK  # lab tests run once an hour: the lab only needs the objects
LAB_MANUAL_FILE = "lab_manual_tasks.md"
ALIAS_PATTERN = re.compile(r"^SBX-G\d{2,}$")
A2A_TYPE = "agent-to-agent"
A2A_MAX_SOURCES = 3
LAB_TAG = ("sbx-lab", "agents")  # agent tag on every lab device (for the agent-tag widget)

NOT_SEEDED_DASHBOARDS = ("dashboards and suppression windows: no API shape for them is used by this project; "
                         "build them by hand (see %s)" % LAB_MANUAL_FILE)


# ----------------------------------------------------------------------------------------- map file
class LabMap(object):
    """Validated operator map for the plan layout. ``groups``: alias -> sandbox aid."""

    def __init__(self, groups, vms, xlsx=None, plan=None, prefix_map=None, clusters=None):
        self.groups = groups
        self.vms = vms
        self.xlsx = xlsx
        self.plan = plan
        self.prefix_map = prefix_map
        self.clusters = clusters or {}
        self.agents = {}


def _optional_text(value, where):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SafetyGate("map: %s must be a non-empty string" % where)
    return value.strip()


def parse_lab_map(data):
    """Validate the map (dict). Raise ``SafetyGate`` on any problem."""
    if not isinstance(data, dict):
        raise SafetyGate("map: the file must contain a JSON object")
    if data.get("sandbox") is not True:
        raise SafetyGate('map: the key "sandbox": true is missing. Refusing to run.')
    if data.get("layout", "plan") != "plan":
        raise SafetyGate('map: "layout" must be "plan" for this layout')
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, dict) or not raw_groups:
        raise SafetyGate('map: "groups" must be an object (alias -> sandbox group id)')
    groups = {}
    seen = {}
    for alias, value in raw_groups.items():
        if not ALIAS_PATTERN.match(str(alias)):
            raise SafetyGate("map: group alias %r is not of the form SBX-G01. Refusing to run." % alias)
        aid = base._text_id(value, "groups.%s" % alias)
        if aid in seen:
            raise SafetyGate("map: group id repeats (%s and %s). Refusing to run." % (seen[aid], alias))
        seen[aid] = alias
        groups[alias] = aid
    raw_vms = data.get("vms", [])
    if not isinstance(raw_vms, list):
        raise SafetyGate('map: "vms" must be a list')
    vms = []
    names = set()
    ips = set()
    for index, item in enumerate(raw_vms, 1):
        if not isinstance(item, dict):
            raise SafetyGate("map: vms[%d] must be an object with name and ip" % index)
        name = _optional_text(item.get("name"), "vms[%d].name" % index)
        ip = _optional_text(item.get("ip"), "vms[%d].ip" % index)
        agent_id = item.get("agent_id")
        agent_id = base._text_id(agent_id, "vms[%d].agent_id" % index) if agent_id is not None else None
        if not name and not ip and not agent_id:
            raise SafetyGate("map: vms[%d] needs a name, an ip or an agent_id" % index)
        if ip:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise SafetyGate("map: vms[%d].ip is not an IP address" % index)
        if (name and name.casefold() in names) or (ip and ip in ips):
            raise SafetyGate("map: vms[%d] repeats a name or an ip" % index)
        if name:
            names.add(name.casefold())
        if ip:
            ips.add(ip)
        vms.append({"name": name, "ip": ip, "agent_id": agent_id})
    clusters = {}
    raw_clusters = data.get("clusters")
    if raw_clusters is not None:
        if not isinstance(raw_clusters, dict):
            raise SafetyGate('map: "clusters" must be an object')
        for label, value in raw_clusters.items():
            if label not in base.CLUSTER_LABELS:
                raise SafetyGate("map: unknown cluster %r" % label)
            members = value.get("members") if isinstance(value, dict) else None
            if not isinstance(members, list) or len(members) < 2:
                raise SafetyGate("map: cluster %s needs a list of at least 2 members" % label)
            clusters[label] = [base._text_id(m, "clusters.%s.members" % label) for m in members]
    return LabMap(groups, vms,
                  xlsx=_optional_text(data.get("xlsx"), "xlsx"),
                  plan=_optional_text(data.get("plan"), "plan"),
                  prefix_map=_optional_text(data.get("prefix_map"), "prefix_map"),
                  clusters=clusters)


def load_lab_map(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SafetyGate("map: cannot read %s: %s" % (path, exc))
    return parse_lab_map(data)


# ----------------------------------------------------------------------------------------- tests of the lab
class LabTest(object):
    """One test of the lab, with device aliases (no agent ids yet). Pure data."""

    def __init__(self, group, test_type, name, index, sources, target=None, extra=False):
        self.group = group
        self.test_type = test_type
        self.name = name
        self.index = index
        self.sources = list(sources)
        self.target = target
        self.extra = extra


def visible_devices(plan, wave, group):
    """Aliases of the wave devices that see ``group`` (owner group or shared into it)."""
    result = []
    for alias in sorted(wave):
        device = plan.devices[alias]
        if group == device["source"] or group in device["shares"]:
            result.append(alias)
    return result


def groups_in_scope(plan, wave):
    """Groups that at least one wave device sees. The source group is first, the rest by alias."""
    found = set()
    for alias in wave:
        device = plan.devices[alias]
        if device["source"]:
            found.add(device["source"])
        found.update(device["shares"])
    return sorted(found, key=lambda group: (group != plan.source, group))


def _a2a_sources(visible, target, index):
    others = [alias for alias in visible if alias != target]
    start = (index - 1) % len(others)
    count = min(A2A_MAX_SOURCES, len(others))
    return [others[(start + step) % len(others)] for step in range(count)]


def plan_group_tests(plan, wave, max_tests):
    """Return ``(tests, notes)``.

    ``tests``: list of ``LabTest`` for every group that a wave device sees: the mix of the real group,
    scaled to ``max_tests``, only the types that have a known payload shape. Each test is assigned to all
    wave devices that see the group. An agent-to-agent test has a lab device as its TARGET.
    ``notes``: dict with counts for the "not seeded" lines.
    """
    tests = []
    notes = {"groups_without_device": 0, "groups_without_device_tests": 0,
             "unsupported": {}, "a2a_skipped": 0, "groups": 0}
    for group in sorted(plan.groups, key=lambda name: (name != plan.source, name)):
        info = plan.groups[group]
        visible = visible_devices(plan, wave, group)
        if not visible:
            if info["tests"]:
                notes["groups_without_device"] += 1
                notes["groups_without_device_tests"] += info["tests"]
            continue
        notes["groups"] += 1
        for test_type, count in info.get("not_seedable", {}).items():
            notes["unsupported"][test_type] = notes["unsupported"].get(test_type, 0) + count
        scaled = lab_plan.scale_counts(info["tests_by_type"], max_tests, lab_plan.SEEDABLE_TYPES)
        for test_type in sorted(scaled):
            for number in range(1, scaled[test_type] + 1):
                name = "%s-%s-%02d" % (group, test_type, number)
                if test_type == A2A_TYPE:
                    if len(visible) < 2:
                        notes["a2a_skipped"] += 1
                        continue
                    target = visible[(number - 1) % len(visible)]
                    tests.append(LabTest(group, test_type, name, number,
                                         _a2a_sources(visible, target, number), target))
                else:
                    tests.append(LabTest(group, test_type, name, number, visible))
        if group == plan.source and A2A_TYPE not in scaled:
            if len(visible) >= 2:
                target = visible[0]
                tests.append(LabTest(group, A2A_TYPE, "%s-%s-LAB-01" % (group, A2A_TYPE), 1,
                                     _a2a_sources(visible, target, 1), target, extra=True))
            else:
                notes["a2a_skipped"] += 1
    return tests, notes


def expected_assignments(tests):
    """group -> device alias -> number of tests that name the device as a source agent."""
    result = {}
    for test in tests:
        per_device = result.setdefault(test.group, {})
        for alias in test.sources:
            per_device[alias] = per_device.get(alias, 0) + 1
    return result


def estimate_calls(plan, wave, tests, groups):
    """Upper bound of the API calls of one full apply run (reads and writes)."""
    http = len([t for t in tests if t.test_type == "http-server"])
    devices = len(wave)
    reads = 1 + 1 + len(groups) * 2 + 1 + devices + http
    writes = devices + len(tests) + len(groups) + http + 1 + (devices + base.ASSIGN_CHUNK - 1) // base.ASSIGN_CHUNK
    return reads + writes


# ----------------------------------------------------------------------------------------- call budget
class CallBudget(object):
    """Count every API call. The call after ``limit`` raises ``SeedError`` (0 = no limit)."""

    def __init__(self, api, limit):
        self._api = api
        self._limit = int(limit)
        self.calls = 0

    def __getattr__(self, name):
        if name.startswith("__") or name in ("_api", "_limit", "calls"):
            raise AttributeError(name)
        attr = getattr(self._api, name)
        if name == "close" or not callable(attr):
            return attr

        def counted(*args, **kwargs):
            self.calls += 1
            if self._limit and self.calls > self._limit:
                raise SeedError("max calls (%d) reached. Run stopped. Raise --max-calls or reduce the lab." %
                                self._limit)
            return attr(*args, **kwargs)

        counted.__name__ = name
        return counted


# ----------------------------------------------------------------------------------------- the seeder
class PlanSeeder(base.Seeder):
    """Seeder for the plan layout. Uses the safety rules and helpers of ``seed_sandbox.Seeder``."""

    def __init__(self, api, cfg, console, plan, wave, max_tests=lab_plan.DEFAULT_MAX_TESTS_PER_GROUP,
                 apply=False, sleep=None, token_hex=None):
        kwargs = {} if sleep is None else {"sleep": sleep}
        super(PlanSeeder, self).__init__(api, cfg, console, apply=apply, token_hex=token_hex, **kwargs)
        self.lab = plan
        self.wave = sorted(wave)
        self.max_tests = max_tests
        self.source_label = plan.source
        self.tag_defs = ((LAB_TAG[0], LAB_TAG[1], "agent", "v-agent", tuple(self.wave), ()),)
        self.tag_pairs = (LAB_TAG,)
        self.vm_of = dict(zip(self.wave, cfg.vms))
        self.agent_ids = None
        self.unresolved = set()
        self.not_seeded = []
        self.lab_tests, self.notes = plan_group_tests(plan, self.wave, max_tests)
        self.scope_groups = groups_in_scope(plan, self.wave)
        self._created = set()
        self._rule_ids = {}
        self.result["layout"] = "plan"
        self.result["wave_devices"] = len(self.wave)
        self.result["not_seeded"] = self.not_seeded

    # ------------------------------------------------------------------ hooks
    def group_labels(self):
        return sorted(self.cfg.groups)

    def test_groups(self):
        return list(self.scope_groups)

    def _note_not_seeded(self, reason):
        if reason not in self.not_seeded:
            self.not_seeded.append(reason)
        self.console.info("not seeded", reason)

    # ------------------------------------------------------------------ agents
    @staticmethod
    def _match_vm(records, vm):
        """The agent record of a VM: same IP, or same name or host name. Online and highest id first."""
        name = (vm.get("name") or "").casefold()
        ip = vm.get("ip") or ""
        found = []
        for record in records:
            if record.is_cluster:
                continue
            by_ip = bool(ip) and ip in record.ip_addresses
            by_name = bool(name) and name in ((record.name or "").casefold(), (record.hostname or "").casefold())
            if by_ip or by_name:
                found.append(record)
        if not found:
            return None

        def order(record):
            numeric = int(record.agent_id) if record.agent_id.isdigit() else -1
            return (record.online, numeric, record.agent_id)

        return max(found, key=order)

    def resolve_agents(self):
        """alias -> agent id of the VM of each wave device. Read once.

        A VM that is not found is a hard error with ``--apply``. In a dry run (or ``--verify``) it gets a
        placeholder id and the alias is kept in ``self.unresolved``.
        """
        if self.agent_ids is not None:
            return self.agent_ids
        self.console.scope = "agents"
        source_aid = self.cfg.groups[self.source_label]
        records = None
        ids = {}
        for alias in self.wave:
            vm = self.vm_of[alias]
            if vm.get("agent_id"):
                ids[alias] = str(vm["agent_id"])
                continue
            if records is None:
                records = self._call("global", "list agents", self.api.list_agents_with_clusters, source_aid)
            record = self._match_vm(records, vm)
            if record is None:
                if self.apply:
                    self._line(StepStatus.FAILED, alias, "agents", "no VM found in the source group for this device")
                    raise SeedError("%s: no VM found in the source group" % alias)
                self.unresolved.add(alias)
                ids[alias] = "dry-" + alias
                self._line(StepStatus.SKIPPED, alias, "agents",
                           "no VM found in the source group (placeholder id used, dry run only)")
            else:
                ids[alias] = record.agent_id
        if len(set(ids.values())) != len(ids):
            raise SeedError("two lab devices matched the same VM. Check the vms list in the map.")
        self.agent_ids = ids
        return ids

    def _agent_id_for(self, label):
        return self.resolve_agents()[label]

    # ------------------------------------------------------------------ test specs
    def plan(self):
        if self._plan is None:
            self._plan = self.build_specs(self.resolve_agents())
        return self._plan

    def build_specs(self, ids):
        specs = []
        make_hex = self.token_hex or secrets.token_hex
        for test in self.lab_tests:
            sources = [ids[alias] for alias in test.sources]
            kind = test.test_type
            name = test.name
            if kind == "http-server":
                payload = base.build_http_server(name, sources, interval=LAB_INTERVAL)
            elif kind == "agent-to-server":
                protocol = "icmp" if test.index % 2 else "tcp"
                payload = base.build_agent_to_server(name, sources, protocol, interval=LAB_INTERVAL)
            elif kind == A2A_TYPE:
                payload = base.build_agent_to_agent(name, sources, ids[test.target], interval=LAB_INTERVAL)
            elif kind == "dns-server":
                payload = base.build_dns_server(name, sources, interval=LAB_INTERVAL)
            elif kind == "page-load":
                payload = base.build_page_load(name, sources, interval=LAB_INTERVAL)
            elif kind == "web-transactions":
                script = base.build_transaction_script(make_hex(8), make_hex(8))
                payload = base.build_web_transaction(name, sources, script, interval=LAB_INTERVAL)
            else:  # cannot happen: plan_group_tests keeps only seedable types
                continue
            label = ",".join(test.sources) if kind != A2A_TYPE else "%s>%s" % (",".join(test.sources), test.target)
            specs.append(base.TestSpec(test.group, kind, name, kind, payload, label))
        return specs

    # ------------------------------------------------------------------ steps
    def step_shares(self):
        self.console.scope = "shares"
        ids = self.resolve_agents()
        source = self.lab.source
        for alias in self.wave:
            device = self.lab.devices[alias]
            wanted = [source] + [group for group in device["shares"] if group != source]
            self._share_one(alias, ids[alias], source, wanted)

    def step_tests(self):
        for reason in self.test_notes():
            self._note_not_seeded(reason)
        super(PlanSeeder, self).step_tests()

    def test_notes(self):
        notes = self.notes
        reasons = []
        if notes["groups_without_device"]:
            reasons.append("%d group(s) with %d real tests have no lab device shared into them" % (
                notes["groups_without_device"], notes["groups_without_device_tests"]))
        if notes["unsupported"]:
            reasons.append("test types without a known payload shape (%s)" % ", ".join(
                "%s x%d" % (key, notes["unsupported"][key]) for key in sorted(notes["unsupported"])))
        if notes["a2a_skipped"]:
            reasons.append("%d agent-to-agent test(s): a group needs 2 lab devices" % notes["a2a_skipped"])
        return reasons

    def step_alerts(self):
        self.console.scope = "alerts"
        task = "alerts"
        for group in self.scope_groups:
            aid = self.cfg.groups[group]
            name = "%s-ALERT" % group
            rules = self._call(group, "list alert rules", self.api.list_alert_rules, aid)
            rule = next((r for r in rules if str(r.get("ruleName")) == name), None)
            rule_id = None
            if rule is not None:
                rule_id = base._id_of(rule, "ruleId", "alertRuleId", "id")
                self._count_existing("alert_rules")
                self._line(StepStatus.OK, name, task, "alert rule already exists (id %s)" % rule_id)
            else:
                if self.apply:
                    created = self._call(name, "create alert rule", self.api.create_alert_rule, aid,
                                         base.build_alert_rule(name, "http-server"))
                    self._pace()
                    rule_id = base._id_of(created, "ruleId", "alertRuleId", "id")
                    if not rule_id:
                        self._line(StepStatus.FAILED, name, task, "create alert rule returned no id")
                        raise SeedError("%s: create alert rule returned no id" % name)
                self._record("alert_rules", {"group": group, "name": name, "id": rule_id})
                self._line(StepStatus.CHANGED, name, task, "%s alert rule" % self._verb("created", "create"))
            self._rule_ids[group] = rule_id
            attached = self._attach_rule(group, aid, name, rule_id)
            if attached:
                self._line(StepStatus.CHANGED, name, task, "%s to %d http-server tests" % (
                    self._verb("attached", "attach"), attached))

    def _attach_rule(self, group, aid, rule_name, rule_id):
        existing = self.tests_in(group)
        attached = 0
        for spec in self.plan():
            if spec.group != group or spec.test_type != "http-server":
                continue
            record = existing.get(spec.name)
            if record is None:
                if self.apply:
                    self._line(StepStatus.FAILED, spec.name, "alerts", "test not found (run the tests step first)")
                    raise SeedError("%s: test not found" % spec.name)
                attached += 1
                continue
            ids = []
            if rule_id is not None and spec.name not in self._created:
                current = self._call(spec.name, "get test", self.api.get_test, aid, spec.test_type, record.test_id)
                ids = [base._id_of(a, "ruleId", "id") for a in current.alert_rules]
                if rule_id in ids:
                    self._count_existing("alert_attachments")
                    continue
            attached += 1
            if not self.apply:
                continue
            payload = dict(spec.payload)
            payload["alertRules"] = [value for value in ids if value] + [rule_id]
            payload["alertsEnabled"] = True
            self._call(spec.name, "attach alert rule", self.api.update_test, aid, spec.test_type,
                       record.test_id, payload)
            self._pace()
        self._record("alert_attachments", {"rule": rule_name, "count": attached})
        return attached

    def _record(self, kind, item):
        # A test that this run created (it has an id): the alert step needs no read of it.
        if kind == "tests" and self.apply and item.get("id"):
            self._created.add(item["name"])
        super(PlanSeeder, self)._record(kind, item)

    def step_report(self, output_dir):
        self.console.scope = "report"
        path = base.write_text_file(output_dir, LAB_MANUAL_FILE, self.manual_tasks_md())
        self._line(StepStatus.OK, "report", "report", "wrote %s" % path)
        self._note_not_seeded(NOT_SEEDED_DASHBOARDS)

    # ------------------------------------------------------------------ manual tasks
    def manual_tasks_md(self):
        lines = [
            "# Lab manual tasks",
            "",
            "Do these tasks by hand in the ThousandEyes UI. Use only the sandbox. Name every object SBX-...",
            "No API shape for dashboards or suppression windows is used by this project, so the seed script",
            "does not create them. Build the dashboards in this order of value: at least the source group,",
            "then one target group, then one group that only sees devices (shared).",
            "",
            "Agent tag of the lab devices: %s=%s (the script makes it). Alert rule of each group: <group>-ALERT." %
            LAB_TAG,
            "",
        ]
        for group in self.scope_groups:
            visible = visible_devices(self.lab, self.wave, group)
            first = visible[0]
            vm = self.vm_of.get(first, {})
            lines.extend([
                "## %s" % group,
                "",
                "Devices that see this group: %s." % ", ".join(visible),
                "",
                "| Dashboard name | Widgets and exact filter | Expected flag in te_report |",
                "|---|---|---|",
                "| %s-DASH-1-AGENT-ID | Time Series. Widget filter on AGENT ID: %s (VM %s). | BREAKS (filter_agent_id) |"
                % (group, first, vm.get("name") or vm.get("ip") or "?"),
                "| %s-DASH-2-AGENT-TAG | Color Grid. Widget filter on AGENT TAG %s=%s. | REVIEW |"
                % ((group,) + LAB_TAG),
                "| %s-DASH-3-GROUP-BY-AGENT | Time Series. Test filter: tests named %s-*. GROUP BY: Agent. | REVIEW |"
                % (group, group),
                "| %s-DASH-4-ALERT-LIST | Alert List. Alert rule filter: %s-ALERT. | REVIEW |" % (group, group),
                "",
                "Optional: suppression window %s-SUPPRESS on the agent tag %s=%s." % ((group,) + LAB_TAG),
                "",
            ])
            targets = [t for t in self.lab_tests if t.group == group and t.test_type == A2A_TYPE]
            for test in targets:
                lines.append("- Test %s has the lab device %s (VM %s) as TARGET. Expected flag: BREAKS." % (
                    test.name, test.target, self.vm_of.get(test.target, {}).get("name") or "?"))
            if targets:
                lines.append("")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ verify (read only)
    def verify(self):
        """List, per lab device, the groups it is shared with and the tests assigned, against the plan.

        Read only (GET calls). One ``OK`` or ``MISMATCH`` line per device. Return the number of mismatches.
        """
        self.console.scope = "verify"
        ids = self.resolve_agents()
        expected = expected_assignments(self.lab_tests)
        source_aid = self.cfg.groups[self.source_label]
        mine = {}
        agent_tests = {}
        for group in self.scope_groups:
            aid = self.cfg.groups[group]
            records = self._call(group, "list tests", self.api.list_tests, aid)
            mine[group] = set(r.test_id for r in records if r.name.startswith(group + "-"))
            agents = self._call(group, "list agents", self.api.list_agents_with_clusters, aid)
            agent_tests[group] = dict((a.agent_id, set(a.test_ids)) for a in agents)
        bad = 0
        results = []
        for alias in self.wave:
            device = self.lab.devices[alias]
            want_groups = [self.lab.source] + [g for g in device["shares"] if g != self.lab.source]
            if alias in self.unresolved:
                bad += 1
                results.append({"device": alias, "ok": False, "problem": "VM not found"})
                self.console.info("verify", "MISMATCH %s: no VM found in the source group" % alias)
                continue
            record = self._call(alias, "get agent", self.api.get_agent, source_aid, ids[alias])
            have = set(record.shared_aids)
            want = set(self.cfg.groups[g] for g in want_groups)
            missing = sorted(g for g in want_groups if self.cfg.groups[g] not in have)
            extra = len(have - want)
            problems = []
            if missing:
                problems.append("not shared with %s" % ", ".join(missing))
            planned_total = 0
            assigned_total = 0
            for group in want_groups:
                if group not in agent_tests:
                    continue
                planned = expected.get(group, {}).get(alias, 0)
                assigned = len(agent_tests[group].get(ids[alias], set()) & mine[group])
                planned_total += planned
                assigned_total += assigned
                if planned != assigned:
                    problems.append("%s tests %d of %d" % (group, assigned, planned))
            shared_text = "shares %d of %d" % (len(want_groups) - len(missing), len(want_groups))
            if extra:
                shared_text += " (+%d extra)" % extra
            if problems:
                bad += 1
                results.append({"device": alias, "ok": False, "problem": "; ".join(problems)})
                self.console.info("verify", "MISMATCH %s: %s; %s" % (alias, shared_text, "; ".join(problems)))
            else:
                results.append({"device": alias, "ok": True})
                self.console.info("verify", "OK %s: %s, tests assigned %d of %d in %d groups" % (
                    alias, shared_text, assigned_total, planned_total, len(want_groups)))
        self.result["verify"] = results
        self.console.info("verify", "%d device(s) OK, %d MISMATCH" % (len(results) - bad, bad))
        return bad


# ----------------------------------------------------------------------------------------- CLI flow
def _lab_from_args(args, cfg):
    """Build the plan and its lab section. Return (plan, source_text)."""
    plan_path = args.plan or (cfg.plan if cfg else None)
    if plan_path:
        return lab_plan.read_lab_plan_file(plan_path), "plan file"
    xlsx = args.xlsx or (cfg.xlsx if cfg else None)
    if not xlsx:
        raise SafetyGate("no scope: give --xlsx or --plan, or set xlsx in the map")
    prefix_map = args.prefix_map or (cfg.prefix_map if cfg else None)
    return lab_plan.load_lab_plan(xlsx, prefix_map, args.max_tests_per_group), "workbook"


def _choose_wave(plan, args, cfg):
    vms = args.vms
    if not vms:
        vms = min(lab_plan.DEFAULT_VMS, len(cfg.vms)) if (cfg and cfg.vms) else lab_plan.DEFAULT_VMS
    if vms < 1:
        raise SafetyGate("--vms must be 1 or more")
    lab = lab_plan.build_lab(plan, vms, args.waves)
    wave_number = args.wave or 1
    if args.waves:
        waves = lab["wave_split"]["waves"]
        if wave_number < 1 or wave_number > len(waves):
            raise SafetyGate("--wave %d is outside 1..%d" % (wave_number, len(waves)))
        wave = waves[wave_number - 1]
    else:
        if wave_number != 1:
            raise SafetyGate("--wave needs --waves")
        wave = lab["devices"]
    return lab, list(wave), wave_number


def _check_map_for_wave(plan, cfg, wave, max_tests, max_calls):
    sources = set(plan.devices[alias]["source"] for alias in wave)
    if sources != set([plan.source]):
        raise SafetyGate("the wave has devices of more than one source group. Refusing to run.")
    needed = groups_in_scope(plan, wave)
    missing = [group for group in needed if group not in cfg.groups]
    if missing:
        raise SafetyGate("map: no sandbox group id for: %s" % ", ".join(sorted(missing)))
    if len(cfg.vms) < len(wave):
        raise SafetyGate("map: the wave has %d devices but the map lists %d VMs" % (len(wave), len(cfg.vms)))
    tests, _notes = plan_group_tests(plan, wave, max_tests)
    estimate = estimate_calls(plan, wave, tests, needed)
    if max_calls and estimate > max_calls:
        raise SafetyGate("the run needs up to %d API calls, more than --max-calls %d. Lower "
                         "--max-tests-per-group or --vms, or raise --max-calls." % (estimate, max_calls))
    return needed, tests, estimate


def parse_plan_steps(text, with_clusters=False):
    if text is None or not str(text).strip():
        return [s for s in PLAN_STEPS if with_clusters or s != "clusters"]
    wanted = [item.strip() for item in str(text).split(",") if item.strip()]
    unknown = [item for item in wanted if item not in PLAN_STEPS]
    if unknown:
        raise SafetyGate("unknown step(s): %s. Allowed: %s" % (", ".join(unknown), ", ".join(PLAN_STEPS)))
    return [step for step in PLAN_STEPS if step in wanted]


def main_plan(args, console, api_factory, sleep, environ, token_hex):
    """CLI flow of the plan layout. Returns the exit code (0 clean, 1 error or MISMATCH, 2 gate refused)."""
    seeder = None
    api = None
    try:
        if not args.plan_only and not args.sandbox_ack:
            raise SafetyGate("the flag --i-know-this-is-sandbox is missing. Refusing to run.")
        cfg = load_lab_map(args.map) if args.map else None
        if cfg is None and not args.plan_only:
            raise SafetyGate("--map is required (see examples/sandbox_map.example.json)")
        plan, origin = _lab_from_args(args, cfg)
        lab, wave, wave_number = _choose_wave(plan, args, cfg)
        for line in lab_plan.summary_lines(plan, lab):
            console._write(line)
        if args.write_plan:
            path = lab_plan.write_private_json(args.write_plan, plan.to_dict(lab))
            console.info("lab plan", "wrote %s" % path)
            if plan.alias_map is not None:
                alias_path = lab_plan.write_private_json(
                    os.path.join(args.output, lab_plan.ALIAS_MAP_FILE), plan.alias_map)
                console.info("alias map", "wrote %s (real names; never share this file)" % alias_path)
        if args.plan_only:
            return base.EXIT_OK
        steps = parse_plan_steps(args.steps, args.with_clusters)
        needed, tests, estimate = _check_map_for_wave(plan, cfg, wave, args.max_tests_per_group, args.max_calls)
        token = base._read_token(environ, console)
        if not token:
            raise SafetyGate("no API token. Set TE_OAUTH_TOKEN. Refusing to run.")
        factory = api_factory or base.ThousandEyesApiClient
        api = factory(token)
        budget = CallBudget(api, args.max_calls)
        mode = "apply" if args.apply and not args.verify else "dry-run"
        title = "SANDBOX LAB VERIFY" if args.verify else "SANDBOX LAB SEED%s" % (" CLEANUP" if args.cleanup else "")
        console.play(title, mode.upper(), "sandbox")
        console.info("safety", "sandbox flag set; plan from %s; wave %d: %d devices, %d groups, %d tests planned" % (
            origin, wave_number, len(wave), len(needed), len(tests)))
        console.info("calls", "estimated API calls for a full run: up to %d (--max-calls %d)" % (
            estimate, args.max_calls))
        seeder = PlanSeeder(budget, cfg, console, plan, wave, max_tests=args.max_tests_per_group,
                            apply=(args.apply and not args.verify), sleep=sleep, token_hex=token_hex)
        seeder.preflight()
        bad = 0
        if args.verify:
            seeder.result["steps"] = ["verify"]
            bad = seeder.verify()
        elif args.cleanup:
            seeder.result["steps"] = ["cleanup"]
            seeder.cleanup()
        else:
            seeder.result["steps"] = steps
            for step in steps:
                if step == "report":
                    seeder.step_report(args.output)
                else:
                    getattr(seeder, "step_" + step)()
        code = base.EXIT_ERROR if bad else base.EXIT_OK
        seeder.result["exit_code"] = code
        seeder.result["calls"] = budget.calls
        path = base._write_result(args.output, seeder.result)
        console.info("result", "result file: %s (%d API calls)" % (path, budget.calls))
        console.scope = "recap"
        console.recap()
        return code
    except SafetyGate as exc:
        console.status(StepStatus.FAILED, "safety", "safety gate", "REFUSED: %s" % exc)
        return base.EXIT_REFUSED
    except ConfigurationError as exc:
        console.status(StepStatus.FAILED, "input", "scope", "ERROR: %s" % mask_secrets(str(exc)))
        return base.EXIT_ERROR
    except SeedError as exc:
        console.info("STATE", "stopped at the first error. %s" % seeder.state_text() if seeder else str(exc))
        if seeder is not None:
            seeder.result["error"] = mask_secrets(str(exc))
            seeder.result["exit_code"] = base.EXIT_ERROR
            try:
                path = base._write_result(args.output, seeder.result)
                console.info("result", "result file: %s" % path)
            except OSError as io_exc:
                console.info("result", "result file not written: %s" % mask_secrets(str(io_exc)))
        console.status(StepStatus.FAILED, "error", "seed", mask_secrets(str(exc)))
        console.recap()
        return base.EXIT_ERROR
    except KeyboardInterrupt:
        console.info("STATE", "interrupted. %s" % (seeder.state_text() if seeder else "nothing was created"))
        return 130
    finally:
        closer = getattr(api, "close", None)
        if callable(closer):
            closer()

