"""Registration tokens of the target account groups (one token per target group).

Order of the sources for one target aid:

1. The environment variable ``TE_TARGET_TOKEN_<aid>``, for example ``TE_TARGET_TOKEN_100``.
2. The single-target pair ``TE_TARGET_ACCOUNT_TOKEN`` plus ``TE_TARGET_ACCOUNT_TOKEN_AID``,
   when the aid of the pair is this aid.
3. A hidden prompt.

A token is cleaned (white space and invisible characters removed) and must have exactly
32 letters or digits. A token is never printed, never logged and never written to a file.
Messages name the source and the length only. Each aid is resolved once per run.
"""
import os
import re
import unicodedata

from te_agent_migrate.errors import ConfigurationError

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{32}$")
TOKEN_ENV_PREFIX = "TE_TARGET_TOKEN_"
LEGACY_TOKEN_ENV = "TE_TARGET_ACCOUNT_TOKEN"
LEGACY_AID_ENV = "TE_TARGET_ACCOUNT_TOKEN_AID"


def normalize_token(value):
    """Remove copy/paste whitespace and invisible format characters."""
    return "".join(
        character
        for character in value
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )


def clean_token(value, console):
    """Return the cleaned token or raise ConfigurationError. The message never holds the token."""
    original_length = len(value)
    token = normalize_token(value)
    removed_count = original_length - len(token)
    if removed_count:
        console.info(
            "input",
            "Ignored %s whitespace or invisible copy/paste character(s) in the "
            "target account-group token" % removed_count,
        )
    if not TOKEN_PATTERN.fullmatch(token):
        raise ConfigurationError(
            "Target account-group token must be exactly 32 alphanumeric characters "
            "after removing whitespace and invisible copy/paste characters; received "
            "%s character(s)" % len(token)
        )
    return token


def token_env_name(aid):
    """Name of the environment variable for one target aid. The aid must be digits."""
    aid = str(aid)
    if not aid.isdigit():
        raise ConfigurationError("Target account group id must be digits only: %s" % aid)
    return TOKEN_ENV_PREFIX + aid


def _environment_value(name):
    value = os.environ.get(name)
    if value is None:
        return None
    if not value:
        raise ConfigurationError("%s is set but empty" % name)
    return value


class TargetTokens(object):
    """Resolve and cache the registration token of each target group.

    report     object with ``record_decision(prompt, response)``
    prompter   object with ``secret(prompt)``
    console    object with ``info(scope, message)``
    labels     optional {aid: 'Name [aid]'} for prompts and messages
    """

    def __init__(self, report, prompter, console, labels=None):
        self.report = report
        self.prompter = prompter
        self.console = console
        self.labels = dict(labels or {})
        self._cache = {}

    def label(self, aid):
        return self.labels.get(str(aid)) or str(aid)

    def _from_environment(self, aid, label):
        name = token_env_name(aid)
        value = _environment_value(name)
        if value is None:
            return None
        self.report.record_decision("Target account-group token for %s" % label, "<environment:%s>" % name)
        self.console.info("input", "Target token for %s loaded from %s" % (label, name))
        return value

    def _from_legacy_pair(self, aid, label):
        value = _environment_value(LEGACY_TOKEN_ENV)
        if value is None:
            return None
        bound_aid = _environment_value(LEGACY_AID_ENV)
        if bound_aid is None:
            raise ConfigurationError(
                "%s must also be set when %s comes from the environment. No agents were reset."
                % (LEGACY_AID_ENV, LEGACY_TOKEN_ENV)
            )
        if bound_aid != str(aid):
            return None  # the pair belongs to another target group
        self.report.record_decision(
            "Target account-group token for %s" % label, "<environment:%s>" % LEGACY_TOKEN_ENV
        )
        self.console.info(
            "input",
            "Target token for %s loaded from %s and bound by %s" % (label, LEGACY_TOKEN_ENV, LEGACY_AID_ENV),
        )
        return value

    def get(self, aid):
        """Return the cleaned token of the target group ``aid``. Asks at most once per aid."""
        aid = str(aid)
        if aid in self._cache:
            return self._cache[aid]
        label = self.label(aid)
        value = self._from_environment(aid, label)
        if value is None:
            value = self._from_legacy_pair(aid, label)
        if value is None:
            value = self.prompter.secret("Target account-group token for %s" % label)
            self.report.record_decision("Target account-group token for %s" % label, "hidden prompt")
        token = clean_token(value, self.console)
        self._cache[aid] = token
        return token

    def collect(self, aids):
        """Resolve every aid now. Returns {aid: token}. Call it before the first device is changed."""
        return dict((str(aid), self.get(aid)) for aid in aids)
