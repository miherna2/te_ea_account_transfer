import unittest

import requests

from te_agent_migrate.api_client import AmbiguousAgentMatch, ThousandEyesApiClient, retry_after_seconds
from te_agent_migrate.errors import ApiError, VisibilityTimeout
from te_agent_migrate.http import RetryPolicy
from te_agent_migrate.models import AgentRecord


class FakeResponse(object):
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = b"" if payload is None else b"{}"

    def json(self):
        return self._payload


class FakeSession(object):
    """Requests-like session that never touches the network."""

    def __init__(self, responses=None):
        self.headers = {}
        self.calls = []
        self._responses = list(responses or [])

    def request(self, method, url, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
            }
        )
        if not self._responses:
            return FakeResponse(200, {})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


BASE = "https://api.thousandeyes.com/v7/"


def make_client(responses=None):
    session = FakeSession(responses)
    sleeps = []
    client = ThousandEyesApiClient(
        "synthetic-token",
        session=session,
        sleep=sleeps.append,
        retry_policy=RetryPolicy(attempts=3, initial_backoff_seconds=1.0),
    )
    return client, session, sleeps


CLUSTER_PAYLOAD = {
    "agentId": "100",
    "agentName": "synthetic-cluster",
    "agentType": "enterprise-cluster",
    "clusterMembers": [
        {"memberId": "100", "name": "m1", "ipAddresses": ["10.0.0.1", "10.0.0.2"], "agentState": "online"},
        {"memberId": "101", "name": "m2", "ipAddresses": ["10.0.0.2", "10.0.0.3"], "agentState": "online"},
    ],
    "accountGroups": [{"aid": 11, "name": "src"}, {"accountGroupId": "22"}, 33, "44"],
}


class AgentRecordV7Tests(unittest.TestCase):
    def test_cluster_payload(self):
        agent = AgentRecord.from_api(CLUSTER_PAYLOAD, "11")
        self.assertTrue(agent.is_cluster)
        self.assertEqual(agent.agent_type, "enterprise-cluster")
        self.assertEqual(len(agent.cluster_members), 2)
        self.assertEqual(agent.ip_addresses, ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertTrue(agent.online)
        self.assertEqual(agent.state, "online")
        self.assertEqual(agent.shared_aids, ["11", "22", "33", "44"])

    def test_cluster_with_offline_member_is_not_online(self):
        payload = dict(CLUSTER_PAYLOAD)
        payload["clusterMembers"] = [
            {"memberId": "100", "agentState": "online"},
            {"memberId": "101", "agentState": "offline"},
        ]
        agent = AgentRecord.from_api(payload, "11")
        self.assertFalse(agent.online)
        self.assertEqual(agent.state, "unknown")

    def test_cluster_without_members_is_not_online(self):
        agent = AgentRecord.from_api({"agentId": "1", "agentType": "enterprise-cluster"}, "11")
        self.assertFalse(agent.online)

    def test_explicit_cluster_state_wins(self):
        payload = dict(CLUSTER_PAYLOAD)
        payload["agentState"] = "offline"
        self.assertFalse(AgentRecord.from_api(payload, "11").online)

    def test_shared_standalone_agent(self):
        agent = AgentRecord.from_api(
            {
                "agentId": "7",
                "agentName": "solo",
                "agentType": "Enterprise",
                "agentState": "online",
                "ipAddresses": ["192.0.2.7"],
                "accountGroups": [{"aid": "11", "accountGroupName": "src"}, {"aid": "11"}, {"aid": "55"}],
            },
            "11",
        )
        self.assertFalse(agent.is_cluster)
        self.assertEqual(agent.cluster_members, [])
        self.assertEqual(agent.shared_aids, ["11", "55"])
        self.assertEqual(agent.ip_addresses, ["192.0.2.7"])
        self.assertTrue(agent.online)

    def test_defaults_for_plain_agent(self):
        agent = AgentRecord.from_api({"agentId": "7", "agentName": "x"}, "11")
        self.assertEqual(agent.shared_aids, [])
        self.assertEqual(agent.cluster_members, [])
        self.assertEqual(agent.state, "unknown")
        self.assertFalse(agent.is_cluster)
        self.assertIn("shared_aids", agent.to_dict())


class ReadMethodTests(unittest.TestCase):
    def test_get_agent(self):
        client, session, _ = make_client([FakeResponse(200, CLUSTER_PAYLOAD)])
        agent = client.get_agent("11", "100")
        call = session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], BASE + "agents/100")
        self.assertEqual(call["params"], {"expand": "cluster-member", "aid": "11"})
        self.assertIsNone(call["json"])
        self.assertTrue(agent.is_cluster)
        self.assertEqual(agent.shared_aids, ["11", "22", "33", "44"])

    def test_get_agent_unwraps_nested_payload(self):
        client, _, _ = make_client([FakeResponse(200, {"agent": CLUSTER_PAYLOAD})])
        self.assertEqual(client.get_agent("11", "100").agent_id, "100")
        client, _, _ = make_client([FakeResponse(200, {"agents": [CLUSTER_PAYLOAD]})])
        self.assertEqual(client.get_agent("11", "100").agent_id, "100")

    def test_list_agents_with_clusters(self):
        payload = {"agents": [CLUSTER_PAYLOAD, {"agentId": "7", "agentName": "solo", "agentType": "enterprise"}]}
        client, session, _ = make_client([FakeResponse(200, payload)])
        agents = client.list_agents_with_clusters("11")
        call = session.calls[0]
        self.assertEqual(call["url"], BASE + "agents")
        self.assertEqual(
            call["params"],
            {"agentTypes": "enterprise,enterprise-cluster", "expand": "cluster-member,test-ids", "aid": "11"},
        )
        self.assertEqual([a.agent_id for a in agents], ["100", "7"])

    def test_list_agents_unchanged(self):
        client, session, _ = make_client([FakeResponse(200, {"agents": []})])
        client.list_agents("11")
        self.assertEqual(
            session.calls[0]["params"], {"agentTypes": "ENTERPRISE", "expand": "test-ids", "aid": "11"}
        )

    def test_list_tags_old_and_new_signature(self):
        client, session, _ = make_client([FakeResponse(200, {"tags": [{"id": "1"}]}), FakeResponse(200, {"tags": []})])
        self.assertEqual(client.list_tags("11"), [{"id": "1"}])
        self.assertEqual(session.calls[0]["params"], {"aid": "11"})
        client.list_tags("11", expand_assignments=True)
        self.assertEqual(session.calls[1]["params"], {"expand": "assignments", "aid": "11"})


class WriteMethodTests(unittest.TestCase):
    def test_set_agent_account_groups(self):
        client, session, _ = make_client([FakeResponse(200, {})])
        client.set_agent_account_groups("11", "100", [11, "22", "11", " 22 ", 33])
        call = session.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["url"], BASE + "agents/100")
        self.assertEqual(call["params"], {"aid": "11"})
        self.assertEqual(call["json"], {"accountGroups": ["11", "22", "33"]})

    def test_set_agent_account_groups_refusals(self):
        client, session, _ = make_client()
        for bad in ([], None, [""], ["  "], [None], [[1]], [True]):
            with self.assertRaises(ApiError):
                client.set_agent_account_groups("11", "100", bad)
        self.assertEqual(session.calls, [])

    def test_set_agent_account_groups_unexpected_status(self):
        client, session, _ = make_client([FakeResponse(204, None)])
        with self.assertRaises(ApiError):
            client.set_agent_account_groups("11", "100", ["11"])
        self.assertEqual(len(session.calls), 1)

    def test_cluster_assign(self):
        client, session, _ = make_client([FakeResponse(200, {"agents": []})])
        result = client.cluster_assign("11", "100", ["101", 102])
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], BASE + "agents/100/cluster/assign")
        self.assertEqual(call["params"], {"aid": "11"})
        self.assertEqual(call["json"], {"agents": ["101", "102"]})
        self.assertEqual(result, {"agents": []})

    def test_cluster_assign_empty_list_creates_single_agent_cluster(self):
        client, session, _ = make_client([FakeResponse(200, {"agents": []})])
        client.cluster_assign("11", "100", [])
        self.assertEqual(session.calls[0]["json"], {"agents": []})

    def test_cluster_assign_refuses_base_in_list(self):
        client, session, _ = make_client()
        with self.assertRaises(ApiError):
            client.cluster_assign("11", "100", ["101", 100])
        self.assertEqual(session.calls, [])

    def test_cluster_unassign(self):
        client, session, _ = make_client([FakeResponse(200, {"agents": [{"agentId": "101"}]})])
        result = client.cluster_unassign("11", "100", ["101", "101", 102])
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], BASE + "agents/100/cluster/unassign")
        self.assertEqual(call["params"], {"aid": "11"})
        self.assertEqual(call["json"], {"members": ["101", "102"]})
        self.assertIn("agents", result)

    def test_cluster_unassign_refuses_empty(self):
        client, session, _ = make_client()
        with self.assertRaises(ApiError):
            client.cluster_unassign("11", "100", [])
        self.assertEqual(session.calls, [])

    def test_assign_tag_tuples_and_dicts(self):
        client, session, _ = make_client([FakeResponse(200, {})])
        client.assign_tag("11", "tag1", [("100", "v-agent"), {"id": 5, "type": "test"}])
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], BASE + "tags/tag1/assign")
        self.assertEqual(call["params"], {"aid": "11"})
        self.assertEqual(
            call["json"],
            {"assignments": [{"id": "100", "type": "v-agent"}, {"id": "5", "type": "test"}]},
        )

    def test_assign_tag_refusals(self):
        client, session, _ = make_client()
        for bad in (
            [("100", "agent")],
            [{"id": "1", "type": "bogus"}],
            [("", "v-agent")],
            ["100"],
            [],
        ):
            with self.assertRaises(ApiError):
                client.assign_tag("11", "tag1", bad)
        self.assertEqual(session.calls, [])

    def test_assign_tag_all_allowed_types(self):
        for tag_type in ("test", "v-agent", "endpoint-test", "dashboard", "connected-devices-test"):
            client, session, _ = make_client([FakeResponse(200, {})])
            client.assign_tag("11", "t", [("1", tag_type)])
            self.assertEqual(session.calls[0]["json"]["assignments"][0]["type"], tag_type)

    def test_assign_tests_still_works(self):
        client, session, _ = make_client([FakeResponse(200, {})])
        client.assign_tests("11", "100", [1, "2"])
        self.assertEqual(session.calls[0]["url"], BASE + "agents/100/tests/assign")
        self.assertEqual(session.calls[0]["json"], {"testIds": ["1", "2"]})


class DeleteGuardTests(unittest.TestCase):
    def test_delete_agent_default_still_works(self):
        client, session, _ = make_client([FakeResponse(204, None)])
        client.delete_agent("11", "7")
        self.assertEqual(session.calls[0]["method"], "DELETE")
        self.assertEqual(session.calls[0]["url"], BASE + "agents/7")
        self.assertEqual(session.calls[0]["params"], {"aid": "11"})

    def test_delete_agent_enterprise_type_allowed(self):
        client, session, _ = make_client([FakeResponse(204, None)])
        client.delete_agent("11", "7", agent_type="enterprise")
        self.assertEqual(len(session.calls), 1)

    def test_delete_cluster_refused(self):
        client, session, _ = make_client()
        with self.assertRaises(ApiError) as ctx:
            client.delete_agent("11", "100", agent_type="enterprise-cluster")
        self.assertIn("Refusing to delete a cluster", str(ctx.exception))
        with self.assertRaises(ApiError):
            client.delete_agent("11", "100", agent_type="Enterprise-Cluster")
        self.assertEqual(session.calls, [])


class WriteRetryTests(unittest.TestCase):
    def test_write_retries_429_with_retry_after(self):
        responses = [
            FakeResponse(429, {}, {"retry-after": "7"}),
            FakeResponse(429, {}, {}),
            FakeResponse(200, {"ok": True}),
        ]
        client, session, sleeps = make_client(responses)
        result = client.set_agent_account_groups("11", "100", ["11"])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [7.0, 2.0])  # retry-after, then policy delay(attempt=2)

    def test_write_429_gives_up_after_four_retries(self):
        client, session, sleeps = make_client([FakeResponse(429, {}, {}) for _ in range(10)])
        with self.assertRaises(ApiError):
            client.cluster_assign("11", "100", [])
        self.assertEqual(len(session.calls), 5)
        self.assertEqual(len(sleeps), 4)

    def test_write_not_retried_on_500(self):
        for status in (500, 502, 503, 504, 400):
            client, session, sleeps = make_client([FakeResponse(status, {}), FakeResponse(200, {})])
            with self.assertRaises(ApiError):
                client.cluster_assign("11", "100", [])
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(sleeps, [])

    def test_write_not_retried_on_exception(self):
        client, session, sleeps = make_client([requests.ConnectionError("boom"), FakeResponse(200, {})])
        with self.assertRaises(ApiError):
            client.assign_tag("11", "t", [("1", "v-agent")])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])

    def test_delete_429_retried(self):
        client, session, _ = make_client([FakeResponse(429, {}), FakeResponse(204, None)])
        client.delete_agent("11", "7")
        self.assertEqual(len(session.calls), 2)

    def test_read_retry_behavior_unchanged(self):
        client, session, sleeps = make_client(
            [FakeResponse(500, {}), FakeResponse(429, {}, {"retry-after": "3"}), FakeResponse(200, {"agents": []})]
        )
        client.list_agents("11")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [1.0, 3.0])

    def test_read_exception_retry_unchanged(self):
        client, session, _ = make_client([requests.ConnectionError("x"), FakeResponse(200, {"agents": []})])
        client.list_agents("11")
        self.assertEqual(len(session.calls), 2)


class NotJsonResponse(object):
    status_code = 200
    headers = {}
    content = b"<html>ok</html>"

    def json(self):
        raise ValueError("no json")


class WriteBodyShapeTests(unittest.TestCase):
    """A write that the server applied must not raise because of the body shape."""

    def test_array_body_on_every_write_method(self):
        calls = (
            lambda c: c.cluster_assign("11", "100", ["101"]),
            lambda c: c.cluster_unassign("11", "100", ["101"]),
            lambda c: c.set_agent_account_groups("11", "100", ["11"]),
            lambda c: c.assign_tests("11", "100", ["1"]),
            lambda c: c.assign_tag("11", "t", [("1", "v-agent")]),
            lambda c: c.update_agent_name("11", "100", "n"),
        )
        for call in calls:
            client, session, _ = make_client([FakeResponse(200, [{"agentId": "1"}, {"agentId": "2"}])])
            result = call(client)
            self.assertEqual(len(session.calls), 1)
            if result is not None:
                self.assertEqual(result, {"_list": [{"agentId": "1"}, {"agentId": "2"}]})

    def test_post_put_delete_return_dict_for_array(self):
        for method in ("POST", "PUT", "DELETE"):
            client, _, _ = make_client([FakeResponse(200, [1, 2])])
            self.assertEqual(client.request(method, "x", expected=set((200,))), {"_list": [1, 2]})

    def test_empty_and_non_json_and_scalar_bodies_on_writes(self):
        client, _, _ = make_client([FakeResponse(200, None)])
        self.assertEqual(client.request("POST", "x", expected=set((200,))), {})
        client, _, _ = make_client([NotJsonResponse()])
        self.assertEqual(client.request("PUT", "x", expected=set((200,))), {})
        client, _, _ = make_client([FakeResponse(201, "text")])
        self.assertEqual(client.request("POST", "x", expected=set((200, 201))), {})
        client, _, _ = make_client([FakeResponse(204, [1])])
        self.assertEqual(client.request("DELETE", "x", expected=set((204,))), {})

    def test_write_with_unexpected_status_still_fails(self):
        client, _, _ = make_client([FakeResponse(202, [1])])
        with self.assertRaises(ApiError):
            client.request("POST", "x", expected=set((200,)))

    def test_reads_keep_the_strict_check(self):
        client, _, _ = make_client([FakeResponse(200, [1, 2])])
        with self.assertRaises(ApiError) as caught:
            client.request("GET", "agents")
        self.assertIn("unexpected JSON shape", str(caught.exception))
        client, _, _ = make_client([NotJsonResponse()])
        with self.assertRaises(ApiError):
            client.request("GET", "agents")

    def test_create_test_with_array_body_is_still_refused(self):
        client, _, _ = make_client([FakeResponse(201, [{"testId": "1"}])])
        with self.assertRaises(ApiError):
            client.create_test("11", "http-server", {"testName": "t"})

    def test_set_test_enabled_survives_array_body_on_put(self):
        session = FakeSession([FakeResponse(200, {"testId": "5", "type": "dns-server", "enabled": False}),
                               FakeResponse(200, [{"testId": "5"}])])
        client = ThousandEyesApiClient("synthetic-token", session=session, sleep=lambda s: None)
        client.set_test_enabled("1", "dns-server", "5", True)
        self.assertEqual([c["method"] for c in session.calls], ["GET", "PUT"])


class SetTestEnabledMinimalTests(unittest.TestCase):
    def test_puts_only_the_enabled_flag(self):
        session = FakeSession([FakeResponse(200, {"testId": "5"})])
        client = ThousandEyesApiClient("synthetic-token", session=session, sleep=lambda s: None)
        client.set_test_enabled_minimal("1", "http-server", "5", True)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertTrue(call["url"].endswith("tests/http-server/5"))
        self.assertEqual(call["json"], {"enabled": True})
        self.assertEqual(call["params"], {"aid": "1"})

    def test_false_and_truthy_values_become_booleans(self):
        session = FakeSession([FakeResponse(200, {}), FakeResponse(204, None)])
        client = ThousandEyesApiClient("synthetic-token", session=session, sleep=lambda s: None)
        client.set_test_enabled_minimal("1", "dns-server", "5", False)
        client.set_test_enabled_minimal("1", "dns-server", "6", 1)
        self.assertEqual([c["json"] for c in session.calls], [{"enabled": False}, {"enabled": True}])


class RetryAfterTests(unittest.TestCase):
    def test_parser(self):
        self.assertEqual(retry_after_seconds("7", 2.0), 7.0)
        self.assertEqual(retry_after_seconds("2.5", 2.0), 2.5)
        self.assertEqual(retry_after_seconds(" 0.25 ", 2.0), 0.25)
        self.assertEqual(retry_after_seconds("0", 2.0), 0.0)
        self.assertEqual(retry_after_seconds("120", 2.0), 60.0)
        self.assertEqual(retry_after_seconds("86400.5", 2.0), 60.0)
        for bad in (None, "", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "-3", "nan", "inf"):
            self.assertEqual(retry_after_seconds(bad, 2.0), 2.0, bad)

    def test_write_429_wait_is_capped_and_decimal(self):
        responses = [
            FakeResponse(429, {}, {"retry-after": "3600"}),
            FakeResponse(429, {}, {"retry-after": "1.5"}),
            FakeResponse(200, {}),
        ]
        client, session, sleeps = make_client(responses)
        client.set_agent_account_groups("11", "100", ["11"])
        self.assertEqual(sleeps, [60.0, 1.5])

    def test_read_429_wait_is_capped(self):
        client, _, sleeps = make_client([FakeResponse(429, {}, {"retry-after": "999"}),
                                         FakeResponse(200, {"agents": []})])
        client.list_agents("11")
        self.assertEqual(sleeps, [60.0])

    def test_http_date_falls_back_to_policy_delay(self):
        client, _, sleeps = make_client([FakeResponse(429, {}, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                                         FakeResponse(200, {"agents": []})])
        client.list_agents("11")
        self.assertEqual(sleeps, [1.0])


def agent(agent_id, name, ips, state="online", hostname=None):
    return AgentRecord(agent_id, name, hostname if hostname is not None else name, ips, state, "9",
                       agent_type="enterprise")


class StrictWaitTests(unittest.TestCase):
    def make(self, listings):
        """Client whose list_agents returns the given listings one after the other (the last one repeats)."""
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        client = ThousandEyesApiClient("synthetic-token", session=FakeSession(), sleep=sleep,
                                       monotonic=lambda: clock[0])
        queue = list(listings)
        client.list_agents = lambda aid, agent_types="ENTERPRISE": queue.pop(0) if len(queue) > 1 else queue[0]
        return client, sleeps

    def wait(self, client, known, **kwargs):
        return client.wait_for_agent("9", "EA-A", "10.0.0.5", timeout_seconds=3, poll_interval_seconds=1.0,
                                     known_ids=known, **kwargs)

    def test_match_new_agents_rules(self):
        agents = [
            agent("1", "EA-A", ["10.0.0.5"]),                                  # known id
            agent("2", "EA-A-9f3a", ["10.0.0.5"]),                             # suffix name: ok
            agent("3", "other", ["10.0.0.5"], hostname="ea-a"),                # hostname equal: ok
            agent("4", "EA-A", ["10.9.9.9"]),                                  # wrong IP
            agent("5", "foreign", ["10.0.0.5"]),                               # wrong name
            agent("6", "EA-A", ["10.0.0.5"], state="offline"),                 # offline
            agent("7", "my-EA-A", ["10.0.0.5"]),                               # contains but does not start with
            agent("8", "EA-A2", ["10.0.0.5"]),                                 # shares a prefix only: rejected
            agent("9", "EA-Abox", ["10.0.0.5"]),                               # shares a prefix only: rejected
            agent("10", "ea-a", ["10.0.0.5"]),                                 # exact name, other case: ok
        ]
        found = ThousandEyesApiClient.match_new_agents(agents, ["ea-a"], ["10.0.0.5"], set(["1"]))
        self.assertEqual([a.agent_id for a in found], ["2", "3", "10"])

    def test_match_new_agents_prefix_needs_a_dash(self):
        agents = [agent("2", "EA-A-9f3a", ["10.0.0.5"]), agent("3", "EA-A9f3a", ["10.0.0.5"]),
                  agent("4", "EA-A-", ["10.0.0.5"])]
        found = ThousandEyesApiClient.match_new_agents(agents, ["EA-A"], ["10.0.0.5"], [])
        self.assertEqual([a.agent_id for a in found], ["2", "4"])

    def test_default_mode_is_the_old_behavior(self):
        client, _ = self.make([[agent("4", "EA-A", ["10.9.9.9"]), agent("5", "zzz", ["10.0.0.5"])]])
        found = client.wait_for_agent("9", "EA-A", "10.0.0.5", timeout_seconds=3)
        self.assertEqual(found.agent_id, "5")  # highest id by IP or name: not strict

    def test_strict_ignores_known_foreign_and_returns_the_new_agent(self):
        listing = [agent("1", "EA-A", ["10.0.0.5"]), agent("2", "EA-A-7c1d", ["10.0.0.5"]),
                   agent("3", "intruder", ["10.0.0.5"])]
        client, _ = self.make([listing])
        self.assertEqual(self.wait(client, set(["1"])).agent_id, "2")

    def test_strict_waits_until_the_new_agent_appears(self):
        client, sleeps = self.make([[agent("1", "EA-A", ["10.0.0.5"])],
                                    [agent("1", "EA-A", ["10.0.0.5"]), agent("2", "EA-A-7c1d", ["10.0.0.5"])]])
        self.assertEqual(self.wait(client, set(["1"])).agent_id, "2")
        self.assertEqual(sleeps, [1.0])

    def test_strict_foreign_only_times_out(self):
        client, _ = self.make([[agent("8", "intruder", ["10.0.0.5"]), agent("9", "EA-A", ["10.5.5.5"])]])
        with self.assertRaises(VisibilityTimeout):
            self.wait(client, set())

    def test_strict_two_candidates_raise_with_ids(self):
        client, _ = self.make([[agent("20", "EA-A-1", ["10.0.0.5"]), agent("10", "EA-A-2", ["10.0.0.5"])]])
        with self.assertRaises(AmbiguousAgentMatch) as caught:
            self.wait(client, set())
        self.assertEqual(caught.exception.agent_ids, ["10", "20"])
        self.assertIn("10, 20", str(caught.exception))
        self.assertIsInstance(caught.exception, ApiError)

    def test_alt_names_and_alt_ips(self):
        client, _ = self.make([[agent("2", "renamed-box", ["192.0.2.50"])]])
        found = self.wait(client, set(), alt_names=["renamed"], alt_ips=["192.0.2.50"])
        self.assertEqual(found.agent_id, "2")

    def test_empty_known_set_is_still_strict(self):
        client, _ = self.make([[agent("5", "foreign", ["10.0.0.5"])]])
        with self.assertRaises(VisibilityTimeout):
            self.wait(client, [])

    def test_on_not_visible_is_called_between_polls(self):
        client, _ = self.make([[agent("5", "foreign", ["10.0.0.5"])]])
        seen = []
        with self.assertRaises(VisibilityTimeout):
            self.wait(client, set(), on_not_visible=lambda: seen.append(1))
        self.assertGreaterEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
