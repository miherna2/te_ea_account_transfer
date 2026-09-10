"""Portable console logging with no Rich dependency."""

import os
import sys
from datetime import datetime

from te_agent_migrate.models import StepStatus


STATUS_COLORS = {
    StepStatus.OK: "\033[1;32m",
    StepStatus.CHANGED: "\033[1;33m",
    StepStatus.FAILED: "\033[1;31m",
    StepStatus.SKIPPED: "\033[1;36m",
    StepStatus.PAUSED: "\033[1;35m",
}


class MigrationConsole(object):
    def __init__(self, verbose=False, stream=None, use_color=None):
        self.verbose = verbose
        self.stream = stream or sys.stdout
        if use_color is None:
            use_color = bool(getattr(self.stream, "isatty", lambda: False)()) and not os.getenv("NO_COLOR")
        self.use_color = use_color

    @staticmethod
    def _timestamp():
        return datetime.now().astimezone().strftime("%H:%M:%S")

    def _paint(self, text, color):
        return "%s%s\033[0m" % (color, text) if self.use_color else text

    def _write(self, message):
        print(message, file=self.stream, flush=True)

    def banner(self):
        self._write("=" * 72)
        self._write(" ThousandEyes Enterprise Agent Account-Group Migration")
        self._write("=" * 72)
        self._write("NOTICE: TLS verification is disabled only for agent UI connections.")

    def destination_banner(self, source, target, mode, destructive=False):
        title = "DESTRUCTIVE MIGRATION DESTINATION" if destructive else "MIGRATION DESTINATION"
        self._write("\n" + "!" * 72)
        self._write(" %s" % title)
        self._write(" TARGET ACCOUNT GROUP: %s [%s]" % (target.name, target.aid))
        self._write(" SOURCE ACCOUNT GROUP: %s [%s]" % (source.name, source.aid))
        self._write(" MODE: %s" % mode.value.upper())
        self._write(" Every selected device will register in the TARGET above.")
        self._write("!" * 72 + "\n")

    def info(self, scope, message):
        label = self._paint("INFO", "\033[1;34m")
        self._write("%s %s [%s] %s" % (self._timestamp(), label, scope, message))

    def progress(self, agent, step, detail="in progress"):
        label = self._paint("RUNNING", "\033[1;34m")
        self._write("%s %s [%s] %s - %s" % (self._timestamp(), label, agent, step, detail))

    def status(self, status, agent, step, detail=""):
        label = self._paint(status.value, STATUS_COLORS.get(status, ""))
        suffix = " - %s" % detail if detail else ""
        self._write("%s %s [%s] %s%s" % (self._timestamp(), label, agent, step, suffix))

    def debug(self, message):
        if self.verbose:
            self._write("%s DEBUG %s" % (self._timestamp(), message))

    def recap(self, rows):
        self._write("\nRun recap")
        self._write("-" * 72)
        for row in rows:
            self._write(
                "%s | %s | %s"
                % (row.get("agent", ""), row.get("status", ""), row.get("detail", ""))
            )
