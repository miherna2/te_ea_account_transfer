"""Domain-specific failures with explicit safety semantics."""


class MigrationError(RuntimeError):
    """Base migration failure."""


class ConfigurationError(MigrationError):
    """Invalid or incomplete operator input."""


class AuthenticationError(MigrationError):
    """Authentication or CSRF bootstrap failed."""


class ApiError(MigrationError):
    """ThousandEyes API v7 request failed."""


class UiError(MigrationError):
    """Enterprise Agent HTTPS UI request failed."""


class VisibilityTimeout(MigrationError):
    """Destination identity was not confirmed within the operator-selected window."""


class HumanDecisionRequired(MigrationError):
    """Execution must pause because automated remediation is forbidden."""


class HardStop(MigrationError):
    """Abort the entire run before touching another agent."""
