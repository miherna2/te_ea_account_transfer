"""Rich console output with stable Ansible-style status labels."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from te_agent_migrate.models import StepStatus

STATUS_STYLES = {
    StepStatus.OK: "bold green",
    StepStatus.CHANGED: "bold yellow",
    StepStatus.FAILED: "bold red",
    StepStatus.SKIPPED: "bold cyan",
    StepStatus.PAUSED: "bold magenta",
}


class MigrationConsole:
    def __init__(self, console: Console | None = None, *, verbose: bool = False) -> None:
        self.console = console or Console()
        self.verbose = verbose

    def banner(self) -> None:
        self.console.print("[bold]ThousandEyes Enterprise Agent Account-Group Migration[/bold]")
        self.console.print(
            "[yellow]NOTICE:[/yellow] TLS certificate verification is disabled only for "
            "agent UI connections."
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().strftime("%H:%M:%S")

    def info(self, scope: str, message: str) -> None:
        """Write an immediate timestamped operator-facing log message."""
        self.console.print(
            f"[dim]{self._timestamp()}[/dim] [bold blue]INFO[/bold blue] "
            f"{escape(f'[{scope}] {message}')}",
            soft_wrap=True,
        )

    def progress(self, agent: str, step: str, detail: str = "in progress") -> None:
        """Announce a potentially slow step before it starts."""
        self.console.print(
            f"[dim]{self._timestamp()}[/dim] [bold blue]RUNNING[/bold blue] "
            f"{escape(f'[{agent}] {step} — {detail}')}",
            soft_wrap=True,
        )

    def status(self, status: StepStatus, agent: str, step: str, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        self.console.print(
            f"[dim]{self._timestamp()}[/dim] "
            f"[{STATUS_STYLES[status]}]{status.value}[/{STATUS_STYLES[status]}] "
            f"{escape(f'[{agent}] {step}{suffix}')}"
        )

    def debug(self, message: str) -> None:
        if self.verbose:
            self.console.print(f"[dim]{self._timestamp()} DEBUG {escape(message)}[/dim]")

    def recap(self, rows: list[dict[str, str]]) -> None:
        table = Table(title="Run recap")
        table.add_column("Agent")
        table.add_column("Status")
        table.add_column("Detail")
        for row in rows:
            table.add_row(row.get("agent", ""), row.get("status", ""), row.get("detail", ""))
        self.console.print(table)
