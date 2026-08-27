"""API-driven test sharing and recreation strategies."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.models import AgentRecord, TestRecord, TestStrategy

# API v7 test resource names documented by ThousandEyes. Unknown types are reported
# rather than sent to an endpoint without an implemented request adapter.
API_TEST_TYPES = frozenset(
    {
        "api",
        "agent-to-agent",
        "agent-to-server",
        "bgp",
        "dns-server",
        "dns-trace",
        "dnssec",
        "ftp-server",
        "http-server",
        "page-load",
        "sip-server",
        "voice",
        "web-transactions",
    }
)

RESPONSE_ONLY_FIELDS = frozenset(
    {
        "_links",
        "apiLinks",
        "createdBy",
        "createdDate",
        "createdAt",
        "id",
        "liveShare",
        "modifiedBy",
        "modifiedDate",
        "modifiedAt",
        "savedEvent",
        "sslVersion",
        "testId",
        "testResults",
        "testType",
        "type",
        "versionId",
    }
)


@dataclass(frozen=True, slots=True)
class TestAction:
    original_test_id: str
    original_name: str
    test_type: str
    action: str
    status: str
    target_test_id: str | None = None
    target_test_name: str | None = None
    detail: str = ""


def _request_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(raw)
    for field in RESPONSE_ONLY_FIELDS:
        payload.pop(field, None)
    return payload


def _monitor_ids(raw_monitors: Any) -> list[str]:
    if not isinstance(raw_monitors, list):
        return []
    monitor_ids: list[str] = []
    for monitor in raw_monitors:
        value = (
            monitor.get("monitorId", monitor.get("id")) if isinstance(monitor, dict) else monitor
        )
        if value is not None and str(value):
            monitor_ids.append(str(value))
    return monitor_ids


def _agent_ids(agents: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for agent in agents:
        value = agent.get("agentId", agent.get("id")) if isinstance(agent, dict) else agent
        if value is not None and str(value) and str(value) not in values:
            values.append(str(value))
    return values


def build_recreate_payload(
    test: TestRecord,
    target_agent: AgentRecord,
    *,
    agent_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload = _request_payload(test.raw)
    payload.pop("labels", None)
    payload.pop("tags", None)
    payload.pop("alerts", None)
    payload.pop("alertRules", None)
    payload.pop("sharedWithAccounts", None)
    if "monitors" in payload:
        payload["monitors"] = _monitor_ids(payload["monitors"])
    payload["testName"] = test.name
    payload["enabled"] = True
    payload["alertsEnabled"] = False
    assigned_agent_ids = agent_ids or [target_agent.agent_id]
    payload["agents"] = [
        {"agentId": agent_id} for agent_id in dict.fromkeys(map(str, assigned_agent_ids))
    ]
    return payload


def build_share_payload(test: TestRecord, target_aid: str) -> dict[str, Any]:
    payload = _request_payload(test.raw)
    shares = list(payload.get("sharedWithAccounts", []) or [])
    target_value: int | str = int(target_aid) if target_aid.isdigit() else target_aid
    if not any(
        str(item.get("aid", item.get("accountGroupId", item))) == target_aid
        if isinstance(item, dict)
        else str(item) == target_aid
        for item in shares
    ):
        shares.append({"aid": target_value})
    payload["sharedWithAccounts"] = shares
    return payload


def target_is_shared(test: TestRecord, target_aid: str) -> bool:
    for item in test.shared_with:
        if str(item.get("aid", item.get("accountGroupId", ""))) == target_aid:
            return True
    return False

class TestStrategyExecutor:
    def __init__(self, api: ThousandEyesApiClient) -> None:
        self.api = api

    def apply(
        self,
        *,
        strategy: TestStrategy,
        source_test: TestRecord,
        source_aid: str,
        target_aid: str,
        target_agent: AgentRecord,
        target_tests: list[TestRecord],
        dry_run: bool,
    ) -> TestAction:
        if strategy is TestStrategy.AGENTS_ONLY:
            return TestAction(
                source_test.test_id,
                source_test.name,
                source_test.test_type,
                "none",
                "skipped",
                detail="Strategy A leaves source tests unchanged and unassigned in target",
            )
        if strategy is TestStrategy.SHARE:
            if not target_is_shared(source_test, target_aid) and not dry_run:
                payload = build_share_payload(source_test, target_aid)
                self.api.update_test(
                    source_aid, source_test.test_type, source_test.test_id, payload
                )
            if not dry_run:
                self.api.assign_tests(target_aid, target_agent.agent_id, [source_test.test_id])
            return TestAction(
                source_test.test_id,
                source_test.name,
                source_test.test_type,
                "share-and-assign",
                "projected" if dry_run else "changed",
                target_test_id=source_test.test_id,
                target_test_name=source_test.name,
                detail="Source ownership retained; target share/assignment is additive",
            )

        target_name = source_test.name
        existing = next(
            (
                test
                for test in target_tests
                if test.name == target_name and test.test_type == source_test.test_type
            ),
            None,
        )
        if existing is not None:
            if not dry_run:
                current = self.api.get_test(target_aid, existing.test_type, existing.test_id)
                assigned_agent_ids = _agent_ids(current.agents)
                if target_agent.agent_id not in assigned_agent_ids:
                    assigned_agent_ids.append(target_agent.agent_id)
                payload = build_recreate_payload(
                    source_test,
                    target_agent,
                    agent_ids=assigned_agent_ids,
                )
                self.api.update_test(
                    target_aid,
                    existing.test_type,
                    existing.test_id,
                    payload,
                )
                self.api.assign_tests(target_aid, target_agent.agent_id, [existing.test_id])
            return TestAction(
                source_test.test_id,
                source_test.name,
                source_test.test_type,
                "reuse-and-assign",
                "existing",
                target_test_id=existing.test_id,
                target_test_name=existing.name,
                detail="Idempotent rerun reused the existing migrated test",
            )
        if source_test.test_type not in API_TEST_TYPES:
            return TestAction(
                source_test.test_id,
                source_test.name,
                source_test.test_type,
                "report-only",
                "unsupported",
                target_test_name=target_name,
                detail="Test type has no supported API v7 recreation adapter",
            )
        payload = build_recreate_payload(source_test, target_agent)
        created = (
            None if dry_run else self.api.create_test(target_aid, source_test.test_type, payload)
        )
        if created is not None:
            self.api.assign_tests(target_aid, target_agent.agent_id, [created.test_id])
        return TestAction(
            source_test.test_id,
            source_test.name,
            source_test.test_type,
            "recreate",
            "projected" if dry_run else "changed",
            target_test_id=None if created is None else created.test_id,
            target_test_name=target_name,
            detail="Created enabled without source labels or alert rules",
        )


def action_dict(action: TestAction) -> dict[str, Any]:
    return asdict(action)
