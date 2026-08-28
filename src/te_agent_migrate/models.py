"""Typed domain models shared by the CLI, clients, workflow, and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Mode(StrEnum):
    DRY_RUN = "dry-run"
    APPLY = "apply"


class TestStrategy(StrEnum):
    AGENTS_ONLY = "A"
    SHARE = "B"
    RECREATE = "C"


class StalePolicy(StrEnum):
    KEEP = "keep"
    REMOVE = "remove"


class StepStatus(StrEnum):
    OK = "ok"
    CHANGED = "changed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class InventoryAgent:
    hostname: str
    ip_address: str


@dataclass(frozen=True, slots=True)
class AccountGroup:
    aid: str
    name: str
    is_current: bool = False

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> AccountGroup:
        aid = value.get("aid", value.get("accountGroupId", value.get("id", "")))
        name = value.get("accountGroupName", value.get("name", str(aid)))
        return cls(str(aid), str(name), bool(value.get("current", value.get("isCurrent", False))))


@dataclass(slots=True)
class AgentRecord:
    agent_id: str
    name: str
    hostname: str | None
    ip_addresses: list[str]
    state: str
    aid: str
    account_group_name: str | None = None
    labels: list[dict[str, Any]] = field(default_factory=list)
    test_ids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    agent_type: str | None = None

    @property
    def online(self) -> bool:
        return self.state.lower() == "online"

    @classmethod
    def from_api(cls, value: dict[str, Any], aid: str) -> AgentRecord:
        raw_ips = value.get("ipAddresses", value.get("ipAddress", []))
        if isinstance(raw_ips, str):
            ips = [raw_ips]
        elif isinstance(raw_ips, list):
            ips = [
                str(ip.get("ipAddress", ip)) if isinstance(ip, dict) else str(ip) for ip in raw_ips
            ]
        else:
            ips = []
        raw_tests = value.get("testIds", value.get("tests", [])) or []
        tests: list[str] = []
        for item in raw_tests:
            test_id = item.get("testId", item.get("id")) if isinstance(item, dict) else item
            if test_id is not None:
                tests.append(str(test_id))
        return cls(
            agent_id=str(value.get("agentId", value.get("id", ""))),
            name=str(value.get("agentName", value.get("name", ""))),
            hostname=value.get("hostname"),
            ip_addresses=ips,
            state=str(value.get("agentState", value.get("state", "unknown"))),
            aid=aid,
            account_group_name=value.get("accountGroupName"),
            labels=list(value.get("labels", value.get("tags", [])) or []),
            test_ids=tests,
            raw=value,
            agent_type=(
                str(value["agentType"]).casefold()
                if value.get("agentType") is not None
                else None
            ),
        )


@dataclass(slots=True)
class TestRecord:
    test_id: str
    name: str
    test_type: str
    aid: str
    enabled: bool
    agents: list[dict[str, Any]] = field(default_factory=list)
    shared_with: list[dict[str, Any]] = field(default_factory=list)
    alert_rules: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, value: dict[str, Any], aid: str) -> TestRecord:
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


@dataclass(slots=True)
class LocalSnapshot:
    agent: str
    captured_at: str
    state: dict[str, Any]


@dataclass(slots=True)
class StepEvent:
    timestamp: str
    correlation_id: str
    agent: str | None
    step: str
    status: StepStatus
    duration_seconds: float
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True, slots=True)
class RunOptions:
    mode: Mode
    strategy: TestStrategy
    stale_policy: StalePolicy
    timeout_seconds: int = 180
    parallelism: int = 1
    verbose: bool = False
    create_missing_tags: bool = False
    create_missing_alerts: bool = False
