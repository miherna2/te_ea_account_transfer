from __future__ import annotations

import json

import httpx
import pytest

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.errors import ApiError, VisibilityTimeout
from te_agent_migrate.http import RetryPolicy


def test_agent_requests_are_scoped_and_parse_test_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["aid"] == "123"
        assert request.url.params["agentTypes"] == "ENTERPRISE"
        assert request.url.params["expand"] == "test-ids"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "agentId": "9",
                        "agentName": "agent-a",
                        "hostname": "agent-a",
                        "ipAddresses": ["192.0.2.10"],
                        "agentState": "online",
                        "testIds": [1, {"testId": 2}],
                    }
                ]
            },
            request=request,
        )

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )

    agents = client.list_agents("123")

    assert agents[0].test_ids == ["1", "2"]
    assert agents[0].online


def test_wait_for_agent_uses_full_180_second_window() -> None:
    clock = 0.0
    calls = 0
    polls: list[tuple[int, float]] = []

    def monotonic() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"agents": []}, request=request)

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
        monotonic=monotonic,
        sleep=sleep,
    )

    with pytest.raises(VisibilityTimeout, match="180 seconds"):
        client.wait_for_agent(
            "target",
            hostname="agent-a",
            ip_address="192.0.2.10",
            timeout_seconds=180,
            on_poll=lambda attempt, remaining: polls.append((attempt, remaining)),
        )

    assert clock == 180
    assert calls == 37
    assert polls[0] == (1, 180)
    assert polls[-1] == (37, 0)


def test_wait_for_source_absence_and_update_exact_agent_name() -> None:
    clock = 0.0
    source_deleted = False
    renamed = False
    put_payloads: list[dict[str, object]] = []

    def monotonic() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal source_deleted, renamed
        if request.method == "DELETE":
            assert request.url.path == "/v7/agents/9"
            assert request.url.params["aid"] == "source"
            source_deleted = True
            return httpx.Response(204, request=request)
        if request.method == "PUT":
            assert request.url.path == "/v7/agents/9"
            assert request.url.params["aid"] == "target"
            put_payloads.append(json.loads(request.content))
            renamed = True
            return httpx.Response(200, json={}, request=request)
        if request.url.params["aid"] == "source":
            return httpx.Response(
                200,
                json={"agents": [] if source_deleted else [{"agentId": "9"}]},
                request=request,
            )
        name = "agent-a" if renamed else "agent-a-old"
        return httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "agentId": "9",
                        "agentName": name,
                        "hostname": "agent-a",
                        "ipAddresses": ["192.0.2.10"],
                        "agentState": "online",
                    }
                ]
            },
            request=request,
        )

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
        monotonic=monotonic,
        sleep=sleep,
    )

    client.delete_agent("source", "9")
    client.wait_for_agent_absent(
        "source",
        agent_id="9",
        timeout_seconds=180,
    )
    client.update_agent_name("target", "9", "agent-a")
    target = client.wait_for_agent_name(
        "target",
        agent_id="9",
        expected_name="agent-a",
        timeout_seconds=180,
    )

    assert source_deleted
    assert target.online and target.name == "agent-a"
    assert clock == 0
    assert put_payloads == [{"agentName": "agent-a"}]


def test_wait_for_source_absence_times_out_while_exact_id_is_still_returned() -> None:
    clock = 0.0

    def monotonic() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"agents": [{"agentId": "9", "agentState": "offline"}]},
            request=request,
        )

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
        monotonic=monotonic,
        sleep=sleep,
    )

    with pytest.raises(VisibilityTimeout, match="still returned"):
        client.wait_for_agent_absent("source", agent_id="9", timeout_seconds=180)

    assert clock == 180


def test_delete_test_uses_typed_v7_endpoint_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=3),
    )

    client.delete_test("source", "http-server", "123")

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/v7/tests/http-server/123"
    assert requests[0].url.params["aid"] == "source"


def test_assign_tests_preserves_v7_string_test_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )

    client.assign_tests("target", "agent-9", ["123"])

    assert requests[0].url.path == "/v7/agents/agent-9/tests/assign"
    assert json.loads(requests[0].content) == {"testIds": ["123"]}


def test_wait_for_test_ready_requires_enabled_and_agent_association() -> None:
    clock = 0.0
    calls = 0

    def monotonic() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        ready = calls >= 3
        return httpx.Response(
            200,
            json={
                "test": {
                    "testId": "123",
                    "testName": "Migrated",
                    "type": "http-server",
                    "enabled": ready,
                    "agents": [{"agentId": "9"}] if ready else [],
                }
            },
            request=request,
        )

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
        monotonic=monotonic,
        sleep=sleep,
    )

    test = client.wait_for_test_ready(
        "target",
        test_type="http-server",
        test_id="123",
        agent_id="9",
        timeout_seconds=180,
    )

    assert test.enabled
    assert calls == 3
    assert clock == 10


def test_wait_for_deleted_test_absence() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        tests = (
            [{"testId": "123", "testName": "Old", "type": "http-server"}]
            if calls == 1
            else []
        )
        return httpx.Response(200, json={"tests": tests}, request=request)

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
        sleep=lambda _: None,
    )

    client.wait_for_test_absent("source", test_id="123", timeout_seconds=180)

    assert calls == 2


def test_create_test_error_reports_only_payload_field_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, request=request)

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )

    with pytest.raises(ApiError) as raised:
        client.create_test(
            "target",
            "http-server",
            {"testName": "safe-name", "password": "must-not-appear", "agents": []},
        )

    detail = str(raised.value)
    assert "submitted field names=[agents, password, testName]" in detail
    assert "must-not-appear" not in detail


def test_transient_get_is_retried_but_delete_is_not() -> None:
    get_calls = 0
    delete_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls, delete_calls
        if request.method == "GET":
            get_calls += 1
            if get_calls == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, json={"accountGroups": []}, request=request)
        delete_calls += 1
        return httpx.Response(503, request=request)

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=2, initial_backoff_seconds=0),
        sleep=lambda _: None,
    )

    assert client.list_account_groups() == []
    assert get_calls == 2
    with pytest.raises(Exception, match="503"):
        client.delete_agent("123", "9")
    assert delete_calls == 1


def test_hal_cursor_pagination_follows_links_next() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "_embedded": {"accountGroups": [{"aid": "1", "accountGroupName": "One"}]},
                    "_links": {"next": {"href": "/v7/account-groups?cursor=next"}},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"_embedded": {"accountGroups": [{"aid": "2", "accountGroupName": "Two"}]}},
            request=request,
        )

    client = ThousandEyesApiClient(
        "oauth",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )

    groups = client.list_account_groups()

    assert [group.aid for group in groups] == ["1", "2"]
    assert "cursor=next" in calls[1]
