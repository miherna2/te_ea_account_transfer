from typing import Any

from te_agent_migrate.models import AgentRecord
from te_agent_migrate.models import TestRecord as TeTestRecord
from te_agent_migrate.models import TestStrategy as Strategy
from te_agent_migrate.strategies import TestStrategyExecutor as StrategyExecutor
from te_agent_migrate.strategies import build_recreate_payload, build_share_payload


class FakeApi:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.assigned: list[list[str]] = []
        self.target_tests: dict[str, TeTestRecord] = {}

    def create_test(self, _aid: str, _type: str, payload: dict[str, Any]) -> TeTestRecord:
        self.created.append(payload)
        created = TeTestRecord(
            "new", payload["testName"], _type, _aid, True, agents=payload["agents"], raw=payload
        )
        self.target_tests[created.test_id] = created
        return created

    def update_test(self, _aid: str, _type: str, _id: str, payload: dict[str, Any]) -> TeTestRecord:
        self.updated.append(payload)
        updated = TeTestRecord(
            _id, payload["testName"], _type, _aid, True, agents=payload["agents"], raw=payload
        )
        self.target_tests[_id] = updated
        return updated

    def get_test(self, _aid: str, _type: str, test_id: str) -> TeTestRecord:
        return self.target_tests[test_id]

    def assign_tests(self, _aid: str, _agent_id: str, test_ids: list[str]) -> None:
        self.assigned.append(test_ids)


def source_test(test_type: str = "http-server") -> TeTestRecord:
    raw = {
        "testId": 10,
        "type": test_type,
        "testName": "Original",
        "url": "https://example.test",
        "interval": 60,
        "enabled": False,
        "alertsEnabled": True,
        "labels": [{"labelId": 1}],
        "alertRules": [{"ruleId": 2}],
        "sharedWithAccounts": [],
        "agents": [{"agentId": 1}],
        "monitors": [{"monitorId": 3, "monitorName": "Public"}, "4"],
        "pathTraceMode": "classic",
        "modifiedBy": "operator@example.test",
        "sslVersion": "Auto",
        "testResults": [{"href": "https://api.example.test/results"}],
        "_links": {"self": {"href": "https://api.example.test/test"}},
    }
    return TeTestRecord(
        "10",
        "Original",
        test_type,
        "source",
        False,
        agents=raw["agents"],
        alert_rules=raw["alertRules"],
        raw=raw,
    )


def target_agent() -> AgentRecord:
    return AgentRecord("20", "Target", "agent-a", ["192.0.2.10"], "online", "target")


def test_recreate_payload_preserves_advanced_settings_but_removes_labels_and_alerts() -> None:
    payload = build_recreate_payload(source_test(), target_agent())

    assert payload["testName"] == "Original"
    assert payload["enabled"] is True
    assert payload["alertsEnabled"] is False
    assert payload["agents"] == [{"agentId": "20"}]
    assert payload["monitors"] == ["3", "4"]
    assert payload["pathTraceMode"] == "classic"
    assert "labels" not in payload
    assert "alertRules" not in payload
    assert "testId" not in payload
    assert "modifiedBy" not in payload
    assert "sslVersion" not in payload
    assert "testResults" not in payload
    assert "_links" not in payload


def test_unknown_recreate_type_is_report_only() -> None:
    api = FakeApi()
    action = StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=source_test("future-test"),
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
    )

    assert action.status == "unsupported"
    assert api.created == []


def test_api_test_type_has_a_recreation_adapter() -> None:
    api = FakeApi()

    action = StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=source_test("api"),
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
    )

    assert action.status == "changed"
    assert api.created[0]["testName"] == "Original"
    assert api.assigned == [["new"]]


def test_recreate_rerun_reuses_existing_exact_name_test() -> None:
    api = FakeApi()
    existing = TeTestRecord(
        "99",
        "Original",
        "http-server",
        "target",
        False,
        agents=[{"agentId": "30"}],
    )
    api.target_tests[existing.test_id] = existing
    action = StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=source_test(),
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[existing],
        dry_run=False,
    )

    assert action.status == "existing"
    assert api.created == []
    assert api.assigned == [["99"]]
    assert api.updated[0]["enabled"] is True
    assert api.updated[0]["agents"] == [{"agentId": "30"}, {"agentId": "20"}]


def test_recreate_does_not_repeat_existing_migration_suffix() -> None:
    api = FakeApi()
    test = source_test()
    test.name = "Original (MT)"
    test.raw["testName"] = test.name

    StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=test,
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
    )

    assert api.created[0]["testName"] == "Original (MT)"


def test_share_payload_preserves_existing_shares_and_appends_target_once() -> None:
    test = source_test()
    test.raw["sharedWithAccounts"] = [
        {"aid": "source", "name": "Source"},
        {"aid": 200, "name": "Existing"},
    ]

    payload = build_share_payload(test, "300")
    repeated = build_share_payload(
        TeTestRecord(
            test.test_id,
            test.name,
            test.test_type,
            test.aid,
            test.enabled,
            raw=payload,
        ),
        "300",
    )

    assert payload["sharedWithAccounts"] == [
        {"aid": "source", "name": "Source"},
        {"aid": 200, "name": "Existing"},
        {"aid": 300},
    ]
    assert repeated["sharedWithAccounts"].count({"aid": 300}) == 1


def test_share_updates_only_when_needed_then_assigns_additively() -> None:
    api = FakeApi()
    test = source_test()

    action = StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.SHARE,
        source_test=test,
        source_aid="source",
        target_aid="300",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
    )

    assert action.status == "changed"
    assert api.updated[0]["sharedWithAccounts"] == [{"aid": 300}]
    assert api.assigned == [["10"]]


def test_share_rerun_skips_update_but_still_assigns_idempotently() -> None:
    api = FakeApi()
    test = source_test()
    test.shared_with = [{"aid": 300, "name": "Target"}]

    StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.SHARE,
        source_test=test,
        source_aid="source",
        target_aid="300",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
    )

    assert api.updated == []
    assert api.assigned == [["10"]]
