"""Operator prompts whose non-secret decisions are audit logged."""

from __future__ import annotations

import getpass
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import click

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import AccountGroup


class OperatorPrompter:
    def __init__(self, record_decision: Callable[[dict[str, Any]], None]) -> None:
        self._record = record_decision

    def _audit(self, prompt: str, response: str, *, agent: str | None = None) -> None:
        self._record(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "agent": agent,
                "prompt": prompt,
                "response": response,
            }
        )

    def record(self, prompt: str, response: str, *, agent: str | None = None) -> None:
        self._audit(prompt, response, agent=agent)

    def secret(self, prompt: str) -> str:
        value = getpass.getpass(f"{prompt}: ")
        self._audit(prompt, "<redacted>")
        if not value:
            raise ConfigurationError(f"{prompt} cannot be empty")
        return value

    def choose_account_group(self, prompt: str, groups: list[AccountGroup]) -> AccountGroup:
        if not groups:
            raise ConfigurationError("The OAuth token cannot access any account groups")
        click.echo(prompt)
        for index, group in enumerate(groups, start=1):
            current = " (current)" if group.is_current else ""
            click.echo(f"  {index}. {group.name} [{group.aid}]{current}")
        selected = int(click.prompt("Selection", type=click.IntRange(1, len(groups))))
        group = groups[selected - 1]
        self._audit(prompt, f"{group.name} [{group.aid}]")
        return group

    def confirm_destructive(self) -> bool:
        prompt = "Begin destructive migration actions"
        answer = click.confirm(f"{prompt}?", default=False)
        self._audit(prompt, "yes" if answer else "no")
        return answer

    def continue_after_batch(self, batch_number: int, agents: list[str]) -> bool:
        agent_summary = ", ".join(agents)
        prompt = f"Continue after batch {batch_number} ({agent_summary})"
        answer = click.confirm(f"{prompt}?", default=False)
        self._audit(prompt, "yes" if answer else "no")
        return answer

    def failure_decision(self, agent: str, detail: str, *, allow_recheck: bool = True) -> str:
        choices = ["recheck", "skip", "abort"] if allow_recheck else ["skip", "abort"]
        action_text = "read-only recheck, skip, or abort" if allow_recheck else "skip or abort"
        prompt = f"Failure on {agent}: {detail}. Choose {action_text}"
        answer = str(
            click.prompt(
                prompt,
                type=click.Choice(choices, case_sensitive=False),
                default="abort",
                show_choices=True,
            )
        ).lower()
        self._audit(prompt, answer, agent=agent)
        return answer
