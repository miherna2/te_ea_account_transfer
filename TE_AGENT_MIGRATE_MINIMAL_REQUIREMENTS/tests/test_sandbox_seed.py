"""Tests for sandbox/seed_sandbox.py. A fake API object is used: no network, no real API call."""

import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sandbox import seed_sandbox as seed
from te_agent_migrate.errors import ApiError
from te_agent_migrate.models import AccountGroup, AgentRecord, TestRecord

FAKE_API_TOKEN = "fake-sandbox-api-value"  # not a credential: only proves that the token is never printed

GROUPS = {"SOURCE": "1001", "TARGET": "1002", "VIEW1": "1003", "VIEW2": "1004", "OTHER": "1005"}
AGENTS = {"S1": "2001", "S2": "2002", "S3": "2003", "S4": "2004", "S5": "2005", "S6": "2006", "X1": "2007"}
CLUSTERS = {"C1": ["3001", "3002"], "C2": ["3003", "3004", "3005"]}


def good_map():
    return {
        "sandbox": True,
        "groups": dict(GROUPS),
        "agents": dict(AGENTS),
        "clusters": dict((k, {"members": list(v)}) for k, v in CLUSTERS.items()),
    }


class FakeApi(object):
    """Keeps state like the real API for the calls that the seeder uses. Records every write."""

    def __init__(self, fail_on_create_number=None):
        self.writes = []  # (method name, aid, detail)
        self.reads = []
        self.closed = False
        self.fail_on_create_number = fail_on_create_number
        self.create_count = 0
        self.next_id = 5000
        self.tests = dict((aid, {}) for aid in GROUPS.values())
        self.tags = []
        self.rules = []
        self.shares = {}
        self.cluster_of = {}  # cluster id -> member ids
        self.group_names = dict((aid, "SBX-" + label) for label, aid in GROUPS.items())

    def _new_id(self):
        self.next_id += 1
        return str(self.next_id)

    # ---------------------------------------------------------------- reads
    def list_account_groups(self):
        self.reads.append("list_account_groups")
        return [AccountGroup(aid, name) for aid, name in self.group_names.items()]

    def _agent_dict(self, agent_id, aid=GROUPS["SOURCE"]):
        return {"agentId": agent_id, "agentName": "agent-" + agent_id, "agentType": "enterprise",
                "agentState": "online", "accountGroups": [{"aid": a} for a in self.shares.get(agent_id, [aid])]}

    def list_agents_with_clusters(self, aid):
        self.reads.append("list_agents_with_clusters")
        clustered = set(m for members in self.cluster_of.values() for m in members)
        standalone = [v for k, v in AGENTS.items() if k != "X1"]
        for members in CLUSTERS.values():
            standalone.extend(members)
        result = [AgentRecord.from_api(self._agent_dict(a), aid) for a in standalone if a not in clustered]
        for base, members in self.cluster_of.items():
            raw = {"agentId": base, "agentName": "cluster-" + base, "agentType": "enterprise-cluster",
                   "clusterMembers": [{"memberId": m, "agentState": "online"} for m in members]}
            result.append(AgentRecord.from_api(raw, aid))
        return result

    def get_agent(self, aid, agent_id):
        self.reads.append("get_agent")
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
        return copy.deepcopy(self.rules)

    def request(self, method, path, aid=None, params=None, json_body=None, expected=None, safe_to_retry=None):
        if method.upper() == "GET":
            self.reads.append("request GET " + path)
            return {}
        self.writes.append(("request " + method.upper(), aid, path))
        if path.startswith("tags/"):
            self.tags = [t for t in self.tags if t["tagId"] != path.split("/")[1]]
        if path.startswith("alerts/rules/"):
            self.rules = [r for r in self.rules if r["ruleId"] != path.split("/")[2]]
        return {}

    # ---------------------------------------------------------------- writes
    def add_test(self, aid, name, test_type="http-server", agents=None):
        """Test helper: put a test in the fake account without a write record."""
        test_id = self._new_id()
        raw = {"testId": test_id, "testName": name, "type": test_type, "enabled": True}
        self.tests[str(aid)][name] = TestRecord(test_id, name, test_type, aid, True, agents or [], raw=raw)
        return test_id

    def create_test(self, aid, test_type, payload):
        self.create_count += 1
        if self.fail_on_create_number and self.create_count == self.fail_on_create_number:
            raise ApiError("API POST tests/%s returned HTTP 400 (request-id=x)" % test_type)
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
        self.rules.append(rule)
        return {"ruleId": rule["ruleId"]}

    def set_agent_account_groups(self, aid, agent_id, account_group_ids):
        self.writes.append(("set_agent_account_groups", aid, (agent_id, list(account_group_ids))))
        if not account_group_ids:
            raise ApiError("empty list")
        self.shares[agent_id] = list(account_group_ids)
        return {}

    def cluster_assign(self, aid, base_agent_id, agent_ids):
        self.writes.append(("cluster_assign", aid, (base_agent_id, list(agent_ids))))
        self.cluster_of[base_agent_id] = [base_agent_id] + list(agent_ids)
        return {}

    def close(self):
        self.closed = True

    # ---------------------------------------------------------------- guards: never called by the seeder
    def delete_agent(self, *args, **kwargs):
        self.writes.append(("delete_agent", None, args))
        raise AssertionError("agents must never be deleted")


class SeedTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbxtest")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.map_path = os.path.join(self.tmp, "map.json")
        self.out_dir = os.path.join(self.tmp, "out")
        self.write_map(good_map())

    def write_map(self, data):
        with open(self.map_path, "w") as handle:
            json.dump(data, handle)

    def run_cli(self, api, *extra, **kwargs):
        argv = ["--map", self.map_path, "--output", self.out_dir]
        if kwargs.get("ack", True):
            argv.append("--i-know-this-is-sandbox")
        argv.extend(extra)
        stream = io.StringIO()
        sleeps = []
        code = seed.main(
            argv,
            api_factory=lambda token: api,
            sleep=sleeps.append,
            stream=stream,
            environ=kwargs.get("environ", {"TE_OAUTH_TOKEN": FAKE_API_TOKEN}),
            token_hex=kwargs.get("token_hex"),
        )
        self.last_sleeps = sleeps
        return code, stream.getvalue()

    def result(self):
        with open(os.path.join(self.out_dir, seed.RESULT_FILE)) as handle:
            return json.load(handle)


def first_index(writes, name):
    for index, item in enumerate(writes):
        if item[0] == name:
            return index
    return None


class MapValidationTests(SeedTestCase):
    def test_good_map_parses(self):
        cfg = seed.parse_map(good_map())
        self.assertEqual(cfg.groups["SOURCE"], "1001")
        self.assertEqual(cfg.clusters["C1"], ["3001", "3002"])

    def test_missing_sandbox_flag_in_map(self):
        data = good_map()
        del data["sandbox"]
        with self.assertRaises(seed.SafetyGate):
            seed.parse_map(data)
        data["sandbox"] = "true"  # a string is not enough
        with self.assertRaises(seed.SafetyGate):
            seed.parse_map(data)
        self.write_map(data)
        api = FakeApi()
        code, _out = self.run_cli(api, "--apply")
        self.assertEqual(code, 2)
        self.assertEqual(api.writes, [])

    def test_missing_cli_flag(self):
        api = FakeApi()
        code, out = self.run_cli(api, "--apply", ack=False)
        self.assertEqual(code, 2)
        self.assertIn("--i-know-this-is-sandbox", out)
        self.assertEqual(api.writes, [])
        self.assertEqual(api.reads, [])

    def test_duplicate_group_ids(self):
        data = good_map()
        data["groups"]["VIEW2"] = data["groups"]["VIEW1"]
        with self.assertRaises(seed.SafetyGate):
            seed.parse_map(data)

    def test_source_equals_target(self):
        data = good_map()
        data["groups"]["TARGET"] = data["groups"]["SOURCE"]
        with self.assertRaises(seed.SafetyGate) as ctx:
            seed.parse_map(data)
        self.assertIn("SOURCE and TARGET", str(ctx.exception))
        self.write_map(data)
        code, _out = self.run_cli(FakeApi(), "--apply")
        self.assertEqual(code, 2)

    def test_unknown_group(self):
        data = good_map()
        data["groups"]["PROD"] = "9999"
        with self.assertRaises(seed.SafetyGate):
            seed.parse_map(data)

    def test_missing_required_group_agent_or_cluster(self):
        for section, key in (("groups", "VIEW1"), ("agents", "S3"), ("clusters", "C1")):
            data = good_map()
            del data[section][key]
            with self.assertRaises(seed.SafetyGate):
                seed.parse_map(data)

    def test_repeated_agent_id(self):
        data = good_map()
        data["clusters"]["C1"]["members"][0] = data["agents"]["S1"]
        with self.assertRaises(seed.SafetyGate):
            seed.parse_map(data)

    def test_group_not_visible_with_token_is_refused(self):
        api = FakeApi()
        del api.group_names[GROUPS["VIEW2"]]
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", out)
        self.assertEqual(api.writes, [])

    def test_unknown_step_and_no_token_are_refused(self):
        self.assertEqual(self.run_cli(FakeApi(), "--steps", "tests,bogus")[0], 2)
        api = FakeApi()
        with mock.patch.object(seed.getpass, "getpass", return_value=""):
            self.assertEqual(self.run_cli(api, "--apply", environ={})[0], 2)  # no token, empty prompt
        self.assertEqual(api.writes, [])
        with self.assertRaises(seed.SafetyGate):
            seed.parse_steps("nope")
        self.assertEqual(seed.parse_steps("alerts,tests"), ["tests", "alerts"])  # fixed order


class DryRunTests(SeedTestCase):
    def test_dry_run_makes_zero_writes(self):
        api = FakeApi()
        code, out = self.run_cli(api)
        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])
        self.assertIn("changed:  [SBX-S1-HTTP-01] => would create http-server on S1", out)
        self.assertEqual(self.last_sleeps, [])
        data = self.result()
        self.assertEqual(data["mode"], "dry-run")
        self.assertEqual(len(data["planned"]["tests"]), 195)
        self.assertEqual(data["created"]["tests"], [])

    def test_dry_run_guard_blocks_a_write(self):
        api = FakeApi()
        console = seed.SeedConsole(stream=io.StringIO(), use_color=False)
        seeder = seed.Seeder(api, seed.parse_map(good_map()), console, apply=False)
        with self.assertRaises(RuntimeError):
            seeder.api.create_test("1001", "http-server", {})
        with self.assertRaises(RuntimeError):
            seeder.api.request("DELETE", "tests/1")
        self.assertEqual(api.writes, [])

    def test_dry_run_writes_report_files_but_no_api_write(self):
        api = FakeApi()
        code, _out = self.run_cli(api)
        self.assertEqual(code, 0)
        path = os.path.join(self.out_dir, seed.MANUAL_TASKS_FILE)
        with open(path) as handle:
            text = handle.read()
        for dashboard in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"):
            self.assertIn("| %s |" % dashboard, text)
        self.assertIn("Agent notification rule on S1", text)
        self.assertIn("Suppression window", text)
        self.assertEqual(oct(os.stat(self.out_dir).st_mode & 0o777), oct(0o700))

    def test_token_is_never_printed_or_stored(self):
        api = FakeApi()
        _code, out = self.run_cli(api, "--apply")
        self.assertNotIn(FAKE_API_TOKEN, out)
        with open(os.path.join(self.out_dir, seed.RESULT_FILE)) as handle:
            self.assertNotIn(FAKE_API_TOKEN, handle.read())


class ApplyTests(SeedTestCase):
    def apply_all(self, api=None, **kwargs):
        api = api or FakeApi()
        code, out = self.run_cli(api, "--apply", **kwargs)
        return api, code, out

    def test_apply_order(self):
        api, code, _out = self.apply_all()
        self.assertEqual(code, 0)
        order = [first_index(api.writes, name) for name in (
            "cluster_assign", "set_agent_account_groups", "create_test", "create_tag", "create_alert_rule")]
        self.assertNotIn(None, order)
        self.assertEqual(order, sorted(order))
        self.assertLess(first_index(api.writes, "assign_tag"), first_index(api.writes, "create_alert_rule"))
        self.assertLess(first_index(api.writes, "create_alert_rule"), first_index(api.writes, "update_test"))
        last_create = max(i for i, w in enumerate(api.writes) if w[0] == "create_test")
        self.assertLess(last_create, first_index(api.writes, "create_tag"))
        self.assertEqual(self.result()["mode"], "apply")

    def test_clusters_are_built_from_members(self):
        api, _code, _out = self.apply_all()
        calls = [w for w in api.writes if w[0] == "cluster_assign"]
        self.assertEqual([(c[1], c[2]) for c in calls], [
            ("1001", ("3001", ["3002"])),
            ("1001", ("3003", ["3004", "3005"])),
        ])

    def test_shares_send_the_full_list_never_empty(self):
        api, _code, _out = self.apply_all()
        shares = [w for w in api.writes if w[0] == "set_agent_account_groups"]
        self.assertEqual(len(shares), 4)  # S3, C1, C2, X1
        by_agent = dict((w[2][0], (w[1], w[2][1])) for w in shares)
        for agent_id in ("2003", "3001", "3003"):
            aid, groups = by_agent[agent_id]
            self.assertEqual(aid, "1001")
            self.assertEqual(groups, ["1001", "1003", "1004"])
        aid, groups = by_agent["2007"]  # X1: owned by OTHER, call with aid=OTHER
        self.assertEqual(aid, "1005")
        self.assertEqual(groups, ["1005", "1001"])
        for w in shares:
            self.assertTrue(w[2][1])

    def test_share_keeps_existing_shares(self):
        api = FakeApi()
        api.shares["2003"] = ["1001", "7777"]
        self.apply_all(api)
        call = [w for w in api.writes if w[0] == "set_agent_account_groups" and w[2][0] == "2003"][0]
        self.assertEqual(call[2][1], ["1001", "1003", "1004", "7777"])

    def test_counts_per_set(self):
        api, code, _out = self.apply_all()
        self.assertEqual(code, 0)

        def count(group, prefix):
            return len([n for n in api.tests[GROUPS[group]] if n.startswith(prefix)])

        expected = [
            ("SOURCE", "SBX-S1-HTTP-", 5), ("SOURCE", "SBX-S2-HTTP-", 5), ("SOURCE", "SBX-S3-NET-", 10),
            ("SOURCE", "SBX-A2A-S1-S4-", 2), ("SOURCE", "SBX-C1-HTTP-", 30), ("SOURCE", "SBX-C1-BULK-", 120),
            ("SOURCE", "SBX-S2-DNS-", 3), ("SOURCE", "SBX-S2-PAGE-", 1), ("SOURCE", "SBX-S1-TXN-", 1),
            ("SOURCE", "SBX-S5-", 2), ("SOURCE", "SBX-C1-DISABLED-", 3),
            ("VIEW1", "SBX-V1-S3-", 5), ("VIEW1", "SBX-V1-C1-", 5), ("VIEW2", "SBX-V2-C1-", 3),
        ]
        for group, prefix, number in expected:
            self.assertEqual(count(group, prefix), number, "%s %s" % (group, prefix))
        self.assertEqual(len(api.tests[GROUPS["SOURCE"]]), 182)
        self.assertEqual(len(api.tests[GROUPS["VIEW1"]]), 10)
        self.assertEqual(len(api.tests[GROUPS["VIEW2"]]), 3)
        self.assertEqual(len(api.tests[GROUPS["TARGET"]]), 0)
        self.assertEqual(len(api.tests[GROUPS["OTHER"]]), 0)
        for tests in api.tests.values():
            for name in tests:
                self.assertTrue(name.startswith("SBX-"))

    def test_bulk_set_is_in_one_group_on_c1(self):
        api, _code, _out = self.apply_all()
        bulk = [r for r in api.tests[GROUPS["SOURCE"]].values() if r.name.startswith("SBX-C1-BULK-")]
        self.assertEqual(len(bulk), 120)
        self.assertGreater(len(bulk), seed.ASSIGN_CHUNK)
        for record in bulk:
            self.assertEqual(record.payload["agents"], [{"agentId": "3001"}])
            self.assertEqual(record.test_type, "http-server")
        for group in ("VIEW1", "VIEW2", "TARGET", "OTHER"):
            self.assertFalse([n for n in api.tests[GROUPS[group]] if n.startswith("SBX-C1-BULK-")])

    def test_test_shapes(self):
        api, _code, _out = self.apply_all()
        source = api.tests[GROUPS["SOURCE"]]
        a2a = source["SBX-A2A-S1-S4-01"]
        self.assertEqual(a2a.test_type, "agent-to-agent")
        self.assertEqual(a2a.payload["agents"], [{"agentId": "2001"}])
        self.assertEqual(a2a.payload["targetAgentId"], "2004")
        for index in (1, 2, 3):
            self.assertFalse(source["SBX-C1-DISABLED-%02d" % index].payload["enabled"])
        self.assertEqual(source["SBX-S5-HTTP-01"].payload["agents"], [{"agentId": "2005"}])
        self.assertEqual(source["SBX-S1-HTTP-01"].payload["interval"], 300)
        self.assertEqual(source["SBX-S2-DNS-01"].test_type, "dns-server")
        self.assertEqual(source["SBX-S2-PAGE-01"].test_type, "page-load")
        self.assertEqual(source["SBX-S3-NET-01"].payload["protocol"], "icmp")
        self.assertEqual(source["SBX-S3-NET-02"].payload["protocol"], "tcp")
        self.assertEqual(api.tests[GROUPS["VIEW1"]]["SBX-V1-S3-01"].payload["agents"], [{"agentId": "2003"}])
        self.assertEqual(api.tests[GROUPS["VIEW2"]]["SBX-V2-C1-01"].payload["agents"], [{"agentId": "3001"}])
        for tests in api.tests.values():
            for record in tests.values():
                payload = record.payload
                self.assertTrue(payload["testName"].startswith("SBX-"))
                if record.test_type == "agent-to-agent":
                    continue  # target is an agent, not a host
                text = json.dumps(payload)
                self.assertTrue("example.com" in text or "iana-servers.net" in text)

    def test_builders_have_the_minimal_fields(self):
        payload = seed.build_http_server("SBX-X", ["1"])
        self.assertEqual(sorted(payload), ["agents", "enabled", "interval", "testName", "url"])
        self.assertEqual(payload["agents"], [{"agentId": "1"}])
        self.assertIn("port", seed.build_agent_to_server("SBX-X", ["1"], "tcp"))
        self.assertNotIn("port", seed.build_agent_to_server("SBX-X", ["1"], "icmp"))
        self.assertEqual(seed.build_alert_rule("SBX-R", "http-server", 2)["minimumSources"], 2)
        self.assertNotIn("minimumSources", seed.build_alert_rule("SBX-R", "http-server"))
        tag = seed.build_tag_payload("env", "sbx", "agent")
        self.assertEqual((tag["key"], tag["value"], tag["objectType"]), ("env", "sbx", "agent"))

    def test_tags_are_created_in_source_only_and_assigned(self):
        api, _code, _out = self.apply_all()
        creates = [w for w in api.writes if w[0] == "create_tag"]
        self.assertEqual([c[2] for c in creates], ["env=sbx", "site=a", "app=web", "app=dns"])
        self.assertEqual(set(c[1] for c in creates), set(["1001"]))
        by_key = dict(("%s=%s" % (t["key"], t["value"]), t) for t in api.tags)
        self.assertEqual(len(by_key["env=sbx"]["assignments"]), 3)
        self.assertEqual(set(a["type"] for a in by_key["env=sbx"]["assignments"]), set(["v-agent"]))
        self.assertEqual(
            sorted(a["id"] for a in by_key["env=sbx"]["assignments"]), sorted(["2001", "2003", "3001"]))
        self.assertEqual(len(by_key["app=web"]["assignments"]), 10)
        self.assertEqual(len(by_key["app=dns"]["assignments"]), 3)
        self.assertEqual(set(a["type"] for a in by_key["app=dns"]["assignments"]), set(["test"]))

    def test_alert_rules(self):
        api, _code, _out = self.apply_all()
        by_name = dict((r["ruleName"], r) for r in api.rules)
        self.assertEqual(set(by_name), set([seed.ALERT_DEFAULT_NAME, seed.ALERT_MIN_SOURCES_NAME]))
        self.assertEqual(by_name[seed.ALERT_MIN_SOURCES_NAME]["minimumSources"], 2)
        self.assertNotIn("minimumSources", by_name[seed.ALERT_DEFAULT_NAME])
        updates = [w for w in api.writes if w[0] == "update_test"]
        self.assertEqual(len(updates), 10 + 30)
        self.assertEqual(set(w[1] for w in updates), set(["1001"]))

    def test_manual_tasks_are_printed(self):
        _api, _code, out = self.apply_all()
        self.assertIn("MANUAL TASK", out)
        self.assertIn("Agent notification rule on S1", out)
        self.assertIn("Suppression window", out)

    def test_write_pacing(self):
        api, _code, _out = self.apply_all()
        self.assertEqual(self.last_sleeps.count(0.2), len(api.writes))
        self.assertEqual(set(self.last_sleeps), set([0.2]))

    def test_idempotent_second_run(self):
        api = FakeApi()
        self.assertEqual(self.run_cli(api, "--apply")[0], 0)
        writes_after_first = len(api.writes)
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(api.writes), writes_after_first)
        self.assertIn("ok:       [SBX-S1-HTTP-01] => already exists", out)
        self.assertIn("already exists", out)
        data = self.result()
        self.assertEqual(data["created"]["tests"], [])
        self.assertEqual(data["existing"]["tests"], 195)

    def test_existing_name_is_skipped(self):
        api = FakeApi()
        api.add_test(GROUPS["SOURCE"], "SBX-S1-HTTP-01")
        code, out = self.run_cli(api, "--apply", "--steps", "tests")
        self.assertEqual(code, 0)
        self.assertIn("[SBX-S1-HTTP-01] => already exists", out)
        created = [w[2] for w in api.writes if w[0] == "create_test"]
        self.assertNotIn("SBX-S1-HTTP-01", created)
        self.assertEqual(len(created), 194)

    def test_steps_selection_runs_only_those_steps(self):
        api = FakeApi()
        code, _out = self.run_cli(api, "--apply", "--steps", "clusters")
        self.assertEqual(code, 0)
        self.assertEqual(set(w[0] for w in api.writes), set(["cluster_assign"]))

    def test_fake_credentials_not_printed_or_stored(self):
        made = []
        counter = [0]

        def token_hex(nbytes):
            counter[0] += 1
            value = "%02x" % (counter[0] + 10) * nbytes  # fake, run-time made, 16 hex chars
            made.append(value)
            return value

        api = FakeApi()
        code, out = self.run_cli(api, "--apply", token_hex=token_hex)
        self.assertEqual(code, 0)
        self.assertEqual(len(made), 2)
        self.assertEqual([len(v) for v in made], [16, 16])
        script = api.tests[GROUPS["SOURCE"]]["SBX-S1-TXN-01"].payload["transactionScript"]
        for value in made:
            self.assertIn(value, script)  # the values are in the test script that goes to the API
            self.assertNotIn(value, out)
            with open(os.path.join(self.out_dir, seed.RESULT_FILE)) as handle:
                self.assertNotIn(value, handle.read())
            with open(os.path.join(self.out_dir, seed.MANUAL_TASKS_FILE)) as handle:
                self.assertNotIn(value, handle.read())
        record = api.tests[GROUPS["SOURCE"]]["SBX-S1-TXN-01"]
        self.assertEqual(record.test_type, "web-transactions")
        self.assertEqual(record.payload["description"], "SBX fake credentials")

    def test_fake_credentials_differ_per_run_by_default(self):
        cfg = seed.parse_map(good_map())
        ids = {"C1": "3001"}
        first = [s for s in seed.build_test_plan(cfg, ids) if s.name == "SBX-S1-TXN-01"][0]
        second = [s for s in seed.build_test_plan(cfg, ids) if s.name == "SBX-S1-TXN-01"][0]
        self.assertNotEqual(first.payload["transactionScript"], second.payload["transactionScript"])

    def test_hard_error_stops_with_state_line_and_exit_1(self):
        api = FakeApi(fail_on_create_number=4)
        code, out = self.run_cli(api, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("STATE", out)
        self.assertIn("tests=3", out)
        self.assertEqual(len([w for w in api.writes if w[0] == "create_test"]), 3)
        self.assertEqual(first_index(api.writes, "create_tag"), None)
        data = self.result()
        self.assertEqual(data["exit_code"], 1)
        self.assertIn("create test", data["error"])
        self.assertEqual(len(data["created"]["tests"]), 3)
        self.assertEqual(len(data["created"]["clusters"]), 2)

    def test_exit_code_zero_when_clean(self):
        self.assertEqual(self.run_cli(FakeApi())[0], 0)
        self.assertEqual(self.run_cli(FakeApi(), "--apply")[0], 0)
        self.assertTrue(FakeApi().writes == [])

    def test_api_is_closed(self):
        api = FakeApi()
        self.run_cli(api)
        self.assertTrue(api.closed)


class CleanupTests(SeedTestCase):
    def seeded_api(self):
        api = FakeApi()
        self.assertEqual(self.run_cli(api, "--apply")[0], 0)
        # Objects that are NOT ours.
        api.add_test(GROUPS["SOURCE"], "PROD-keep-me")
        api.add_test(GROUPS["VIEW1"], "other-test")
        api.add_test(GROUPS["TARGET"], "SBX-LEFTOVER-IN-TARGET")
        api.tags.append({"tagId": "keep1", "key": "owner", "value": "team", "objectType": "test", "assignments": []})
        api.tags.append({"tagId": "keep2", "key": "env", "value": "prod", "objectType": "agent", "assignments": []})
        api.rules.append({"ruleId": "keeprule", "ruleName": "Default HTTP rule"})
        del api.writes[:]
        return api

    def test_cleanup_dry_run_lists_only(self):
        api = self.seeded_api()
        before = (len(api.tests["1001"]), len(api.tags), len(api.rules))
        code, out = self.run_cli(api, "--cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])
        self.assertEqual((len(api.tests["1001"]), len(api.tags), len(api.rules)), before)
        self.assertIn("SBX-C1-BULK-120", out)
        self.assertNotIn("PROD-keep-me", out)
        self.assertIn("dry run: would delete", out)
        data = self.result()
        self.assertEqual(len(data["planned_delete"]["tests"]), 195 + 1)
        self.assertEqual(data["deleted"]["tests"], [])

    def test_cleanup_apply_deletes_only_sbx_objects(self):
        api = self.seeded_api()
        code, _out = self.run_cli(api, "--cleanup", "--apply")
        self.assertEqual(code, 0)
        names = [n for tests in api.tests.values() for n in tests]
        self.assertEqual(sorted(names), ["PROD-keep-me", "other-test"])
        self.assertEqual(sorted(t["tagId"] for t in api.tags), ["keep1", "keep2"])
        self.assertEqual([r["ruleId"] for r in api.rules], ["keeprule"])
        kinds = set(w[0] for w in api.writes)
        self.assertEqual(kinds, set(["delete_test", "request DELETE"]))
        self.assertNotIn("delete_agent", kinds)
        for w in api.writes:
            if w[0] == "request DELETE":
                self.assertTrue(w[2].startswith(("tags/", "alerts/rules/")))
                self.assertFalse(w[2].startswith("agents/"))
        self.assertEqual(len([w for w in api.writes if w[0] == "delete_test"]), 196)
        self.assertEqual(self.last_sleeps.count(0.2), len(api.writes))
        self.assertEqual(self.result()["mode"], "apply")

    def test_cleanup_never_touches_unlisted_groups(self):
        api = self.seeded_api()
        api.tests["9999"] = {}
        api.add_test("9999", "SBX-OTHER-GROUP")
        self.run_cli(api, "--cleanup", "--apply")
        self.assertIn("SBX-OTHER-GROUP", api.tests["9999"])
        for w in api.writes:
            self.assertIn(w[1], set(GROUPS.values()))

    def test_cleanup_without_confirm_flag_is_refused(self):
        api = self.seeded_api()
        code, _out = self.run_cli(api, "--cleanup", "--apply", ack=False)
        self.assertEqual(code, 2)
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
