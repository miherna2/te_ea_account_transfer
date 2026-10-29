"""Tests for the plan-driven sandbox seed (lab_plan.py, lab_seed.py). All data is synthetic.

A tiny workbook is built in a temp dir. The words MARK... stand for real names: no plan file may hold them.
A fake API object is used: no network, no real API call.
"""

import copy
import io
import json
import os
import shutil
import tempfile
import unittest

from dev.sandbox import lab_plan, lab_seed
from dev.sandbox import seed_sandbox as seed
from dev.tests.test_scope import make_xlsx, summary_row
from te_agent_migrate import scope
from te_agent_migrate.errors import ApiError
from te_agent_migrate.models import AccountGroup, AgentRecord, TestRecord

FAKE_API_TOKEN = "fake-sandbox-api-value"  # not a credential: only proves that the token is never printed

G_S = "Group MARKS [900]"
G_A = "Div_MARKA [100]"
G_B = "Div_MARKB [200]"
G_X = "BU_MARKX [300]"
G_Y = "Ent_MARKY [400]"
MARKERS = ("MARK", "192.0.2.", "Group S")

PREFIXES = [
    scope.PrefixEntry("AAAA", "Div_MARKA", "100", "OK"),
    scope.PrefixEntry("BBBB", "Div_MARKB", "200", "OK"),
    scope.PrefixEntry("CCCC", "BU_MARKX", "300", "OK"),
    scope.PrefixEntry("DDDD", "Div_MARKA", "100", "OK"),
    scope.PrefixEntry("EEEE", "Div_MARKA", "100", "OK"),
]


def summary_rows():
    def row(name, number, state, groups, login="good"):
        return summary_row(name, 5000 + number, state, "192.0.2.%d" % (10 + number),
                           "; ".join(groups), login, 1, source=G_S.split(" [")[0])
    return [
        row("AAAA - MARKHOST01", 1, "online", [G_S, G_A, G_Y]),
        row("AAAA - MARKHOST02", 2, "online", [G_S, G_A, G_X]),
        row("AAAA - MARKHOST03", 3, "online", [G_S, G_A]),
        row("BBBB - MARKHOST04", 4, "online", [G_S, G_B, G_X]),
        row("BBBB - MARKHOST05", 5, "online", [G_S, G_B, G_Y]),
        row("CCCC - MARKHOST06", 6, "online", [G_S, G_Y]),  # anomaly: target X is not in the list
        row("DDDD - MARKHOST07", 7, "offline", [G_S, G_A], "offline"),
        row("EEEE - MARKHOST08", 8, "online", [G_S, G_A], "cant login"),
    ]


def test_rows():
    rows = []

    def add(agent, aid, test_id, kind):
        rows.append([agent, 1, "context", aid, test_id, "test-" + test_id, kind])

    for index in range(8):
        add("AAAA - MARKHOST01", "900", "h%d" % index, "http-server")
    for index in range(3):
        add("AAAA - MARKHOST01", "900", "v%d" % index, "voice")
    for index in range(40):
        add("AAAA - MARKHOST02", "100", "s%d" % index, "agent-to-server")
    for index in range(2):
        add("AAAA - MARKHOST02", "100", "x%d" % index, "agent-to-agent")
    for index in range(2):
        add("AAAA - MARKHOST03", "400", "d%d" % index, "dns-server")
    add("AAAA - MARKHOST03", "400", "w0", "web-transactions")
    for index in range(3):
        add("BBBB - MARKHOST04", "200", "b%d" % index, "http-server")
    for index in range(3):
        add("", "500", "o%d" % index, "http-server")  # a group that owns tests and sees no device
    return rows


def make_workbook(directory):
    path = os.path.join(directory, "scope.xlsx")
    make_xlsx(path, summary_rows(), test_rows())
    prefix_path = os.path.join(directory, "prefix_map.csv")
    scope.write_prefix_map(prefix_path, PREFIXES)
    return path, prefix_path


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbxplan")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.xlsx, self.prefix_path = make_workbook(self.tmp)
        self.plan = lab_plan.load_lab_plan(self.xlsx, self.prefix_path)

    def device_with(self, **wanted):
        for alias, device in self.plan.devices.items():
            if all(device[key] == value for key, value in wanted.items()):
                return alias
        return None


class AliasTests(PlanCase):
    def test_group_and_device_aliases(self):
        self.assertEqual(self.plan.source, "SBX-G01")
        self.assertEqual(list(self.plan.groups), ["SBX-G0%d" % n for n in range(1, 7)])
        self.assertEqual(list(self.plan.devices), ["SBX-D00%d" % n for n in range(1, 9)])
        groups = self.plan.alias_map["groups"]
        self.assertEqual(groups["SBX-G01"]["aid"], "900")  # the source appears first
        self.assertEqual(groups["SBX-G02"]["aid"], "100")  # then the target of device 1
        self.assertEqual(groups["SBX-G06"]["aid"], "500")  # a group that only owns tests comes last
        self.assertEqual(self.plan.alias_map["devices"]["SBX-D001"], "AAAA - MARKHOST01")

    def test_no_real_name_in_the_plan_json(self):
        lab = lab_plan.build_lab(self.plan, 3, 2)
        text = json.dumps(self.plan.to_dict(lab), indent=2)
        for marker in MARKERS:
            self.assertNotIn(marker, text)
        for real in ("AAAA", "BBBB", "HOST0", "context", "test-h0"):
            self.assertNotIn(real, text)
        for aid in ("900", "100", "200", "300", "400", "500", "5001", "5002"):
            self.assertNotIn('"%s"' % aid, text)
        self.assertNotIn("alias_map", text)
        self.assertNotIn("name", set(self.plan.to_dict()))

    def test_written_plan_file_and_alias_map_are_private_and_clean(self):
        out = os.path.join(self.tmp, "out")
        plan_file = os.path.join(self.tmp, "sub", "plan.json")
        stream = io.StringIO()
        code = seed.main(["--xlsx", self.xlsx, "--prefix-map", self.prefix_path, "--plan-only", "--vms", "3",
                          "--write-plan", plan_file, "--output", out], stream=stream)
        self.assertEqual(code, 0)
        with open(plan_file) as handle:
            plan_text = handle.read()
        for text in (stream.getvalue(), plan_text):
            for marker in MARKERS:
                self.assertNotIn(marker, text)
        self.assertEqual(oct(os.stat(plan_file).st_mode & 0o777), oct(0o600))
        alias_path = os.path.join(out, lab_plan.ALIAS_MAP_FILE)
        with open(alias_path) as handle:
            alias_map = json.load(handle)
        self.assertEqual(alias_map["groups"]["SBX-G01"]["aid"], "900")
        self.assertIn("MARK", json.dumps(alias_map))  # the only file with real names
        self.assertEqual(oct(os.stat(alias_path).st_mode & 0o777), oct(0o600))
        # The plan file can be read again and gives the same plan.
        again = lab_plan.read_lab_plan_file(plan_file)
        self.assertEqual(list(again.devices), list(self.plan.devices))
        self.assertIsNone(again.alias_map)

    def test_plan_file_with_a_non_sbx_alias_is_refused(self):
        data = self.plan.to_dict()
        data["groups"]["PROD-G01"] = data["groups"].pop("SBX-G01")
        with self.assertRaises(lab_plan.ConfigurationError):
            lab_plan.LabPlan.from_dict(data)

    def test_statuses_and_anomaly_are_kept(self):
        statuses = [d["status"] for d in self.plan.devices.values()]
        self.assertEqual(statuses.count(scope.STATUS_READY), 6)
        self.assertEqual(statuses.count(scope.STATUS_EXCLUDED_OFFLINE), 1)
        self.assertEqual(statuses.count(scope.STATUS_EXCLUDED_LOGIN), 1)
        offline = self.plan.devices["SBX-D007"]
        self.assertEqual(offline["lab_state"], lab_plan.LAB_OFFLINE)
        self.assertEqual(self.plan.devices["SBX-D001"]["lab_state"], lab_plan.LAB_ONLINE)
        anomaly = self.plan.devices["SBX-D006"]
        self.assertTrue(anomaly["anomaly"])
        self.assertEqual(anomaly["target"], "SBX-G04")
        self.assertNotIn("SBX-G04", anomaly["shares"])  # the target is not in the share list
        self.assertEqual([a for a, d in self.plan.devices.items() if d["anomaly"]], ["SBX-D006"])

    def test_device_and_group_counters(self):
        self.assertEqual(self.plan.devices["SBX-D001"]["tests_by_type"], {"http-server": 8, "voice": 3})
        group = self.plan.groups["SBX-G02"]
        self.assertEqual(group["tests"], 42)
        self.assertEqual(group["tests_by_type"], {"agent-to-agent": 2, "agent-to-server": 40})
        self.assertIn("target", group["roles"])
        self.assertIn("source", self.plan.groups["SBX-G01"]["roles"])
        self.assertEqual(self.plan.groups["SBX-G06"]["roles"], ["test-owner"])
        self.assertEqual(self.plan.groups["SBX-G01"]["not_seedable"], {"voice": 3})

    def test_shapes_are_relative_to_the_target(self):
        self.assertEqual(self.plan.devices["SBX-D001"]["shape"], "@T+SBX-G01+SBX-G03")
        self.assertEqual(self.plan.devices["SBX-D005"]["shape"], self.plan.devices["SBX-D001"]["shape"])
        self.assertEqual(lab_plan.shape_of(None, ["SBX-G01"]), None)
        self.assertEqual(lab_plan.shape_of("SBX-G02", ["SBX-G03", "SBX-G02", "SBX-G01"]),
                         "@T+SBX-G01+SBX-G03")


class ScaleTests(unittest.TestCase):
    def test_small_mix_is_not_changed(self):
        self.assertEqual(dict(lab_plan.scale_counts({"a": 3, "b": 2}, 25)), {"a": 3, "b": 2})

    def test_scaled_mix_has_the_limit_the_ratio_and_every_type(self):
        mix = {"agent-to-server": 1000, "http-server": 100, "dns-server": 4, "page-load": 20}
        scaled = lab_plan.scale_counts(mix, 25)
        self.assertEqual(sum(scaled.values()), 25)
        self.assertEqual(set(scaled), set(mix))
        self.assertGreater(scaled["agent-to-server"], scaled["http-server"])
        self.assertGreater(scaled["http-server"], scaled["dns-server"])
        self.assertEqual(dict(scaled), dict(lab_plan.scale_counts(mix, 25)))  # deterministic

    def test_a_type_never_gets_more_than_its_real_count(self):
        scaled = lab_plan.scale_counts({"a": 100, "b": 1}, 10)
        self.assertEqual(dict(scaled), {"a": 9, "b": 1})

    def test_more_types_than_the_limit_keeps_the_biggest(self):
        scaled = lab_plan.scale_counts({"a": 5, "b": 4, "c": 3, "d": 2}, 2)
        self.assertEqual(dict(scaled), {"a": 1, "b": 1})

    def test_allowed_types_only_and_empty(self):
        scaled = lab_plan.scale_counts({"voice": 50, "http-server": 50}, 10, lab_plan.SEEDABLE_TYPES)
        self.assertEqual(dict(scaled), {"http-server": 10})
        self.assertEqual(dict(lab_plan.scale_counts({}, 10)), {})
        self.assertEqual(dict(lab_plan.scale_counts({"a": 3}, 0)), {})


class GroupScaleTests(PlanCase):
    def test_group_plan_scaled_values(self):
        plan = lab_plan.load_lab_plan(self.xlsx, self.prefix_path, max_tests_per_group=10)
        self.assertEqual(sum(plan.groups["SBX-G02"]["scaled"].values()), 10)
        self.assertEqual(plan.groups["SBX-G01"]["scaled"], {"http-server": 8})  # voice is not seedable
        self.assertEqual(plan.max_tests_per_group, 10)

    def test_lab_tests_are_scaled_per_group_and_named_by_group_and_type(self):
        wave = lab_plan.choose_archetypes(self.plan, 6)["devices"]
        tests, notes = lab_seed.plan_group_tests(self.plan, wave, 5)
        for group in ("SBX-G01", "SBX-G02", "SBX-G03", "SBX-G04", "SBX-G05"):
            own = [t for t in tests if t.group == group and not t.extra]
            self.assertLessEqual(len(own), 5)
        names = [t.name for t in tests]
        self.assertEqual(len(names), len(set(names)))
        for test in tests:
            self.assertTrue(test.name.startswith(test.group + "-" + test.test_type + "-"))
        self.assertEqual(notes["groups_without_device"], 1)
        self.assertEqual(notes["unsupported"], {"voice": 3})


class ArchetypeTests(PlanCase):
    def test_deterministic(self):
        first = lab_plan.choose_archetypes(self.plan, 4)
        second = lab_plan.choose_archetypes(lab_plan.load_lab_plan(self.xlsx, self.prefix_path), 4)
        self.assertEqual(json.dumps(first), json.dumps(second))
        self.assertEqual(first["devices"], sorted(first["devices"]))

    def test_covers_targets_shapes_and_the_anomaly(self):
        lab = lab_plan.choose_archetypes(self.plan, 4)
        cover = lab["coverage"]
        self.assertEqual(cover["target_groups"], [3, 3])
        self.assertEqual(cover["shapes"], [4, 4])
        self.assertEqual(cover["anomaly"], "covered")
        self.assertIn("SBX-D006", lab["devices"])
        self.assertEqual(lab["not_covered"]["target_groups"], [])
        self.assertEqual(lab["not_covered"]["shapes"], [])
        self.assertFalse(lab["not_covered"]["anomaly"])

    def test_small_lab_reports_what_is_not_covered(self):
        lab = lab_plan.choose_archetypes(self.plan, 3)
        self.assertEqual(lab["devices"].count("SBX-D006"), 1)  # the anomaly takes one VM first
        self.assertEqual(lab["coverage"]["target_groups"], [3, 3])
        self.assertEqual(lab["coverage"]["shapes"], [3, 4])
        self.assertEqual(len(lab["not_covered"]["shapes"]), 1)
        one = lab_plan.choose_archetypes(self.plan, 1)
        self.assertEqual(one["devices"], ["SBX-D006"])
        self.assertEqual(one["coverage"]["target_groups"], [1, 3])
        self.assertEqual(len(one["not_covered"]["target_groups"]), 2)
        self.assertEqual(lab_plan.choose_archetypes(self.plan, 0)["devices"], [])

    def test_smallest_target_groups_first(self):
        lab = lab_plan.choose_archetypes(self.plan, 2)
        targets = set(self.plan.devices[a]["target"] for a in lab["devices"])
        self.assertEqual(targets, set(["SBX-G04", "SBX-G05"]))  # X (anomaly) and B (2 devices) before A (3)

    def test_only_ready_devices_are_chosen_and_never_more_than_ready(self):
        lab = lab_plan.choose_archetypes(self.plan, 50)
        self.assertEqual(len(lab["devices"]), 6)
        self.assertNotIn("SBX-D007", lab["devices"])
        self.assertNotIn("SBX-D008", lab["devices"])
        self.assertEqual(lab["not_covered"]["devices_never_started"], 2)
        self.assertEqual(lab["not_covered"]["ready_devices_not_in_lab"], 0)

    def test_widest_test_type_mix(self):
        lab = lab_plan.choose_archetypes(self.plan, 6)
        self.assertEqual(lab["coverage"]["test_types"], [6, 6])
        self.assertEqual(lab["not_covered"]["test_types"], [])

    def test_no_anomaly_in_ready_devices(self):
        data = copy.deepcopy(self.plan.to_dict())
        for device in data["devices"].values():
            device["anomaly"] = False
        plan = lab_plan.LabPlan.from_dict(data)
        self.assertEqual(lab_plan.choose_archetypes(plan, 3)["coverage"]["anomaly"], "none in READY")


class WaveTests(PlanCase):
    def ready(self):
        return sorted(self.plan.ready_devices())

    def test_waves_partition_all_ready_devices_exactly_once(self):
        for vms in (1, 2, 3, 4, 6):
            for waves in (None, 1, 2, 3, 9):
                split = lab_plan.split_waves(self.plan, vms, waves)
                flat = [alias for wave in split["waves"] for alias in wave]
                self.assertEqual(sorted(flat), self.ready(), "vms=%s waves=%s" % (vms, waves))
                self.assertEqual(len(flat), len(set(flat)))
                for wave in split["waves"]:
                    self.assertTrue(0 < len(wave) <= vms)
                self.assertEqual(split["waves"][0], lab_plan.choose_archetypes(self.plan, vms)["devices"])

    def test_waves_count_is_raised_or_lowered_to_fit(self):
        self.assertEqual(lab_plan.split_waves(self.plan, 2, 1)["used"], 3)  # 6 READY, 2 per wave
        self.assertEqual(lab_plan.split_waves(self.plan, 2, 1)["requested"], 1)
        self.assertEqual(lab_plan.split_waves(self.plan, 2, 99)["used"], 5)  # no empty wave
        self.assertEqual(lab_plan.split_waves(self.plan, 6, 3)["used"], 1)
        self.assertEqual(lab_plan.waves_needed(self.plan, 4), 2)
        self.assertEqual(lab_plan.waves_needed(self.plan, 12), 1)

    def test_waves_after_the_first_are_grouped_by_target(self):
        split = lab_plan.split_waves(self.plan, 2, 3)
        self.assertEqual([len(w) for w in split["waves"]], [2, 2, 2])

    def test_summary_is_counts_only(self):
        lab = lab_plan.build_lab(self.plan, 3, 2)
        text = "\n".join(lab_plan.summary_lines(self.plan, lab))
        self.assertIn("devices chosen ............. 3", text)
        self.assertIn("re-registers the SAME VMs", text)
        self.assertNotIn("SBX-D", text)
        self.assertNotIn("SBX-G", text)


class LabMapTests(unittest.TestCase):
    def good(self):
        return {
            "sandbox": True,
            "groups": {"SBX-G01": "1", "SBX-G02": "2"},
            "vms": [{"name": "vm-a", "ip": "192.0.2.1"}, {"name": "vm-b", "ip": "192.0.2.2"}],
        }

    def test_good_map(self):
        cfg = lab_seed.parse_lab_map(self.good())
        self.assertEqual(cfg.groups["SBX-G02"], "2")
        self.assertEqual(len(cfg.vms), 2)

    def test_refused_maps(self):
        def broken(change):
            data = self.good()
            change(data)
            with self.assertRaises(seed.SafetyGate):
                lab_seed.parse_lab_map(data)

        broken(lambda d: d.pop("sandbox"))
        broken(lambda d: d.update(sandbox="true"))
        broken(lambda d: d["groups"].update({"PROD-G03": "3"}))  # SBX prefix guard
        broken(lambda d: d["groups"].update({"G03": "3"}))
        broken(lambda d: d["groups"].update({"SBX-G03": "2"}))  # repeated id
        broken(lambda d: d["vms"].append({"name": "VM-A", "ip": "192.0.2.9"}))  # repeated name
        broken(lambda d: d["vms"].append({"name": "vm-c", "ip": "192.0.2.1"}))  # repeated ip
        broken(lambda d: d["vms"].append({"name": "vm-c", "ip": "not-an-ip"}))
        broken(lambda d: d["vms"].append({}))
        broken(lambda d: d.update(layout="generic"))
        broken(lambda d: d.update(groups={}))

    def test_example_map_parses(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "examples", "sandbox_map.example.json")
        cfg = lab_seed.load_lab_map(path)
        self.assertEqual(len(cfg.groups), 21)
        self.assertEqual(len(cfg.vms), 12)
        self.assertTrue(cfg.xlsx.endswith(".xlsx"))
        for ip in [vm["ip"] for vm in cfg.vms]:
            self.assertTrue(ip.startswith("192.0.2."))  # documentation range only


# ----------------------------------------------------------------------------------------- fake API
class FakePlanApi(object):
    """Keeps state for the calls that the plan seeder uses. Records every write."""

    def __init__(self, groups, vms):
        self.groups = groups  # alias -> aid
        self.writes = []
        self.reads = []
        self.closed = False
        self.next_id = 9000
        self.group_names = dict((aid, alias) for alias, aid in groups.items())
        self.tests = dict((aid, {}) for aid in groups.values())
        self.rules = dict((aid, []) for aid in groups.values())
        self.tags = []
        self.agents = {}
        source = groups["SBX-G01"]
        for name, ip in vms:
            agent_id = self._new_id()
            self.agents[agent_id] = {"name": name, "ip": ip, "shares": [source]}

    def _new_id(self):
        self.next_id += 1
        return str(self.next_id)

    def _agent_dict(self, agent_id, aid):
        info = self.agents[agent_id]
        tests = [r.test_id for r in self.tests[str(aid)].values()
                 if agent_id in [a["agentId"] for a in r.agents]]
        return {"agentId": agent_id, "agentName": info["name"], "agentType": "enterprise",
                "agentState": "online", "ipAddresses": [info["ip"]],
                "accountGroups": [{"aid": a} for a in info["shares"]], "testIds": tests}

    def list_account_groups(self):
        self.reads.append("list_account_groups")
        return [AccountGroup(aid, name) for aid, name in self.group_names.items()]

    def list_agents_with_clusters(self, aid):
        self.reads.append("list_agents_with_clusters")
        return [AgentRecord.from_api(self._agent_dict(i, aid), aid)
                for i, info in sorted(self.agents.items()) if str(aid) in info["shares"]]

    def get_agent(self, aid, agent_id):
        self.reads.append("get_agent")
        if agent_id not in self.agents:
            raise ApiError("not found")
        return AgentRecord.from_api(self._agent_dict(agent_id, aid), aid)

    def list_tests(self, aid):
        self.reads.append("list_tests")
        return list(self.tests[str(aid)].values())

    def get_test(self, aid, test_type, test_id):
        self.reads.append("get_test")
        for record in self.tests[str(aid)].values():
            if record.test_id == test_id:
                return record
        raise ApiError("not found")

    def list_tags(self, aid, expand_assignments=False):
        self.reads.append("list_tags")
        return copy.deepcopy(self.tags)

    def list_alert_rules(self, aid):
        self.reads.append("list_alert_rules")
        return copy.deepcopy(self.rules[str(aid)])

    def request(self, method, path, aid=None, params=None, json_body=None, expected=None, safe_to_retry=None):
        if method.upper() == "GET":
            self.reads.append("request GET " + path)
            return {}
        self.writes.append(("request " + method.upper(), aid, path))
        if path.startswith("tags/"):
            self.tags = [t for t in self.tags if t["tagId"] != path.split("/")[1]]
        if path.startswith("alerts/rules/"):
            self.rules[str(aid)] = [r for r in self.rules[str(aid)] if r["ruleId"] != path.split("/")[2]]
        return {}

    def create_test(self, aid, test_type, payload):
        self.writes.append(("create_test", aid, payload["testName"]))
        test_id = self._new_id()
        raw = dict(payload)
        raw.update({"testId": test_id, "type": test_type})
        record = TestRecord(test_id, payload["testName"], test_type, aid, payload.get("enabled", True),
                            payload.get("agents", []), raw=raw)
        record.payload = copy.deepcopy(payload)
        self.tests[str(aid)][payload["testName"]] = record
        return record

    def update_test(self, aid, test_type, test_id, payload):
        self.writes.append(("update_test", aid, test_id))
        for record in self.tests[str(aid)].values():
            if record.test_id == test_id:
                record.alert_rules = list(payload.get("alertRules", []))
                return record
        raise ApiError("not found")

    def delete_test(self, aid, test_type, test_id):
        self.writes.append(("delete_test", aid, test_id))
        for name, record in list(self.tests[str(aid)].items()):
            if record.test_id == test_id:
                del self.tests[str(aid)][name]

    def create_tag(self, aid, payload):
        self.writes.append(("create_tag", aid, payload["key"] + "=" + payload["value"]))
        tag = dict(payload)
        tag.update({"tagId": self._new_id(), "assignments": []})
        self.tags.append(tag)
        return {"tagId": tag["tagId"]}

    def assign_tag(self, aid, tag_id, assignments):
        self.writes.append(("assign_tag", aid, len(assignments)))
        for tag in self.tags:
            if tag["tagId"] == tag_id:
                for object_id, object_type in assignments:
                    tag["assignments"].append({"id": object_id, "type": object_type})

    def create_alert_rule(self, aid, payload):
        self.writes.append(("create_alert_rule", aid, payload["ruleName"]))
        rule = dict(payload)
        rule["ruleId"] = self._new_id()
        self.rules[str(aid)].append(rule)
        return {"ruleId": rule["ruleId"]}

    def set_agent_account_groups(self, aid, agent_id, account_group_ids):
        self.writes.append(("set_agent_account_groups", aid, (agent_id, list(account_group_ids))))
        if not account_group_ids:
            raise ApiError("empty list")
        self.agents[agent_id]["shares"] = [str(a) for a in account_group_ids]
        return {}

    def close(self):
        self.closed = True


class SeedPlanCase(PlanCase):
    VM_COUNT = 6

    def setUp(self):
        super(SeedPlanCase, self).setUp()
        self.group_ids = dict(("SBX-G%02d" % n, str(7000 + n)) for n in range(1, 7))
        self.vms = [("sbx-vm-%02d" % n, "192.0.2.%d" % (100 + n)) for n in range(1, self.VM_COUNT + 1)]
        self.map_path = os.path.join(self.tmp, "map.json")
        self.out_dir = os.path.join(self.tmp, "out")
        self.write_map()

    def map_data(self):
        return {
            "sandbox": True,
            "layout": "plan",
            "xlsx": self.xlsx,
            "prefix_map": self.prefix_path,
            "vms": [{"name": n, "ip": ip} for n, ip in self.vms],
            "groups": dict(self.group_ids),
        }

    def write_map(self, data=None):
        with open(self.map_path, "w") as handle:
            json.dump(data or self.map_data(), handle)

    def new_api(self):
        return FakePlanApi(self.group_ids, self.vms)

    def run_cli(self, api, *extra, **kwargs):
        argv = ["--map", self.map_path, "--output", self.out_dir]
        if kwargs.get("ack", True):
            argv.append("--i-know-this-is-sandbox")
        argv.extend(extra)
        stream = io.StringIO()
        sleeps = []
        code = seed.main(
            argv, api_factory=lambda token: api, sleep=sleeps.append, stream=stream,
            environ=kwargs.get("environ", {"TE_OAUTH_TOKEN": FAKE_API_TOKEN}),
            token_hex=kwargs.get("token_hex"),
        )
        self.last_sleeps = sleeps
        return code, stream.getvalue()

    def result(self):
        with open(os.path.join(self.out_dir, seed.RESULT_FILE)) as handle:
            return json.load(handle)

    def apply(self, *extra, **kwargs):
        api = kwargs.pop("api", None) or self.new_api()
        code, out = self.run_cli(api, "--apply", *extra, **kwargs)
        return api, code, out


class PlanOnlyTests(SeedPlanCase):
    def test_plan_only_needs_no_api_no_token_no_flag_no_map(self):
        def no_api(token):
            raise AssertionError("the API must not be used")

        stream = io.StringIO()
        code = seed.main(["--xlsx", self.xlsx, "--prefix-map", self.prefix_path, "--plan-only", "--vms", "4"],
                         api_factory=no_api, stream=stream, environ={})
        self.assertEqual(code, 0)
        out = stream.getvalue()
        self.assertIn("LAB PLAN SUMMARY", out)
        self.assertIn("devices chosen ............. 4", out)
        self.assertIn("anomaly .................... covered", out)
        for marker in MARKERS:
            self.assertNotIn(marker, out)

    def test_plan_only_with_waves_prints_wave_sizes(self):
        stream = io.StringIO()
        seed.main(["--xlsx", self.xlsx, "--prefix-map", self.prefix_path, "--plan-only", "--vms", "2",
                   "--waves", "3"], stream=stream)
        self.assertIn("waves requested/used ....... 3 / 3 (sizes: 2, 2, 2)", stream.getvalue())

    def test_plan_only_without_scope_is_refused(self):
        code = seed.main(["--plan-only"], stream=io.StringIO())
        self.assertEqual(code, 2)

    def test_plan_from_a_plan_file(self):
        plan_file = os.path.join(self.tmp, "p.json")
        lab_plan.write_private_json(plan_file, self.plan.to_dict(lab_plan.build_lab(self.plan, 3)))
        stream = io.StringIO()
        code = seed.main(["--plan", plan_file, "--plan-only", "--vms", "3"], stream=stream)
        self.assertEqual(code, 0)
        self.assertIn("devices chosen ............. 3", stream.getvalue())


class DryRunTests(SeedPlanCase):
    def test_dry_run_makes_no_write_call(self):
        api = self.new_api()
        code, out = self.run_cli(api)
        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])
        self.assertIn("would create", out)
        self.assertIn("would share agent", out)
        data = self.result()
        self.assertEqual(data["mode"], "dry-run")
        self.assertEqual(data["created"]["tests"], [])
        self.assertGreater(len(data["planned"]["tests"]), 20)
        self.assertEqual(len(data["planned"]["alert_rules"]), 5)  # 5 groups have a lab device
        self.assertEqual(self.last_sleeps, [])
        self.assertTrue(api.closed)

    def test_dry_run_guard_blocks_a_write(self):
        api = self.new_api()
        console = seed.SeedConsole(stream=io.StringIO(), use_color=False)
        plan = self.plan
        cfg = lab_seed.parse_lab_map(self.map_data())
        seeder = lab_seed.PlanSeeder(api, cfg, console, plan, ["SBX-D001"], apply=False)
        with self.assertRaises(RuntimeError):
            seeder.api.create_test("7001", "http-server", {})
        self.assertEqual(api.writes, [])

    def test_dry_run_with_a_missing_vm_uses_a_placeholder(self):
        api = self.new_api()
        for info in api.agents.values():
            info["ip"] = "198.51.100.1"
            info["name"] = "other"
        code, out = self.run_cli(api)
        self.assertEqual(code, 0)
        self.assertIn("no VM found in the source group (placeholder id used", out)
        self.assertEqual(api.writes, [])

    def test_missing_cli_flag_and_missing_map(self):
        api = self.new_api()
        code, out = self.run_cli(api, "--apply", ack=False)
        self.assertEqual(code, 2)
        self.assertIn("--i-know-this-is-sandbox", out)
        self.assertEqual((api.writes, api.reads), ([], []))
        self.assertEqual(seed.main(["--i-know-this-is-sandbox"], stream=io.StringIO()), 2)

    def test_token_is_never_printed_or_stored(self):
        _api, _code, out = self.apply()
        self.assertNotIn(FAKE_API_TOKEN, out)
        with open(os.path.join(self.out_dir, seed.RESULT_FILE)) as handle:
            self.assertNotIn(FAKE_API_TOKEN, handle.read())


class ApplyTests(SeedPlanCase):
    def test_apply_creates_the_lab(self):
        api, code, out = self.apply("--max-tests-per-group", "10")
        self.assertEqual(code, 0, out)
        for tests in api.tests.values():
            for name in tests:
                self.assertTrue(name.startswith("SBX-G"))
        source = api.tests["7001"]
        self.assertIn("SBX-G01-http-server-01", source)
        self.assertEqual(len([n for n in source if "-http-server-" in n]), 8)
        self.assertEqual(source["SBX-G01-http-server-01"].test_type, "http-server")
        self.assertEqual(sorted(api.tests["7006"]), [])  # a group that sees no lab device gets nothing
        # tests of group 2 (42 real tests) are scaled to 10, in the ratio of the real mix
        group2 = [r for r in api.tests["7002"].values()]
        self.assertEqual(len(group2), 10)
        self.assertEqual(len([r for r in group2 if r.test_type == "agent-to-server"]), 9)
        self.assertEqual(len([r for r in group2 if r.test_type == "agent-to-agent"]), 1)
        self.assertIn("not seeded: test types without a known payload shape (voice x3)", out)
        self.assertIn("not seeded: 1 group(s) with 3 real tests have no lab device", out)
        self.assertIn("not seeded: dashboards and suppression windows", out)

    def test_every_test_is_assigned_to_the_devices_shared_into_its_group(self):
        api, _code, _out = self.apply()
        by_id = dict((i, info["name"]) for i, info in api.agents.items())
        source = api.tests["7001"]["SBX-G01-http-server-01"]
        self.assertEqual(len(source.payload["agents"]), 6)  # all six lab devices see the source group
        group3 = api.tests["7003"]  # group 3 is shared to the devices 1, 5 and 6 only
        for record in group3.values():
            ids = [a["agentId"] for a in record.payload["agents"]]
            self.assertTrue(0 < len(ids) <= 3)
        names_in_g3 = set(by_id[a["agentId"]] for r in group3.values() for a in r.payload["agents"])
        self.assertEqual(len(names_in_g3), 3)

    def test_shares_send_the_full_list_with_the_owner_first(self):
        api, _code, _out = self.apply()
        shares = [w for w in api.writes if w[0] == "set_agent_account_groups"]
        self.assertEqual(len(shares), 6)
        for write in shares:
            self.assertEqual(write[1], "7001")  # the call uses the owner group
            self.assertEqual(write[2][1][0], "7001")
            self.assertTrue(len(write[2][1]) >= 2)
        by_vm = dict((api.agents[w[2][0]]["name"], w[2][1]) for w in shares)
        self.assertEqual(sorted(by_vm["sbx-vm-01"]), sorted(["7001", "7002", "7003"]))  # source, target, Y
        # the anomaly device (D006) is shared to Y and the source, NOT to its target group (X = 7004)
        self.assertNotIn("7004", by_vm["sbx-vm-06"])

    def test_alert_rule_per_group_and_attached_to_http_tests_only(self):
        api, _code, _out = self.apply()
        names = []
        for aid in ("7001", "7002", "7003", "7004", "7005"):
            self.assertEqual(len(api.rules[aid]), 1)
            names.append(api.rules[aid][0]["ruleName"])
        self.assertEqual(names, ["SBX-G0%d-ALERT" % n for n in range(1, 6)])
        self.assertEqual(api.rules["7006"], [])
        updates = [w for w in api.writes if w[0] == "update_test"]
        http_tests = [r for tests in api.tests.values() for r in tests.values() if r.test_type == "http-server"]
        self.assertEqual(len(updates), len(http_tests))
        for record in http_tests:
            self.assertEqual(len(record.alert_rules), 1)
        for tests in api.tests.values():
            for record in tests.values():
                if record.test_type != "http-server":
                    self.assertEqual(record.alert_rules, [])
        # a test made in this run is not read again before the attach
        self.assertNotIn("get_test", api.reads)

    def test_lab_tag_on_every_device(self):
        api, _code, _out = self.apply()
        creates = [w for w in api.writes if w[0] == "create_tag"]
        self.assertEqual([c[2] for c in creates], ["sbx-lab=agents"])
        self.assertEqual(creates[0][1], "7001")
        tag = api.tags[0]
        self.assertEqual(tag["objectType"], "agent")
        self.assertEqual(sorted(a["id"] for a in tag["assignments"]), sorted(api.agents))
        self.assertEqual(set(a["type"] for a in tag["assignments"]), set(["v-agent"]))

    def test_agent_to_agent_test_targets_a_lab_device(self):
        api, _code, _out = self.apply()
        a2a = [r for tests in api.tests.values() for r in tests.values() if r.test_type == "agent-to-agent"]
        self.assertTrue(a2a)
        for record in a2a:
            target = record.payload["targetAgentId"]
            self.assertIn(target, api.agents)
            sources = [a["agentId"] for a in record.payload["agents"]]
            self.assertNotIn(target, sources)
            self.assertTrue(1 <= len(sources) <= lab_seed.A2A_MAX_SOURCES)
        # the source group has no agent-to-agent test in its real mix: one extra test is made
        self.assertIn("SBX-G01-agent-to-agent-LAB-01", api.tests["7001"])

    def test_test_names_and_targets_are_safe(self):
        api, _code, _out = self.apply()
        for tests in api.tests.values():
            for record in tests.values():
                payload = record.payload
                self.assertTrue(payload["testName"].startswith("SBX-"))
                self.assertEqual(payload["interval"], lab_seed.LAB_INTERVAL)
                if record.test_type != "agent-to-agent":
                    text = json.dumps(payload)
                    self.assertTrue("example.com" in text or "iana-servers.net" in text)

    def test_fake_credentials_not_printed_or_stored(self):
        made = []

        def token_hex(nbytes):
            value = "%02x" % (len(made) + 40) * nbytes
            made.append(value)
            return value

        api, code, out = self.apply(token_hex=token_hex)
        self.assertEqual(code, 0)
        self.assertEqual(len(made), 2)  # one web-transactions test in the lab: two fake values
        record = [r for r in api.tests["7003"].values() if r.test_type == "web-transactions"][0]
        for value in made:
            self.assertIn(value, record.payload["transactionScript"])
            self.assertNotIn(value, out)
            with open(os.path.join(self.out_dir, seed.RESULT_FILE)) as handle:
                self.assertNotIn(value, handle.read())
            with open(os.path.join(self.out_dir, lab_seed.LAB_MANUAL_FILE)) as handle:
                self.assertNotIn(value, handle.read())

    def test_second_run_is_idempotent(self):
        api, code, _out = self.apply()
        self.assertEqual(code, 0)
        first = len(api.writes)
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(api.writes), first)
        self.assertIn("already exists", out)

    def test_manual_tasks_file_lists_the_dashboards(self):
        _api, _code, _out = self.apply()
        with open(os.path.join(self.out_dir, lab_seed.LAB_MANUAL_FILE)) as handle:
            text = handle.read()
        self.assertIn("SBX-G01-DASH-1-AGENT-ID", text)
        self.assertIn("BREAKS (filter_agent_id)", text)
        self.assertIn("AGENT TAG sbx-lab=agents", text)
        self.assertIn("GROUP BY: Agent", text)
        self.assertIn("Alert List", text)
        self.assertIn("as TARGET. Expected flag: BREAKS", text)
        self.assertIn("suppression window SBX-G01-SUPPRESS", text)
        for marker in MARKERS[:1]:
            self.assertNotIn(marker, text)

    def test_clusters_step_is_off_by_default(self):
        _api, _code, _out = self.apply()
        self.assertNotIn("clusters", self.result()["steps"])
        self.assertEqual(lab_seed.parse_plan_steps(None), ["shares", "tests", "tags", "alerts", "report"])
        self.assertEqual(lab_seed.parse_plan_steps(None, True)[0], "clusters")
        with self.assertRaises(seed.SafetyGate):
            lab_seed.parse_plan_steps("bogus")
        api = self.new_api()
        code, _out = self.run_cli(api, "--apply", "--with-clusters")
        self.assertEqual(code, 0)
        self.assertEqual(self.result()["steps"][0], "clusters")
        self.assertFalse([w for w in api.writes if w[0] == "cluster_assign"])

    def test_missing_vm_is_a_hard_error_with_apply(self):
        api = self.new_api()
        for info in api.agents.values():
            info["ip"] = "198.51.100.1"
            info["name"] = "other"
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(api.writes, [])
        self.assertIn("no VM found", out)

    def test_hard_error_stops_with_a_state_line(self):
        api = self.new_api()
        original = api.create_test
        counter = [0]

        def failing(aid, test_type, payload):
            counter[0] += 1
            if counter[0] == 4:
                raise ApiError("API POST tests/x returned HTTP 400 (request-id=x)")
            return original(aid, test_type, payload)

        api.create_test = failing
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("STATE", out)
        self.assertIn("tests=3", out)
        self.assertEqual(self.result()["exit_code"], 1)


class GuardTests(SeedPlanCase):
    def test_non_sbx_alias_in_the_map_is_refused(self):
        data = self.map_data()
        data["groups"]["PROD-G01"] = data["groups"].pop("SBX-G01")
        self.write_map(data)
        api = self.new_api()
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", out)
        self.assertEqual((api.writes, api.reads), ([], []))

    def test_group_not_visible_with_token_is_refused(self):
        api = self.new_api()
        del api.group_names["7003"]
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 2)
        self.assertEqual(api.writes, [])

    def test_group_name_without_sbx_prefix_gives_a_warning(self):
        api = self.new_api()
        api.group_names["7002"] = "PROD-name"
        code, out = self.run_cli(api)
        self.assertEqual(code, 0)
        self.assertIn("does not start with SBX-", out)

    def test_missing_alias_in_the_map_is_refused(self):
        data = self.map_data()
        del data["groups"]["SBX-G03"]
        self.write_map(data)
        api = self.new_api()
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("SBX-G03", out)
        self.assertEqual((api.writes, api.reads), ([], []))

    def test_too_few_vms_in_the_map_is_refused(self):
        data = self.map_data()
        data["vms"] = data["vms"][:3]
        self.write_map(data)
        api = self.new_api()
        code, out = self.run_cli(api, "--apply", "--vms", "6")
        self.assertEqual(code, 2)
        self.assertIn("lists 3 VMs", out)

    def test_max_calls_guard_refuses_before_any_call(self):
        api = self.new_api()
        code, out = self.run_cli(api, "--apply", "--max-calls", "10")
        self.assertEqual(code, 2)
        self.assertIn("--max-calls", out)
        self.assertEqual((api.writes, api.reads), ([], []))

    def test_estimate_is_an_upper_bound_of_the_real_calls(self):
        api, code, out = self.apply("--max-calls", "0")
        self.assertEqual(code, 0)
        calls = self.result()["calls"]
        self.assertGreater(calls, 0)
        self.assertEqual(calls, len(api.reads) + len(api.writes))
        self.assertIn("estimated API calls", out)
        wave = lab_plan.choose_archetypes(self.plan, 6)["devices"]
        tests, _notes = lab_seed.plan_group_tests(self.plan, wave, 25)
        groups = lab_seed.groups_in_scope(self.plan, wave)
        self.assertGreaterEqual(lab_seed.estimate_calls(self.plan, wave, tests, groups), calls)

    def test_call_budget_stops_the_run(self):
        api = self.new_api()
        budget = lab_seed.CallBudget(api, 2)
        budget.list_account_groups()
        budget.list_account_groups()
        with self.assertRaises(seed.SeedError):
            budget.list_account_groups()
        self.assertEqual(len(api.reads), 2)
        unlimited = lab_seed.CallBudget(api, 0)
        for _ in range(5):
            unlimited.list_account_groups()
        budget.close()
        self.assertTrue(api.closed)

    def test_run_stops_when_the_budget_ends_during_the_run(self):
        api = self.new_api()
        code, out = self.run_cli(api, "--apply", "--max-calls", "1000")
        self.assertEqual(code, 0)
        # same run, but the guard is patched to a smaller limit than the estimate
        api2 = self.new_api()
        original = lab_seed.estimate_calls
        lab_seed.estimate_calls = lambda *args, **kwargs: 1
        self.addCleanup(setattr, lab_seed, "estimate_calls", original)
        code, out = self.run_cli(api2, "--apply", "--max-calls", "30")
        self.assertEqual(code, 1)
        self.assertIn("max calls (30) reached", out)
        self.assertLessEqual(len(api2.reads) + len(api2.writes), 30)

    def test_wave_selection(self):
        api = self.new_api()
        code, _out = self.run_cli(api, "--waves", "3", "--vms", "2", "--wave", "2")
        self.assertEqual(code, 0)
        self.assertEqual(self.result()["wave_devices"], 2)
        split = lab_plan.split_waves(self.plan, 2, 3)["waves"]
        planned = set(w["name"] for w in self.result()["planned"]["tests"])
        self.assertTrue(planned)
        self.assertEqual(self.run_cli(self.new_api(), "--waves", "3", "--vms", "2", "--wave", "4")[0], 2)
        self.assertEqual(self.run_cli(self.new_api(), "--wave", "2")[0], 2)  # --wave needs --waves
        self.assertEqual(len(split), 3)

    def test_wave_devices_map_to_the_first_vms_in_alias_order(self):
        api = self.new_api()
        code, _out = self.run_cli(api, "--apply", "--waves", "3", "--vms", "2", "--wave", "2")
        self.assertEqual(code, 0)
        shares = [w for w in api.writes if w[0] == "set_agent_account_groups"]
        self.assertEqual(len(shares), 2)
        names = sorted(api.agents[w[2][0]]["name"] for w in shares)
        self.assertEqual(names, ["sbx-vm-01", "sbx-vm-02"])


class VerifyTests(SeedPlanCase):
    def seeded(self):
        api = self.new_api()
        self.assertEqual(self.run_cli(api, "--apply")[0], 0)
        del api.writes[:]
        del api.reads[:]
        return api

    def test_verify_ok_after_a_seed(self):
        api = self.seeded()
        code, out = self.run_cli(api, "--verify")
        self.assertEqual(code, 0, out)
        self.assertEqual(api.writes, [])
        self.assertEqual(len([line for line in out.splitlines() if "verify: OK SBX-D" in line]), 6)
        self.assertNotIn("MISMATCH SBX-D", out)
        self.assertIn("6 device(s) OK, 0 MISMATCH", out)
        self.assertIn("shares 3 of 3", out)
        self.assertEqual(self.result()["steps"], ["verify"])

    def test_verify_never_writes_even_with_apply(self):
        api = self.seeded()
        code, _out = self.run_cli(api, "--verify", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])

    def test_verify_reports_a_missing_share_and_a_missing_test(self):
        api = self.seeded()
        vm1 = [i for i, info in api.agents.items() if info["name"] == "sbx-vm-01"][0]
        api.agents[vm1]["shares"] = ["7001"]
        del api.tests["7001"]["SBX-G01-http-server-01"]
        code, out = self.run_cli(api, "--verify")
        self.assertEqual(code, 1)
        mismatches = [line for line in out.splitlines() if "MISMATCH SBX-D" in line]
        self.assertTrue(mismatches)
        self.assertTrue(any("not shared with" in line for line in mismatches))
        self.assertTrue(any("SBX-G01 tests 7 of 8" in line for line in mismatches))
        self.assertEqual([r["ok"] for r in self.result()["verify"]].count(False), len(mismatches))
        self.assertEqual(api.writes, [])

    def test_verify_on_an_empty_sandbox_is_all_mismatch(self):
        api = self.new_api()
        code, out = self.run_cli(api, "--verify")
        self.assertEqual(code, 1)
        self.assertIn("0 device(s) OK, 6 MISMATCH", out)

    def test_verify_with_a_missing_vm(self):
        api = self.seeded()
        for info in api.agents.values():
            info["name"] = "other"
            info["ip"] = "198.51.100.1"
        code, out = self.run_cli(api, "--verify")
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH SBX-D", out)
        self.assertIn("no VM found", out)


class CleanupTests(SeedPlanCase):
    def test_cleanup_deletes_only_sbx_objects(self):
        api = self.new_api()
        self.assertEqual(self.run_cli(api, "--apply")[0], 0)
        record = TestRecord("keep1", "PROD-keep-me", "http-server", "7001", True, [], raw={})
        api.tests["7001"]["PROD-keep-me"] = record
        api.rules["7002"].append({"ruleId": "keeprule", "ruleName": "Default HTTP rule"})
        api.tags.append({"tagId": "keeptag", "key": "env", "value": "prod", "objectType": "agent",
                         "assignments": []})
        del api.writes[:]
        code, _out = self.run_cli(api, "--cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])  # dry run
        code, _out = self.run_cli(api, "--cleanup", "--apply")
        self.assertEqual(code, 0)
        names = [n for tests in api.tests.values() for n in tests]
        self.assertEqual(names, ["PROD-keep-me"])
        self.assertEqual([t["tagId"] for t in api.tags], ["keeptag"])
        self.assertEqual([r["ruleId"] for rules in api.rules.values() for r in rules], ["keeprule"])
        kinds = set(w[0] for w in api.writes)
        self.assertEqual(kinds, set(["delete_test", "request DELETE"]))

    def test_cleanup_without_the_flag_is_refused(self):
        api = self.new_api()
        code, _out = self.run_cli(api, "--cleanup", "--apply", ack=False)
        self.assertEqual(code, 2)
        self.assertEqual(api.writes, [])


class GenericLayoutTests(unittest.TestCase):
    def test_generic_layout_is_still_available(self):
        parser = seed.build_parser()
        self.assertEqual(parser.parse_args([]).layout, "plan")
        self.assertEqual(parser.parse_args(["--layout", "generic"]).layout, "generic")
        stream = io.StringIO()
        code = seed.main(["--layout", "generic", "--i-know-this-is-sandbox"], stream=stream)
        self.assertEqual(code, 2)
        self.assertIn("--map is required", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
