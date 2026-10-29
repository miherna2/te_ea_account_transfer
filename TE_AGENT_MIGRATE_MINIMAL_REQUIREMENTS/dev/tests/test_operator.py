import unittest

from te_agent_migrate.models import AccountGroup
from te_agent_migrate.operator import OperatorPrompter


class OperatorTests(unittest.TestCase):
    def test_blank_is_not_a_default_failure_action(self):
        answers = iter(("", "recheck"))
        audit = []
        prompter = OperatorPrompter(audit.append, input_function=lambda unused: next(answers))
        self.assertEqual(prompter.failure_decision("TE1", "offline"), "recheck")
        self.assertEqual(audit[-1]["response"], "recheck")

    def test_blank_is_not_a_default_confirmation(self):
        answers = iter(("", "n"))
        prompter = OperatorPrompter(lambda unused: None, input_function=lambda unused: next(answers))
        self.assertFalse(prompter.confirm_destructive())

    def test_blank_account_selection_is_rejected(self):
        answers = iter(("", "1"))
        prompter = OperatorPrompter(lambda unused: None, input_function=lambda unused: next(answers))
        selected = prompter.choose_account_group(
            "Select target", [AccountGroup("123", "Target")]
        )
        self.assertEqual(selected.aid, "123")


if __name__ == "__main__":
    unittest.main()
