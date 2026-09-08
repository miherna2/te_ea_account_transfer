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
        while True:
            value = getpass.getpass(f"{prompt}: ")
            if value:
                self._audit(prompt, "<redacted>")
                return value
            click.echo(f"Error: {prompt} cannot be empty; type a value.", err=True)

    @staticmethod
    def _required_choice(prompt: str, choices: list[str]) -> str:
        return str(
            click.prompt(
                prompt,
                type=click.Choice(choices, case_sensitive=False),
                show_choices=True,
            )
        ).lower()

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
        answer = self._required_choice(f"{prompt}?", ["y", "n"]) == "y"
        self._audit(prompt, "yes" if answer else "no")
        return answer

    def continue_after_batch(self, batch_number: int, agents: list[str]) -> bool:
        agent_summary = ", ".join(agents)
        prompt = f"Continue after batch {batch_number} ({agent_summary})"
        answer = self._required_choice(f"{prompt}?", ["y", "n"]) == "y"
        self._audit(prompt, "yes" if answer else "no")
        return answer

    def failure_decision(self, agent: str, detail: str, *, allow_recheck: bool = True) -> str:
        choices = ["recheck", "skip", "abort"] if allow_recheck else ["skip", "abort"]
        action_text = "read-only recheck, skip, or abort" if allow_recheck else "skip or abort"
        prompt = f"Failure on {agent}: {detail}. Choose {action_text}"
        answer = self._required_choice(prompt, choices)
        self._audit(prompt, answer, agent=agent)
        return answer
