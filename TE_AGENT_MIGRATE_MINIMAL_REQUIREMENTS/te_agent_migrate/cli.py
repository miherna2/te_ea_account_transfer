"""Argparse command-line entry point for Python 3.6 through 3.9."""

import argparse
import os
import re
import sys
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.errors import ConfigurationError, MigrationError
from te_agent_migrate.inventory import load_inventory
from te_agent_migrate.models import Mode, RunOptions, StalePolicy, StepStatus, TestStrategy
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.output import MigrationConsole
from te_agent_migrate.reporting import RunReport
from te_agent_migrate.ui_client import TevaUiClient
from te_agent_migrate.workflow import MigrationRunner


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{32}$")
UI_USERNAME_ENV = "TE_AGENT_UI_USERNAME"
UI_PASSWORD_ENV = "TE_AGENT_UI_PASSWORD"
TARGET_ACCOUNT_TOKEN_ENV = "TE_TARGET_ACCOUNT_TOKEN"
TARGET_ACCOUNT_TOKEN_AID_ENV = "TE_TARGET_ACCOUNT_TOKEN_AID"


def _environment_value(name):
    value = os.getenv(name)
    if value is None:
        return None
    if not value:
        raise ConfigurationError("%s is set but empty" % name)
    return value


def _normalize_account_group_token(value):
    """Remove copy/paste whitespace and invisible format characters."""
    return "".join(
        character
        for character in value
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )


def _positive_integer(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def _parallelism(value):
    result = _positive_integer(value)
    if result > 5:
        raise argparse.ArgumentTypeError("must be between 1 and 5")
    return result


def build_parser():
    parser = argparse.ArgumentParser(
        prog="te-agent-migrate",
        description="Migrate ThousandEyes Enterprise Agents between account groups.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default).")
    mode.add_argument("--apply", action="store_true", help="Execute the migration.")
    parser.add_argument("--inventory", default="inventory.csv", help="Inventory CSV path.")
    parser.add_argument("--strategy", help="A=agents only, B=share tests, C=recreate tests.")
    parser.add_argument("--stale", help="Strategy C source-test policy: keep or remove.")
    parser.add_argument(
        "--create-missing-tags",
        action="store_true",
        help="Strategy C: create/reuse missing target test tags.",
    )
    parser.add_argument(
        "--create-missing-alerts",
        action="store_true",
        help="Strategy C: create/reuse missing target alert rules.",
    )
    parser.add_argument("--timeout", type=_positive_integer, default=180)
    parser.add_argument("--parallel", type=_parallelism, default=1, dest="parallelism")
    parser.add_argument("--output", default="reports", help="Artifact directory.")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _resolve_agent_ui_inputs(report, prompter, console):
    ui_username = _environment_value(UI_USERNAME_ENV)
    if ui_username is None:
        ui_username = prompter.text("Agent UI username")
    else:
        report.record_decision("Agent UI username", "%s via %s" % (ui_username, UI_USERNAME_ENV))
        console.info("input", "Agent UI username loaded from %s" % UI_USERNAME_ENV)
    ui_password = _environment_value(UI_PASSWORD_ENV)
    if ui_password is None:
        ui_password = prompter.secret("Agent UI password")
    else:
        report.record_decision("Agent UI password", "<environment:%s>" % UI_PASSWORD_ENV)
        console.info("input", "Agent UI password loaded from %s" % UI_PASSWORD_ENV)
    return ui_username, ui_password


def _resolve_target_account_token(target, report, prompter, console):
    target_token = _environment_value(TARGET_ACCOUNT_TOKEN_ENV)
    if target_token is None:
        target_token = prompter.secret(
            "Target account-group token for %s [%s]" % (target.name, target.aid)
        )
        report.record_decision(
            "Target account-group token binding",
            "hidden prompt bound to %s [%s]" % (target.name, target.aid),
        )
    else:
        token_aid = _environment_value(TARGET_ACCOUNT_TOKEN_AID_ENV)
        if token_aid is None:
            raise ConfigurationError(
                "%s must also be set when %s comes from the environment; expected %s. "
                "No agents were reset."
                % (TARGET_ACCOUNT_TOKEN_AID_ENV, TARGET_ACCOUNT_TOKEN_ENV, target.aid)
            )
        if token_aid != target.aid:
            raise ConfigurationError(
                "%s=%s does not match selected target %s [%s]. No agents were reset."
                % (TARGET_ACCOUNT_TOKEN_AID_ENV, token_aid, target.name, target.aid)
            )
        report.record_decision(
            "Target account-group token", "<environment:%s>" % TARGET_ACCOUNT_TOKEN_ENV
        )
        report.record_decision(
            "Target account-group token binding",
            "%s [%s] via %s" % (target.name, target.aid, TARGET_ACCOUNT_TOKEN_AID_ENV),
        )
        console.info(
            "input",
            "Target token loaded from %s and bound to %s [%s] via %s"
            % (TARGET_ACCOUNT_TOKEN_ENV, target.name, target.aid, TARGET_ACCOUNT_TOKEN_AID_ENV),
        )
    original_length = len(target_token)
    target_token = _normalize_account_group_token(target_token)
    removed_count = original_length - len(target_token)
    if removed_count:
        console.info(
            "input",
            "Ignored %s whitespace or invisible copy/paste character(s) in the "
            "target account-group token" % removed_count,
        )
    if not TOKEN_PATTERN.fullmatch(target_token):
        raise ConfigurationError(
            "Target account-group token must be exactly 32 alphanumeric characters "
            "after removing whitespace and invisible copy/paste characters; received "
            "%s character(s)" % len(target_token)
        )
    return target_token


def _resolve_source_test_policy(strategy, supplied_policy, report, prompter, console):
    if strategy is TestStrategy.RECREATE:
        value = supplied_policy or prompter.required_choice(
            "Strategy C source-test policy", ("keep", "remove")
        )
        value = value.lower()
        if value not in ("keep", "remove"):
            raise ConfigurationError("--stale must be keep or remove")
        policy = StalePolicy(value)
        report.record_decision("Strategy C source-test policy", policy.value)
        console.info("configuration", "Strategy C source-test policy=%s" % policy.value)
        return policy
    supplied = "not supplied" if supplied_policy is None else "--stale=%s" % supplied_policy.lower()
    detail = "not applicable to Strategy %s; %s and ignored" % (strategy.value, supplied)
    report.record_decision("Strategy C source-test policy", detail)
    console.info("configuration", "stale policy %s" % detail)
    return StalePolicy.KEEP


def _group_from_env(groups, env_name, label, prompter):
    value = os.getenv(env_name)
    if value:
        group = next((item for item in groups if item.aid == value), None)
        if group is None:
            raise ConfigurationError("%s=%s is not accessible to the OAuth token" % (env_name, value))
        prompter.record(
            "Select %s account group" % label,
            "%s [%s] via environment" % (group.name, group.aid),
        )
        return group
    return prompter.choose_account_group("Select %s account group" % label, groups)


def main(argv=None):
    load_dotenv()
    args = build_parser().parse_args(argv)
    selected_mode = Mode.APPLY if args.apply else Mode.DRY_RUN
    inventory = Path(args.inventory)
    output = Path(args.output)
    console = MigrationConsole(verbose=args.verbose)
    console.banner()
    console.info("startup", "mode=%s; report output=%s" % (selected_mode.value, output))
    report = RunReport(output, selected_mode.value, {"inventory": str(inventory)})
    prompter = OperatorPrompter(report.append_decision)
    try:
        if (
            os.getenv("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
            and os.getenv("TE_ALLOW_CODEX_SANDBOX_NETWORK") != "1"
        ):
            raise ConfigurationError(
                "Codex network sandbox blocks direct agent HTTPS/443 connections. "
                "Run this command from a local terminal outside Codex."
            )
        console.progress("global", "inventory", "reading %s" % inventory)
        agents = load_inventory(inventory)
        console.status(StepStatus.OK, "global", "inventory", "loaded %s agent(s)" % len(agents))
        strategy_value = args.strategy or prompter.required_choice(
            "Test-handling strategy", ("A", "B", "C")
        )
        strategy_value = strategy_value.upper()
        if strategy_value not in ("A", "B", "C"):
            raise ConfigurationError("--strategy must be A, B, or C")
        selected_strategy = TestStrategy(strategy_value)
        report.record_decision("Test-handling strategy", selected_strategy.value)
        selected_stale_policy = _resolve_source_test_policy(
            selected_strategy, args.stale, report, prompter, console
        )
        strategy_c = selected_strategy is TestStrategy.RECREATE
        create_tags = strategy_c and args.create_missing_tags
        create_alerts = strategy_c and args.create_missing_alerts
        report.record_decision(
            "Strategy C create missing tags",
            "enabled" if create_tags else "disabled" if strategy_c else "not-applicable",
        )
        report.record_decision(
            "Strategy C create missing alert rules",
            "enabled" if create_alerts else "disabled" if strategy_c else "not-applicable",
        )
        ui_username, ui_password = _resolve_agent_ui_inputs(report, prompter, console)
        api_token = os.getenv("TE_OAUTH_TOKEN") or os.getenv("THOUSANDEYES_OAUTH_TOKEN")
        if api_token:
            report.record_decision("API v7 OAuth bearer token", "<environment>")
            console.info("input", "API OAuth token loaded from environment")
        else:
            api_token = prompter.secret("API v7 OAuth bearer token")
        options = RunOptions(
            mode=selected_mode,
            strategy=selected_strategy,
            stale_policy=selected_stale_policy,
            timeout_seconds=args.timeout,
            parallelism=args.parallelism,
            create_missing_tags=create_tags,
            create_missing_alerts=create_alerts,
            verbose=args.verbose,
        )
        with ThousandEyesApiClient(api_token) as api:
            console.progress("global", "api_account_groups", "connecting to ThousandEyes API v7")
            groups = api.list_account_groups()
            console.status(
                StepStatus.OK,
                "global",
                "api_account_groups",
                "OAuth token can access %s account group(s)" % len(groups),
            )
            source = _group_from_env(groups, "TE_SOURCE_AID", "source", prompter)
            target = _group_from_env(groups, "TE_TARGET_AID", "target", prompter)
            if source.aid == target.aid:
                raise ConfigurationError("Source and target account groups must be different")
            console.info(
                "configuration",
                "source=%s [%s]; target=%s [%s]"
                % (source.name, source.aid, target.name, target.aid),
            )
            console.destination_banner(source, target, selected_mode)
            target_token = _resolve_target_account_token(target, report, prompter, console)
            runner = MigrationRunner(
                api=api,
                ui_factory=lambda item: TevaUiClient(item.ip_address, ui_username, ui_password),
                report=report,
                prompter=prompter,
                console=console,
                options=options,
                source_group=source,
                target_group=target,
                target_account_token=target_token,
            )
            console.info(
                "run",
                "starting batched processing for %s agent(s); parallel connections=%s"
                % (len(agents), args.parallelism),
            )
            runner.run(agents)
    except MigrationError as exc:
        report.record_error("run", str(exc))
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", str(exc))
        print("Error: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:
        detail = "Unexpected failure (%s)" % type(exc).__name__
        report.record_error("run", detail)
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", detail)
        if args.verbose:
            raise
        print("Error: %s" % detail, file=sys.stderr)
        return 1
    console.info("run", "completed; final audit artifacts were written")
    print("JSON report: %s" % report.json_path)
    print("CSV reports: %s" % report.csv_dir)
    return 0
