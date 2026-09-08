from __future__ import annotations

from typing import Any

import click
from click.testing import CliRunner

from te_agent_migrate.models import AccountGroup
from te_agent_migrate.operator import OperatorPrompter


def _prompter(decisions: list[dict[str, Any]]) -> OperatorPrompter:
    return OperatorPrompter(decisions.append)


def test_destructive_confirmation_reprompts_after_empty_enter() -> None:
    decisions: list[dict[str, Any]] = []
    prompter = _prompter(decisions)

    @click.command()
    def command() -> None:
        click.echo(f"answer={prompter.confirm_destructive()}")

    result = CliRunner().invoke(command, input="\ny\n")

    assert result.exit_code == 0
    assert result.output.count("Begin destructive migration actions?") == 2
    assert "[y/N]" not in result.output
    assert "answer=True" in result.output
    assert decisions[-1]["response"] == "yes"


def test_batch_confirmation_reprompts_after_empty_enter() -> None:
    decisions: list[dict[str, Any]] = []
    prompter = _prompter(decisions)

    @click.command()
    def command() -> None:
        click.echo(f"answer={prompter.continue_after_batch(1, ['TE1', 'TE2'])}")

    result = CliRunner().invoke(command, input="\nn\n")

    assert result.exit_code == 0
    assert result.output.count("Continue after batch 1 (TE1, TE2)?") == 2
    assert "[y/N]" not in result.output
    assert "answer=False" in result.output
    assert decisions[-1]["response"] == "no"


def test_failure_decision_reprompts_after_empty_enter() -> None:
    decisions: list[dict[str, Any]] = []
    prompter = _prompter(decisions)

    @click.command()
    def command() -> None:
        click.echo(f"answer={prompter.failure_decision('TE5', 'host is down')}")

    result = CliRunner().invoke(command, input="\nrecheck\n")

    assert result.exit_code == 0
    assert result.output.count("Failure on TE5:") == 2
    assert "[abort]" not in result.output
    assert "answer=recheck" in result.output
    assert decisions[-1]["response"] == "recheck"


def test_failure_decision_rejects_unavailable_recheck_choice() -> None:
    decisions: list[dict[str, Any]] = []
    prompter = _prompter(decisions)

    @click.command()
    def command() -> None:
        click.echo(
            f"answer={prompter.failure_decision('TE5', 'unsafe retry', allow_recheck=False)}"
        )

    result = CliRunner().invoke(command, input="recheck\nskip\n")

    assert result.exit_code == 0
    assert result.output.count("Failure on TE5:") == 2
    assert "answer=skip" in result.output
    assert decisions[-1]["response"] == "skip"


def test_account_group_selection_reprompts_after_empty_enter() -> None:
    decisions: list[dict[str, Any]] = []
    prompter = _prompter(decisions)
    groups = [AccountGroup("144165", "AGENTS_SRC")]

    @click.command()
    def command() -> None:
        selected = prompter.choose_account_group("Select source account group", groups)
        click.echo(f"selected={selected.aid}")

    result = CliRunner().invoke(command, input="\n1\n")

    assert result.exit_code == 0
    assert result.output.count("Selection:") == 2
    assert "selected=144165" in result.output


def test_secret_reprompts_after_empty_enter(monkeypatch: Any) -> None:
    decisions: list[dict[str, Any]] = []
    prompter = _prompter(decisions)
    responses = iter(["", "secret-value"])
    prompts: list[str] = []

    def getpass(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("te_agent_migrate.operator.getpass.getpass", getpass)

    assert prompter.secret("Agent UI password") == "secret-value"
    assert prompts == ["Agent UI password: ", "Agent UI password: "]
    assert [item["response"] for item in decisions] == ["<redacted>"]
