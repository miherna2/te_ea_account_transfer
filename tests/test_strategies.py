from typing import Any

import pytest

from te_agent_migrate.errors import ApiError
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
        self.source_tags: list[dict[str, Any]] = []
        self.target_tags: list[dict[str, Any]] = []
        self.source_alert_rules: list[dict[str, Any]] = []
        self.target_alert_rules: list[dict[str, Any]] = []
        self.created_tags: list[dict[str, Any]] = []
        self.created_alert_rules: list[dict[str, Any]] = []

    def create_test(self, _aid: str, _type: str, payload: dict[str, Any]) -> TeTestRecord:
        self.created.append(payload)
        created = TeTestRecord(
            "new",
            payload["testName"],
            _type,
            _aid,
            True,
            agents=payload.get("agents", []),
            raw=payload,
        )
        self.target_tests[created.test_id] = created
        return created

    def update_test(self, _aid: str, _type: str, _id: str, payload: dict[str, Any]) -> TeTestRecord:
        self.updated.append(payload)
        updated = TeTestRecord(
            _id,
            payload["testName"],
            _type,
            _aid,
            True,
            agents=payload.get("agents", []),
            raw=payload,
        )
        self.target_tests[_id] = updated
        return updated

    def get_test(self, _aid: str, _type: str, test_id: str) -> TeTestRecord:
        return self.target_tests[test_id]

    def assign_tests(self, _aid: str, _agent_id: str, test_ids: list[str]) -> None:
        self.assigned.append(test_ids)

    def list_tags(self, aid: str) -> list[dict[str, Any]]:
        return list(self.source_tags if aid == "source" else self.target_tags)

    def create_tag(self, _aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_tags.append(payload)
        created = {**payload, "id": f"target-tag-{len(self.created_tags)}"}
        return created

    def list_alert_rules(self, aid: str) -> list[dict[str, Any]]:
        return list(
            self.source_alert_rules if aid == "source" else self.target_alert_rules
        )

    def create_alert_rule(self, _aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_alert_rules.append(payload)
        created = {**payload, "ruleId": f"target-rule-{len(self.created_alert_rules)}"}
        return created


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


def source_tag() -> dict[str, Any]:
    return {
        "id": "source-tag",
        "key": "team",
        "value": "netops",
        "objectType": "test",
        "type": "static",
        "color": "#005073",
        "description": "Network operations",
        "accessType": "all",
    }


def source_alert_rule(expression: str = "responseTime > 1000") -> dict[str, Any]:
    return {
        "ruleId": "source-rule",
        "ruleName": "High latency",
        "expression": expression,
        "alertType": "http-server",
        "roundsViolatingOutOf": 3,
        "roundsViolatingRequired": 2,
        "roundsViolatingMode": "any",
        "sensitivityLevel": "medium",
        "notifyOnClear": True,
        "severity": "major",
        "notifications": {"email": {"recipients": ["netops@example.invalid"]}},
    }


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


def test_bgp_recreate_payload_preserves_monitors_and_omits_agents() -> None:
    test = source_test("bgp")
    test.raw.pop("agents")

    payload = build_recreate_payload(test, target_agent(), include_agents=False)

    assert payload["testName"] == "Original"
    assert payload["enabled"] is True
    assert payload["monitors"] == ["3", "4"]
    assert "agents" not in payload


def test_bgp_recreate_creates_once_without_agent_assignment() -> None:
    api = FakeApi()
    test = source_test("bgp")
    test.raw.pop("agents")

    action = StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=test,
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
    )

    assert action.action == "recreate-monitor-based"
    assert len(api.created) == 1
    assert "agents" not in api.created[0]
    assert api.assigned == []


def test_recreate_payload_uses_only_target_tag_and_alert_rule_ids() -> None:
    payload = build_recreate_payload(
        source_test(),
        target_agent(),
        tag_ids=["target-tag"],
        alert_rule_ids=["target-rule"],
    )

    assert payload["tags"] == ["target-tag"]
    assert payload["alertRules"] == ["target-rule"]


def test_recreate_preserves_portable_cloud_agent_ids() -> None:
    api = FakeApi()

    StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=source_test(),
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
        preserved_agent_ids=["320"],
    )

    payload = api.created[0]
    assert payload["agents"] == [
        {"agentId": "320"},
        {"agentId": "20"},
    ]


def test_recreate_reconciles_and_creates_missing_tags_and_alert_rules() -> None:
    api = FakeApi()
    api.source_tags = [source_tag()]
    api.source_alert_rules = [source_alert_rule()]
    test = source_test()
    test.raw["tags"] = [{"id": "source-tag"}]
    test.raw["alertRules"] = [{"ruleId": "source-rule"}]

    action = StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=test,
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
        create_missing_tags=True,
        create_missing_alerts=True,
    )

    assert api.created_tags == [
        {
            "key": "team",
            "value": "netops",
            "objectType": "test",
            "type": "static",
            "color": "#005073",
            "description": "Network operations",
            "accessType": "all",
        }
    ]
    assert api.created_alert_rules[0]["ruleName"] == "High latency"
    assert "ruleId" not in api.created_alert_rules[0]
    assert api.created[0]["tags"] == ["target-tag-1"]
    assert api.created[0]["alertRules"] == ["target-rule-1"]
    assert api.created[0]["alertsEnabled"] is True
    assert action.tags_reconciled == 1
    assert action.alert_rules_reconciled == 1


def test_recreate_reuses_semantically_identical_target_resources() -> None:
    api = FakeApi()
    api.source_tags = [source_tag()]
    api.target_tags = [
        {
            "id": "target-agent-tag",
            "key": "agent-scope",
            "value": "ignored",
            "objectType": "agent",
            "type": "static",
        },
        {**source_tag(), "id": "target-tag"},
    ]
    api.source_alert_rules = [source_alert_rule()]
    api.target_alert_rules = [{**source_alert_rule(), "ruleId": "target-rule"}]
    test = source_test()
    test.raw["tags"] = [{"id": "source-tag"}]
    test.raw["alertRules"] = [{"ruleId": "source-rule"}]

    StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=test,
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
        create_missing_tags=True,
        create_missing_alerts=True,
    )

    assert api.created_tags == []
    assert api.created_alert_rules == []
    assert api.created[0]["tags"] == ["target-tag"]
    assert api.created[0]["alertRules"] == ["target-rule"]


def test_recreate_dry_run_projects_resources_without_creating_them() -> None:
    api = FakeApi()
    api.source_tags = [source_tag()]
    api.source_alert_rules = [source_alert_rule()]
    test = source_test()
    test.raw["tags"] = [{"id": "source-tag"}]
    test.raw["alertRules"] = [{"ruleId": "source-rule"}]

    action = StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=test,
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=True,
        create_missing_tags=True,
        create_missing_alerts=True,
    )

    assert action.status == "projected"
    assert api.created_tags == []
    assert api.created_alert_rules == []
    assert api.created == []
    assert action.tag_actions == ("projected-create:team:netops",)
    assert action.alert_rule_actions == ("projected-create:High latency",)


def test_recreate_stops_on_same_name_alert_rule_with_different_definition() -> None:
    api = FakeApi()
    api.source_alert_rules = [source_alert_rule()]
    api.target_alert_rules = [
        {**source_alert_rule("responseTime > 2000"), "ruleId": "target-rule"}
    ]
    test = source_test()
    test.raw["alertRules"] = [{"ruleId": "source-rule"}]

    with pytest.raises(ApiError, match="same name and type but different"):
        StrategyExecutor(api).apply(  # type: ignore[arg-type]
            strategy=Strategy.RECREATE,
            source_test=test,
            source_aid="source",
            target_aid="target",
            target_agent=target_agent(),
            target_tests=[],
            dry_run=False,
            create_missing_alerts=True,
        )

    assert api.created == []


def test_unrelated_incomplete_target_alert_rule_does_not_block_reconciliation() -> None:
    api = FakeApi()
    api.source_alert_rules = [source_alert_rule()]
    api.target_alert_rules = [
        {"ruleId": "unrelated", "ruleName": "Other", "alertType": "bgp"}
    ]
    test = source_test()
    test.raw["alertRules"] = [{"ruleId": "source-rule"}]

    StrategyExecutor(api).apply(  # type: ignore[arg-type]
        strategy=Strategy.RECREATE,
        source_test=test,
        source_aid="source",
        target_aid="target",
        target_agent=target_agent(),
        target_tests=[],
        dry_run=False,
        create_missing_alerts=True,
    )

    assert api.created_alert_rules[0]["ruleName"] == "High latency"
    assert "roundsViolatingMode" not in api.created_alert_rules[0]
    assert "sensitivityLevel" not in api.created_alert_rules[0]
    assert api.created_alert_rules[0]["notifications"] == {
        "email": {"recipients": ["netops@example.invalid"]}
    }
    assert api.created[0]["alertRules"] == ["target-rule-1"]


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
