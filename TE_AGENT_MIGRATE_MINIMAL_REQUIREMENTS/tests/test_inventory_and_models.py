import tempfile
import unittest
from pathlib import Path

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.inventory import load_inventory
from te_agent_migrate.models import AccountGroup, StepEvent, StepStatus


class InventoryAndModelsTests(unittest.TestCase):
    def test_inventory_loads_strict_two_column_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.csv"
            path.write_text("hostname,ip_address\nTE1,192.0.2.10\n", encoding="utf-8")
            values = load_inventory(path)
        self.assertEqual(values[0].hostname, "TE1")
        self.assertEqual(values[0].ip_address, "192.0.2.10")

    def test_inventory_rejects_credential_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.csv"
            path.write_text(
                "hostname,ip_address,password\nTE1,192.0.2.10,secret\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_inventory(path)

    def test_account_group_and_step_serialization(self):
        group = AccountGroup.from_api({"aid": 123, "accountGroupName": "Target"})
        self.assertEqual(group.aid, "123")
        event = StepEvent("now", "cid", "TE1", "check", StepStatus.OK, 0.1)
        self.assertEqual(event.to_dict()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
