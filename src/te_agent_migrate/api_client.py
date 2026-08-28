"""ThousandEyes API v7 client with account-group scoping and safe retries."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urljoin

import httpx

from te_agent_migrate.errors import ApiError, VisibilityTimeout
from te_agent_migrate.http import TRANSIENT_STATUS_CODES, RetryPolicy
from te_agent_migrate.models import AccountGroup, AgentRecord, TestRecord


class ThousandEyesApiClient:
    def __init__(
        self,
        bearer_token: str,
        *,
        base_url: str = "https://api.thousandeyes.com/v7/",
        retry_policy: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"},
        )

    def __enter__(self) -> ThousandEyesApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        aid: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expected: set[int] | None = None,
        safe_to_retry: bool | None = None,
    ) -> dict[str, Any]:
        expected = expected or {200}
        retryable = method.upper() in {"GET", "HEAD"} if safe_to_retry is None else safe_to_retry
        attempts = self._retry.attempts if retryable else 1
        query = dict(params or {})
        if aid is not None:
            query["aid"] = aid
        response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(
                    method, path, params=query if query else None, json=json_body
                )
            except httpx.RequestError as exc:
                if attempt == attempts:
                    raise ApiError(f"API {method} {path} failed: {exc}") from exc
                self._sleep(self._retry.delay(attempt))
                continue
            if response.status_code in expected:
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ApiError(f"API {method} {path} returned invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise ApiError(f"API {method} {path} returned an unexpected JSON shape")
                return payload
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < attempts:
                retry_after = response.headers.get("retry-after")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else self._retry.delay(attempt)
                )
                self._sleep(delay)
                continue
            break
        assert response is not None
        request_id = response.headers.get("x-request-id", "not-provided")
        raise ApiError(
            f"API {method} {path} returned HTTP {response.status_code} (request-id={request_id})"
        )

    @staticmethod
    def _items(payload: dict[str, Any], preferred_key: str) -> list[dict[str, Any]]:
        candidates = payload.get(preferred_key, payload.get("items"))
        if candidates is None:
            embedded = payload.get("_embedded", {})
            candidates = embedded.get(preferred_key, []) if isinstance(embedded, dict) else []
        if not isinstance(candidates, list):
            raise ApiError(f"API response field {preferred_key!r} was not a list")
        return [item for item in candidates if isinstance(item, dict)]

    def _paginate(
        self,
        path: str,
        *,
        preferred_key: str,
        aid: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        next_path: str | None = path
        query = dict(params or {})
        while next_path:
            payload = self.request("GET", next_path, aid=aid, params=query)
            yield from self._items(payload, preferred_key)
            links = payload.get("_links", {})
            next_value: Any = links.get("next") if isinstance(links, dict) else None
            if isinstance(next_value, dict):
                next_value = next_value.get("href")
            if isinstance(next_value, str) and next_value:
                next_path = urljoin(self.base_url, next_value)
                query = {}
                aid = None
            else:
                next_path = None

    def list_account_groups(self) -> list[AccountGroup]:
        return [
            AccountGroup.from_api(item)
            for item in self._paginate("account-groups", preferred_key="accountGroups")
        ]

    def list_agents(
        self, aid: str, *, agent_types: str = "ENTERPRISE"
    ) -> list[AgentRecord]:
        params = {"agentTypes": agent_types, "expand": "test-ids"}
        return [
            AgentRecord.from_api(item, aid)
            for item in self._paginate("agents", preferred_key="agents", aid=aid, params=params)
        ]

    @staticmethod
    def match_agent(
        agents: list[AgentRecord], *, hostname: str, ip_address: str, online_only: bool = False
    ) -> AgentRecord | None:
        candidates = [
            agent
            for agent in agents
            if ip_address in agent.ip_addresses
            or (agent.hostname or "").casefold() == hostname.casefold()
            or agent.name.casefold() == hostname.casefold()
        ]
        if online_only:
            candidates = [agent for agent in candidates if agent.online]

        def identity_order(agent: AgentRecord) -> tuple[bool, int, str]:
            numeric = int(agent.agent_id) if agent.agent_id.isdigit() else -1
            return agent.online, numeric, agent.agent_id

        return max(candidates, key=identity_order) if candidates else None

    def wait_for_agent(
        self,
        aid: str,
        *,
        hostname: str,
        ip_address: str,
        timeout_seconds: int,
        poll_interval_seconds: float = 5.0,
        on_poll: Callable[[int, float], None] | None = None,
    ) -> AgentRecord:
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            agent = self.match_agent(
                self.list_agents(aid), hostname=hostname, ip_address=ip_address, online_only=True
            )
            if agent is not None:
                return agent
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    f"Destination agent was not visible and online within {timeout_seconds} seconds"
                )
            self._sleep(min(poll_interval_seconds, remaining))

    @staticmethod
    def find_agent_by_id(agents: list[AgentRecord], agent_id: str) -> AgentRecord | None:
        return next((agent for agent in agents if agent.agent_id == agent_id), None)

    def wait_for_agent_absent(
        self,
        aid: str,
        *,
        agent_id: str,
        timeout_seconds: int,
        poll_interval_seconds: float = 5.0,
        on_poll: Callable[[int, float], None] | None = None,
    ) -> None:
        """Wait until the deleted source identity is no longer returned."""
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            agent = self.find_agent_by_id(self.list_agents(aid), agent_id)
            if agent is None:
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    f"Source agent {agent_id} was still returned within "
                    f"{timeout_seconds} seconds after deletion"
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def wait_for_agent_name(
        self,
        aid: str,
        *,
        agent_id: str,
        expected_name: str,
        timeout_seconds: int,
        poll_interval_seconds: float = 5.0,
        on_poll: Callable[[int, float], None] | None = None,
    ) -> AgentRecord:
        """Wait until one online identity has the exact required display name."""
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            agent = self.find_agent_by_id(self.list_agents(aid), agent_id)
            if agent is not None and agent.online and agent.name == expected_name:
                return agent
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    f"Destination agent {agent_id} did not become online with exact name "
                    f"{expected_name!r} within {timeout_seconds} seconds"
                )
            self._sleep(min(poll_interval_seconds, remaining))


    def list_tests(self, aid: str) -> list[TestRecord]:
        return [
            TestRecord.from_api(item, aid)
            for item in self._paginate("tests", preferred_key="tests", aid=aid)
        ]

    def list_tags(self, aid: str) -> list[dict[str, Any]]:
        """List tags visible in one account group."""
        return list(self._paginate("tags", preferred_key="tags", aid=aid))

    def list_monitors(self, aid: str) -> list[dict[str, Any]]:
        """List BGP monitors visible in one account group."""
        return list(self._paginate("monitors", preferred_key="monitors", aid=aid))

    def create_tag(self, aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one tag and return its API representation."""
        response = self.request(
            "POST",
            "tags",
            aid=aid,
            json_body=payload,
            expected={200, 201},
            safe_to_retry=False,
        )
        values = self._items(response, "tags") if isinstance(response.get("tags"), list) else []
        raw = values[0] if values else response.get("tag", response)
        if not isinstance(raw, dict):
            raise ApiError("Created tag response had an unexpected shape")
        return raw

    def list_alert_rules(self, aid: str) -> list[dict[str, Any]]:
        """List alert rules visible in one account group."""
        return list(
            self._paginate("alerts/rules", preferred_key="alertRules", aid=aid)
        )

    def create_alert_rule(self, aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one alert rule and return its API representation."""
        response = self.request(
            "POST",
            "alerts/rules",
            aid=aid,
            json_body=payload,
            expected={200, 201},
            safe_to_retry=False,
        )
        values = (
            self._items(response, "alertRules")
            if isinstance(response.get("alertRules"), list)
            else []
        )
        raw = values[0] if values else response.get("alertRule", response)
        if not isinstance(raw, dict):
            raise ApiError("Created alert-rule response had an unexpected shape")
        return raw

    def get_test(self, aid: str, test_type: str, test_id: str) -> TestRecord:
        payload = self.request(
            "GET",
            f"tests/{test_type}/{test_id}",
            aid=aid,
            params={"expand": "agent,alert-rule,monitor,label,tag,shared-with-account"},
        )
        values = self._items(payload, "test") if isinstance(payload.get("test"), list) else []
        raw = values[0] if values else payload.get("test", payload)
        if not isinstance(raw, dict):
            raise ApiError(f"Test {test_id} returned an unexpected JSON shape")
        return TestRecord.from_api(raw, aid)

    def create_test(self, aid: str, test_type: str, payload: dict[str, Any]) -> TestRecord:
        try:
            response = self.request(
                "POST", f"tests/{test_type}", aid=aid, json_body=payload, expected={200, 201}
            )
        except ApiError as exc:
            fields = ", ".join(sorted(payload))
            raise ApiError(f"{exc}; submitted field names=[{fields}] (values redacted)") from exc
        raw = response.get("test", response)
        if not isinstance(raw, dict):
            raise ApiError("Created test response had an unexpected shape")
        return TestRecord.from_api(raw, aid)

    def update_test(
        self, aid: str, test_type: str, test_id: str, payload: dict[str, Any]
    ) -> TestRecord:
        response = self.request(
            "PUT",
            f"tests/{test_type}/{test_id}",
            aid=aid,
            json_body=payload,
            expected={200},
        )
        raw = response.get("test", response)
        if not isinstance(raw, dict):
            raise ApiError("Updated test response had an unexpected shape")
        return TestRecord.from_api(raw, aid)

    def delete_test(self, aid: str, test_type: str, test_id: str) -> None:
        self.request(
            "DELETE",
            f"tests/{test_type}/{test_id}",
            aid=aid,
            expected={200, 204},
            safe_to_retry=False,
        )

    @staticmethod
    def _test_agent_ids(test: TestRecord) -> set[str]:
        agent_ids: set[str] = set()
        for agent in test.agents:
            value = agent.get("agentId", agent.get("id")) if isinstance(agent, dict) else agent
            if value is not None and str(value):
                agent_ids.add(str(value))
        return agent_ids

    def wait_for_test_ready(
        self,
        aid: str,
        *,
        test_type: str,
        test_id: str,
        agent_id: str,
        timeout_seconds: int,
        poll_interval_seconds: float = 5.0,
        on_poll: Callable[[int, float], None] | None = None,
    ) -> TestRecord:
        """Wait until a target test is enabled and visibly assigned to an agent."""
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            test = self.get_test(aid, test_type, test_id)
            if test.enabled and agent_id in self._test_agent_ids(test):
                return test
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    f"Target test {test_id} was not enabled and assigned to agent {agent_id} "
                    f"within {timeout_seconds} seconds"
                )
            self._sleep(min(poll_interval_seconds, remaining))

    @staticmethod
    def _test_monitor_ids(test: TestRecord) -> set[str]:
        monitor_ids: set[str] = set()
        raw_monitors = test.raw.get("monitors", [])
        if not isinstance(raw_monitors, list):
            return monitor_ids
        for monitor in raw_monitors:
            value = (
                monitor.get("monitorId", monitor.get("id"))
                if isinstance(monitor, dict)
                else monitor
            )
            if value is not None and str(value):
                monitor_ids.add(str(value))
        return monitor_ids

    def wait_for_monitor_test_ready(
        self,
        aid: str,
        *,
        test_type: str,
        test_id: str,
        expected_name: str,
        expected_prefix: str,
        expected_use_public_bgp: bool,
        expected_monitor_ids: set[str],
        timeout_seconds: int,
        poll_interval_seconds: float = 5.0,
        on_poll: Callable[[int, float], None] | None = None,
    ) -> TestRecord:
        """Wait for an enabled monitor-based test with an exact preserved configuration."""
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            test = self.get_test(aid, test_type, test_id)
            ready = (
                test.enabled
                and test.name == expected_name
                and str(test.raw.get("prefix", "")) == expected_prefix
                and bool(test.raw.get("usePublicBgp", False))
                is expected_use_public_bgp
                and self._test_monitor_ids(test) == expected_monitor_ids
                and not self._test_agent_ids(test)
            )
            if ready:
                return test
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    f"Target monitor-based test {test_id} did not become enabled with its "
                    f"exact name, prefix, monitor set, and no agent associations within "
                    f"{timeout_seconds} seconds"
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def wait_for_test_absent(
        self,
        aid: str,
        *,
        test_id: str,
        timeout_seconds: int,
        poll_interval_seconds: float = 5.0,
        on_poll: Callable[[int, float], None] | None = None,
    ) -> None:
        """Wait until a deleted source test is no longer returned by the API."""
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            if all(test.test_id != test_id for test in self.list_tests(aid)):
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    f"Source test {test_id} was still returned within "
                    f"{timeout_seconds} seconds after deletion"
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def delete_agent(self, aid: str, agent_id: str) -> None:
        self.request(
            "DELETE",
            f"agents/{agent_id}",
            aid=aid,
            expected={200, 204},
            safe_to_retry=False,
        )

    def update_agent_name(self, aid: str, agent_id: str, name: str) -> None:
        self.request(
            "PUT",
            f"agents/{agent_id}",
            aid=aid,
            json_body={"agentName": name},
            expected={200},
            safe_to_retry=False,
        )

    def assign_tests(self, aid: str, agent_id: str, test_ids: list[str]) -> None:
        if not test_ids:
            return
        self.request(
            "POST",
            f"agents/{agent_id}/tests/assign",
            aid=aid,
            json_body={"testIds": [str(value) for value in test_ids]},
            expected={200, 201, 204},
        )
