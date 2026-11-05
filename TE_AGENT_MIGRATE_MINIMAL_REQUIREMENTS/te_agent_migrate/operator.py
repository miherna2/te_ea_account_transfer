"""Dependency-free operator prompts with mandatory explicit answers."""

import getpass
import sys
from datetime import datetime, timezone

from te_agent_migrate.errors import ConfigurationError


class OperatorPrompter(object):
    def __init__(self, record_decision, input_function=input, secret_function=None):
        self._record = record_decision
        self._input = input_function
        self._secret = secret_function or getpass.getpass

    def _audit(self, prompt, response, agent=None):
        self._record(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": agent,
                "prompt": prompt,
                "response": response,
            }
        )

    def record(self, prompt, response, agent=None):
        self._audit(prompt, response, agent=agent)

    def text(self, prompt):
        while True:
            value = self._input("%s: " % prompt).strip()
            if value:
                self._audit(prompt, value)
                return value
            print("Error: %s cannot be empty; type a value." % prompt, file=sys.stderr)

    def secret(self, prompt):
        while True:
            value = self._secret("%s: " % prompt)
            if value:
                self._audit(prompt, "<redacted>")
                return value
            print("Error: %s cannot be empty; type a value." % prompt, file=sys.stderr)

    def required_choice(self, prompt, choices):
        normalized = [str(choice).lower() for choice in choices]
        suffix = ", ".join(normalized)
        while True:
            answer = self._input("%s (%s): " % (prompt, suffix)).strip().lower()
            if answer in normalized:
                return answer
            if not answer:
                print("Error: Enter alone is not accepted; type one option.", file=sys.stderr)
            else:
                print("Error: choose one of: %s." % suffix, file=sys.stderr)

    def choose_account_group(self, prompt, groups):
        if not groups:
            raise ConfigurationError("The OAuth token cannot access any account groups")
        print(prompt)
        for index, group in enumerate(groups, start=1):
            current = " (current)" if group.is_current else ""
            print("  %s. %s [%s]%s" % (index, group.name, group.aid, current))
        while True:
            value = self._input("Selection (1-%s): " % len(groups)).strip()
            if not value:
                print("Error: Enter alone is not accepted; type a selection.", file=sys.stderr)
                continue
            try:
                selected = int(value)
            except ValueError:
                selected = 0
            if 1 <= selected <= len(groups):
                group = groups[selected - 1]
                self._audit(prompt, "%s [%s]" % (group.name, group.aid))
                return group
            print("Error: selection must be between 1 and %s." % len(groups), file=sys.stderr)

    def confirm_destructive(self):
        prompt = "Begin destructive migration actions"
        answer = self.required_choice("%s?" % prompt, ("y", "n")) == "y"
        self._audit(prompt, "yes" if answer else "no")
        return answer

    def continue_after_batch(self, batch_number, agents):
        agent_summary = ", ".join(agents)
        prompt = "Continue after batch %s (%s)" % (batch_number, agent_summary)
        answer = self.required_choice("%s?" % prompt, ("y", "n")) == "y"
        self._audit(prompt, "yes" if answer else "no")
        return answer

    def failure_decision(self, agent, detail, allow_recheck=True):
        choices = ("recheck", "skip", "abort") if allow_recheck else ("skip", "abort")
        action_text = "read-only recheck, skip, or abort" if allow_recheck else "skip or abort"
        prompt = "Failure on %s: %s. Choose %s" % (agent, detail, action_text)
        answer = self.required_choice(prompt, choices)
        self._audit(prompt, answer, agent=agent)
        return answer
