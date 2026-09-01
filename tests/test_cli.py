import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from rich.console import Console

from te_agent_migrate.cli import (
    _resolve_agent_ui_inputs,
    _resolve_source_test_policy,
    _resolve_target_account_token,
    main,
)
from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import AccountGroup, Mode, StalePolicy
from te_agent_migrate.models import TestStrategy as Strategy
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.output import MigrationConsole
from te_agent_migrate.reporting import RunReport


def test_cli_defaults_to_dry_run_180_seconds_and_one_connection() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "Preview only, no changes (default)" in result.output
    assert "default: 180" in result.output
    assert "--parallel" in result.output
    assert "default: 1" in result.output
    assert "Strategy C source-test policy; ignored for A/B" in result.output
    assert "--create-missing-tags" in result.output
    assert "--create-missing-alerts" in result.output


def test_cli_rejects_parallelism_outside_one_to_five() -> None:
    for value in ("0", "6"):
        result = CliRunner().invoke(main, ["--parallel", value])

        assert result.exit_code != 0
        assert "1<=x<=5" in result.output


def test_agent_inputs_are_loaded_from_environment_without_secret_prompts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    username = "admin"
    password = "environment-password"
    monkeypatch.setenv("TE_AGENT_UI_USERNAME", username)
    monkeypatch.setenv("TE_AGENT_UI_PASSWORD", password)
    report = RunReport(tmp_path, "dry-run")
    prompter = OperatorPrompter(report.append_decision)
    monkeypatch.setattr(
        "te_agent_migrate.cli.click.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    monkeypatch.setattr(
        prompter,
        "secret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    resolved = _resolve_agent_ui_inputs(report, prompter, MigrationConsole())

    assert resolved == (username, password)
    serialized_report = json.dumps(report.to_dict())
    assert password not in serialized_report
    assert "TE_AGENT_UI_PASSWORD" in serialized_report


def test_missing_agent_input_environment_variables_fall_back_to_prompts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    for name in (
        "TE_AGENT_UI_USERNAME",
        "TE_AGENT_UI_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    report = RunReport(tmp_path, "dry-run")
    prompter = OperatorPrompter(report.append_decision)
    prompt_labels: list[str] = []
    secret_labels: list[str] = []
    secrets = iter(["prompt-password"])

    def prompt(label: str, **_kwargs: Any) -> str:
        prompt_labels.append(label)
        return "admin"

    def secret(label: str) -> str:
        secret_labels.append(label)
        return next(secrets)

    monkeypatch.setattr("te_agent_migrate.cli.click.prompt", prompt)
    monkeypatch.setattr(prompter, "secret", secret)

    resolved = _resolve_agent_ui_inputs(report, prompter, MigrationConsole())

    assert resolved == ("admin", "prompt-password")
    assert prompt_labels == ["Agent UI username"]
    assert secret_labels == ["Agent UI password"]


@pytest.mark.parametrize(
    "empty_variable",
    ["TE_AGENT_UI_USERNAME", "TE_AGENT_UI_PASSWORD"],
)
def test_empty_agent_input_environment_variable_is_rejected(
    monkeypatch: Any, tmp_path: Path, empty_variable: str
) -> None:
    monkeypatch.setenv("TE_AGENT_UI_USERNAME", "admin")
    monkeypatch.setenv("TE_AGENT_UI_PASSWORD", "password")
    monkeypatch.setenv(empty_variable, "")
    report = RunReport(tmp_path, "dry-run")
    prompter = OperatorPrompter(report.append_decision)

    with pytest.raises(ConfigurationError, match=f"{empty_variable} is set but empty"):
        _resolve_agent_ui_inputs(report, prompter, MigrationConsole())


def test_target_token_is_bound_to_selected_target_aid(
    monkeypatch: Any, tmp_path: Path
) -> None:
    token = "A" * 32
    target = AccountGroup("144165", "AGENTS_SRC")
    monkeypatch.setenv("TE_TARGET_ACCOUNT_TOKEN", token)
    monkeypatch.setenv("TE_TARGET_ACCOUNT_TOKEN_AID", target.aid)
    report = RunReport(tmp_path, "apply")
    prompter = OperatorPrompter(report.append_decision)
    monkeypatch.setattr(
        prompter,
        "secret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    resolved = _resolve_target_account_token(target, report, prompter, MigrationConsole())

    assert resolved == token
    serialized_report = json.dumps(report.to_dict())
    assert token not in serialized_report
    assert "TE_TARGET_ACCOUNT_TOKEN_AID" in serialized_report
    assert target.aid in serialized_report


def test_target_token_aid_mismatch_stops_before_agent_reset(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("TE_TARGET_ACCOUNT_TOKEN", "A" * 32)
    monkeypatch.setenv("TE_TARGET_ACCOUNT_TOKEN_AID", "2136224")
    target = AccountGroup("144165", "AGENTS_SRC")
    report = RunReport(tmp_path, "apply")
    prompter = OperatorPrompter(report.append_decision)

    with pytest.raises(ConfigurationError, match="does not match selected target"):
        _resolve_target_account_token(target, report, prompter, MigrationConsole())


def test_environment_target_token_requires_aid_binding(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("TE_TARGET_ACCOUNT_TOKEN", "A" * 32)
    monkeypatch.delenv("TE_TARGET_ACCOUNT_TOKEN_AID", raising=False)
    target = AccountGroup("144165", "AGENTS_SRC")
    report = RunReport(tmp_path, "apply")
    prompter = OperatorPrompter(report.append_decision)

    with pytest.raises(ConfigurationError, match="TE_TARGET_ACCOUNT_TOKEN_AID must also be set"):
        _resolve_target_account_token(target, report, prompter, MigrationConsole())


def test_prompted_target_token_names_selected_destination(
    monkeypatch: Any, tmp_path: Path
) -> None:
    token = "B" * 32
    target = AccountGroup("144165", "AGENTS_SRC")
    monkeypatch.delenv("TE_TARGET_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("TE_TARGET_ACCOUNT_TOKEN_AID", raising=False)
    report = RunReport(tmp_path, "apply")
    prompter = OperatorPrompter(report.append_decision)
    labels: list[str] = []

    def secret(label: str) -> str:
        labels.append(label)
        return token

    monkeypatch.setattr(prompter, "secret", secret)

    resolved = _resolve_target_account_token(target, report, prompter, MigrationConsole())

    assert resolved == token
    assert labels == ["Target account-group token for AGENTS_SRC [144165]"]


def test_destination_banner_emphasizes_exact_target_account_group() -> None:
    output = StringIO()
    console = MigrationConsole(
        Console(file=output, force_terminal=False, color_system=None, width=100)
    )

    console.destination_banner(
        AccountGroup("2136224", "AGENTS_DST"),
        AccountGroup("144165", "AGENTS_SRC"),
        Mode.APPLY,
        destructive=True,
    )

    rendered = output.getvalue()
    assert "DESTRUCTIVE MIGRATION DESTINATION" in rendered
    assert "AGENTS_SRC [144165]" in rendered
    assert "Source: AGENTS_DST [2136224]" in rendered
    assert "Every selected device will register in this account group" in rendered


def test_stale_policy_is_not_prompted_and_is_ignored_for_strategy_a(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "te_agent_migrate.cli.click.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    report = RunReport(tmp_path, "dry-run")

    policy = _resolve_source_test_policy(
        Strategy.AGENTS_ONLY,
        "remove",
        report,
        MigrationConsole(),
    )

    assert policy is StalePolicy.KEEP
    assert "not applicable to Strategy A" in report.decisions[-1]["response"]


def test_stale_policy_is_prompted_only_for_strategy_c(monkeypatch: Any, tmp_path: Path) -> None:
    prompts: list[str] = []

    def prompt(label: str, **_kwargs: Any) -> str:
        prompts.append(label)
        return "remove"

    monkeypatch.setattr("te_agent_migrate.cli.click.prompt", prompt)
    report = RunReport(tmp_path, "dry-run")

    policy = _resolve_source_test_policy(
        Strategy.RECREATE,
        None,
        report,
        MigrationConsole(),
    )

    assert policy is StalePolicy.REMOVE
    assert prompts == ["Strategy C source-test policy"]


def test_cli_reports_inventory_error_without_prompting(monkeypatch: Any) -> None:
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            ["--inventory", "missing.csv", "--strategy", "A", "--stale", "keep"],
        )

    assert result.exit_code != 0
    assert "Inventory file does not exist" in result.output


def test_cli_rejects_codex_network_sandbox_before_prompting(monkeypatch: Any) -> None:
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.delenv("TE_ALLOW_CODEX_SANDBOX_NETWORK", raising=False)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            ["--inventory", "inventory.csv", "--strategy", "A", "--stale", "keep"],
        )

    assert result.exit_code != 0
    assert "blocks direct agent HTTPS/443 connections" in result.output
    assert "Agent UI username" not in result.output


def test_cli_finalizes_reports_for_unexpected_exception(monkeypatch: Any) -> None:
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)

    def fail_unexpectedly(_path: object) -> object:
        raise ValueError("request body must not be serialized")

    monkeypatch.setattr("te_agent_migrate.cli.load_inventory", fail_unexpectedly)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            ["--inventory", "inventory.csv", "--strategy", "A", "--stale", "keep"],
        )

        report_paths = list(Path("reports").glob("*.json"))
        workbook_paths = list(Path("reports").glob("*.xlsx"))
        payload = json.loads(report_paths[0].read_text())

    assert result.exit_code != 0
    assert "Unexpected failure (ValueError)" in result.output
    assert "request body must not be serialized" not in result.output
    assert len(report_paths) == 1
    assert len(workbook_paths) == 1
    assert payload["status"] == "final"
    assert payload["errors"][0]["cause"] == "Unexpected failure (ValueError)"
