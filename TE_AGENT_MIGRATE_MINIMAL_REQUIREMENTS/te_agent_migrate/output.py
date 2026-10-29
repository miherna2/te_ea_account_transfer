"""Portable Ansible-style console output with no Rich dependency."""

import os
import sys
import threading

from te_agent_migrate.models import StepStatus
from te_agent_migrate.redaction import redact_value


LINE_WIDTH = 72
LOCALHOST = "localhost"
RUN_LEVEL_HOSTS = ("global", "", None)
LABEL_WIDTH = 10

STATUS_COLORS = {
    StepStatus.OK: "\033[1;32m",
    StepStatus.CHANGED: "\033[1;33m",
    StepStatus.FAILED: "\033[1;31m",
    StepStatus.SKIPPED: "\033[1;36m",
    StepStatus.PAUSED: "\033[1;35m",
}
RECAP_COLORS = {
    "ok": STATUS_COLORS[StepStatus.OK],
    "changed": STATUS_COLORS[StepStatus.CHANGED],
    "skipped": STATUS_COLORS[StepStatus.SKIPPED],
    "failed": STATUS_COLORS[StepStatus.FAILED],
    "paused": STATUS_COLORS[StepStatus.PAUSED],
}
COUNTER_NAMES = ("ok", "changed", "skipped", "failed", "paused")
RESET = "\033[0m"


def mask_secrets(text):
    """Mask Bearer tokens and 32-character account-group tokens in free text."""
    if text is None:
        return ""
    return redact_value(str(text))


def _stars(prefix, width=LINE_WIDTH):
    prefix = prefix + " "
    return prefix + "*" * max(width - len(prefix), 3)


class MigrationConsole(object):
    def __init__(self, verbose=False, stream=None, use_color=None):
        self.verbose = verbose
        self.stream = stream or sys.stdout
        if use_color is None:
            use_color = bool(getattr(self.stream, "isatty", lambda: False)()) and not os.getenv("NO_COLOR")
        self.use_color = use_color
        self._lock = threading.RLock()
        self._last_task = None
        self._counters = {}

    # ------------------------------------------------------------------ helpers
    def _paint(self, text, color):
        return "%s%s%s" % (color, text, RESET) if self.use_color and color else text

    def _write(self, message):
        with self._lock:
            print(message, file=self.stream, flush=True)

    @staticmethod
    def _host(agent):
        if agent is None or agent in RUN_LEVEL_HOSTS:
            return LOCALHOST
        return str(agent)

    @staticmethod
    def _clean(text):
        return " ".join(mask_secrets(text).split())

    @staticmethod
    def _status_key(status):
        value = getattr(status, "value", status)
        return str(value).lower()

    def _bump(self, host, key):
        counters = self._counters.setdefault(host, dict((name, 0) for name in COUNTER_NAMES))
        if key in counters:
            counters[key] += 1

    # ------------------------------------------------------------------ banners
    def banner(self):
        self._write("=" * 72)
        self._write(" ThousandEyes Enterprise Agent Account-Group Migration")
        self._write("=" * 72)
        self._write("NOTICE: TLS verification is disabled only for agent UI connections.")

    def destination_banner(self, source, target, mode, destructive=False):
        title = "DESTRUCTIVE MIGRATION DESTINATION" if destructive else "MIGRATION DESTINATION"
        mode_text = str(getattr(mode, "value", mode)).upper()
        with self._lock:
            self._write("\n" + "!" * 72)
            self._write(" %s" % title)
            self._write(" TARGET ACCOUNT GROUP: %s [%s]" % (target.name, target.aid))
            self._write(" SOURCE ACCOUNT GROUP: %s [%s]" % (source.name, source.aid))
            self._write(" MODE: %s" % mode_text)
            self._write(" Every selected device will register in the TARGET above.")
            self._write("!" * 72 + "\n")

    def targets_banner(self, source, targets, mode, destructive=False):
        """Banner for a run with several target groups. ``targets`` is a list of (label, device count)."""
        title = "DESTRUCTIVE MIGRATION DESTINATIONS" if destructive else "MIGRATION DESTINATIONS"
        mode_text = str(getattr(mode, "value", mode)).upper()
        with self._lock:
            self._write("\n" + "!" * 72)
            self._write(" %s" % title)
            self._write(" SOURCE ACCOUNT GROUP: %s [%s]" % (source.name, source.aid))
            self._write(" MODE: %s" % mode_text)
            for label, count in targets:
                self._write(" TARGET ACCOUNT GROUP: %s (%d device(s) in the inventory)" % (label, count))
            self._write(" Each device registers in the TARGET named in its inventory row.")
            self._write("!" * 72 + "\n")

    # ------------------------------------------------------------------ play / task
    def play(self, title, mode, strategy):
        mode_text = str(getattr(mode, "value", mode)).upper()
        strategy_text = str(getattr(strategy, "value", strategy))
        with self._lock:
            self._write("\n" + _stars("PLAY [%s]" % self._clean(title)))
            self._write("MODE: %s   STRATEGY: %s" % (mode_text, strategy_text))
            self._last_task = None

    def task(self, name):
        name = self._clean(name)
        with self._lock:
            if name == self._last_task:
                return
            self._last_task = name
            self._write("\n" + _stars("TASK [%s]" % name))

    # ------------------------------------------------------------------ messages
    def info(self, scope, message):
        self._write("  [info] %s: %s" % (scope, self._clean(message)))

    def progress(self, agent, step, detail="in progress"):
        if not self.verbose:
            return
        self._write("  .. [%s] %s - %s" % (self._host(agent), step, self._clean(detail)))

    def status(self, status, agent, step, detail=""):
        key = self._status_key(status)
        host = self._host(agent)
        label = ("%s:" % key).ljust(LABEL_WIDTH)
        text = self._clean(detail)
        with self._lock:
            if step:
                self.task(step)
            self._bump(host, key)
            painted = self._paint(label, STATUS_COLORS.get(status, ""))
            if text:
                self._write("%s[%s] => %s" % (painted, host, text))
            else:
                self._write("%s[%s]" % (painted, host))

    def debug(self, message):
        if self.verbose:
            self._write("  [debug] %s" % self._clean(message))

    # ------------------------------------------------------------------ recap
    def has_failures(self):
        with self._lock:
            return any(counts["failed"] > 0 for counts in self._counters.values())

    def counters(self):
        """Return a copy of per-host counters."""
        with self._lock:
            return dict((host, dict(counts)) for host, counts in self._counters.items())

    def recap(self, rows=None):
        with self._lock:
            self._write("\n" + _stars("PLAY RECAP"))
            hosts = sorted(self._counters)
            width = max([len(host) for host in hosts] + [1])
            for host in hosts:
                counts = self._counters[host]
                parts = []
                for name in ("ok", "changed", "skipped", "failed"):
                    parts.append(self._paint("%s=%-4d" % (name, counts[name]), self._recap_color(name, counts[name])))
                if counts["paused"]:
                    parts.append(self._paint("paused=%-4d" % counts["paused"], RECAP_COLORS["paused"]))
                self._write(("%s : %s" % (host.ljust(width), " ".join(parts))).rstrip())
            if rows:
                self._write("\nRun summary")
                self._write("-" * LINE_WIDTH)
                labels = []
                for row in rows:
                    label = row.get("target")
                    if label and label not in labels:
                        labels.append(label)
                if not labels:
                    self._write_rows(rows)
                    return
                # one block per target group; rows without a target (run-level) come first
                self._write_rows([row for row in rows if not row.get("target")])
                for label in labels:
                    block = [row for row in rows if row.get("target") == label]
                    counts = {}
                    for row in block:
                        key = self._status_key(row.get("status", ""))
                        counts[key] = counts.get(key, 0) + 1
                    summary = " ".join("%s=%d" % (key, counts[key]) for key in sorted(counts))
                    self._write("Target %s: %s" % (self._clean(label), summary))
                    self._write_rows(block)

    def _write_rows(self, rows):
        for row in rows:
            status = row.get("status", "")
            status = getattr(status, "value", status)
            self._write(
                "%s | %s | %s" % (row.get("agent", ""), status, self._clean(row.get("detail", "")))
            )

    @staticmethod
    def _recap_color(name, value):
        if name == "failed":
            return RECAP_COLORS["failed"] if value else ""
        if not value:
            return ""
        return RECAP_COLORS[name]
