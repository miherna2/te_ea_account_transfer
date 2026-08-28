"""API-driven test sharing and recreation strategies."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.errors import ApiError
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

# API v7 tests whose execution resources are BGP monitors rather than agents.
MONITOR_BASED_TEST_TYPES = frozenset({"bgp"})

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

TAG_CREATE_FIELDS = (
    "key",
    "value",
    "objectType",
    "type",
    "color",
    "description",
    "accessType",
)

ALERT_RULE_CREATE_FIELDS = (
    "ruleName",
    "expression",
    "alertType",
    "roundsViolatingOutOf",
    "description",
    "direction",
    "notifyOnClear",
    "isDefault",
    "alertGroupType",
    "minimumSources",
    "minimumSourcesPct",
    "roundsViolatingRequired",
    "includeCoveredPrefixes",
    "severity",
    "notifications",
)

ALERT_RULE_REQUIRED_FIELDS = (
    "ruleName",
    "expression",
    "alertType",
    "roundsViolatingOutOf",
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
    tags_reconciled: int = 0
    alert_rules_reconciled: int = 0
    tag_actions: tuple[str, ...] = ()
    alert_rule_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedTestResources:
    tag_ids: tuple[str, ...] = ()
    alert_rule_ids: tuple[str, ...] = ()
    tag_actions: tuple[str, ...] = ()
    alert_rule_actions: tuple[str, ...] = ()


@dataclass(slots=True)
class ResourcePreparationSummary:
    tags_created: int = 0
    tags_reused: int = 0
    tags_projected: int = 0
    alert_rules_created: int = 0
    alert_rules_reused: int = 0
    alert_rules_projected: int = 0


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


def _resource_id(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and str(candidate):
            return str(candidate)
    return None


def _resource_references(value: Any) -> list[dict[str, Any] | str | int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, (dict, str, int))]


def _tag_references(test: TestRecord) -> list[dict[str, Any] | str | int]:
    tags = _resource_references(test.raw.get("tags"))
    return tags or _resource_references(test.raw.get("labels"))


def _alert_rule_references(test: TestRecord) -> list[dict[str, Any] | str | int]:
    rules = _resource_references(test.raw.get("alertRules"))
    return rules or _resource_references(test.alert_rules)


def _resource_index(
    resources: list[dict[str, Any]], *keys: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for resource in resources:
        for key in keys:
            identifier = resource.get(key)
            if identifier is not None and str(identifier):
                result[str(identifier)] = resource
    return result


def _resolve_reference(
    reference: dict[str, Any] | str | int,
    inventory: dict[str, dict[str, Any]],
    *keys: str,
) -> dict[str, Any]:
    if isinstance(reference, dict):
        identifier = _resource_id(reference, *keys)
        base = inventory.get(identifier, {}) if identifier is not None else {}
        return {**base, **reference}
    return dict(inventory.get(str(reference), {}))


def _tag_payload(source_tag: dict[str, Any]) -> dict[str, Any]:
    payload = {
        field: deepcopy(source_tag[field])
        for field in TAG_CREATE_FIELDS
        if field in source_tag and source_tag[field] is not None
    }
    payload.setdefault("objectType", "test")
    payload.setdefault("type", "static")
    missing = [field for field in ("key", "value") if field not in payload]
    if missing:
        raise ApiError(
            "Source tag cannot be reconciled because required metadata is missing: "
            + ", ".join(missing)
        )
    if str(payload["objectType"]) != "test" or str(payload["type"]) != "static":
        raise ApiError(
            "Only static test tags can be reconciled for Strategy C test migration"
        )
    return payload


def _tag_identity(value: dict[str, Any]) -> tuple[str, str, str, str]:
    payload = _tag_payload(value)
    return (
        str(payload["objectType"]),
        str(payload["type"]),
        str(payload["key"]),
        str(payload["value"]),
    )


def _alert_rule_payload(source_rule: dict[str, Any]) -> dict[str, Any]:
    payload = {
        field: deepcopy(source_rule[field])
        for field in ALERT_RULE_CREATE_FIELDS
        if field in source_rule and source_rule[field] is not None
    }
    missing = [field for field in ALERT_RULE_REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ApiError(
            "Source alert rule cannot be reconciled because required metadata is missing: "
            + ", ".join(missing)
        )
    notifications = payload.get("notifications")
    if notifications is not None and not isinstance(notifications, dict):
        raise ApiError(
            "Source alert rule cannot be reconciled because notifications must be an object"
        )
    return payload


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


def _alert_rule_fingerprint(value: dict[str, Any]) -> str:
    return json.dumps(
        _canonical(_alert_rule_payload(value)),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def build_recreate_payload(
    test: TestRecord,
    target_agent: AgentRecord,
    *,
    agent_ids: list[str] | None = None,
    tag_ids: list[str] | None = None,
    alert_rule_ids: list[str] | None = None,
    include_agents: bool = True,
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
    if tag_ids:
        payload["tags"] = list(dict.fromkeys(map(str, tag_ids)))
    if alert_rule_ids:
        payload["alertRules"] = list(dict.fromkeys(map(str, alert_rule_ids)))
        payload["alertsEnabled"] = bool(test.raw.get("alertsEnabled", True))
    else:
        payload["alertsEnabled"] = False
    payload.pop("agents", None)
    if include_agents:
        assigned_agent_ids = agent_ids or [target_agent.agent_id]
        payload["agents"] = [
            {"agentId": agent_id}
            for agent_id in dict.fromkeys(map(str, assigned_agent_ids))
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
        self._source_tags: dict[str, list[dict[str, Any]]] = {}
        self._target_tags: dict[str, list[dict[str, Any]]] = {}
        self._source_alert_rules: dict[str, list[dict[str, Any]]] = {}
        self._target_alert_rules: dict[str, list[dict[str, Any]]] = {}
        self._prepared: dict[
            tuple[str, str, str, bool, bool, bool], PreparedTestResources
        ] = {}

    def prepare_recreate_resources(
        self,
        *,
        source_tests: list[TestRecord],
        source_aid: str,
        target_aid: str,
        create_missing_tags: bool,
        create_missing_alerts: bool,
        dry_run: bool,
    ) -> ResourcePreparationSummary:
        """Resolve account-local IDs before any Strategy C agent is moved."""
        summary = ResourcePreparationSummary()
        if not create_missing_tags and not create_missing_alerts:
            for test in source_tests:
                key: tuple[str, str, str, bool, bool, bool] = (
                    source_aid,
                    target_aid,
                    test.test_id,
                    create_missing_tags,
                    create_missing_alerts,
                    dry_run,
                )
                self._prepared[key] = PreparedTestResources()
            return summary

        source_tag_index: dict[str, dict[str, Any]] = {}
        target_tags_by_identity: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        if create_missing_tags and any(_tag_references(test) for test in source_tests):
            if source_aid not in self._source_tags:
                self._source_tags[source_aid] = self.api.list_tags(source_aid)
            if target_aid not in self._target_tags:
                self._target_tags[target_aid] = self.api.list_tags(target_aid)
            source_tags = self._source_tags[source_aid]
            target_tags = self._target_tags[target_aid]
            source_tag_index = _resource_index(
                source_tags, "id", "tagId", "legacyId", "labelId"
            )
            for tag in target_tags:
                try:
                    identity = _tag_identity(tag)
                except ApiError:
                    # Tags for agents or other object types are unrelated to
                    # Strategy C test migration and must not block preflight.
                    continue
                if identity in target_tags_by_identity:
                    raise ApiError(
                        "Target account group contains duplicate tags with identity "
                        f"{identity!r}; reconciliation is ambiguous"
                    )
                target_tags_by_identity[identity] = tag

        source_rule_index: dict[str, dict[str, Any]] = {}
        target_rules_by_fingerprint: dict[str, dict[str, Any]] = {}
        target_rules_by_name_type: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if create_missing_alerts and any(
            _alert_rule_references(test) for test in source_tests
        ):
            if source_aid not in self._source_alert_rules:
                self._source_alert_rules[source_aid] = self.api.list_alert_rules(source_aid)
            if target_aid not in self._target_alert_rules:
                self._target_alert_rules[target_aid] = self.api.list_alert_rules(target_aid)
            source_rules = self._source_alert_rules[source_aid]
            target_rules = self._target_alert_rules[target_aid]
            source_rule_index = _resource_index(
                source_rules, "ruleId", "alertRuleId", "id"
            )
            for rule in target_rules:
                try:
                    payload = _alert_rule_payload(rule)
                    fingerprint = _alert_rule_fingerprint(payload)
                except ApiError:
                    rule_name = rule.get("ruleName")
                    alert_type = rule.get("alertType")
                    if rule_name is not None and alert_type is not None:
                        target_rules_by_name_type.setdefault(
                            (str(rule_name), str(alert_type)), []
                        ).append(rule)
                    continue
                if fingerprint in target_rules_by_fingerprint:
                    raise ApiError(
                        "Target account group contains duplicate semantically equivalent alert "
                        "rules; reconciliation is ambiguous"
                    )
                target_rules_by_fingerprint[fingerprint] = rule
                name_type = (str(payload["ruleName"]), str(payload["alertType"]))
                target_rules_by_name_type.setdefault(name_type, []).append(rule)

        resolved_tags: dict[tuple[str, str, str, str], str] = {}
        resolved_rules: dict[str, str] = {}
        for test in source_tests:
            key = (
                source_aid,
                target_aid,
                test.test_id,
                create_missing_tags,
                create_missing_alerts,
                dry_run,
            )
            if key in self._prepared:
                continue

            tag_ids: list[str] = []
            tag_actions: list[str] = []
            if create_missing_tags:
                for reference in _tag_references(test):
                    source_tag = _resolve_reference(
                        reference,
                        source_tag_index,
                        "id",
                        "tagId",
                        "legacyId",
                        "labelId",
                    )
                    payload = _tag_payload(source_tag)
                    identity = _tag_identity(payload)
                    target_id = resolved_tags.get(identity)
                    action = "reused"
                    if target_id is None:
                        target_tag = target_tags_by_identity.get(identity)
                        if target_tag is not None:
                            target_id = _resource_id(target_tag, "id", "tagId")
                            summary.tags_reused += 1
                        else:
                            access_type = str(payload.get("accessType", "all")).lower()
                            if access_type in {"system", "partner"}:
                                raise ApiError(
                                    f"Tag {payload['key']}:{payload['value']} has accessType "
                                    f"{access_type!r} and cannot be user-created in the target"
                                )
                            if dry_run:
                                target_id = "projected-tag:" + "|".join(identity)
                                summary.tags_projected += 1
                                action = "projected-create"
                            else:
                                created = self.api.create_tag(target_aid, payload)
                                target_id = _resource_id(created, "id", "tagId")
                                self._target_tags[target_aid].append(created)
                                target_tags_by_identity[identity] = created
                                summary.tags_created += 1
                                action = "created"
                        if target_id is None:
                            raise ApiError("Target tag response did not include an id")
                        resolved_tags[identity] = target_id
                    tag_ids.append(target_id)
                    tag_actions.append(f"{action}:{payload['key']}:{payload['value']}")

            alert_rule_ids: list[str] = []
            alert_rule_actions: list[str] = []
            if create_missing_alerts:
                for reference in _alert_rule_references(test):
                    source_rule = _resolve_reference(
                        reference,
                        source_rule_index,
                        "ruleId",
                        "alertRuleId",
                        "id",
                    )
                    payload = _alert_rule_payload(source_rule)
                    fingerprint = _alert_rule_fingerprint(payload)
                    target_id = resolved_rules.get(fingerprint)
                    action = "reused"
                    if target_id is None:
                        target_rule = target_rules_by_fingerprint.get(fingerprint)
                        if target_rule is not None:
                            target_id = _resource_id(
                                target_rule, "ruleId", "alertRuleId", "id"
                            )
                            summary.alert_rules_reused += 1
                        else:
                            name_type = (
                                str(payload["ruleName"]),
                                str(payload["alertType"]),
                            )
                            conflicting = target_rules_by_name_type.get(name_type, [])
                            if conflicting:
                                raise ApiError(
                                    "Target alert rule has the same name and type but different "
                                    f"configuration: {name_type[0]!r} / {name_type[1]!r}"
                                )
                            if bool(payload.get("isDefault")):
                                default_conflict = next(
                                    (
                                        rule
                                        for rules in target_rules_by_name_type.values()
                                        for rule in rules
                                        if str(rule.get("alertType")) == name_type[1]
                                        and bool(rule.get("isDefault"))
                                    ),
                                    None,
                                )
                                if default_conflict is not None:
                                    raise ApiError(
                                        "Target account group already has a different default "
                                        f"alert rule for alertType {name_type[1]!r}"
                                    )
                            if dry_run:
                                target_id = "projected-alert-rule:" + fingerprint
                                summary.alert_rules_projected += 1
                                action = "projected-create"
                            else:
                                created = self.api.create_alert_rule(target_aid, payload)
                                target_id = _resource_id(
                                    created, "ruleId", "alertRuleId", "id"
                                )
                                self._target_alert_rules[target_aid].append(created)
                                target_rules_by_fingerprint[fingerprint] = created
                                target_rules_by_name_type.setdefault(name_type, []).append(
                                    created
                                )
                                summary.alert_rules_created += 1
                                action = "created"
                        if target_id is None:
                            raise ApiError("Target alert-rule response did not include an id")
                        resolved_rules[fingerprint] = target_id
                    alert_rule_ids.append(target_id)
                    alert_rule_actions.append(f"{action}:{payload['ruleName']}")

            self._prepared[key] = PreparedTestResources(
                tag_ids=tuple(dict.fromkeys(tag_ids)),
                alert_rule_ids=tuple(dict.fromkeys(alert_rule_ids)),
                tag_actions=tuple(dict.fromkeys(tag_actions)),
                alert_rule_actions=tuple(dict.fromkeys(alert_rule_actions)),
            )
        return summary

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
        create_missing_tags: bool = False,
        create_missing_alerts: bool = False,
        preserved_agent_ids: list[str] | None = None,
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

        self.prepare_recreate_resources(
            source_tests=[source_test],
            source_aid=source_aid,
            target_aid=target_aid,
            create_missing_tags=create_missing_tags,
            create_missing_alerts=create_missing_alerts,
            dry_run=dry_run,
        )
        prepared = self._prepared[
            (
                source_aid,
                target_aid,
                source_test.test_id,
                create_missing_tags,
                create_missing_alerts,
                dry_run,
            )
        ]
        monitor_based = source_test.test_type in MONITOR_BASED_TEST_TYPES
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
                assigned_agent_ids: list[str] | None = None
                if not monitor_based:
                    current = self.api.get_test(
                        target_aid, existing.test_type, existing.test_id
                    )
                    assigned_agent_ids = _agent_ids(current.agents)
                    assigned_agent_ids.extend(preserved_agent_ids or [])
                    assigned_agent_ids.append(target_agent.agent_id)
                    assigned_agent_ids = list(dict.fromkeys(assigned_agent_ids))
                payload = build_recreate_payload(
                    source_test,
                    target_agent,
                    agent_ids=assigned_agent_ids,
                    tag_ids=list(prepared.tag_ids),
                    alert_rule_ids=list(prepared.alert_rule_ids),
                    include_agents=not monitor_based,
                )
                self.api.update_test(
                    target_aid,
                    existing.test_type,
                    existing.test_id,
                    payload,
                )
                if not monitor_based:
                    self.api.assign_tests(
                        target_aid, target_agent.agent_id, [existing.test_id]
                    )
            return TestAction(
                source_test.test_id,
                source_test.name,
                source_test.test_type,
                "reuse-monitor-based" if monitor_based else "reuse-and-assign",
                "existing",
                target_test_id=existing.test_id,
                target_test_name=existing.name,
                detail=(
                    "Idempotent rerun reused the existing monitor-based test"
                    if monitor_based
                    else "Idempotent rerun reused the existing migrated test"
                ),
                tags_reconciled=len(prepared.tag_ids),
                alert_rules_reconciled=len(prepared.alert_rule_ids),
                tag_actions=prepared.tag_actions,
                alert_rule_actions=prepared.alert_rule_actions,
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
        payload = build_recreate_payload(
            source_test,
            target_agent,
            agent_ids=(
                None
                if monitor_based
                else list(
                    dict.fromkeys([*(preserved_agent_ids or []), target_agent.agent_id])
                )
            ),
            tag_ids=list(prepared.tag_ids),
            alert_rule_ids=list(prepared.alert_rule_ids),
            include_agents=not monitor_based,
        )
        created = (
            None if dry_run else self.api.create_test(target_aid, source_test.test_type, payload)
        )
        if created is not None and not monitor_based:
            self.api.assign_tests(target_aid, target_agent.agent_id, [created.test_id])
        return TestAction(
            source_test.test_id,
            source_test.name,
            source_test.test_type,
            "recreate-monitor-based" if monitor_based else "recreate",
            "projected" if dry_run else "changed",
            target_test_id=None if created is None else created.test_id,
            target_test_name=target_name,
            detail=(
                "Created enabled monitor-based test with preserved monitors"
                if monitor_based
                else (
                    "Created enabled with opt-in tag/alert reconciliation"
                    if prepared.tag_ids or prepared.alert_rule_ids
                    else "Created enabled without source labels or alert rules"
                )
            ),
            tags_reconciled=len(prepared.tag_ids),
            alert_rules_reconciled=len(prepared.alert_rule_ids),
            tag_actions=prepared.tag_actions,
            alert_rule_actions=prepared.alert_rule_actions,
        )


def action_dict(action: TestAction) -> dict[str, Any]:
    return asdict(action)
