from io import StringIO

from rich.console import Console

from te_agent_migrate.models import StepStatus
from te_agent_migrate.output import MigrationConsole


def test_console_announces_long_running_steps_and_completion() -> None:
    stream = StringIO()
    output = MigrationConsole(
        Console(file=stream, force_terminal=False, color_system=None),
        verbose=True,
    )

    output.info("startup", "initializing")
    output.progress("agent-a", "ui_authentication", "connecting")
    output.status(StepStatus.OK, "agent-a", "ui_authentication", "authenticated")
    output.debug("inventory hosts=agent-a")

    rendered = stream.getvalue()
    assert "INFO [startup] initializing" in rendered
    assert "RUNNING [agent-a] ui_authentication — connecting" in rendered
    assert "ok [agent-a] ui_authentication — authenticated" in rendered
    assert "DEBUG inventory hosts=agent-a" in rendered
