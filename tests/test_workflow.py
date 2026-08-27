from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

import pytest

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.errors import ApiError, HardStop, HumanDecisionRequired, VisibilityTimeout
from te_agent_migrate.models import (
    AccountGroup,
    AgentRecord,
    InventoryAgent,
    LocalSnapshot,
    Mode,
    RunOptions,
    StalePolicy,
)
from te_agent_migrate.models import TestRecord as TeTestRecord
from te_agent_migrate.models import TestStrategy as Strategy
from te_agent_migrate.reporting import RunReport
from te_agent_migrate.workflow import MigrationRunner, recursive_diff


class FakeConsole:
    def info(self, *_: Any) -> None:
        pass

    def progress(self, *_: Any) -> None:
        pass

    def status(self, *_: Any) -> None:
        pass

    def recap(self, *_: Any) -> None:
        pass


class FakePrompter:
    def __init__(
        self,
        decision: str = "abort",
        *,
        continue_batches: list[bool] | None = None,
    ) -> None:
        self.decision = decision
        self.continue_batches = continue_batches or []
        self.confirm_calls = 0
        self.failure_calls = 0
        self.failure_recheck_options: list[bool] = []
        self.batch_calls: list[tuple[int, list[str]]] = []

    def confirm_destructive(self) -> bool:
        self.confirm_calls += 1
        return True

    def continue_after_batch(self, batch_number: int, agents: list[str]) -> bool:
        self.batch_calls.append((batch_number, agents))
        if self.continue_batches:
            return self.continue_batches.pop(0)
        return True

    def failure_decision(self, _agent: str, _detail: str, *, allow_recheck: bool = True) -> str:
        self.failure_calls += 1
        self.failure_recheck_options.append(allow_recheck)
        return self.decision


class FakeApi:
    match_agent = staticmethod(ThousandEyesApiClient.match_agent)

    def __init__(
        self,
        *,
        target_existing: bool = False,
        timeout: bool = False,
        target_on_recheck: bool = False,
        source_delete_fails: bool = False,
        target_name_collision: bool = False,
        source_tests: list[TeTestRecord] | None = None,
        test_create_fails: bool = False,
    ) -> None:
        self.source = AgentRecord("100", "agent-a", "agent-a", ["192.0.2.10"], "online", "source")
        self.target = AgentRecord(
            "200", "agent-a-new", "agent-a", ["192.0.2.10"], "online", "target"
        )
        self.target_existing = target_existing
        self.timeout = timeout
        self.target_on_recheck = target_on_recheck
        self.source_delete_fails = source_delete_fails
        self.target_name_collision = target_name_collision
        self.source_tests = source_tests or []
        self.target_tests: list[TeTestRecord] = []
        self.test_create_fails = test_create_fails
        self.source.test_ids = [
            test.test_id
            for test in self.source_tests
            if any(str(agent.get("agentId")) == self.source.agent_id for agent in test.agents)
        ]
        self.source_deleted = False
        self.target_list_calls = 0
        self.wait_calls = 0
        self.wait_timeout: int | None = None
        self.absence_wait_timeout: int | None = None
        self.deleted: list[str] = []
        self.deleted_tests: list[tuple[str, str, str]] = []
        self.renamed: list[tuple[str, str, str]] = []
        self.operation_order: list[str] = []
        self.collision = AgentRecord(
            "300", "agent-a", "old-agent", ["192.0.2.99"], "offline", "target"
        )

    def list_agents(self, aid: str) -> list[AgentRecord]:
        if aid == "source":
            return [] if self.source_deleted else [self.source]
        self.target_list_calls += 1
        visible = self.target_existing or (self.target_on_recheck and self.target_list_calls > 1)
        agents = [self.target] if visible else []
        if self.target_name_collision:
            agents.append(self.collision)
        return agents

    def list_tests(self, aid: str) -> list[TeTestRecord]:
        return self.source_tests if aid == "source" else list(self.target_tests)

    def get_test(self, _aid: str, _type: str, test_id: str) -> TeTestRecord:
        tests = self.source_tests if _aid == "source" else self.target_tests
        return next(test for test in tests if test.test_id == test_id)

    def create_test(self, aid: str, test_type: str, payload: dict[str, Any]) -> TeTestRecord:
        self.operation_order.append("create_test")
        if self.test_create_fails:
            raise ApiError("API POST tests/http-server returned HTTP 400")
        created = TeTestRecord(
            "900",
            payload["testName"],
            test_type,
            aid,
            bool(payload["enabled"]),
            agents=list(payload["agents"]),
            raw=dict(payload),
        )
        self.target_tests.append(created)
        return created

    def update_test(
        self, aid: str, test_type: str, test_id: str, payload: dict[str, Any]
    ) -> TeTestRecord:
        updated = TeTestRecord(
            test_id,
            payload["testName"],
            test_type,
            aid,
            bool(payload["enabled"]),
            agents=list(payload["agents"]),
            raw=dict(payload),
        )
        self.target_tests = [
            updated if test.test_id == test_id else test for test in self.target_tests
        ]
        return updated

    def assign_tests(self, _aid: str, _agent_id: str, _test_ids: list[str]) -> None:
        self.operation_order.append("assign_tests")
        for test in self.target_tests:
            if test.test_id in _test_ids and not any(
                str(agent.get("agentId")) == _agent_id for agent in test.agents
            ):
                test.agents.append({"agentId": _agent_id})

    def wait_for_test_ready(self, aid: str, **kwargs: Any) -> TeTestRecord:
        self.operation_order.append("wait_test_ready")
        test = self.get_test(aid, kwargs["test_type"], kwargs["test_id"])
        assert test.enabled
        assert any(
            str(agent.get("agentId")) == kwargs["agent_id"] for agent in test.agents
        )
        return test

    def wait_for_agent(self, _aid: str, **kwargs: Any) -> AgentRecord:
        self.wait_calls += 1
        self.wait_timeout = kwargs["timeout_seconds"]
        if self.timeout:
            raise VisibilityTimeout("not visible within 180 seconds")
        self.target_existing = True
        self.operation_order.append("target_online")
        return self.target

    def wait_for_agent_absent(self, _aid: str, **kwargs: Any) -> None:
        self.absence_wait_timeout = kwargs["timeout_seconds"]
        self.operation_order.append("wait_absent")
        if not self.source_deleted:
            raise VisibilityTimeout("source remained visible")

    def update_agent_name(self, aid: str, agent_id: str, name: str) -> None:
        self.operation_order.append("rename")
        self.renamed.append((aid, agent_id, name))
        if aid == "target" and agent_id == self.target.agent_id:
            self.target.name = name

    def wait_for_agent_name(self, _aid: str, **kwargs: Any) -> AgentRecord:
        self.operation_order.append("wait_name")
        assert kwargs["agent_id"] == self.target.agent_id
        assert kwargs["expected_name"] == self.target.name
        return self.target

    def delete_agent(self, _aid: str, agent_id: str) -> None:
        self.operation_order.append("delete_agent")
        if self.source_delete_fails:
            raise ApiError("delete failed")
        self.deleted.append(agent_id)
        self.source_deleted = True

    def delete_test(self, aid: str, test_type: str, test_id: str) -> None:
        self.operation_order.append("delete_test")
        self.deleted_tests.append((aid, test_type, test_id))
        self.source_tests = [test for test in self.source_tests if test.test_id != test_id]

    def wait_for_test_absent(self, aid: str, **kwargs: Any) -> None:
        self.operation_order.append("wait_test_absent")
        tests = self.source_tests if aid == "source" else self.target_tests
        assert all(test.test_id != kwargs["test_id"] for test in tests)


class FakeUi:
    def __init__(self, calls: list[str], *, reachable: bool = True) -> None:
        self.calls = calls
        self.reachable = reachable

    def __enter__(self) -> FakeUi:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def authenticate(self) -> None:
        self.calls.append("authenticate")

    def snapshot_state(self) -> LocalSnapshot:
        self.calls.append("snapshot")
        return LocalSnapshot("192.0.2.10", "now", {"browserbot": True, "crash_reports": True})

    def reset_agent(self) -> None:
        self.calls.append("reset")

    def is_reachable(self) -> bool:
        self.calls.append("reachable")
        return self.reachable

    def set_account_group_token(self, *_: Any, **__: Any) -> None:
        self.calls.append("token")


def runner(
    tmp_path: Path,
    *,
    mode: Mode,
    api: FakeApi,
    calls: list[str],
    prompter: FakePrompter,
    reachable: bool = True,
    strategy: Strategy = Strategy.AGENTS_ONLY,
    stale_policy: StalePolicy = StalePolicy.KEEP,
    parallelism: int = 1,
) -> MigrationRunner:
    return MigrationRunner(
        api=api,  # type: ignore[arg-type]
        ui_factory=lambda _item: FakeUi(calls, reachable=reachable),  # type: ignore[arg-type]
        report=RunReport(tmp_path, mode.value),
        prompter=prompter,  # type: ignore[arg-type]
        console=FakeConsole(),  # type: ignore[arg-type]
        options=RunOptions(mode, strategy, stale_policy, parallelism=parallelism),
        source_group=AccountGroup("source", "Source"),
        target_group=AccountGroup("target", "Target"),
        target_account_token="A" * 32,
    )


def test_dry_run_performs_no_modifying_ui_calls(tmp_path: Path) -> None:
    calls: list[str] = []
    prompter = FakePrompter()
    api = FakeApi()
    migration = runner(tmp_path, mode=Mode.DRY_RUN, api=api, calls=calls, prompter=prompter)

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert calls == ["authenticate", "snapshot"]
    assert api.deleted == []
    assert prompter.confirm_calls == 0
    readiness = next(
        step for step in migration.report.steps if step["step"] == "dry_run_test_readiness"
    )
    assert readiness["data"] == {
        "target_test_count": 0,
        "source_assigned_test_count": 0,
    }


def test_parallel_batches_respect_limit_and_prompt_between_batches(tmp_path: Path) -> None:
    prompter = FakePrompter()
    migration = runner(
        tmp_path,
        mode=Mode.DRY_RUN,
        api=FakeApi(),
        calls=[],
        prompter=prompter,
        parallelism=2,
    )
    inventory = [InventoryAgent(f"agent-{index}", f"192.0.2.{index}") for index in range(1, 6)]
    state_lock = Lock()
    active = 0
    maximum_active = 0
    completed: list[str] = []

    def fake_run_agent(agent: InventoryAgent, _correlation_id: str) -> None:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with state_lock:
            completed.append(agent.hostname)
            active -= 1

    migration._run_agent = fake_run_agent  # type: ignore[method-assign]

    migration.run(inventory)

    assert maximum_active == 2
    assert sorted(completed) == sorted(agent.hostname for agent in inventory)
    assert prompter.batch_calls == [
        (1, ["agent-1", "agent-2"]),
        (2, ["agent-3", "agent-4"]),
    ]
    assert migration.report.metadata["parallelism"] == 2


def test_declining_batch_prompt_prevents_next_batch(tmp_path: Path) -> None:
    prompter = FakePrompter(continue_batches=[False])
    migration = runner(
        tmp_path,
        mode=Mode.DRY_RUN,
        api=FakeApi(),
        calls=[],
        prompter=prompter,
        parallelism=2,
    )
    completed: list[str] = []

    def fake_run_agent(agent: InventoryAgent, _correlation_id: str) -> None:
        completed.append(agent.hostname)

    migration._run_agent = fake_run_agent  # type: ignore[method-assign]

    with pytest.raises(HumanDecisionRequired, match="between batches"):
        migration.run(
            [
                InventoryAgent("agent-1", "192.0.2.1"),
                InventoryAgent("agent-2", "192.0.2.2"),
                InventoryAgent("agent-3", "192.0.2.3"),
            ]
        )

    assert sorted(completed) == ["agent-1", "agent-2"]
    assert prompter.batch_calls == [(1, ["agent-1", "agent-2"])]


def test_parallel_hard_stop_allows_active_peer_to_finish_but_blocks_later_batch(
    tmp_path: Path,
) -> None:
    migration = runner(
        tmp_path,
        mode=Mode.DRY_RUN,
        api=FakeApi(),
        calls=[],
        prompter=FakePrompter(),
        parallelism=2,
    )
    completed: list[str] = []

    def fake_run_agent(agent: InventoryAgent, _correlation_id: str) -> None:
        if agent.hostname == "agent-1":
            raise HardStop("batch hard stop")
        time.sleep(0.02)
        completed.append(agent.hostname)

    migration._run_agent = fake_run_agent  # type: ignore[method-assign]

    with pytest.raises(HardStop, match="batch hard stop"):
        migration.run(
            [
                InventoryAgent("agent-1", "192.0.2.1"),
                InventoryAgent("agent-2", "192.0.2.2"),
                InventoryAgent("agent-3", "192.0.2.3"),
            ]
        )

    assert completed == ["agent-2"]
    assert migration.report.errors[0]["agent"] == "agent-1"


def test_parallel_strategy_c_reuses_shared_test_instead_of_creating_duplicate(
    tmp_path: Path,
) -> None:
    api = FakeApi(source_tests=[assigned_source_test()])
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.KEEP,
        parallelism=2,
    )
    source_test = api.source_tests[0]
    agents = [
        (
            AgentRecord("201", "agent-a", "agent-a", ["192.0.2.10"], "online", "target"),
            InventoryAgent("agent-a", "192.0.2.10"),
        ),
        (
            AgentRecord("202", "agent-b", "agent-b", ["192.0.2.11"], "online", "target"),
            InventoryAgent("agent-b", "192.0.2.11"),
        ),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                migration._handle_tests,
                [source_test],
                target,
                f"correlation-{index}",
                inventory_agent,
            )
            for index, (target, inventory_agent) in enumerate(agents)
        ]
        for future in futures:
            future.result()

    assert api.operation_order.count("create_test") == 1
    assert api.operation_order.count("assign_tests") == 2
    migrated = api.target_tests[0]
    assert migrated.enabled
    assert {str(agent["agentId"]) for agent in migrated.agents} == {"201", "202"}


def test_option_c_readiness_has_no_target_licensing_constraint(tmp_path: Path) -> None:
    migration = runner(
        tmp_path,
        mode=Mode.DRY_RUN,
        api=FakeApi(),
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
    )
    source_tests = [TeTestRecord("10", "Test", "http-server", "source", True)]

    migration._record_dry_run_test_readiness(
        source_tests,
        [],
        "correlation",
        InventoryAgent("agent-a", "192.0.2.10"),
    )

    readiness = migration.report.steps[-1]["data"]["option_c_type_readiness"]
    assert readiness == [{"test_type": "http-server", "adapter_supported": True}]


def assigned_source_test() -> TeTestRecord:
    raw = {
        "testId": 10,
        "type": "http-server",
        "testName": "Source test",
        "url": "https://example.test",
        "agents": [{"agentId": 100}],
    }
    return TeTestRecord(
        "10",
        "Source test",
        "http-server",
        "source",
        True,
        agents=raw["agents"],
        raw=raw,
    )


def unassigned_source_test() -> TeTestRecord:
    raw = {
        "testId": 11,
        "type": "agent-to-server",
        "testName": "Unassigned source test",
        "targetAgentId": 1,
        "enabled": False,
        "alerts": [{"alertRuleId": 9}],
        "agents": [],
    }
    return TeTestRecord(
        "11",
        "Unassigned source test",
        "agent-to-server",
        "source",
        False,
        agents=[],
        alert_rules=raw["alerts"],
        raw=raw,
    )

def test_strategy_c_unassigned_test_is_enabled_and_assigned_to_every_target(
    tmp_path: Path,
) -> None:
    api = FakeApi(source_tests=[unassigned_source_test()])
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.KEEP,
    )
    targets = [
        (
            InventoryAgent("agent-a", "192.0.2.10"),
            AgentRecord("201", "agent-a", "agent-a", ["192.0.2.10"], "online", "target"),
        ),
        (
            InventoryAgent("agent-b", "192.0.2.11"),
            AgentRecord("202", "agent-b", "agent-b", ["192.0.2.11"], "online", "target"),
        ),
    ]
    migration._strategy_c_unassigned_tests = [api.source_tests[0]]
    migration._completed_target_agents = {
        item.hostname: (item, target) for item, target in targets
    }

    migration._handle_strategy_c_unassigned_tests([item for item, _ in targets])

    migrated = api.target_tests[0]
    assert migrated.name == "Unassigned source test"
    assert migrated.enabled
    assert {str(agent["agentId"]) for agent in migrated.agents} == {"201", "202"}
    assert "alerts" not in migrated.raw
    assert migrated.raw["alertsEnabled"] is False


def test_apply_deletes_source_then_restores_exact_name(tmp_path: Path) -> None:
    calls: list[str] = []
    api = FakeApi()
    prompter = FakePrompter()
    migration = runner(tmp_path, mode=Mode.APPLY, api=api, calls=calls, prompter=prompter)

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert calls == [
        "authenticate",
        "snapshot",
        "reset",
        "reachable",
        "authenticate",
        "token",
        "snapshot",
    ]
    assert api.wait_timeout == 180
    assert api.absence_wait_timeout == 180
    assert api.deleted == ["100"]
    assert api.renamed == [("target", "200", "agent-a")]
    assert api.target.name == "agent-a"
    assert prompter.confirm_calls == 1
    source_post = next(row for row in migration.report.agents if row["phase"] == "source-removed")
    assert source_post["state"] == "removed"
    assert source_post["source_absent_confirmed"] is True
    assert api.operation_order.index("target_online") < api.operation_order.index("delete_agent")
    assert api.operation_order.index("delete_agent") < api.operation_order.index("wait_absent")
    assert api.operation_order.index("wait_absent") < api.operation_order.index("rename")


def test_existing_target_recovery_deletes_source_without_reset(tmp_path: Path) -> None:
    calls: list[str] = []
    api = FakeApi(target_existing=True)
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=calls,
        prompter=FakePrompter(),
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert "reset" not in calls
    assert "token" not in calls
    assert api.deleted == ["100"]
    assert api.absence_wait_timeout == 180
    assert api.renamed == [("target", "200", "agent-a")]
    assert any(step["step"] == "dual_online_detected" for step in migration.report.steps)


def test_source_deletion_failure_hard_stops_without_renaming_or_next_agent(tmp_path: Path) -> None:
    calls: list[str] = []
    api = FakeApi(target_existing=True, source_delete_fails=True)
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=calls,
        prompter=FakePrompter(),
    )

    with pytest.raises(HardStop, match="could not be deleted and confirmed absent"):
        migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert "reset" not in calls
    assert "token" not in calls
    assert api.renamed == []


@pytest.mark.parametrize(
    ("strategy", "stale_policy"),
    [
        (Strategy.AGENTS_ONLY, StalePolicy.KEEP),
        (Strategy.AGENTS_ONLY, StalePolicy.REMOVE),
        (Strategy.SHARE, StalePolicy.KEEP),
        (Strategy.RECREATE, StalePolicy.KEEP),
        (Strategy.RECREATE, StalePolicy.REMOVE),
    ],
)
def test_source_agent_is_always_deleted_independent_of_strategy_and_stale(
    tmp_path: Path, strategy: Strategy, stale_policy: StalePolicy
) -> None:
    tests = [assigned_source_test()] if strategy is Strategy.RECREATE else []
    api = FakeApi(target_existing=True, source_tests=tests)
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=strategy,
        stale_policy=stale_policy,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert api.deleted == ["100"]


def test_strategy_c_handles_tests_before_source_agent_deletion(tmp_path: Path) -> None:
    api = FakeApi(target_existing=True, source_tests=[assigned_source_test()])
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.KEEP,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert api.operation_order.index("create_test") < api.operation_order.index("delete_agent")
    assert api.deleted_tests == []


def test_strategy_c_api_validation_failure_does_not_offer_read_only_recheck(
    tmp_path: Path,
) -> None:
    api = FakeApi(
        target_existing=True,
        source_tests=[assigned_source_test()],
        test_create_fails=True,
    )
    prompter = FakePrompter(decision="skip")
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=prompter,
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.REMOVE,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert prompter.failure_recheck_options == [False]
    assert api.deleted == []
    assert api.deleted_tests == []


def test_strategy_c_remove_deletes_recreated_source_test_once(tmp_path: Path) -> None:
    api = FakeApi(target_existing=True, source_tests=[assigned_source_test()])
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.REMOVE,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert api.deleted_tests == [("source", "http-server", "10")]
    assert api.operation_order.index("delete_agent") < api.operation_order.index("delete_test")


def test_strategy_c_remove_deduplicates_shared_source_test_cleanup(tmp_path: Path) -> None:
    api = FakeApi()
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.REMOVE,
    )
    test = assigned_source_test()

    migration._record_source_test_policy(
        test,
        "changed",
        InventoryAgent("agent-a", "192.0.2.10"),
        False,
        source_agent_id="100",
    )
    migration._record_source_test_policy(
        test,
        "existing",
        InventoryAgent("agent-b", "192.0.2.11"),
        False,
        source_agent_id="100",
    )
    migration._completed_source_agent_ids.add("100")
    migration._cleanup_source_tests()

    assert api.deleted_tests == [("source", "http-server", "10")]


def test_strategy_c_remove_keeps_only_test_with_incomplete_agent_associations(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.REMOVE,
    )
    complete = assigned_source_test()
    incomplete = TeTestRecord(
        "11",
        "Shared source test",
        "http-server",
        "source",
        True,
        agents=[{"agentId": 100}, {"agentId": 101}],
        raw={"testId": 11, "type": "http-server", "testName": "Shared source test"},
    )
    inventory_agent = InventoryAgent("agent-a", "192.0.2.10")
    migration._record_source_test_policy(
        complete, "changed", inventory_agent, False, source_agent_id="100"
    )
    migration._record_source_test_policy(
        incomplete, "changed", inventory_agent, False, source_agent_id="100"
    )
    migration._completed_source_agent_ids.add("100")

    migration._cleanup_source_tests()

    assert api.deleted_tests == [("source", "http-server", "10")]
    retained = next(
        row
        for row in migration.report.stale_entries
        if row.get("action") == "kept-incomplete-associations"
    )
    assert retained["source_test_id"] == "11"
    assert retained["missing_source_agent_ids"] == ["101"]


def test_strategy_c_remove_keeps_unsupported_source_test(tmp_path: Path) -> None:
    test = assigned_source_test()
    test.test_type = "future-test"
    test.raw["type"] = "future-test"
    api = FakeApi(target_existing=True, source_tests=[test])
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=Strategy.RECREATE,
        stale_policy=StalePolicy.REMOVE,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert api.deleted == ["100"]
    assert api.deleted_tests == []
    assert any(row["action"] == "kept-unsupported" for row in migration.report.stale_entries)


@pytest.mark.parametrize("strategy", [Strategy.AGENTS_ONLY, Strategy.SHARE])
def test_strategies_a_and_b_never_delete_source_tests(tmp_path: Path, strategy: Strategy) -> None:
    api = FakeApi(target_existing=True)
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=[],
        prompter=FakePrompter(),
        strategy=strategy,
        stale_policy=StalePolicy.REMOVE,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert api.deleted_tests == []


def test_exact_target_name_collision_stops_before_ui_or_reset(tmp_path: Path) -> None:
    calls: list[str] = []
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=FakeApi(target_name_collision=True),
        calls=calls,
        prompter=FakePrompter(),
    )

    with pytest.raises(HardStop, match="display name 'agent-a' is already owned"):
        migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert calls == []


def test_unreachable_after_reset_is_hard_stop_before_next_agent(tmp_path: Path) -> None:
    calls: list[str] = []
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=FakeApi(),
        calls=calls,
        prompter=FakePrompter(),
        reachable=False,
    )

    with pytest.raises(HardStop, match="remaining agents were not touched"):
        migration.run(
            [InventoryAgent("agent-a", "192.0.2.10"), InventoryAgent("agent-b", "192.0.2.11")]
        )
    assert calls.count("reset") == 1


def test_visibility_skip_does_not_retry_reset_or_token(tmp_path: Path) -> None:
    calls: list[str] = []
    prompter = FakePrompter(decision="skip")
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=FakeApi(timeout=True),
        calls=calls,
        prompter=prompter,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert calls.count("reset") == 1
    assert calls.count("token") == 1
    assert prompter.failure_calls == 1


def test_visibility_recheck_is_one_shot_instead_of_repeating_full_poll(tmp_path: Path) -> None:
    calls: list[str] = []
    api = FakeApi(timeout=True, target_on_recheck=True)
    prompter = FakePrompter(decision="recheck")
    migration = runner(
        tmp_path,
        mode=Mode.APPLY,
        api=api,
        calls=calls,
        prompter=prompter,
    )

    migration.run([InventoryAgent("agent-a", "192.0.2.10")])

    assert api.wait_calls == 1
    assert prompter.failure_calls == 1
    assert calls.count("reset") == 1
    assert calls.count("token") == 1


def test_recursive_diff_reports_leaf_changes() -> None:
    assert recursive_diff({"a": {"b": 1}, "gone": True}, {"a": {"b": 2}, "new": 3}) == [
        {"path": "a.b", "before": 1, "after": 2},
        {"path": "gone", "before": True, "after": None},
        {"path": "new", "before": None, "after": 3},
    ]
