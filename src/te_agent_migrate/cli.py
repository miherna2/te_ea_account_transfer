"""Command-line entry point."""

from __future__ import annotations

import os
import re
from pathlib import Path

import click

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.errors import ConfigurationError, MigrationError
from te_agent_migrate.inventory import load_inventory
from te_agent_migrate.models import (
    AccountGroup,
    Mode,
    RunOptions,
    StalePolicy,
    StepStatus,
    TestStrategy,
)
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.output import MigrationConsole
from te_agent_migrate.reporting import RunReport
from te_agent_migrate.ui_client import TevaUiClient
from te_agent_migrate.workflow import MigrationRunner

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{32}$")
UI_USERNAME_ENV = "TE_AGENT_UI_USERNAME"
UI_PASSWORD_ENV = "TE_AGENT_UI_PASSWORD"
TARGET_ACCOUNT_TOKEN_ENV = "TE_TARGET_ACCOUNT_TOKEN"


def _environment_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    if not value:
        raise ConfigurationError(f"{name} is set but empty")
    return value


def _resolve_agent_inputs(
    report: RunReport,
    prompter: OperatorPrompter,
    console: MigrationConsole,
) -> tuple[str, str, str]:
    ui_username = _environment_value(UI_USERNAME_ENV)
    if ui_username is None:
        ui_username = click.prompt("Agent UI username")
        report.record_decision("Agent UI username", ui_username)
    else:
        report.record_decision("Agent UI username", f"{ui_username} via {UI_USERNAME_ENV}")
        console.info("input", f"Agent UI username loaded from {UI_USERNAME_ENV}")

    ui_password = _environment_value(UI_PASSWORD_ENV)
    if ui_password is None:
        ui_password = prompter.secret("Agent UI password")
    else:
        report.record_decision("Agent UI password", f"<environment:{UI_PASSWORD_ENV}>")
        console.info("input", f"Agent UI password loaded from {UI_PASSWORD_ENV}")

    target_token = _environment_value(TARGET_ACCOUNT_TOKEN_ENV)
    if target_token is None:
        target_token = prompter.secret("Target account-group token")
    else:
        report.record_decision(
            "Target account-group token", f"<environment:{TARGET_ACCOUNT_TOKEN_ENV}>"
        )
        console.info("input", f"Target account-group token loaded from {TARGET_ACCOUNT_TOKEN_ENV}")
    if not TOKEN_PATTERN.fullmatch(target_token):
        raise ConfigurationError(
            "Target account-group token must be exactly 32 alphanumeric characters"
        )
    return ui_username, ui_password, target_token


def _resolve_source_test_policy(
    strategy: TestStrategy,
    supplied_policy: str | None,
    report: RunReport,
    console: MigrationConsole,
) -> StalePolicy:
    if strategy is TestStrategy.RECREATE:
        value = supplied_policy or click.prompt(
            "Strategy C source-test policy",
            type=click.Choice([item.value for item in StalePolicy], case_sensitive=False),
        )
        policy = StalePolicy(value.lower())
        report.record_decision("Strategy C source-test policy", policy.value)
        console.info("configuration", f"Strategy C source-test policy={policy.value}")
        return policy

    supplied = "not supplied" if supplied_policy is None else f"--stale={supplied_policy.lower()}"
    detail = f"not applicable to Strategy {strategy.value}; {supplied} and ignored"
    report.record_decision("Strategy C source-test policy", detail)
    console.info("configuration", f"stale policy {detail}")
    return StalePolicy.KEEP


def _group_from_env(
    groups: list[AccountGroup], env_name: str, label: str, prompter: OperatorPrompter
) -> AccountGroup:
    value = os.getenv(env_name)
    if value:
        group = next((item for item in groups if item.aid == value), None)
        if group is None:
            raise ConfigurationError(f"{env_name}={value} is not accessible to the OAuth token")
        prompter.record(
            f"Select {label} account group", f"{group.name} [{group.aid}] via environment"
        )
        return group
    return prompter.choose_account_group(f"Select {label} account group", groups)


@click.command(context_settings={"help_option_names": ["--help"]})
@click.option(
    "--dry-run",
    "mode",
    flag_value=Mode.DRY_RUN,
    default=True,
    help="Preview only, no changes (default).",
)
@click.option("--apply", "mode", flag_value=Mode.APPLY, help="Execute the migration.")
@click.option(
    "--inventory",
    type=click.Path(path_type=Path),
    default=Path("inventory.csv"),
    show_default=True,
    help="Inventory CSV.",
)
@click.option(
    "--strategy",
    type=click.Choice([item.value for item in TestStrategy], case_sensitive=False),
    help="Test handling: A=agents only, B=share, C=recreate.",
)
@click.option(
    "--stale",
    "stale_policy",
    type=click.Choice([item.value for item in StalePolicy], case_sensitive=False),
    help="Strategy C source-test policy; ignored for A/B.",
)
@click.option(
    "--timeout",
    type=click.IntRange(min=1),
    default=180,
    show_default=True,
    help="Post-token validation window.",
)
@click.option(
    "--parallel",
    "parallelism",
    type=click.IntRange(min=1, max=5),
    default=1,
    show_default=True,
    help="Parallel agent connections per batch (1-5).",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=Path("reports"),
    show_default=True,
    help="Artifact directory.",
)
@click.option("-v", "verbose", is_flag=True, help="Verbose output.")
def main(
    mode: str | Mode,
    inventory: Path,
    strategy: str | None,
    stale_policy: str | None,
    timeout: int,
    parallelism: int,
    output: Path,
    verbose: bool,
) -> None:
    """Migrate ThousandEyes Enterprise Agents between account groups."""
    selected_mode = Mode(mode)
    console = MigrationConsole(verbose=verbose)
    console.banner()
    console.info(
        "startup",
        f"mode={selected_mode.value}; initializing report output in {output}",
    )
    report = RunReport(output, selected_mode.value, {"inventory": str(inventory)})
    prompter = OperatorPrompter(report.append_decision)
    try:
        if (
            os.getenv("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
            and os.getenv("TE_ALLOW_CODEX_SANDBOX_NETWORK") != "1"
        ):
            raise ConfigurationError(
                "Codex network sandbox blocks direct agent HTTPS/443 connections. "
                "Run this command from macOS Terminal outside Codex."
            )
        console.progress("global", "inventory", f"reading {inventory}")
        agents = load_inventory(inventory)
        console.status(
            StepStatus.OK,
            "global",
            "inventory",
            f"loaded {len(agents)} agent(s)",
        )
        console.debug("inventory hosts=" + ", ".join(item.hostname for item in agents))
        strategy_value = strategy or click.prompt(
            "Test-handling strategy",
            type=click.Choice([item.value for item in TestStrategy], case_sensitive=False),
        )
        selected_strategy = TestStrategy(strategy_value.upper())
        report.record_decision("Test-handling strategy", selected_strategy.value)
        console.info("configuration", f"test strategy={selected_strategy.value}")
        selected_stale_policy = _resolve_source_test_policy(
            selected_strategy, stale_policy, report, console
        )
        console.info(
            "input",
            "resolving UI credentials and tokens; secret values are hidden and never logged",
        )
        ui_username, ui_password, target_token = _resolve_agent_inputs(report, prompter, console)
        api_token = os.getenv("TE_OAUTH_TOKEN") or os.getenv("THOUSANDEYES_OAUTH_TOKEN")
        if api_token:
            report.record_decision("API v7 OAuth bearer token", "<environment>")
            console.info("input", "API OAuth token loaded from environment")
        else:
            api_token = prompter.secret("API v7 OAuth bearer token")
            console.info("input", "API OAuth token received from hidden prompt")
        options = RunOptions(
            mode=selected_mode,
            strategy=selected_strategy,
            stale_policy=selected_stale_policy,
            timeout_seconds=timeout,
            parallelism=parallelism,
            verbose=verbose,
        )
        with ThousandEyesApiClient(api_token) as api:
            console.progress(
                "global",
                "api_account_groups",
                "connecting to ThousandEyes API v7",
            )
            groups = api.list_account_groups()
            console.status(
                StepStatus.OK,
                "global",
                "api_account_groups",
                f"OAuth token can access {len(groups)} account group(s)",
            )
            source = _group_from_env(groups, "TE_SOURCE_AID", "source", prompter)
            target = _group_from_env(groups, "TE_TARGET_AID", "target", prompter)
            if source.aid == target.aid:
                raise ConfigurationError("Source and target account groups must be different")
            console.info(
                "configuration",
                f"source={source.name} [{source.aid}]; target={target.name} [{target.aid}]",
            )
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
                f"starting batched processing for {len(agents)} agent(s); "
                f"parallel connections={parallelism}",
            )
            runner.run(agents)
    except MigrationError as exc:
        report.record_error("run", str(exc))
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", str(exc))
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        # Do not serialize an arbitrary exception message: third-party failures can
        # echo request data. Preserve the exception type for diagnostics and always
        # attempt the required final artifacts.
        detail = f"Unexpected failure ({type(exc).__name__})"
        report.record_error("run", detail)
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", detail)
        raise click.ClickException(detail) from exc
    console.info("run", "completed; final audit artifacts were written")
    click.echo(f"JSON report: {report.json_path}")
    click.echo(f"Excel report: {report.xlsx_path}")
