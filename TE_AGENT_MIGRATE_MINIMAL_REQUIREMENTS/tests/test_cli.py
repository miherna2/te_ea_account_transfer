import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from te_agent_migrate import cli
from te_agent_migrate.cli import _resolve_target_account_token
from te_agent_migrate.errors import ApiError, ConfigurationError
from te_agent_migrate.models import AccountGroup, InventoryAgent
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.strategy_d import runner
from te_agent_migrate.strategy_d.context import FlowAborted, FlowResult, PreflightItem


class TargetGroup(object):
    name = "AGENTS_SRC"
    aid = "144165"


class RecordingReport(object):
    def __init__(self):
        self.decisions = []

    def record_decision(self, prompt, response):
        self.decisions.append((prompt, response))


class RecordingConsole(object):
    def __init__(self):
        self.messages = []

    def info(self, category, message):
        self.messages.append((category, message))


class FixedSecretPrompter(object):
    def __init__(self, value):
        self.value = value

    def secret(self, unused_prompt):
        return self.value


class TargetTokenTests(unittest.TestCase):
    def test_pasted_token_ignores_whitespace_and_invisible_characters(self):
        token = "a" * 32
        pasted = " \t\u200b%s\ufeff\n" % token
        report = RecordingReport()
        console = RecordingConsole()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = _resolve_target_account_token(
                TargetGroup(),
                report,
                FixedSecretPrompter(pasted),
                console,
            )
        self.assertEqual(result, token)
        self.assertTrue(
            any("Ignored 5 whitespace or invisible" in message for unused, message in console.messages)
        )

    def test_invalid_token_reports_length_without_exposing_value(self):
        invalid = "secret-value"
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError) as caught:
                _resolve_target_account_token(
                    TargetGroup(),
                    RecordingReport(),
                    FixedSecretPrompter(invalid),
                    RecordingConsole(),
                )
        message = str(caught.exception)
        self.assertIn("received %s character(s)" % len(invalid), message)
        self.assertNotIn(invalid, message)


FAKE_TOKEN = "a1b2c3d4" * 4  # fake 32-character value for masking tests, not a credential
FAKE_API_TOKEN = "fake-oauth-value"
INVENTORY_TEXT = "type,name,ip_address\nagent,EA-ONE,10.0.0.1\nagent,EA-OFF,10.0.0.2\ncluster,CL-ONE,\n"


class FakeApi(object):
    created = 0
    list_error = None

    def __init__(self, token):
        FakeApi.created += 1

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def list_account_groups(self):
        if FakeApi.list_error is not None:
            raise FakeApi.list_error
        return [AccountGroup("1", "Source"), AccountGroup("9", "Target")]


class FlowRecorder(object):
    def __init__(self):
        self.contexts = []
        self.names = []
        self.fail_on = {}

    def reset(self):
        del self.contexts[:]
        del self.names[:]

    def standalone(self, ctx, item):
        self.contexts.append(ctx)
        self.names.append(item.name)
        action = self.fail_on.get(item.name)
        if isinstance(action, BaseException):
            raise action
        return FlowResult(item.name, True, "ok", new_agent_id="99")

    def cluster(self, ctx, item):
        self.contexts.append(ctx)
        self.names.append(item.name)
        return FlowResult(item.name, True, "ok", new_cluster_id="55")


def preflight_items(inventory_agents, **unused):
    items = []
    for inv in inventory_agents:
        status = "excluded" if inv.hostname == "EA-OFF" else "ready"
        items.append(PreflightItem(inv, status=status, reasons=["offline"] if status == "excluded" else [],
                                   owner_aid="1", share_aids=["1"]))
    return items


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.inventory = self.dir / "inventory.csv"
        self.inventory.write_text(INVENTORY_TEXT, encoding="utf-8")
        self.out = self.dir / "out"
        self.flows = FlowRecorder()
        FakeApi.created = 0
        FakeApi.list_error = None
        self.env = {
            "TE_OAUTH_TOKEN": FAKE_API_TOKEN,
            "TE_SOURCE_AID": "1",
            "TE_TARGET_AID": "9",
            "TE_AGENT_UI_USERNAME": "fake-user",
            "TE_AGENT_UI_PASSWORD": "fake-pass",
        }

    def run_main(self, argv, env=None, confirm=True, secret=None, preflight=preflight_items):
        base = ["--inventory", str(self.inventory), "--output", str(self.out)]
        environment = dict(self.env)
        environment.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        secret_mock = mock.Mock(side_effect=secret or AssertionError("secret prompt must not be used"))
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(cli, "load_dotenv"), \
                mock.patch.object(cli, "ThousandEyesApiClient", FakeApi), \
                mock.patch.object(runner, "run_preflight", side_effect=preflight), \
                mock.patch.object(cli, "run_preflight", side_effect=preflight), \
                mock.patch.object(runner, "migrate_standalone", side_effect=self.flows.standalone), \
                mock.patch.object(runner, "migrate_cluster", side_effect=self.flows.cluster), \
                mock.patch.object(OperatorPrompter, "secret", secret_mock), \
                mock.patch.object(OperatorPrompter, "confirm_destructive", return_value=confirm) as confirm_mock, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(base + argv)
        self.secret_mock = secret_mock
        self.confirm_mock = confirm_mock
        return code, out.getvalue(), err.getvalue()

    def dry_run_file(self):
        return self.out / "dry-run-ok.json"

    def apply_env(self):
        return {"TE_TARGET_ACCOUNT_TOKEN": FAKE_TOKEN, "TE_TARGET_ACCOUNT_TOKEN_AID": "9"}


class MainTests(CliTestBase):
    # ---- dry run
    def test_dry_run_strategy_d_needs_no_target_token(self):
        code, out, err = self.run_main(["--strategy", "D", "--dry-run"])
        self.assertEqual(code, 0, out + err)
        self.secret_mock.assert_not_called()
        self.assertEqual(self.flows.names, ["EA-ONE", "CL-ONE"])
        self.assertEqual([c.target_account_token for c in self.flows.contexts], ["", ""])
        self.assertFalse(self.flows.contexts[0].apply)
        self.confirm_mock.assert_not_called()
        self.assertIn("Inventory entries: 3", out)
        self.assertIn("PLAY RECAP", out)

    def test_dry_run_writes_hash_of_inventory(self):
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 0)
        data = json.loads(self.dry_run_file().read_text(encoding="utf-8"))
        expected = hashlib.sha256(self.inventory.read_bytes()).hexdigest()
        self.assertEqual(data["inventory_sha256"], expected)
        self.assertEqual(data["strategy"], "D")
        self.assertTrue(data["created_utc"].endswith("Z"))
        self.assertNotIn(FAKE_API_TOKEN, self.dry_run_file().read_text(encoding="utf-8"))

    def test_dry_run_with_blocker_is_not_confirmed_and_exit_is_1(self):
        def blocked(inventory_agents, **unused):
            items = preflight_items(inventory_agents)
            items[0].status = "blocked"
            items[0].reasons = ["name collision"]
            return items

        self.run_main(["--strategy", "D"])
        self.assertTrue(self.dry_run_file().exists())
        code, out, err = self.run_main(["--strategy", "D"], preflight=blocked)
        self.assertEqual(code, 1)
        self.assertFalse(self.dry_run_file().exists())

    def test_dry_run_writes_json_summary_without_secrets(self):
        self.run_main(["--strategy", "D"])
        summaries = list(self.out.glob("*_strategy_d.json"))
        self.assertEqual(len(summaries), 1)
        text = summaries[0].read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertEqual(data["mode"], "dry-run")
        self.assertEqual([r["name"] for r in data["results"]], ["EA-ONE", "CL-ONE"])
        for key in ("ok", "detail", "new_agent_id", "new_cluster_id", "warnings", "snapshot_path"):
            self.assertIn(key, data["results"][0])
        for secret in (FAKE_API_TOKEN, "fake-pass", FAKE_TOKEN):
            self.assertNotIn(secret, text)
        statuses = dict((r["agent"], r["status"]) for r in data["recap"])
        self.assertEqual(statuses["EA-OFF"], "skipped")

    # ---- apply gate
    def test_apply_strategy_d_without_dry_run_file_is_refused(self):
        code, out, err = self.run_main(["--strategy", "D", "--apply"], env=self.apply_env())
        self.assertEqual(code, 2)
        self.assertIn("--confirmed-dry-run-file", err)
        self.assertEqual(self.flows.names, [])
        self.assertEqual(FakeApi.created, 0)
        self.confirm_mock.assert_not_called()

    def test_apply_with_missing_dry_run_file_is_refused(self):
        argv = ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.dir / "nope.json")]
        code, out, err = self.run_main(argv, env=self.apply_env())
        self.assertEqual(code, 2)
        self.assertEqual(self.flows.names, [])

    def test_apply_with_stale_hash_is_refused(self):
        self.run_main(["--strategy", "D"])
        self.inventory.write_text(INVENTORY_TEXT + "agent,EA-NEW,10.0.0.9\n", encoding="utf-8")
        self.flows.reset()
        argv = ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.dry_run_file())]
        code, out, err = self.run_main(argv, env=self.apply_env())
        self.assertEqual(code, 2)
        self.assertIn("stale", err)
        self.assertEqual(self.flows.names, [])
        self.confirm_mock.assert_not_called()

    def test_apply_with_dry_run_for_other_groups_is_refused(self):
        self.run_main(["--strategy", "D"])
        self.flows.reset()
        argv = ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.dry_run_file())]
        env = self.apply_env()
        env["TE_TARGET_AID"] = "1"
        env["TE_SOURCE_AID"] = "9"
        env["TE_TARGET_ACCOUNT_TOKEN_AID"] = "1"
        code, out, err = self.run_main(argv, env=env)
        self.assertEqual(code, 2)
        self.assertEqual(self.flows.names, [])

    def test_apply_with_valid_dry_run_file_runs_after_confirmation(self):
        self.run_main(["--strategy", "D"])
        self.flows.reset()
        argv = ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.dry_run_file())]
        code, out, err = self.run_main(argv, env=self.apply_env())
        self.assertEqual(code, 0, out + err)
        self.confirm_mock.assert_called_once_with()
        self.assertEqual(self.flows.names, ["EA-ONE", "CL-ONE"])
        self.assertTrue(self.flows.contexts[0].apply)
        self.assertEqual(self.flows.contexts[0].target_account_token, FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, out + err)
        self.assertTrue((self.out / "snapshots").is_dir())

    def test_apply_shows_preflight_before_confirmation(self):
        self.run_main(["--strategy", "D"])
        self.flows.reset()
        argv = ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.dry_run_file())]
        code, out, err = self.run_main(argv, env=self.apply_env())
        self.assertIn("preflight before confirmation", out)
        self.assertLess(out.index("preflight before confirmation"), out.index("TASK [Strategy D") if "TASK [Strategy D" in out else len(out))

    def test_apply_declined_confirmation_changes_nothing(self):
        self.run_main(["--strategy", "D"])
        self.flows.reset()
        argv = ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.dry_run_file())]
        code, out, err = self.run_main(argv, env=self.apply_env(), confirm=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.flows.names, [])

    # ---- strategies
    def test_strategy_c_apply_is_blocked_with_exit_2(self):
        code, out, err = self.run_main(["--strategy", "C", "--apply", "--stale", "keep"], env=self.apply_env())
        self.assertEqual(code, 2)
        self.assertIn("Strategy C cannot keep tests in the source account group; use D", err)
        self.assertEqual(FakeApi.created, 0)

    def test_strategy_c_dry_run_warns_and_continues(self):
        fake_runner = mock.Mock()
        env = self.apply_env()
        with mock.patch.object(cli, "MigrationRunner", return_value=fake_runner):
            code, out, err = self.run_main(["--strategy", "C", "--stale", "keep"], env=env)
        self.assertEqual(code, 0, out + err)
        self.assertIn("WARNING: Strategy C cannot keep tests in the source account group", out)
        fake_runner.run.assert_called_once()

    def test_strategy_a_prints_warning_and_continues(self):
        fake_runner = mock.Mock()
        with mock.patch.object(cli, "MigrationRunner", return_value=fake_runner):
            code, out, err = self.run_main(["--strategy", "A"], env=self.apply_env())
        self.assertEqual(code, 0, out + err)
        self.assertIn("shares or recreates tests differently from D", out)
        fake_runner.run.assert_called_once()

    def test_parallel_is_forced_to_one_for_strategy_d(self):
        code, out, err = self.run_main(["--strategy", "D", "--parallel", "4"])
        self.assertEqual(code, 0)
        self.assertIn("--parallel 4 forced to 1", out)

    def test_parallel_one_prints_no_force_line(self):
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertNotIn("forced to 1", out)

    # ---- exit codes and errors
    def test_failure_stops_batch_and_exit_code_is_1(self):
        self.flows.fail_on["EA-ONE"] = RuntimeError("boom")
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 1)
        self.assertEqual(self.flows.names, ["EA-ONE"])
        self.assertFalse(self.dry_run_file().exists())

    def test_offline_items_are_skipped_and_untouched(self):
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertNotIn("EA-OFF", self.flows.names)

    def test_flow_aborted_from_the_runner_is_printed_masked_not_unexpected(self):
        message = "reset sent, token %s may be set; manual action needed" % FAKE_TOKEN
        with mock.patch.object(cli, "run_strategy_d", side_effect=FlowAborted(message)):
            code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 1)
        self.assertIn("manual action needed", err)
        self.assertNotIn("Unexpected failure", out + err)
        self.assertNotIn(FAKE_TOKEN, out + err)

    def test_flow_aborted_inside_a_flow_is_reported_and_exit_is_1(self):
        self.flows.fail_on["EA-ONE"] = FlowAborted("state unknown, token %s" % FAKE_TOKEN)
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 1)
        self.assertIn("state unknown", out)
        self.assertNotIn(FAKE_TOKEN, out + err)

    def test_secrets_are_masked_in_stderr_for_known_errors(self):
        FakeApi.list_error = ApiError("HTTP 401 for Bearer %s and %s" % (FAKE_API_TOKEN, FAKE_TOKEN))
        code, out, err = self.run_main(["--strategy", "D"])
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_TOKEN, out + err)
        self.assertNotIn(FAKE_API_TOKEN, out + err)
        self.assertIn("[REDACTED]", err)

    def test_verbose_traceback_is_masked(self):
        FakeApi.list_error = ValueError("odd value %s" % FAKE_TOKEN)
        code, out, err = self.run_main(["--strategy", "D", "-v"])
        self.assertEqual(code, 1)
        self.assertIn("Unexpected failure (ValueError)", err)
        self.assertNotIn(FAKE_TOKEN, out + err)

    def test_strategy_d_is_accepted_and_help_lists_new_options(self):
        text = cli.build_parser().format_help()
        self.assertIn("--confirmed-dry-run-file", text)
        self.assertIn("D=", text)
        args = cli.build_parser().parse_args(["--strategy", "D", "--confirmed-dry-run-file", "x"])
        self.assertEqual(args.confirmed_dry_run_file, "x")


class Rec(object):
    """Minimal AgentRecord stand-in: the CLI reads only ``agent_id``."""

    def __init__(self, agent_id):
        self.agent_id = agent_id


IDS = {"EA-ONE": "11", "EA-OFF": "12", "CL-ONE": "13"}


def bound_preflight(shares=None, online=(), owner_assumed=(), blocked=(), extra_ready=()):
    """Preflight stand-in whose items carry record ids, share sets and owner flags."""
    shares = shares or {}

    def run(inventory_agents, **unused):
        items = []
        for inv in inventory_agents:
            name = inv.hostname
            if name in blocked:
                status = "blocked"
            elif name == "EA-OFF" and name not in online:
                status = "excluded"
            else:
                status = "ready"
            item = PreflightItem(
                inv, record=Rec(IDS[name]), status=status,
                reasons=["why"] if status != "ready" else [],
                owner_aid="1", share_aids=shares.get(name, ["1"]),
            )
            item.owner_assumed = name in owner_assumed
            items.append(item)
        return items

    return run


class BindingTests(CliTestBase):
    """Dry-run file binding, single use, interrupts, umask and the new flag."""

    def apply_argv(self, *extra):
        return ["--strategy", "D", "--apply", "--confirmed-dry-run-file", str(self.dry_run_file())] + list(extra)

    def make_dry_run(self, preflight=None, argv=()):
        code, out, err = self.run_main(
            ["--strategy", "D"] + list(argv), preflight=preflight or bound_preflight()
        )
        self.assertEqual(code, 0, out + err)
        self.flows.reset()

    def assert_refused(self, code, out, err, text=None):
        self.assertEqual(code, 2, out + err)
        self.assertIn("Dry-run confirmation does not match", err)
        if text:
            self.assertIn(text, err)
        self.assertEqual(self.flows.names, [])
        self.confirm_mock.assert_not_called()
        self.assertTrue(self.dry_run_file().exists())

    # ---- file content
    def test_dry_run_file_is_bound_to_device_ids_shares_and_owner_flag(self):
        self.make_dry_run(bound_preflight(shares={"EA-ONE": ["9x", "1"][::-1], "CL-ONE": ["1", "5"]},
                                          owner_assumed=("CL-ONE",)), argv=["--owner-is-source"])
        data = json.loads(self.dry_run_file().read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 2)
        self.assertTrue(data["owner_is_source"])
        self.assertEqual(data["excluded_ids"], ["12"])
        ready = dict((entry["name"], entry) for entry in data["ready"])
        self.assertEqual(sorted(ready), ["CL-ONE", "EA-ONE"])
        self.assertEqual(ready["EA-ONE"]["agent_id"], "11")
        self.assertEqual(ready["EA-ONE"]["kind"], "agent")
        self.assertEqual(ready["EA-ONE"]["share_aids"], ["1", "9x"])
        self.assertEqual(ready["EA-ONE"]["owner"], "proven")
        self.assertEqual(ready["CL-ONE"]["kind"], "cluster")
        self.assertEqual(ready["CL-ONE"]["owner"], "assumed")
        self.assertEqual(ready["CL-ONE"]["share_aids"], ["1", "5"])

    def test_dry_run_owner_flag_is_false_without_the_option(self):
        self.make_dry_run()
        data = json.loads(self.dry_run_file().read_text(encoding="utf-8"))
        self.assertFalse(data["owner_is_source"])

    # ---- binding refusals (exit 2, nothing changed)
    def test_apply_with_identical_state_runs(self):
        self.make_dry_run(bound_preflight(shares={"EA-ONE": ["1", "5"]}))
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(),
                                       preflight=bound_preflight(shares={"EA-ONE": ["5", "1"]}))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.flows.names, ["EA-ONE", "CL-ONE"])

    def test_apply_refused_when_a_new_device_became_ready(self):
        self.make_dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(),
                                       preflight=bound_preflight(online=("EA-OFF",)))
        self.assert_refused(code, out, err, "EA-OFF is ready now")

    def test_apply_refused_when_a_share_set_changed(self):
        self.make_dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(),
                                       preflight=bound_preflight(shares={"EA-ONE": ["1", "5"]}))
        self.assert_refused(code, out, err, "share set changed")

    def test_apply_refused_when_the_owner_flag_of_a_device_changed(self):
        self.make_dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(),
                                       preflight=bound_preflight(owner_assumed=("EA-ONE",)))
        self.assert_refused(code, out, err, "owner was proven")

    def test_apply_refused_when_owner_is_source_flag_differs(self):
        self.make_dry_run()
        code, out, err = self.run_main(self.apply_argv("--owner-is-source"), env=self.apply_env(),
                                       preflight=bound_preflight())
        self.assert_refused(code, out, err, "--owner-is-source was not set")

    def test_apply_refused_when_owner_is_source_flag_was_dropped(self):
        self.make_dry_run(argv=["--owner-is-source"])
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assert_refused(code, out, err, "--owner-is-source was set")

    def test_apply_refused_when_a_device_became_blocked_even_in_a_re_run(self):
        self.make_dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(),
                                       preflight=bound_preflight(blocked=("EA-ONE",)))
        self.assert_refused(code, out, err, "EA-ONE was ready in the dry run but is now blocked")

    def test_apply_refused_when_a_ready_device_became_excluded(self):
        self.make_dry_run(bound_preflight(online=("EA-OFF",)))
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assert_refused(code, out, err, "EA-OFF was ready in the dry run but is now excluded")

    def test_apply_refused_when_the_dry_run_file_has_no_binding(self):
        self.make_dry_run()
        data = json.loads(self.dry_run_file().read_text(encoding="utf-8"))
        for key in ("ready", "excluded_ids", "owner_is_source"):
            data.pop(key)
        self.dry_run_file().write_text(json.dumps(data), encoding="utf-8")
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assertEqual(code, 2)
        self.assertIn("no device binding", err)
        self.assertEqual(self.flows.names, [])

    # ---- single use
    def test_apply_renames_the_dry_run_file_before_the_first_flow(self):
        self.make_dry_run()
        seen = []
        original = self.flows.standalone

        def standalone(ctx, item):
            seen.append((self.dry_run_file().exists(), Path(str(self.dry_run_file()) + ".used").exists()))
            return original(ctx, item)

        self.flows.standalone = standalone
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [(False, True)])
        self.assertFalse(self.dry_run_file().exists())
        self.assertTrue((self.out / "dry-run-ok.json.used").exists())

    def test_second_apply_with_the_same_file_is_refused(self):
        self.make_dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assertEqual(code, 0, out + err)
        self.flows.reset()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)
        self.assertEqual(self.flows.names, [])

    def test_rename_failure_refuses_to_start(self):
        self.make_dry_run()
        with mock.patch.object(cli.os, "replace", side_effect=OSError("read-only")):
            code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assertEqual(code, 2)
        self.assertIn("refusing to start", err)
        self.assertEqual(self.flows.names, [])

    def test_declined_confirmation_keeps_the_dry_run_file(self):
        self.make_dry_run()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), confirm=False,
                                       preflight=bound_preflight())
        self.assertEqual(code, 1)
        self.assertTrue(self.dry_run_file().exists())
        self.assertFalse((self.out / "dry-run-ok.json.used").exists())

    def test_refused_apply_keeps_the_dry_run_file_unused(self):
        self.make_dry_run()
        self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight(online=("EA-OFF",)))
        self.assertTrue(self.dry_run_file().exists())
        self.assertFalse((self.out / "dry-run-ok.json.used").exists())

    # ---- M1 summary content
    def test_summary_reads_snapshot_of_a_result(self):
        def standalone(ctx, item):
            return FlowResult(item.name, True, "ok", new_agent_id="99", snapshot="/snap/a.json")

        self.flows.standalone = standalone
        self.run_main(["--strategy", "D"], preflight=bound_preflight())
        data = json.loads(next(self.out.glob("*_strategy_d.json")).read_text(encoding="utf-8"))
        self.assertEqual(data["results"][0]["snapshot_path"], "/snap/a.json")

    def test_summary_keeps_ids_snapshot_and_state_of_a_failed_flow(self):
        self.make_dry_run()
        exc = FlowAborted("delete not confirmed")
        exc.new_agent_id = "4711"
        exc.snapshot = "/snap/b.json"
        exc.state = "old agent still present"
        self.flows.fail_on["EA-ONE"] = exc
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assertEqual(code, 1, out + err)
        data = json.loads(next(self.out.glob("*apply*_strategy_d.json")).read_text(encoding="utf-8"))
        failed = data["results"][0]
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["new_agent_id"], "4711")
        self.assertEqual(failed["snapshot_path"], "/snap/b.json")
        self.assertIn("old agent still present", failed["detail"])

    # ---- M3 interrupt
    def test_ctrl_c_in_a_flow_writes_summary_and_report_and_exits_130(self):
        self.make_dry_run()
        self.flows.fail_on["EA-ONE"] = KeyboardInterrupt()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
        self.assertEqual(code, 130, out + err)
        summaries = sorted(self.out.glob("*apply*_strategy_d.json"))
        self.assertTrue(summaries)
        data = json.loads(summaries[-1].read_text(encoding="utf-8"))
        self.assertEqual(data["exit_code"], 130)
        self.assertFalse(data["results"][0]["ok"])
        self.assertIn("interrupted", data["results"][0]["detail"])
        rows = dict((r["agent"], r["status"]) for r in data["recap"])
        self.assertEqual(rows["EA-ONE"], "failed")
        self.assertEqual(rows["CL-ONE"], "skipped")
        report_path = [path for path in self.out.glob("te_agent_migration_apply_*.json")
                       if not path.name.endswith("_strategy_d.json")][0]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "final")
        self.assertIn("PLAY RECAP", out)
        self.assertIn("Interrupted by the operator", err)

    def test_ctrl_c_in_a_dry_run_does_not_write_the_confirmation_file(self):
        self.flows.fail_on["EA-ONE"] = KeyboardInterrupt()
        code, out, err = self.run_main(["--strategy", "D"], preflight=bound_preflight())
        self.assertEqual(code, 130)
        self.assertFalse(self.dry_run_file().exists())
        self.assertTrue(list(self.out.glob("*_strategy_d.json")))

    def test_system_exit_in_a_flow_writes_summary_and_propagates(self):
        self.flows.fail_on["EA-ONE"] = SystemExit(4)
        with self.assertRaises(SystemExit):
            self.run_main(["--strategy", "D"], preflight=bound_preflight())
        self.assertTrue(list(self.out.glob("*_strategy_d.json")))

    # ---- L6 umask
    def test_report_files_are_private_and_umask_is_restored(self):
        self.make_dry_run()
        old = os.umask(0o022)
        try:
            code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(), preflight=bound_preflight())
            self.assertEqual(code, 0, out + err)
            self.assertEqual(os.umask(0o022), 0o022)
        finally:
            os.umask(old)
        self.assertTrue(self.out.is_dir())
        self.assertEqual(self.out.stat().st_mode & 0o077, 0)
        names = []
        for path in sorted(self.out.rglob("*")):
            names.append(path.name)
            self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
        self.assertTrue(any(name.endswith("_strategy_d.json") for name in names))
        self.assertIn("dry-run-ok.json.used", names)

    def test_dry_run_file_is_0600_under_a_loose_umask(self):
        old = os.umask(0o000)
        try:
            self.run_main(["--strategy", "D"], preflight=bound_preflight())
        finally:
            os.umask(old)
        self.assertEqual(self.dry_run_file().stat().st_mode & 0o777, 0o600)
        for path in self.out.glob("*_strategy_d.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_umask_is_restored_after_a_refusal(self):
        old = os.umask(0o022)
        try:
            self.run_main(["--strategy", "D", "--apply"], env=self.apply_env())
            self.assertEqual(os.umask(0o022), 0o022)
        finally:
            os.umask(old)

    # ---- --auto-reenable-tests
    def capture_contexts(self):
        seen = []

        class Ctx(cli.FlowContext):
            def __init__(self, auto_reenable_tests=False, **kwargs):
                seen.append(auto_reenable_tests)
                super(Ctx, self).__init__(**kwargs)

        return seen, Ctx

    def test_auto_reenable_tests_is_off_by_default(self):
        seen, Ctx = self.capture_contexts()
        with mock.patch.object(cli, "FlowContext", Ctx):
            code, out, err = self.run_main(["--strategy", "D"], preflight=bound_preflight())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [False])

    def test_auto_reenable_tests_is_passed_to_the_context(self):
        seen, Ctx = self.capture_contexts()
        with mock.patch.object(cli, "FlowContext", Ctx):
            code, out, err = self.run_main(["--strategy", "D", "--auto-reenable-tests"],
                                           preflight=bound_preflight())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [True])

    def test_auto_reenable_tests_reaches_the_apply_and_preflight_contexts(self):
        self.make_dry_run()
        seen, Ctx = self.capture_contexts()
        with mock.patch.object(cli, "FlowContext", Ctx):
            code, out, err = self.run_main(self.apply_argv("--auto-reenable-tests"), env=self.apply_env(),
                                           preflight=bound_preflight())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [True, True])

    def test_auto_reenable_tests_with_an_old_context_warns_and_continues(self):
        class OldCtx(cli.FlowContext):
            def __init__(self, api, ui_factory, console, source_aid, target_aid, target_account_token="",
                         apply=False, timeout_seconds=600, poll_seconds=10, snapshot_dir=None):
                super(OldCtx, self).__init__(
                    api, ui_factory, console, source_aid, target_aid, target_account_token, apply,
                    timeout_seconds, poll_seconds, snapshot_dir=snapshot_dir)

        with mock.patch.object(cli, "FlowContext", OldCtx):
            code, out, err = self.run_main(["--strategy", "D", "--auto-reenable-tests"],
                                           preflight=bound_preflight())
            self.assertEqual(code, 0, out + err)
            self.assertIn("auto_reenable_tests is not supported by this build", out)
            code, out, err = self.run_main(["--strategy", "D"], preflight=bound_preflight())
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("not supported by this build", out)

    def test_auto_reenable_tests_is_ignored_with_a_message_outside_strategy_d(self):
        fake_runner = mock.Mock()
        with mock.patch.object(cli, "MigrationRunner", return_value=fake_runner):
            code, out, err = self.run_main(["--strategy", "A", "--auto-reenable-tests"], env=self.apply_env())
        self.assertEqual(code, 0, out + err)
        self.assertIn("--auto-reenable-tests applies only to Strategy D", out)

    def test_help_describes_the_flag(self):
        text = cli.build_parser().format_help()
        self.assertIn("--auto-reenable-tests", text)
        self.assertIn("lab check 7", text)
        self.assertFalse(cli.build_parser().parse_args(["--strategy", "D"]).auto_reenable_tests)
        self.assertTrue(cli.build_parser().parse_args(["--auto-reenable-tests"]).auto_reenable_tests)


class FlowContextBuildTests(unittest.TestCase):
    def test_snapshot_dir_is_passed_when_supported(self):
        seen = {}

        class Ctx(object):
            def __init__(self, snapshot_dir=None, **kwargs):
                seen["snapshot_dir"] = snapshot_dir

        with mock.patch.object(cli, "FlowContext", Ctx):
            cli._build_flow_context({"api": None, "console": RecordingConsole()}, "/tmp/snaps")
        self.assertEqual(seen["snapshot_dir"], "/tmp/snaps")

    def test_falls_back_when_snapshot_dir_is_unknown(self):
        class Ctx(object):
            def __init__(self, api=None, console=None):
                self.api = api

        console = RecordingConsole()
        with mock.patch.object(cli, "FlowContext", Ctx):
            ctx = cli._build_flow_context({"api": "x", "console": console}, "/tmp/snaps")
        self.assertEqual(ctx.api, "x")
        self.assertTrue(console.messages)

    def test_other_type_errors_are_not_hidden(self):
        class Ctx(object):
            def __init__(self, snapshot_dir=None, **kwargs):
                raise TypeError("something else")

        with mock.patch.object(cli, "FlowContext", Ctx):
            with self.assertRaises(TypeError):
                cli._build_flow_context({"console": RecordingConsole()}, "/tmp/snaps")


class FlowContextOptionalParamTests(unittest.TestCase):
    def test_both_optional_params_are_passed_when_supported(self):
        seen = {}

        class Ctx(object):
            def __init__(self, snapshot_dir=None, auto_reenable_tests=False, **kwargs):
                seen.update(snapshot_dir=snapshot_dir, auto=auto_reenable_tests)

        with mock.patch.object(cli, "FlowContext", Ctx):
            cli._build_flow_context({"console": RecordingConsole(), "auto_reenable_tests": True}, "/s")
        self.assertEqual(seen, {"snapshot_dir": "/s", "auto": True})

    def test_unsupported_auto_flag_true_is_announced_and_false_is_silent(self):
        class Ctx(object):
            def __init__(self, snapshot_dir=None, console=None):
                self.console = console

        for value, expect_message in ((True, True), (False, False)):
            console = RecordingConsole()
            with mock.patch.object(cli, "FlowContext", Ctx):
                cli._build_flow_context({"console": console, "auto_reenable_tests": value}, "/s")
            self.assertEqual(
                any("auto_reenable_tests" in message for unused, message in console.messages), expect_message
            )


class UiFactoryTests(unittest.TestCase):
    def test_factory_takes_an_ip_and_builds_a_ui_client_lazily(self):
        calls = []
        resolver = mock.Mock(return_value=("user", "pw"))
        with mock.patch.object(cli, "TevaUiClient", side_effect=lambda ip, u, p: calls.append((ip, u)) or "client"):
            factory = cli._make_ip_ui_factory(cli._lazy(resolver))
            resolver.assert_not_called()
            self.assertEqual(factory("10.0.0.5"), "client")
            factory("10.0.0.6")
        resolver.assert_called_once_with()
        self.assertEqual(calls, [("10.0.0.5", "user"), ("10.0.0.6", "user")])


if __name__ == "__main__":
    unittest.main()
