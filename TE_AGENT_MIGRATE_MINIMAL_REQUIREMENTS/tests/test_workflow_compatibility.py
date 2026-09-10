import unittest

from te_agent_migrate.models import InventoryAgent, Mode, RunOptions, StalePolicy, TestStrategy
from te_agent_migrate.workflow import MigrationRunner


class WorkflowCompatibilityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
