import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from te_agent_migrate.cli import _resolve_agent_inputs, _resolve_source_test_policy, main
from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import StalePolicy
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
    target_token = "A" * 32
    monkeypatch.setenv("TE_AGENT_UI_USERNAME", username)
    monkeypatch.setenv("TE_AGENT_UI_PASSWORD", password)
    monkeypatch.setenv("TE_TARGET_ACCOUNT_TOKEN", target_token)
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

    resolved = _resolve_agent_inputs(report, prompter, MigrationConsole())

    assert resolved == (username, password, target_token)
    serialized_report = json.dumps(report.to_dict())
    assert password not in serialized_report
    assert target_token not in serialized_report
    assert "TE_AGENT_UI_PASSWORD" in serialized_report
    assert "TE_TARGET_ACCOUNT_TOKEN" in serialized_report


def test_missing_agent_input_environment_variables_fall_back_to_prompts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    for name in (
        "TE_AGENT_UI_USERNAME",
        "TE_AGENT_UI_PASSWORD",
        "TE_TARGET_ACCOUNT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    report = RunReport(tmp_path, "dry-run")
    prompter = OperatorPrompter(report.append_decision)
    prompt_labels: list[str] = []
    secret_labels: list[str] = []
    secrets = iter(["prompt-password", "B" * 32])

    def prompt(label: str, **_kwargs: Any) -> str:
        prompt_labels.append(label)
        return "admin"

    def secret(label: str) -> str:
        secret_labels.append(label)
        return next(secrets)

    monkeypatch.setattr("te_agent_migrate.cli.click.prompt", prompt)
    monkeypatch.setattr(prompter, "secret", secret)

    resolved = _resolve_agent_inputs(report, prompter, MigrationConsole())

    assert resolved == ("admin", "prompt-password", "B" * 32)
    assert prompt_labels == ["Agent UI username"]
    assert secret_labels == ["Agent UI password", "Target account-group token"]


@pytest.mark.parametrize(
    "empty_variable",
    ["TE_AGENT_UI_USERNAME", "TE_AGENT_UI_PASSWORD", "TE_TARGET_ACCOUNT_TOKEN"],
)
def test_empty_agent_input_environment_variable_is_rejected(
    monkeypatch: Any, tmp_path: Path, empty_variable: str
) -> None:
    monkeypatch.setenv("TE_AGENT_UI_USERNAME", "admin")
    monkeypatch.setenv("TE_AGENT_UI_PASSWORD", "password")
    monkeypatch.setenv("TE_TARGET_ACCOUNT_TOKEN", "A" * 32)
    monkeypatch.setenv(empty_variable, "")
    report = RunReport(tmp_path, "dry-run")
    prompter = OperatorPrompter(report.append_decision)

    with pytest.raises(ConfigurationError, match=f"{empty_variable} is set but empty"):
        _resolve_agent_inputs(report, prompter, MigrationConsole())


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
