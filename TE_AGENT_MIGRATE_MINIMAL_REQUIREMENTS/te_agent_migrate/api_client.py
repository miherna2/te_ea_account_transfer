"""ThousandEyes API v7 client using the approved Requests dependency."""

import json
import re
import time
from threading import RLock
from urllib.parse import urljoin

import requests

from te_agent_migrate.errors import ApiError, VisibilityTimeout
from te_agent_migrate.http import RetryPolicy, TRANSIENT_STATUS_CODES
from te_agent_migrate.models import AccountGroup, AgentRecord, TestRecord

WRITE_METHODS = ("POST", "PUT", "DELETE", "PATCH")
WRITE_429_RETRIES = 4
MAX_RETRY_AFTER_SECONDS = 60.0
CLUSTER_AGENT_TYPE = "enterprise-cluster"
TAG_ASSIGNMENT_TYPES = (
    "test",
    "v-agent",
    "endpoint-test",
    "dashboard",
    "connected-devices-test",
)


def _api_reason(response, limit=400):
    """The short reason text of a 4xx answer: ``: <text>``. Empty when there is none."""
    from te_agent_migrate.output import mask_secrets
    try:
        body = response.json()
    except ValueError:
        body = None
    parts = []
    if isinstance(body, dict):
        for key in ("errorMessage", "message", "error", "detail", "errors"):
            value = body.get(key)
            if value:
                parts.append("%s=%s" % (key, value))
    else:
        text = getattr(response, "text", "") or ""
        if text.strip():
            parts.append(text.strip())
    if not parts and isinstance(body, dict) and body:
        parts.append("body=%s" % json.dumps(body, sort_keys=True))  # unknown shape: show the whole answer
    if not parts:
        return ""
    return ": " + mask_secrets(" ".join(" ".join(parts).split()))[:limit]


RESPONSE_ONLY_FIELDS = frozenset({
    '_links', 'apiLinks', 'createdBy', 'createdDate', 'createdAt', 'id', 'liveShare', 'modifiedBy',
    'modifiedDate', 'modifiedAt', 'savedEvent', 'sslVersion', 'testId', 'testResults', 'testType',
    'type', 'versionId',
})  # fields of a test GET that a PUT must not send back


def _contains_masked_secret(value, sensitive=False):
    """Recognize placeholders under secret fields without reporting their values."""
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key).casefold().replace('_', '').replace('-', '')
            secret_field = any(part in name for part in (
                'password', 'passwd', 'secret', 'token', 'authorization', 'cookie',
                'credentials', 'headers', 'oauth', 'transactionscript', 'privatekey', 'clientcertificate',
            ))
            if _contains_masked_secret(child, sensitive or secret_field):
                return True
    elif isinstance(value, list):
        return any(_contains_masked_secret(child, sensitive) for child in value)
    elif sensitive and isinstance(value, str):
        text = value.strip().casefold()
        # Headers, OAuth form bodies and scripts can embed a masked value in a larger string.
        # Split only at value separators so ordinary glob patterns and partial words stay intact.
        candidates = [text] + [part.strip('"\'(){}') for part in re.split(r'[\s=:;&,]+', text)]
        return any(
            candidate in ('[redacted]', '<redacted>', 'redacted', '[masked]', '<masked>', 'masked')
            or (len(candidate) >= 3 and set(candidate) <= set('*\u2022'))
            or (len(candidate) >= 4 and set(candidate) == set('x'))
            for candidate in candidates
        )
    return False


class AmbiguousAgentMatch(ApiError):
    """More than one new agent qualifies as the registered device. The caller must stop."""

    def __init__(self, message, agent_ids=None):
        ApiError.__init__(self, message)
        self.agent_ids = list(agent_ids or [])


class A2aTargetChanged(ApiError):
    """The target agent of an agent-to-agent test is not the expected old id. Nothing was written."""


def retry_after_seconds(value, fallback):
    """Seconds to wait from a Retry-After header: decimals allowed, capped at 60. Bad value -> fallback."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return fallback
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _write_body(response):
    """Body of a successful write. The server already applied it, so any 2xx body is accepted.

    Object -> the object. Array -> ``{'_list': [...]}``. Empty, non-JSON or scalar -> ``{}``.
    """
    if response.status_code == 204 or not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"_list": payload}
    return {}


class ThousandEyesApiClient(object):
    def __init__(
        self,
        bearer_token,
        base_url="https://api.thousandeyes.com/v7/",
        retry_policy=None,
        session=None,
        sleep=time.sleep,
        monotonic=time.monotonic,
        error_detail=False,
    ):
        # error_detail: add the reason that the API sends with a 4xx answer to the error text.
        # OFF by default: an answer may repeat submitted values. Only the sandbox tools turn it on.
        self._error_detail = bool(error_detail)
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
        # Writes are never retried, except on HTTP 429: the server rejected the
        # request without executing it, so repeating it cannot duplicate effects.
        reject_only_retry = (not retryable) and method.upper() in WRITE_METHODS
        if retryable:
            attempts = self._retry.attempts
        elif reject_only_retry:
            attempts = 1 + WRITE_429_RETRIES
        else:
            attempts = 1
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
                if attempt == attempts or not retryable:
                    raise ApiError("API %s %s failed: %s" % (method, path, exc))
                self._sleep(self._retry.delay(attempt))
                continue
            if response.status_code in expected:
                if method.upper() in WRITE_METHODS:
                    return _write_body(response)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    payload = response.json()
                except ValueError:
                    raise ApiError("API %s %s returned invalid JSON" % (method, path))
                if not isinstance(payload, dict):
                    raise ApiError("API %s %s returned an unexpected JSON shape" % (method, path))
                return payload
            retry_status = (
                response.status_code == 429
                if reject_only_retry
                else response.status_code in TRANSIENT_STATUS_CODES
            )
            if retry_status and attempt < attempts:
                retry_after = response.headers.get("retry-after")
                delay = retry_after_seconds(retry_after, self._retry.delay(attempt))
                self._sleep(delay)
                continue
            break
        request_id = response.headers.get("x-request-id", "not-provided")
        detail = ""
        if self._error_detail and 400 <= response.status_code < 500:
            detail = _api_reason(response)
        raise ApiError(
            "API %s %s returned HTTP %s (request-id=%s)%s"
            % (method, path, response.status_code, request_id, detail)
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

    def list_agents(self, aid, agent_types="enterprise"):
        params = {"agentTypes": agent_types, "expand": "test-ids"}
        return [
            AgentRecord.from_api(item, aid)
            for item in self._paginate("agents", "agents", aid=aid, params=params)
        ]

    def list_agents_with_clusters(self, aid):
        params = {
            "agentTypes": "enterprise,enterprise-cluster",
            "expand": "cluster-member,test-ids",
        }
        return [
            AgentRecord.from_api(item, aid)
            for item in self._paginate("agents", "agents", aid=aid, params=params)
        ]

    def get_agent(self, aid, agent_id):
        payload = self.request(
            "GET",
            "agents/%s" % agent_id,
            aid=aid,
            params={"expand": "cluster-member"},
        )
        raw = payload
        if "agentId" not in payload and "id" not in payload:
            nested = payload.get("agent")
            if nested is None and isinstance(payload.get("agents"), list) and payload["agents"]:
                nested = payload["agents"][0]
            if isinstance(nested, dict):
                raw = nested
        if not isinstance(raw, dict):
            raise ApiError("Agent %s returned an unexpected JSON shape" % agent_id)
        return AgentRecord.from_api(raw, aid)

    def set_agent_account_groups(self, aid, agent_id, account_group_ids):
        cleaned = []
        for value in account_group_ids or []:
            if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
                raise ApiError("Account group ids must be non-empty strings or integers")
            text = str(value).strip()
            if text not in cleaned:
                cleaned.append(text)
        if not cleaned:
            raise ApiError(
                "Refusing to set an empty account group list: it would remove every share"
            )
        return self.request(
            "PUT",
            "agents/%s" % agent_id,
            aid=aid,
            json_body={"accountGroups": cleaned},
            expected=set((200,)),
            safe_to_retry=False,
        )

    def cluster_assign(self, aid, base_agent_id, agent_ids):
        base = str(base_agent_id)
        members = [str(value) for value in (agent_ids or [])]
        if base in members:
            raise ApiError("The base agent %s must not be in its own cluster assign list" % base)
        deduped = []
        for value in members:
            if not value.strip():
                raise ApiError("Cluster member agent ids must be non-empty")
            if value not in deduped:
                deduped.append(value)
        return self.request(
            "POST",
            "agents/%s/cluster/assign" % base,
            aid=aid,
            json_body={"agents": deduped},
            expected=set((200, 201, 204)),
            safe_to_retry=False,
        )

    def cluster_unassign(self, aid, cluster_id, member_ids):
        members = []
        for value in member_ids or []:
            text = str(value).strip()
            if not text:
                raise ApiError("Cluster member ids must be non-empty")
            if text not in members:
                members.append(text)
        if not members:
            raise ApiError("Refusing cluster unassign with no member ids")
        return self.request(
            "POST",
            "agents/%s/cluster/unassign" % cluster_id,
            aid=aid,
            json_body={"members": members},
            expected=set((200, 201, 204)),
            safe_to_retry=False,
        )

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

    @staticmethod
    def match_new_agents(agents, names, ips, known_ids, online_only=True):
        """Strict match for a device that registered again after a reset. Returns every qualifying record.

        A record qualifies when ALL of these hold: its id is not in ``known_ids``; it is online (when
        ``online_only``); one of its IPs is in ``ips``; and its hostname or its name equals one of ``names``,
        or its name starts with ``<name>-`` (case-insensitive). The agent may register under a name such
        as ``name-xxxx``. A name that only shares a prefix (``name2``, ``namebox``) does not qualify.
        """
        folded = [str(n).casefold() for n in names if n]
        wanted_ips = set(str(i) for i in ips if i)
        known = set(str(i) for i in known_ids)
        result = []
        for agent in agents:
            if agent.agent_id in known:
                continue
            if online_only and not agent.online:
                continue
            if not wanted_ips.intersection(agent.ip_addresses):
                continue
            name = (agent.name or "").casefold()
            host = (agent.hostname or "").casefold()
            if any(host == n or name == n or name.startswith(n + "-") for n in folded):
                result.append(agent)
        return result

    def wait_for_agent(
        self,
        aid,
        hostname,
        ip_address,
        timeout_seconds,
        poll_interval_seconds=5.0,
        on_poll=None,
        on_not_visible=None,
        known_ids=None,
        alt_names=None,
        alt_ips=None,
    ):
        """Wait for an online agent in ``aid``.

        Default (``known_ids`` is None): old behavior, IP or name or hostname, highest id wins.
        Strict mode (``known_ids`` is a collection of agent ids, may be empty): only a record that is not
        in ``known_ids`` and whose IP and name both match qualifies (see ``match_new_agents``);
        ``alt_names`` and ``alt_ips`` add more accepted names and IPs. Two or more qualifying records
        raise ``AmbiguousAgentMatch``.
        """
        deadline = self._monotonic() + timeout_seconds
        poll_number = 0
        while True:
            poll_number += 1
            remaining = max(0.0, deadline - self._monotonic())
            if on_poll is not None:
                on_poll(poll_number, remaining)
            if known_ids is None:
                agent = self.match_agent(
                    self.list_agents(aid),
                    hostname=hostname,
                    ip_address=ip_address,
                    online_only=True,
                )
            else:
                found = self.match_new_agents(
                    self.list_agents(aid),
                    [hostname] + list(alt_names or []),
                    [ip_address] + list(alt_ips or []),
                    known_ids,
                )
                if len(found) > 1:
                    ids = sorted(item.agent_id for item in found)
                    raise AmbiguousAgentMatch(
                        "More than one new online agent matches %s in account group %s (ids %s)"
                        % (hostname, aid, ", ".join(ids)),
                        ids,
                    )
                agent = found[0] if found else None
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

    def list_tags(self, aid, expand_assignments=False):
        params = {"expand": "assignments"} if expand_assignments else None
        return list(self._paginate("tags", "tags", aid=aid, params=params))

    def assign_tag(self, aid, tag_id, assignments):
        """Assign a tag to objects. ``assignments``: (id, type) tuples or dicts."""
        normalized = []
        for item in assignments or []:
            if isinstance(item, dict):
                object_id, object_type = item.get("id"), item.get("type")
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                object_id, object_type = item
            else:
                raise ApiError("Tag assignments must be (id, type) tuples or dicts")
            if isinstance(object_id, bool) or not isinstance(object_id, (str, int)) or not str(object_id).strip():
                raise ApiError("Tag assignment id must be a non-empty string or integer")
            if object_type not in TAG_ASSIGNMENT_TYPES:
                raise ApiError(
                    "Unsupported tag assignment type %r; allowed: %s"
                    % (object_type, ", ".join(TAG_ASSIGNMENT_TYPES))
                )
            normalized.append({"id": str(object_id).strip(), "type": object_type})
        if not normalized:
            raise ApiError("Refusing to assign a tag with no assignments")
        if not str(tag_id).strip():
            raise ApiError("Tag id must not be empty")
        return self.request(
            "POST",
            "tags/%s/assign" % tag_id,
            aid=aid,
            json_body={"assignments": normalized},
            expected=set((200, 201, 204)),
            safe_to_retry=False,
        )

    def create_tag(self, aid, payload):
        response = self.request(
            "POST", "tags", aid=aid, json_body=payload, expected=set((200, 201)), safe_to_retry=False
        )
        if "_list" in response:
            raise ApiError("Created tag response had an unexpected shape")
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
        if "_list" in response:
            raise ApiError("Created alert-rule response had an unexpected shape")
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
        if "_list" in response or not isinstance(raw, dict):
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
    def _test_agent_assignments(test):
        """Writable agent assignment fields, including an explicitly selected source interface."""
        assignments = {}
        for agent in test.agents:
            value = agent.get("agentId", agent.get("id")) if isinstance(agent, dict) else agent
            agent_id = ThousandEyesApiClient._scalar_id(value)
            if agent_id is None:
                raise ApiError("Test %s has an unreadable source agent id; nothing written" % test.test_id)
            assignment = {"agentId": agent_id}
            if isinstance(agent, dict) and agent.get("sourceIpAddress") is not None:
                if not isinstance(agent["sourceIpAddress"], str):
                    raise ApiError("Test %s has an invalid source interface setting" % test.test_id)
                assignment["sourceIpAddress"] = agent["sourceIpAddress"]
            if agent_id in assignments and assignments[agent_id] != assignment:
                raise ApiError("Test %s has conflicting source interface settings" % test.test_id)
            assignments[agent_id] = assignment
        return [assignments[agent_id] for agent_id in sorted(assignments)]

    def delete_agent(self, aid, agent_id, agent_type=None):
        if str(agent_type or "").casefold() == CLUSTER_AGENT_TYPE:
            raise ApiError(
                "Refusing to delete a cluster: deleting a cluster deletes its member agents"
            )
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

    def _full_put_payload(self, test):
        """Build the body of a full-payload PUT from a test read with ``get_test``.

        Response-only fields are dropped. Expanded objects (agents, tags, alert
        rules, monitors, shared accounts) are reduced to the forms a PUT accepts;
        source interface selections remain attached to their agent ids.
        The caller changes the one field it wants to change and sends the result with ``update_test``.
        Shared by ``set_test_enabled`` and ``set_agent_to_agent_target``.
        """
        from copy import deepcopy

        if _contains_masked_secret(test.raw):
            raise ApiError("Test %s contains masked secret fields; update it manually instead of a full PUT"
                           % test.test_id)
        payload = deepcopy(test.raw)
        for field in RESPONSE_ONLY_FIELDS:
            payload.pop(field, None)
        for field in ("labels", "alerts"):
            payload.pop(field, None)

        def ids_of(values, *keys):
            result = []
            for value in values if isinstance(values, list) else []:
                found = next((value[k] for k in keys if isinstance(value, dict) and value.get(k) is not None), None)
                found = value if not isinstance(value, dict) else found
                if found is not None and str(found) and str(found) not in result:
                    result.append(str(found))
            return result

        payload.pop("agents", None)
        assignments = self._test_agent_assignments(test)
        if assignments:
            payload["agents"] = assignments
        if "alertRules" in payload:
            payload["alertRules"] = ids_of(payload["alertRules"], "ruleId", "id")
        if "tags" in payload:
            payload["tags"] = ids_of(payload["tags"], "tagId", "id")
        if "monitors" in payload:
            payload["monitors"] = ids_of(payload["monitors"], "monitorId", "id")
        if "sharedWithAccounts" in payload:
            shares = []
            for item in payload["sharedWithAccounts"] or []:
                value = item.get("aid", item.get("accountGroupId")) if isinstance(item, dict) else item
                if value is not None and str(value):
                    shares.append(int(value) if str(value).isdigit() else str(value))
            # Lab proof (sandbox, PUT agent-to-agent): the API refuses [{"aid": N}] with HTTP 400
            # "Invalid value for fields: sharedWithAccounts" and accepts a plain list of group ids.
            payload["sharedWithAccounts"] = shares
        return payload

    def set_test_enabled(self, aid, test_type, test_id, enabled):
        """Set only the ``enabled`` flag of one test (read, change one field, PUT the full payload).

        Response-only fields are dropped. Expanded objects (agents, tags, alert
        rules, monitors, shared accounts) are reduced to the id forms a PUT accepts.
        Lab check: a PUT with this payload must not change anything else on the test.
        """
        test = self.get_test(aid, test_type, test_id)
        payload = self._full_put_payload(test)
        payload["enabled"] = bool(enabled)
        return self.update_test(aid, test_type, test_id, payload)

    @staticmethod
    def _scalar_id(value):
        """Text of a plain id value (str or int). Anything else (None, bool, dict, list, empty) -> None."""
        if value is None or isinstance(value, (bool, dict, list, tuple)):
            return None
        text = str(value).strip()
        return text or None

    def set_agent_to_agent_target(self, test_id, new_agent_id, aid, expected_old_agent_id):
        """Point one agent-to-agent test at a new target agent. Change ONLY ``targetAgentId``.

        UNVERIFIED against a real API (lab check): endpoint ``tests/agent-to-agent/{test_id}?aid=<aid>``
        (GET, then PUT of the full payload), field ``targetAgentId`` (scalar id).

        Steps:
        1. GET the test. Its ``targetAgentId`` must be a plain id, else ``ApiError`` (nothing written).
        2. Target already equals ``new_agent_id`` -> return ``'already'`` (no write).
        3. Target differs from ``expected_old_agent_id`` -> raise ``A2aTargetChanged`` (someone changed it;
           nothing written).
        4. PUT the full payload (same builder and same PUT as ``set_test_enabled``: response-only fields
           dropped, expanded objects reduced to ids, 429-only write retry) with only ``targetAgentId`` changed.
        5. GET again. ``targetAgentId`` must be the new id, and ``enabled``, source agent ids and source
           interface selections must be the same as before the PUT, else ``ApiError``. Return ``'changed'``.

        Messages carry ids only, never payload values.
        """
        test_type = "agent-to-agent"
        test_id, new_id, old_id = str(test_id), str(new_agent_id), str(expected_old_agent_id)
        if new_id == old_id:
            raise ApiError("a2a test %s: the new target id equals the old id (%s); nothing written" % (test_id, old_id))
        before = self.get_test(aid, test_type, test_id)
        self._require_a2a(before, test_id)
        current = self._scalar_id(before.raw.get("targetAgentId"))
        if current is None:
            raise ApiError(
                "a2a test %s in aid %s has no readable targetAgentId; nothing written" % (test_id, aid)
            )
        if current == new_id:
            return "already"
        if current != old_id:
            raise A2aTargetChanged(
                "a2a test %s in aid %s: targetAgentId is %s, expected old id %s; someone changed it; "
                "nothing written" % (test_id, aid, current, old_id)
            )
        payload = self._full_put_payload(before)
        payload["targetAgentId"] = new_id
        self.update_test(aid, test_type, test_id, payload)
        after = self.get_test(aid, test_type, test_id)
        found = self._scalar_id(after.raw.get("targetAgentId"))
        if found != new_id:
            raise ApiError(
                "a2a test %s in aid %s: after the PUT targetAgentId is %s, expected %s"
                % (test_id, aid, found or "unreadable", new_id)
            )
        if bool(after.raw.get("enabled", True)) != bool(before.raw.get("enabled", True)):
            raise ApiError("a2a test %s in aid %s: the enabled flag changed during the PUT" % (test_id, aid))
        if self._test_agent_assignments(after) != self._test_agent_assignments(before):
            raise ApiError("a2a test %s in aid %s: the source agent list or interface selections changed during the PUT"
                           % (test_id, aid))
        return "changed"

    @staticmethod
    def _require_a2a(test, test_id):
        kind = str(test.raw.get("type", test.raw.get("testType", "agent-to-agent"))).lower()
        if "agent-to-agent" not in kind:
            raise ApiError("test %s is not an agent-to-agent test (type %s); nothing written" % (test_id, kind))

    def set_test_enabled_minimal(self, aid, test_type, test_id, enabled):
        """EXPERIMENTAL (lab check 7): PUT only ``{"enabled": <bool>}``, no other field.

        Unlike ``set_test_enabled`` this sends no copy of the test, so it cannot overwrite masked
        passwords, auth headers or ``transactionScript``. The lab must prove that the API accepts a
        partial body and leaves every other field unchanged. Until then, use it only on a lab test.
        """
        return self.request(
            "PUT",
            "tests/%s/%s" % (test_type, test_id),
            aid=aid,
            json_body={"enabled": bool(enabled)},
            expected=set((200, 204)),
            safe_to_retry=False,
        )
