"""Python 3.6 compatible domain models."""

from enum import Enum


class ValueEnum(str, Enum):
    """String enum replacement for enum.StrEnum."""

    def __str__(self):
        return self.value


class Mode(ValueEnum):
    DRY_RUN = "dry-run"
    APPLY = "apply"


class TestStrategy(ValueEnum):
    AGENTS_ONLY = "A"
    SHARE = "B"
    RECREATE = "C"
    DIRECT_SHARE = "D"


class StalePolicy(ValueEnum):
    KEEP = "keep"
    REMOVE = "remove"


class StepStatus(ValueEnum):
    OK = "ok"
    CHANGED = "changed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"


def record_to_dict(value):
    """Recursively serialize the small record classes used by this project."""
    if isinstance(value, ValueEnum):
        return value.value
    if isinstance(value, _Record):
        return {
            name: record_to_dict(getattr(value, name))
            for name in value._field_names
        }
    if isinstance(value, dict):
        return {str(key): record_to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [record_to_dict(item) for item in value]
    return value


class _Record(object):
    _field_names = ()

    def __repr__(self):
        values = ", ".join(
            "%s=%r" % (name, getattr(self, name)) for name in self._field_names
        )
        return "%s(%s)" % (self.__class__.__name__, values)

    def __eq__(self, other):
        return (
            type(self) is type(other)
            and all(getattr(self, name) == getattr(other, name) for name in self._field_names)
        )

    def to_dict(self):
        return record_to_dict(self)


class InventoryAgent(_Record):
    """One inventory row: a standalone agent or an Enterprise Agent cluster.

    For clusters ``hostname`` holds the cluster display name and
    ``ip_address`` may be an empty string.
    """

    _field_names = ("hostname", "ip_address", "kind", "agent_id")

    def __init__(self, hostname, ip_address, kind="agent", agent_id=None):
        self.hostname = hostname
        self.ip_address = ip_address
        self.kind = kind
        self.agent_id = agent_id

    @property
    def is_cluster(self):
        return self.kind == "cluster"


class AccountGroup(_Record):
    _field_names = ("aid", "name", "is_current")

    def __init__(self, aid, name, is_current=False):
        self.aid = aid
        self.name = name
        self.is_current = is_current

    @classmethod
    def from_api(cls, value):
        aid = value.get("aid", value.get("accountGroupId", value.get("id", "")))
        name = value.get("accountGroupName", value.get("name", str(aid)))
        current = bool(value.get("current", value.get("isCurrent", False)))
        return cls(str(aid), str(name), current)


class AgentRecord(_Record):
    _field_names = (
        "agent_id",
        "name",
        "hostname",
        "ip_addresses",
        "state",
        "aid",
        "account_group_name",
        "labels",
        "test_ids",
        "raw",
        "agent_type",
        "cluster_members",
        "shared_aids",
    )

    def __init__(
        self,
        agent_id,
        name,
        hostname,
        ip_addresses,
        state,
        aid,
        account_group_name=None,
        labels=None,
        test_ids=None,
        raw=None,
        agent_type=None,
        cluster_members=None,
        shared_aids=None,
    ):
        self.cluster_members = [dict(item) for item in (cluster_members or []) if isinstance(item, dict)]
        self.shared_aids = [str(item) for item in (shared_aids or [])]
        self.agent_id = agent_id
        self.name = name
        self.hostname = hostname
        self.ip_addresses = list(ip_addresses or [])
        self.state = state
        self.aid = aid
        self.account_group_name = account_group_name
        self.labels = list(labels or [])
        self.test_ids = list(test_ids or [])
        self.raw = dict(raw or {})
        self.agent_type = agent_type

    @property
    def online(self):
        return self.state.lower() == "online"

    @property
    def is_cluster(self):
        return self.agent_type == "enterprise-cluster"

    @staticmethod
    def _ip_list(raw_ips):
        if isinstance(raw_ips, str):
            return [raw_ips]
        if isinstance(raw_ips, list):
            return [
                str(item.get("ipAddress", item)) if isinstance(item, dict) else str(item)
                for item in raw_ips
            ]
        return []

    @staticmethod
    def _account_group_ids(raw_groups):
        aids = []
        if not isinstance(raw_groups, list):
            return aids
        for item in raw_groups:
            if isinstance(item, dict):
                value = item.get("aid", item.get("accountGroupId", item.get("id")))
            else:
                value = item
            if value is None or isinstance(value, bool) or not str(value).strip():
                continue
            text = str(value).strip()
            if text not in aids:
                aids.append(text)
        return aids

    @classmethod
    def from_api(cls, value, aid):
        ips = cls._ip_list(value.get("ipAddresses", value.get("ipAddress", [])))
        raw_members = value.get("clusterMembers")
        members = [item for item in raw_members if isinstance(item, dict)] if isinstance(raw_members, list) else []
        for member in members:
            for ip in cls._ip_list(member.get("ipAddresses", member.get("ipAddress", []))):
                if ip not in ips:
                    ips.append(ip)
        tests = []
        for item in value.get("testIds", value.get("tests", [])) or []:
            test_id = item.get("testId", item.get("id")) if isinstance(item, dict) else item
            if test_id is not None:
                tests.append(str(test_id))
        agent_type = value.get("agentType")
        agent_type = str(agent_type).casefold() if agent_type is not None else None
        state_value = value.get("agentState", value.get("state"))
        if state_value is None:
            if (
                agent_type == "enterprise-cluster"
                and members
                and all(str(member.get("agentState", member.get("state", ""))).lower() == "online" for member in members)
            ):
                state_value = "online"
            else:
                state_value = "unknown"
        return cls(
            agent_id=str(value.get("agentId", value.get("id", ""))),
            name=str(value.get("agentName", value.get("name", ""))),
            hostname=value.get("hostname"),
            ip_addresses=ips,
            state=str(state_value),
            aid=aid,
            account_group_name=value.get("accountGroupName"),
            labels=list(value.get("labels", value.get("tags", [])) or []),
            test_ids=tests,
            raw=value,
            agent_type=agent_type,
            cluster_members=members,
            shared_aids=cls._account_group_ids(value.get("accountGroups")),
        )


class TestRecord(_Record):
    _field_names = (
        "test_id",
        "name",
        "test_type",
        "aid",
        "enabled",
        "agents",
        "shared_with",
        "alert_rules",
        "raw",
    )

    def __init__(
        self,
        test_id,
        name,
        test_type,
        aid,
        enabled,
        agents=None,
        shared_with=None,
        alert_rules=None,
        raw=None,
    ):
        self.test_id = test_id
        self.name = name
        self.test_type = test_type
        self.aid = aid
        self.enabled = enabled
        self.agents = list(agents or [])
        self.shared_with = list(shared_with or [])
        self.alert_rules = list(alert_rules or [])
        self.raw = dict(raw or {})

    @classmethod
    def from_api(cls, value, aid):
        return cls(
            test_id=str(value.get("testId", value.get("id", ""))),
            name=str(value.get("testName", value.get("name", ""))),
            test_type=str(value.get("type", value.get("testType", "unknown"))),
            aid=aid,
            enabled=bool(value.get("enabled", True)),
            agents=list(value.get("agents", []) or []),
            shared_with=list(value.get("sharedWithAccounts", []) or []),
            alert_rules=list(value.get("alerts", value.get("alertRules", [])) or []),
            raw=value,
        )


class LocalSnapshot(_Record):
    _field_names = ("agent", "captured_at", "state")

    def __init__(self, agent, captured_at, state):
        self.agent = agent
        self.captured_at = captured_at
        self.state = state


class StepEvent(_Record):
    _field_names = (
        "timestamp",
        "correlation_id",
        "agent",
        "step",
        "status",
        "duration_seconds",
        "detail",
        "data",
    )

    def __init__(
        self,
        timestamp,
        correlation_id,
        agent,
        step,
        status,
        duration_seconds,
        detail="",
        data=None,
    ):
        self.timestamp = timestamp
        self.correlation_id = correlation_id
        self.agent = agent
        self.step = step
        self.status = status
        self.duration_seconds = duration_seconds
        self.detail = detail
        self.data = dict(data or {})


class RunOptions(_Record):
    _field_names = (
        "mode",
        "strategy",
        "stale_policy",
        "timeout_seconds",
        "parallelism",
        "verbose",
        "create_missing_tags",
        "create_missing_alerts",
    )

    def __init__(
        self,
        mode,
        strategy,
        stale_policy,
        timeout_seconds=180,
        parallelism=1,
        verbose=False,
        create_missing_tags=False,
        create_missing_alerts=False,
    ):
        self.mode = mode
        self.strategy = strategy
        self.stale_policy = stale_policy
        self.timeout_seconds = timeout_seconds
        self.parallelism = parallelism
        self.verbose = verbose
        self.create_missing_tags = create_missing_tags
        self.create_missing_alerts = create_missing_alerts
