import unittest

from te_agent_migrate.models import (
    AccountGroup,
    AgentRecord,
    InventoryAgent,
    Mode,
    RunOptions,
    StalePolicy,
    TestRecord,
    TestStrategy,
)
from te_agent_migrate.strategies import TestStrategyExecutor
from te_agent_migrate.workflow import MigrationRunner


class FakeApi(object):
    def __init__(self, source_agents, target_agents, tests):
        self.source_agents = source_agents
        self.target_agents = target_agents
        self.tests = tests
        self.details_requested = []

    def list_agents(self, aid, agent_types="ENTERPRISE"):
        return self.source_agents if aid == "source" else self.target_agents

    def list_tests(self, unused_aid):
        return list(self.tests)

    def get_test(self, unused_aid, unused_type, test_id):
        self.details_requested.append(test_id)
        return next(test for test in self.tests if test.test_id == test_id)

    def list_monitors(self, unused_aid):
        return []


class FakeReport(object):
    def __init__(self):
        self.metadata = {}
        self.steps = []
        self.stale_entries = []

    def record_step(self, event):
        self.steps.append(event)

    def add_stale_entry(self, row):
        self.stale_entries.append(row)


class FakeConsole(object):
    def progress(self, *unused):
        pass

    def status(self, *unused):
        pass

    def info(self, *unused):
        pass


class WorkflowCompatibilityTests(unittest.TestCase):
    def _strategy_c_runner(self, stale_policy, source_agents, target_agents, tests):
        api = FakeApi(source_agents, target_agents, tests)
        report = FakeReport()
        runner = MigrationRunner(
            api=api,
            ui_factory=None,
            report=report,
            prompter=None,
            console=FakeConsole(),
            options=RunOptions(
                mode=Mode.DRY_RUN,
                strategy=TestStrategy.RECREATE,
                stale_policy=stale_policy,
            ),
            source_group=AccountGroup("source", "Source"),
            target_group=AccountGroup("target", "Target"),
            target_account_token="a" * 32,
        )
        runner.strategy = TestStrategyExecutor(api)
        return runner, api, report

    def test_parallel_batch_does_not_require_zip_strict(self):
        runner = MigrationRunner.__new__(MigrationRunner)
        runner.options = RunOptions(
            mode=Mode.DRY_RUN,
            strategy=TestStrategy.AGENTS_ONLY,
            stale_policy=StalePolicy.KEEP,
            parallelism=2,
        )
        runner._run_agent_captured = lambda agent, correlation_id: None
        agents = (
            InventoryAgent("TE1", "192.0.2.10"),
            InventoryAgent("TE2", "192.0.2.11"),
        )
        outcomes = runner._run_batch(agents)
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(item[2] is None for item in outcomes))

    def test_partial_inventory_scopes_strategy_c_to_selected_agent_tests(self):
        selected = AgentRecord(
            "1", "EA-1", "EA-1", ["192.0.2.1"], "online", "source",
            test_ids=["10"], agent_type="enterprise",
        )
        outside = AgentRecord(
            "2", "EA-2", "EA-2", ["192.0.2.2"], "online", "source",
            test_ids=["10", "20"], agent_type="enterprise",
        )
        tests = [
            TestRecord("10", "Shared", "http-server", "source", True, agents=[{"agentId": "1"}, {"agentId": "2"}]),
            TestRecord("20", "Unrelated", "http-server", "source", True, agents=[{"agentId": "2"}]),
            TestRecord("30", "Unassigned", "http-server", "source", True, agents=[]),
            TestRecord("40", "Monitor", "bgp", "source", True, agents=[]),
        ]
        runner, api, report = self._strategy_c_runner(
            StalePolicy.KEEP, [selected, outside], [], tests
        )
        runner._prepare_strategy_c_source_test_inventory(
            [InventoryAgent("EA-1", "192.0.2.1")]
        )
        self.assertEqual(set(runner._strategy_c_source_tests), {"10"})
        self.assertEqual(api.details_requested, ["10"])
        self.assertEqual(runner._strategy_c_unassigned_tests, [])
        self.assertEqual(runner._strategy_c_monitor_tests, [])
        self.assertEqual(report.metadata["strategy_c_scope"], "selected-inventory-agents")
        self.assertEqual(report.metadata["strategy_c_skipped_out_of_scope_test_count"], 3)
        self.assertEqual(report.metadata["strategy_c_tests_with_non_migrated_associations"], 1)

    def test_inventory_boundary_excludes_unassigned_and_monitor_tests_even_when_all_agents_selected(self):
        first = AgentRecord(
            "1", "EA-1", "EA-1", ["192.0.2.1"], "online", "source",
            test_ids=["10"], agent_type="enterprise",
        )
        second = AgentRecord(
            "2", "EA-2", "EA-2", ["192.0.2.2"], "online", "source",
            test_ids=["10"], agent_type="enterprise",
        )
        tests = [
            TestRecord("10", "Assigned", "http-server", "source", True, agents=[{"agentId": "1"}, {"agentId": "2"}]),
            TestRecord("30", "Unassigned", "http-server", "source", True, agents=[]),
            TestRecord("40", "Monitor", "bgp", "source", True, agents=[]),
        ]
        runner, api, report = self._strategy_c_runner(
            StalePolicy.KEEP, [first, second], [], tests
        )
        runner._prepare_strategy_c_source_test_inventory(
            [
                InventoryAgent("EA-1", "192.0.2.1"),
                InventoryAgent("EA-2", "192.0.2.2"),
            ]
        )
        self.assertEqual(set(runner._strategy_c_source_tests), {"10"})
        self.assertEqual(api.details_requested, ["10"])
        self.assertEqual(runner._strategy_c_unassigned_tests, [])
        self.assertEqual(runner._strategy_c_monitor_tests, [])
        self.assertEqual(report.metadata["strategy_c_scope"], "selected-inventory-agents")
        self.assertEqual(report.metadata["strategy_c_skipped_out_of_scope_test_count"], 2)

    def test_remove_dry_run_keeps_shared_test_needed_by_outside_agent(self):
        selected = AgentRecord(
            "1", "EA-1", "EA-1", ["192.0.2.1"], "online", "source",
            test_ids=["10"], agent_type="enterprise",
        )
        outside = AgentRecord(
            "2", "EA-2", "EA-2", ["192.0.2.2"], "online", "source",
            test_ids=["10"], agent_type="enterprise",
        )
        test = TestRecord(
            "10", "Shared", "http-server", "source", True,
            agents=[{"agentId": "1"}, {"agentId": "2"}],
        )
        runner, unused_api, report = self._strategy_c_runner(
            StalePolicy.REMOVE, [selected, outside], [], [test]
        )
        inventory_agent = InventoryAgent("EA-1", "192.0.2.1")
        runner._prepare_strategy_c_source_test_inventory([inventory_agent])
        runner._record_source_test_policy(
            test, "projected", inventory_agent, True, source_agent_id="1"
        )
        self.assertEqual(
            report.stale_entries[-1]["action"],
            "projected-keep-incomplete-associations",
        )
        self.assertEqual(report.stale_entries[-1]["missing_source_agent_ids"], ["2"])


if __name__ == "__main__":
    unittest.main()
