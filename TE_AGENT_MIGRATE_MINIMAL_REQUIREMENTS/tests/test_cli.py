import os
import unittest
from unittest import mock

from te_agent_migrate.cli import _resolve_target_account_token
from te_agent_migrate.errors import ConfigurationError


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


if __name__ == "__main__":
    unittest.main()
