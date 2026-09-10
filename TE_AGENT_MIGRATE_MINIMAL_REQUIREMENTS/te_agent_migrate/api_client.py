"""ThousandEyes API v7 client using the approved Requests dependency."""

import time
from threading import RLock
from urllib.parse import urljoin

import requests

from te_agent_migrate.errors import ApiError, VisibilityTimeout
from te_agent_migrate.http import RetryPolicy, TRANSIENT_STATUS_CODES
from te_agent_migrate.models import AccountGroup, AgentRecord, TestRecord


class ThousandEyesApiClient(object):
    def __init__(
        self,
        bearer_token,
        base_url="https://api.thousandeyes.com/v7/",
        retry_policy=None,
        session=None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._session = session or requests.Session()
        self._request_lock = RLock()
        self._session.headers.update(
            {
                "Authorization": "Bearer %s" % bearer_token,
                "Accept": "application/json",
            }
        )

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        self._session.close()

    def request(
        self,
        method,
        path,
        aid=None,
        params=None,
        json_body=None,
        expected=None,
        safe_to_retry=None,
    ):
        expected = expected or set((200,))
        retryable = method.upper() in ("GET", "HEAD") if safe_to_retry is None else safe_to_retry
        attempts = self._retry.attempts if retryable else 1
        query = dict(params or {})
        if aid is not None:
            query["aid"] = aid
        response = None
        url = path if str(path).startswith(("http://", "https://")) else urljoin(self.base_url, path)
        for attempt in range(1, attempts + 1):
            try:
                with self._request_lock:
                    response = self._session.request(
                        method,
                        url,
                        params=query or None,
                        json=json_body,
                        timeout=30.0,
                        allow_redirects=False,
                    )
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise ApiError("API %s %s failed: %s" % (method, path, exc))
                self._sleep(self._retry.delay(attempt))
                continue
            if response.status_code in expected:
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    payload = response.json()
                except ValueError:
                    raise ApiError("API %s %s returned invalid JSON" % (method, path))
                if not isinstance(payload, dict):
                    raise ApiError("API %s %s returned an unexpected JSON shape" % (method, path))
                return payload
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < attempts:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else self._retry.delay(attempt)
                self._sleep(delay)
                continue
            break
        request_id = response.headers.get("x-request-id", "not-provided")
        raise ApiError(
            "API %s %s returned HTTP %s (request-id=%s)"
            % (method, path, response.status_code, request_id)
        )

    @staticmethod
    def _items(payload, preferred_key):
        candidates = payload.get(preferred_key, payload.get("items"))
        if candidates is None:
            embedded = payload.get("_embedded", {})
            candidates = embedded.get(preferred_key, []) if isinstance(embedded, dict) else []
        if not isinstance(candidates, list):
            raise ApiError("API response field %r was not a list" % preferred_key)
        return [item for item in candidates if isinstance(item, dict)]

    def _paginate(self, path, preferred_key, aid=None, params=None):
        next_path = path
        query = dict(params or {})
        while next_path:
            payload = self.request("GET", next_path, aid=aid, params=query)
            for item in self._items(payload, preferred_key):
                yield item
            links = payload.get("_links", {})
            next_value = links.get("next") if isinstance(links, dict) else None
            if isinstance(next_value, dict):
                next_value = next_value.get("href")
            if isinstance(next_value, str) and next_value:
                next_path = urljoin(self.base_url, next_value)
                query = {}
                aid = None
            else:
                next_path = None

    def list_account_groups(self):
        return [
            AccountGroup.from_api(item)
            for item in self._paginate("account-groups", "accountGroups")
        ]

    def list_agents(self, aid, agent_types="ENTERPRISE"):
        params = {"agentTypes": agent_types, "expand": "test-ids"}
        return [
            AgentRecord.from_api(item, aid)
            for item in self._paginate("agents", "agents", aid=aid, params=params)
        ]

    @staticmethod
    def match_agent(agents, hostname, ip_address, online_only=False):
        candidates = [
            agent
            for agent in agents
            if ip_address in agent.ip_addresses
            or (agent.hostname or "").casefold() == hostname.casefold()
            or agent.name.casefold() == hostname.casefold()
        ]
        if online_only:
            candidates = [agent for agent in candidates if agent.online]

        def identity_order(agent):
            numeric = int(agent.agent_id) if agent.agent_id.isdigit() else -1
            return agent.online, numeric, agent.agent_id

        return max(candidates, key=identity_order) if candidates else None

    def wait_for_agent(
        self,
        aid,
        hostname,
        ip_address,
        timeout_seconds,
        poll_interval_seconds=5.0,
        on_poll=None,
        on_not_visible=None,
    ):
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            agent = self.match_agent(
                self.list_agents(aid),
                hostname=hostname,
                ip_address=ip_address,
                online_only=True,
            )
            if agent is not None:
                return agent
            if on_not_visible is not None:
                on_not_visible()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    "Destination agent was not visible and online within %s seconds"
                    % timeout_seconds
                )
            self._sleep(min(poll_interval_seconds, remaining))

    @staticmethod
    def find_agent_by_id(agents, agent_id):
        return next((agent for agent in agents if agent.agent_id == agent_id), None)

    def wait_for_agent_absent(
        self,
        aid,
        agent_id,
        timeout_seconds,
        poll_interval_seconds=5.0,
        on_poll=None,
    ):
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            if self.find_agent_by_id(self.list_agents(aid), agent_id) is None:
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    "Source agent %s was still returned within %s seconds after deletion"
                    % (agent_id, timeout_seconds)
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def wait_for_agent_name(
        self,
        aid,
        agent_id,
        expected_name,
        timeout_seconds,
        poll_interval_seconds=5.0,
        on_poll=None,
    ):
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
                    "Destination agent %s did not become online with exact name %r within %s seconds"
                    % (agent_id, expected_name, timeout_seconds)
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def list_tests(self, aid):
        return [
            TestRecord.from_api(item, aid)
            for item in self._paginate("tests", "tests", aid=aid)
        ]

    def list_tags(self, aid):
        return list(self._paginate("tags", "tags", aid=aid))

    def list_monitors(self, aid):
        return list(self._paginate("monitors", "monitors", aid=aid))

    def create_tag(self, aid, payload):
        response = self.request(
            "POST", "tags", aid=aid, json_body=payload, expected=set((200, 201)), safe_to_retry=False
        )
        values = self._items(response, "tags") if isinstance(response.get("tags"), list) else []
        raw = values[0] if values else response.get("tag", response)
        if not isinstance(raw, dict):
            raise ApiError("Created tag response had an unexpected shape")
        return raw

    def list_alert_rules(self, aid):
        return list(self._paginate("alerts/rules", "alertRules", aid=aid))

    def create_alert_rule(self, aid, payload):
        response = self.request(
            "POST",
            "alerts/rules",
            aid=aid,
            json_body=payload,
            expected=set((200, 201)),
            safe_to_retry=False,
        )
        values = self._items(response, "alertRules") if isinstance(response.get("alertRules"), list) else []
        raw = values[0] if values else response.get("alertRule", response)
        if not isinstance(raw, dict):
            raise ApiError("Created alert-rule response had an unexpected shape")
        return raw

    def get_test(self, aid, test_type, test_id):
        payload = self.request(
            "GET",
            "tests/%s/%s" % (test_type, test_id),
            aid=aid,
            params={"expand": "agent,alert-rule,monitor,label,tag,shared-with-account"},
        )
        values = self._items(payload, "test") if isinstance(payload.get("test"), list) else []
        raw = values[0] if values else payload.get("test", payload)
        if not isinstance(raw, dict):
            raise ApiError("Test %s returned an unexpected JSON shape" % test_id)
        return TestRecord.from_api(raw, aid)

    def create_test(self, aid, test_type, payload):
        try:
            response = self.request(
                "POST",
                "tests/%s" % test_type,
                aid=aid,
                json_body=payload,
                expected=set((200, 201)),
            )
        except ApiError as exc:
            fields = ", ".join(sorted(payload))
            raise ApiError("%s; submitted field names=[%s] (values redacted)" % (exc, fields))
        raw = response.get("test", response)
        if not isinstance(raw, dict):
            raise ApiError("Created test response had an unexpected shape")
        return TestRecord.from_api(raw, aid)

    def update_test(self, aid, test_type, test_id, payload):
        response = self.request(
            "PUT",
            "tests/%s/%s" % (test_type, test_id),
            aid=aid,
            json_body=payload,
            expected=set((200,)),
        )
        raw = response.get("test", response)
        if not isinstance(raw, dict):
            raise ApiError("Updated test response had an unexpected shape")
        return TestRecord.from_api(raw, aid)

    def delete_test(self, aid, test_type, test_id):
        self.request(
            "DELETE",
            "tests/%s/%s" % (test_type, test_id),
            aid=aid,
            expected=set((200, 204)),
            safe_to_retry=False,
        )

    @staticmethod
    def _test_agent_ids(test):
        values = set()
        for agent in test.agents:
            value = agent.get("agentId", agent.get("id")) if isinstance(agent, dict) else agent
            if value is not None and str(value):
                values.add(str(value))
        return values

    @staticmethod
    def _test_monitor_ids(test):
        values = set()
        raw_monitors = test.raw.get("monitors", [])
        if not isinstance(raw_monitors, list):
            return values
        for monitor in raw_monitors:
            value = monitor.get("monitorId", monitor.get("id")) if isinstance(monitor, dict) else monitor
            if value is not None and str(value):
                values.add(str(value))
        return values

    def wait_for_test_ready(
        self,
        aid,
        test_type,
        test_id,
        agent_id,
        timeout_seconds,
        poll_interval_seconds=5.0,
        on_poll=None,
    ):
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
                    "Target test %s was not enabled and assigned to agent %s within %s seconds"
                    % (test_id, agent_id, timeout_seconds)
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def wait_for_monitor_test_ready(
        self,
        aid,
        test_type,
        test_id,
        expected_name,
        expected_prefix,
        expected_use_public_bgp,
        expected_monitor_ids,
        timeout_seconds,
        poll_interval_seconds=5.0,
        on_poll=None,
    ):
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
                and bool(test.raw.get("usePublicBgp", False)) is expected_use_public_bgp
                and self._test_monitor_ids(test) == expected_monitor_ids
                and not self._test_agent_ids(test)
            )
            if ready:
                return test
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VisibilityTimeout(
                    "Target monitor-based test %s did not become enabled with its exact configuration within %s seconds"
                    % (test_id, timeout_seconds)
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def wait_for_test_absent(
        self,
        aid,
        test_id,
        timeout_seconds,
        poll_interval_seconds=5.0,
        on_poll=None,
    ):
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
                    "Source test %s was still returned within %s seconds after deletion"
                    % (test_id, timeout_seconds)
                )
            self._sleep(min(poll_interval_seconds, remaining))

    def delete_agent(self, aid, agent_id):
        self.request(
            "DELETE",
            "agents/%s" % agent_id,
            aid=aid,
            expected=set((200, 204)),
            safe_to_retry=False,
        )

    def update_agent_name(self, aid, agent_id, name):
        self.request(
            "PUT",
            "agents/%s" % agent_id,
            aid=aid,
            json_body={"agentName": name},
            expected=set((200,)),
            safe_to_retry=False,
        )

    def assign_tests(self, aid, agent_id, test_ids):
        if not test_ids:
            return
        self.request(
            "POST",
            "agents/%s/tests/assign" % agent_id,
            aid=aid,
            json_body={"testIds": [str(value) for value in test_ids]},
            expected=set((200, 201, 204)),
        )
