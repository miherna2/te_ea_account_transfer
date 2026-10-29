import tempfile
import unittest
from pathlib import Path

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.inventory import describe_inventory, load_inventory
from te_agent_migrate.models import InventoryAgent


class InventoryV2Tests(unittest.TestCase):
    def load(self, text, raw=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.csv"
            if raw is not None:
                path.write_bytes(raw)
            else:
                path.write_text(text, encoding="utf-8")
            return load_inventory(path)

    def fails(self, text, fragment, raw=None):
        with self.assertRaises(ConfigurationError) as ctx:
            self.load(text, raw=raw)
        self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    # ---- valid formats -------------------------------------------------
    def test_v1_header_rows_are_agents(self):
        values = self.load("hostname,ip_address\nTE1,192.0.2.10\nTE2,192.0.2.11\n")
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0].kind, "agent")
        self.assertIsNone(values[0].agent_id)
        self.assertFalse(values[0].is_cluster)
        self.assertEqual(values[1].hostname, "TE2")

    def test_v2_header_agent_and_cluster(self):
        values = self.load(
            "type,name,ip_address\n"
            "agent,TE1,192.0.2.10\n"
            "cluster,My Cluster,192.0.2.20\n"
        )
        self.assertEqual(values[0].kind, "agent")
        self.assertTrue(values[1].is_cluster)
        self.assertEqual(values[1].hostname, "My Cluster")
        self.assertEqual(values[1].ip_address, "192.0.2.20")

    def test_cluster_without_ip(self):
        values = self.load("type,name,ip_address\ncluster,Cluster A,\n")
        self.assertTrue(values[0].is_cluster)
        self.assertEqual(values[0].ip_address, "")
        self.assertEqual(values[0].hostname, "Cluster A")

    def test_type_is_case_insensitive_and_fields_trimmed(self):
        values = self.load("type,name,ip_address\n  CLUSTER , Cl 1 , 192.0.2.5 \n Agent,TE1,192.0.2.6\n")
        self.assertEqual(values[0].kind, "cluster")
        self.assertEqual(values[0].hostname, "Cl 1")
        self.assertEqual(values[0].ip_address, "192.0.2.5")
        self.assertEqual(values[1].kind, "agent")

    def test_agent_id_header(self):
        values = self.load(
            "type,name,ip_address,agent_id\n"
            "cluster,Cl,,12345\n"
            "agent,TE1,192.0.2.10,\n"
        )
        self.assertEqual(values[0].agent_id, "12345")
        self.assertEqual(values[0].ip_address, "")
        self.assertIsNone(values[1].agent_id)

    def test_bom_accepted(self):
        raw = b"\xef\xbb\xbf" + b"type,name,ip_address\nagent,TE1,192.0.2.10\n"
        values = self.load("", raw=raw)
        self.assertEqual(values[0].hostname, "TE1")

    def test_blank_lines_skipped_and_line_numbers_physical(self):
        values = self.load("type,name,ip_address\n\nagent,TE1,192.0.2.10\n\n\ncluster,C,\n")
        self.assertEqual(len(values), 2)
        message = self.fails(
            "type,name,ip_address\n\nagent,TE1,192.0.2.10\n\nagent,TE2,bogus\n",
            "line 5",
        )
        self.assertIn("bogus", message)

    # ---- validation errors --------------------------------------------
    def test_bad_type(self):
        self.fails("type,name,ip_address\nrouter,TE1,192.0.2.10\n", "line 2")

    def test_bad_ip_for_agent_and_cluster(self):
        self.fails("type,name,ip_address\nagent,TE1,not-an-ip\n", "invalid IP")
        self.fails("type,name,ip_address\ncluster,C1,999.1.1.1\n", "line 2")

    def test_agent_requires_ip(self):
        self.fails("type,name,ip_address\nagent,TE1,\n", "line 2")

    def test_missing_name(self):
        self.fails("type,name,ip_address\ncluster,,192.0.2.1\n", "line 2")
        self.fails("hostname,ip_address\n,192.0.2.1\n", "line 2")

    def test_bad_agent_id(self):
        self.fails("type,name,ip_address,agent_id\ncluster,C,,abc\n", "agent_id")

    def test_duplicate_names_case_insensitive(self):
        self.fails("type,name,ip_address\nagent,TE1,192.0.2.1\nagent,te1,192.0.2.2\n", "line 3")

    def test_cluster_and_agent_same_name_duplicate(self):
        self.fails("type,name,ip_address\nagent,Same,192.0.2.1\ncluster,SAME,\n", "line 3")

    def test_duplicate_ip(self):
        self.fails("type,name,ip_address\nagent,A,192.0.2.1\ncluster,B,192.0.2.1\n", "line 3")

    def test_empty_ips_are_not_duplicates(self):
        values = self.load("type,name,ip_address\ncluster,A,\ncluster,B,\n")
        self.assertEqual(len(values), 2)

    def test_duplicate_agent_id(self):
        self.fails(
            "type,name,ip_address,agent_id\ncluster,A,,77\ncluster,B,,77\n",
            "line 3",
        )

    def test_v1_duplicates(self):
        self.fails("hostname,ip_address\nA,192.0.2.1\na,192.0.2.2\n", "line 3")
        self.fails("hostname,ip_address\nA,192.0.2.1\nB,192.0.2.1\n", "line 3")

    def test_bad_header_message(self):
        message = self.fails("name,ip\nA,192.0.2.1\n", "Inventory header must be")
        self.assertIn("hostname,ip_address", message)
        self.assertIn("type,name,ip_address", message)
        self.assertIn("type,name,ip_address,agent_id", message)
        self.assertIn("credential", message)
        self.fails("type,name,ip_address,password\nagent,A,192.0.2.1,x\n", "Inventory header must be")
        self.fails("name,type,ip_address\nA,agent,192.0.2.1\n", "Inventory header must be")

    def test_empty_inventory(self):
        self.fails("", "Inventory is empty")
        self.fails("type,name,ip_address\n\n", "Inventory is empty")

    def test_missing_file(self):
        with self.assertRaises(ConfigurationError):
            load_inventory(Path(tempfile.gettempdir()) / "definitely-missing-inventory.csv")

    # ---- model + describe ---------------------------------------------
    def test_inventory_agent_defaults_and_dict(self):
        agent = InventoryAgent("TE1", "192.0.2.1")
        self.assertEqual(agent.kind, "agent")
        self.assertIsNone(agent.agent_id)
        self.assertFalse(agent.is_cluster)
        self.assertEqual(
            agent.to_dict(),
            {
                "hostname": "TE1", "ip_address": "192.0.2.1", "kind": "agent", "agent_id": None,
                "target_aid": None, "share_aids": [],
            },
        )
        self.assertEqual(agent, InventoryAgent("TE1", "192.0.2.1", kind="agent"))
        self.assertNotEqual(agent, InventoryAgent("TE1", "192.0.2.1", kind="cluster"))
        self.assertTrue(InventoryAgent("C", "", kind="cluster", agent_id="1").is_cluster)

    def test_describe_inventory(self):
        values = self.load(
            "type,name,ip_address\nagent,A,192.0.2.1\nagent,B,192.0.2.2\ncluster,C,\n"
        )
        text = describe_inventory(values)
        self.assertIn("\n", text)
        self.assertIn("Standalone agents: 2", text)
        self.assertIn("Clusters: 1", text)
        self.assertIn("3", text.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
