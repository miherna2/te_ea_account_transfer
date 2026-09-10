import unittest

from te_agent_migrate.models import AgentRecord, TestRecord
from te_agent_migrate.strategies import build_recreate_payload


class StrategyPayloadTests(unittest.TestCase):
    def test_recreate_preserves_exact_name_and_enables_assignment(self):
        source = TestRecord(
            "10",
            "Customer HTTP Test",
            "http-server",
            "source",
            False,
            agents=[{"agentId": "old"}],
            raw={
                "testId": "10",
                "testName": "Customer HTTP Test",
                "type": "http-server",
                "enabled": False,
                "agents": [{"agentId": "old"}],
                "url": "https://example.com",
                "tags": [{"id": "source-tag"}],
                "alerts": [{"ruleId": "source-rule"}],
            },
        )
        target = AgentRecord("20", "TE1", "TE1", ["192.0.2.10"], "Online", "target")
        payload = build_recreate_payload(
            source,
            target,
            tag_ids=["target-tag"],
            alert_rule_ids=["target-rule"],
        )
        self.assertEqual(payload["testName"], "Customer HTTP Test")
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["agents"], [{"agentId": "20"}])
        self.assertEqual(payload["tags"], ["target-tag"])
        self.assertEqual(payload["alertRules"], ["target-rule"])
        self.assertNotIn("testId", payload)

    def test_missing_tag_and_alert_flags_create_test_without_resources(self):
        source = TestRecord(
            "10",
            "HTTP Test",
            "http-server",
            "source",
            True,
            raw={"testName": "HTTP Test", "type": "http-server", "alertsEnabled": True},
        )
        target = AgentRecord("20", "TE1", "TE1", ["192.0.2.10"], "Online", "target")
        payload = build_recreate_payload(source, target)
        self.assertNotIn("tags", payload)
        self.assertNotIn("alertRules", payload)
        self.assertFalse(payload["alertsEnabled"])


if __name__ == "__main__":
    unittest.main()
