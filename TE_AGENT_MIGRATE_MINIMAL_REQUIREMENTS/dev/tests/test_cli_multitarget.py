"""CLI tests for Strategy D with several target groups (inventory v3).

Synthetic data only. The API, the flows and the prompts are fakes. No network is used.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from te_agent_migrate import cli
from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import AccountGroup
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.strategy_d import runner
from te_agent_migrate.strategy_d.context import FlowResult, PreflightItem

# Fake 32-character values used as markers. They are not credentials.
TOKEN_A = "Aa1Bb2Cc" * 4
TOKEN_B = "Dd3Ee4Ff" * 4
OAUTH_MARKER = "fake-oauth-marker"

HEADER = "type,name,ip_address,target_aid\n"
ALL_OWN = (
    HEADER
    + "agent,AAAA - HOST01,192.0.2.1,100\n"
    + "agent,AAAA - HOST02,192.0.2.2,200\n"
    + "cluster,AAAA - CL01,,100\n"
)
MIXED = (
    HEADER
    + "agent,AAAA - HOST01,192.0.2.1,200\n"
    + "agent,AAAA - HOST02,192.0.2.2,\n"
)
IDS = {"AAAA - HOST01": "11", "AAAA - HOST02": "12", "AAAA - CL01": "13"}


class FakeApi(object):
    calls = []

    def __init__(self, token):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def list_account_groups(self):
        FakeApi.calls.append("list_account_groups")
        return [AccountGroup("1", "Source"), AccountGroup("100", "Group A"), AccountGroup("200", "Group B")]


class Rec(object):
    def __init__(self, agent_id):
        self.agent_id = agent_id


class Events(object):
    """Ordered record of the prompts and the flows of one run."""

    def __init__(self):
        self.log = []
        self.contexts = []
        self.names = []

    def standalone(self, ctx, item):
        self.log.append("flow")
        self.contexts.append(ctx)
        self.names.append(item.name)
        return FlowResult(item.name, True, "ok", new_agent_id="99")

    def cluster(self, ctx, item):
        self.log.append("flow")
        self.contexts.append(ctx)
        self.names.append(item.name)
        return FlowResult(item.name, True, "ok", new_cluster_id="55")


class MultiTargetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.inventory = self.dir / "inventory.csv"
        self.inventory.write_text(ALL_OWN, encoding="utf-8")
        self.out = self.dir / "out"
        self.events = Events()
        self.preflight_calls = []
        FakeApi.calls = []
        # No TE_TARGET_AID here: every row names its own target.
        self.env = {
            "TE_OAUTH_TOKEN": OAUTH_MARKER,
            "TE_SOURCE_AID": "1",
            "TE_AGENT_UI_USERNAME": "fake-user",
            "TE_AGENT_UI_PASSWORD": "fake-pass",
        }

    def make_preflight(self, retarget=None):
        retarget = retarget or {}

        def run(inventory_agents, **unused):
            self.preflight_calls.append(len(inventory_agents))
            items = []
            for inv in inventory_agents:
                item = PreflightItem(
                    inv, record=Rec(IDS[inv.hostname]), status="ready",
                    owner_aid="1", share_aids=["1"],
                )
                if inv.hostname in retarget:
                    item.target_aid = retarget[inv.hostname]
                items.append(item)
            return items

        return run

    def run_main(self, argv, env=None, secret=None, confirm=True, preflight=None):
        base = ["--inventory", str(self.inventory), "--output", str(self.out)]
        environment = dict(self.env)
        environment.update(env or {})
        preflight = preflight or self.make_preflight()
        out, err = io.StringIO(), io.StringIO()

        def secret_side_effect(prompt):
            self.events.log.append("secret")
            if secret is None:
                raise AssertionError("secret prompt must not be used")
            return secret

        def confirm_side_effect():
            self.events.log.append("confirm")
            return confirm

        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(cli, "load_dotenv"), \
                mock.patch.object(cli, "ThousandEyesApiClient", FakeApi), \
                mock.patch.object(runner, "run_preflight", side_effect=preflight), \
                mock.patch.object(cli, "run_preflight", side_effect=preflight), \
                mock.patch.object(runner, "migrate_standalone", side_effect=self.events.standalone), \
                mock.patch.object(runner, "migrate_cluster", side_effect=self.events.cluster), \
                mock.patch.object(OperatorPrompter, "secret", side_effect=secret_side_effect), \
                mock.patch.object(OperatorPrompter, "confirm_destructive", side_effect=confirm_side_effect), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(base + argv)
        return code, out.getvalue(), err.getvalue()

    def gate_file(self):
        return self.out / "dry-run-ok.json"

    def gate_data(self):
        return json.loads(self.gate_file().read_text(encoding="utf-8"))

    def dry_run(self, **kwargs):
        code, out, err = self.run_main(["--strategy", "D"], **kwargs)
        self.assertEqual(code, 0, out + err)
        self.events = Events()
        del self.preflight_calls[:]
        return out

    def apply_argv(self):
        return ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.gate_file())]

    def both_tokens(self):
        return {"TE_TARGET_TOKEN_100": TOKEN_A, "TE_TARGET_TOKEN_200": TOKEN_B}

    def all_output_text(self, out, err):
        """Console output plus every file the run wrote (JSON, CSV, gate, snapshots)."""
        parts = [out, err]
        for path in sorted(self.out.rglob("*")):
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)


class TargetSelectionTests(MultiTargetBase):
    def test_all_rows_with_target_need_no_run_target(self):
        out = self.dry_run()
        self.assertNotIn("TE_TARGET_AID", out)
        self.assertIn("every inventory row names its own target group", out)

    def test_dry_run_prints_one_play_per_target_and_names_every_target(self):
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("PLAY [Strategy D: agent migration -> Target"), 2)
        self.assertIn("PLAY [Strategy D: agent migration -> Target Group A [100]]", out)
        self.assertIn("PLAY [Strategy D: agent migration -> Target Group B [200]]", out)
        self.assertIn("TARGET ACCOUNT GROUP: Group A [100]", out)
        self.assertIn("TARGET ACCOUNT GROUP: Group B [200]", out)
        # Devices run grouped by target, in inventory order inside a target.
        self.assertEqual(self.events.names, ["AAAA - HOST01", "AAAA - CL01", "AAAA - HOST02"])
        self.assertEqual([c.target_aid for c in self.events.contexts], ["100", "100", "200"])

    def test_dry_run_requests_no_token_and_uses_none(self):
        code, out, err = self.run_main(["--strategy", "D"], env=self.both_tokens())
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("secret", self.events.log)
        self.assertIn("target account-group tokens are not requested or used", out)
        self.assertEqual([c.target_account_token for c in self.events.contexts], ["", "", ""])
        self.assertEqual([c.target_tokens for c in self.events.contexts], [{}, {}, {}])

    def test_unknown_row_target_fails_before_anything_runs(self):
        self.inventory.write_text(
            HEADER + "agent,AAAA - HOST01,192.0.2.1,100\nagent,AAAA - HOST02,192.0.2.2,300\n",
            encoding="utf-8",
        )
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("300", err)
        self.assertEqual(self.preflight_calls, [])
        self.assertEqual(self.events.log, [])
        self.assertEqual(FakeApi.calls, ["list_account_groups"])
        self.assertFalse(self.gate_file().exists())

    def test_apply_with_an_unknown_row_target_runs_and_asks_nothing(self):
        self.dry_run()
        self.inventory.write_text(
            HEADER + "agent,AAAA - HOST01,192.0.2.1,300\n", encoding="utf-8"
        )
        code, out, err = self.run_main(self.apply_argv(), env=self.both_tokens())
        # The gate refuses a changed inventory first; either way nothing runs and nothing is asked.
        self.assertIn(code, (1, 2))
        self.assertEqual(self.events.log, [])
        self.assertEqual(self.preflight_calls, [])

    def test_source_equal_to_a_row_target_is_a_configuration_error(self):
        self.inventory.write_text(
            HEADER + "agent,AAAA - HOST01,192.0.2.1,100\nagent,AAAA - HOST02,192.0.2.2,1\n",
            encoding="utf-8",
        )
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("must be different", err)
        self.assertEqual(self.preflight_calls, [])
        self.assertEqual(self.events.log, [])

    def test_source_equal_to_the_run_target_is_still_refused(self):
        self.inventory.write_text(MIXED, encoding="utf-8")
        code, out, err = self.run_main(["--strategy", "D"], env={"TE_TARGET_AID": "1"})
        self.assertEqual(code, 1, out + err)
        self.assertIn("must be different", err)
        self.assertEqual(self.events.log, [])

    def test_mixed_inventory_uses_the_run_target_for_rows_without_target(self):
        self.inventory.write_text(MIXED, encoding="utf-8")
        code, out, err = self.run_main(["--strategy", "D"], env={"TE_TARGET_AID": "100"})
        self.assertEqual(code, 0, out + err)
        by_name = dict(zip(self.events.names, [c.target_aid for c in self.events.contexts]))
        self.assertEqual(by_name, {"AAAA - HOST01": "200", "AAAA - HOST02": "100"})
        self.assertEqual(out.count("PLAY [Strategy D: agent migration -> Target"), 2)
        self.assertEqual(self.gate_data()["target_aids"], ["100", "200"])
        self.assertEqual(self.gate_data()["target_aid"], "100")

    def test_mixed_inventory_with_unknown_run_target_fails(self):
        self.inventory.write_text(MIXED, encoding="utf-8")
        code, out, err = self.run_main(["--strategy", "D"], env={"TE_TARGET_AID": "999"})
        self.assertEqual(code, 1, out + err)
        self.assertIn("999", err)
        self.assertEqual(self.events.log, [])

    def test_legacy_inventory_without_targets_still_needs_the_run_target(self):
        self.inventory.write_text(
            "type,name,ip_address\nagent,AAAA - HOST01,192.0.2.1\n", encoding="utf-8"
        )
        code, out, err = self.run_main(["--strategy", "D"], env={"TE_TARGET_AID": "100"})
        self.assertEqual(code, 0, out + err)
        self.assertEqual([c.target_aid for c in self.events.contexts], ["100"])
        self.assertEqual(out.count("PLAY [Strategy D: agent migration"), 1)
        self.assertEqual(self.gate_data()["target_aids"], ["100"])

    def test_help_mentions_v3_columns_and_row_override(self):
        text = " ".join(cli.build_parser().format_help().split())
        for word in ("target_aid", "share_aids", "overrides the run target", "TE_TARGET_TOKEN_<aid>"):
            self.assertIn(word, text)


class DryRunGateTests(MultiTargetBase):
    def test_dry_run_writes_schema_4_with_targets(self):
        self.dry_run()
        data = self.gate_data()
        self.assertEqual(data["schema_version"], 4)
        self.assertEqual(data["target_aids"], ["100", "200"])
        self.assertIsNone(data["target_aid"])
        targets = dict((entry["name"], entry["target_aid"]) for entry in data["ready"])
        self.assertEqual(
            targets, {"AAAA - HOST01": "100", "AAAA - HOST02": "200", "AAAA - CL01": "100"}
        )

    def test_apply_with_the_same_state_runs_every_target(self):
        self.dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.both_tokens())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.events.names, ["AAAA - HOST01", "AAAA - CL01", "AAAA - HOST02"])
        self.assertEqual(out.count("PLAY [Strategy D: agent migration -> Target"), 2)

    def test_apply_refused_when_the_target_of_a_device_changed(self):
        self.dry_run()
        code, out, err = self.run_main(
            self.apply_argv(), env=self.both_tokens(),
            preflight=self.make_preflight(retarget={"AAAA - HOST01": "200"}),
        )
        self.assertEqual(code, 2, out + err)
        self.assertIn("AAAA - HOST01: target group changed from 100 to 200", err)
        self.assertIn("Nothing was changed.", err)
        self.assertEqual(self.events.log, [])
        self.assertTrue(self.gate_file().exists())
        self.assertFalse((self.out / "dry-run-ok.json.used").exists())

    def test_apply_refused_for_a_schema_2_file(self):
        self.dry_run()
        data = self.gate_data()
        data["schema_version"] = 2
        self.gate_file().write_text(json.dumps(data), encoding="utf-8")
        code, out, err = self.run_main(self.apply_argv(), env=self.both_tokens())
        self.assertEqual(code, 2, out + err)
        self.assertIn("old file format", err)
        self.assertIn("run --dry-run again", err)
        self.assertIn("Nothing was changed.", err)
        self.assertEqual(self.events.log, [])
        self.assertEqual(self.preflight_calls, [])

    def test_compare_refuses_a_schema_below_3(self):
        data = {"schema_version": 2, "ready": [], "excluded_ids": [], "owner_is_source": False}
        with self.assertRaises(cli.PreconditionRefused) as caught:
            cli._compare_dry_run_binding(data, [], False)
        self.assertIn("old file format", str(caught.exception))
        self.assertIn("run --dry-run again", str(caught.exception))
        self.assertIn("Nothing was changed.", str(caught.exception))
        self.assertIsInstance(caught.exception, ConfigurationError)

    def test_apply_refused_when_the_set_of_targets_changed(self):
        self.inventory.write_text(MIXED, encoding="utf-8")
        self.dry_run(env={"TE_TARGET_AID": "100"})
        self.assertEqual(self.gate_data()["target_aids"], ["100", "200"])
        # Same inventory file, other run target: the set of targets is now {200}.
        code, out, err = self.run_main(
            self.apply_argv(), env=dict(self.both_tokens(), TE_TARGET_AID="200")
        )
        self.assertEqual(code, 2, out + err)
        self.assertIn("target group(s)", err)
        self.assertIn("Nothing was changed.", err)
        self.assertEqual(self.events.log, [])

    def test_check_groups_ignores_the_order_of_targets(self):
        data = {"source_aid": "1", "target_aids": ["200", "100"]}
        source = AccountGroup("1", "Source")
        cli._check_dry_run_groups(data, source, ["100", "200"])
        with self.assertRaises(cli.PreconditionRefused):
            cli._check_dry_run_groups(data, source, ["100"])
        with self.assertRaises(cli.PreconditionRefused):
            cli._check_dry_run_groups(data, AccountGroup("2", "Other"), ["100", "200"])
        with self.assertRaises(cli.PreconditionRefused):
            cli._check_dry_run_groups({"source_aid": "1"}, source, ["100"])


class ApplyTokenTests(MultiTargetBase):
    def test_apply_collects_every_token_before_the_confirmation_and_the_first_flow(self):
        self.dry_run()
        # Token A comes from the environment. Token B comes from the hidden prompt.
        code, out, err = self.run_main(
            self.apply_argv(), env={"TE_TARGET_TOKEN_100": TOKEN_A}, secret=TOKEN_B
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.events.log, ["secret", "confirm", "flow", "flow", "flow"])
        tokens = dict((c.target_aid, c.target_account_token) for c in self.events.contexts)
        self.assertEqual(tokens, {"100": TOKEN_A, "200": TOKEN_B})

    def test_apply_with_both_tokens_from_the_environment_asks_nothing(self):
        self.dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.both_tokens())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.events.log, ["confirm", "flow", "flow", "flow"])
        self.assertIn("Target token for Group A [100] loaded from TE_TARGET_TOKEN_100", out)
        self.assertIn("Target token for Group B [200] loaded from TE_TARGET_TOKEN_200", out)

    def test_apply_context_holds_the_tokens_of_all_targets(self):
        self.dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.both_tokens())
        self.assertEqual(code, 0, out + err)
        for ctx in self.events.contexts:
            self.assertEqual(ctx.token_for("100"), TOKEN_A)
            self.assertEqual(ctx.token_for("200"), TOKEN_B)
            self.assertEqual(ctx.target_names["100"], "Group A")

    def test_missing_token_stops_before_confirmation_and_reset(self):
        self.dry_run()
        code, out, err = self.run_main(
            self.apply_argv(), env={"TE_TARGET_TOKEN_100": TOKEN_A}, secret="short-value"
        )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.events.log, ["secret"])
        self.assertEqual(self.events.names, [])
        self.assertNotIn("short-value", out + err)
        self.assertIn("received 11 character(s)", err)
        # The gate file was not used: the operator can try again.
        self.assertTrue(self.gate_file().exists())
        self.assertFalse((self.out / "dry-run-ok.json.used").exists())

    def test_declined_confirmation_runs_nothing_after_the_tokens(self):
        self.dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.both_tokens(), confirm=False)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.events.log, ["confirm"])
        self.assertTrue(self.gate_file().exists())

    def test_legacy_token_pair_serves_its_own_target_only(self):
        self.dry_run()
        env = {
            "TE_TARGET_ACCOUNT_TOKEN": TOKEN_A,
            "TE_TARGET_ACCOUNT_TOKEN_AID": "100",
            "TE_TARGET_TOKEN_200": TOKEN_B,
        }
        code, out, err = self.run_main(self.apply_argv(), env=env)
        self.assertEqual(code, 0, out + err)
        tokens = dict((c.target_aid, c.target_account_token) for c in self.events.contexts)
        self.assertEqual(tokens, {"100": TOKEN_A, "200": TOKEN_B})

    def test_token_values_never_appear_in_console_json_or_csv(self):
        self.dry_run()
        code, out, err = self.run_main(
            self.apply_argv(), env={"TE_TARGET_TOKEN_100": TOKEN_A}, secret=TOKEN_B
        )
        self.assertEqual(code, 0, out + err)
        text = self.all_output_text(out, err)
        for marker in (TOKEN_A, TOKEN_B, OAUTH_MARKER, "fake-pass"):
            self.assertNotIn(marker, text)
        files = [p.name for p in self.out.rglob("*") if p.is_file()]
        self.assertTrue(any(name.endswith(".json") for name in files))
        self.assertTrue(any(name.endswith(".csv") for name in files))

    def test_token_values_do_not_appear_when_the_token_is_refused(self):
        self.dry_run()
        bad = "bad token " + TOKEN_B  # too long after cleaning, so it is refused
        code, out, err = self.run_main(
            self.apply_argv(), env={"TE_TARGET_TOKEN_100": TOKEN_A}, secret=bad
        )
        self.assertEqual(code, 1, out + err)
        text = self.all_output_text(out, err)
        for marker in (TOKEN_A, TOKEN_B):
            self.assertNotIn(marker, text)


class SummaryTests(MultiTargetBase):
    def test_summary_lists_targets_and_the_target_of_each_result(self):
        self.run_main(["--strategy", "D"])
        summary = next(self.out.glob("*_strategy_d.json"))
        data = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual(data["target_aids"], ["100", "200"])
        self.assertEqual(data["source_aid"], "1")
        targets = dict((r["name"], r["target"]) for r in data["results"])
        self.assertEqual(
            targets, {"AAAA - HOST01": "100", "AAAA - CL01": "100", "AAAA - HOST02": "200"}
        )
        recap = dict((r["agent"], r.get("target")) for r in data["recap"])
        self.assertEqual(recap["AAAA - HOST02"], "Group B [200]")

    def test_summary_of_a_single_target_run_has_the_target_too(self):
        self.inventory.write_text(
            "type,name,ip_address\nagent,AAAA - HOST01,192.0.2.1\n", encoding="utf-8"
        )
        self.run_main(["--strategy", "D"], env={"TE_TARGET_AID": "100"})
        data = json.loads(next(self.out.glob("*_strategy_d.json")).read_text(encoding="utf-8"))
        self.assertEqual(data["target_aids"], ["100"])
        self.assertEqual(data["results"][0]["target"], "100")


if __name__ == "__main__":
    unittest.main()
