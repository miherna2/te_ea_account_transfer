"""Redacted JSON and CSV reporting with standard-library writers."""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from te_agent_migrate.models import StepEvent, record_to_dict
from te_agent_migrate.redaction import redact_value


SECTIONS = (
    ("agents", "agents"),
    ("tests", "tests"),
    ("unsupported_tests", "unsupported_tests"),
    ("stale_entries", "stale_entries"),
    ("diffs", "diffs"),
    ("decisions", "decisions"),
    ("errors", "errors"),
    ("steps", "steps"),
)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_slug():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _plain(value):
    return record_to_dict(value)


def _cell(value):
    value = _plain(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if value is None:
        return ""
    return value


class RunReport(object):
    """Accumulate run state and write redacted incremental/final artifacts."""

    def __init__(self, output_dir, mode, metadata=None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.started_at = utc_now()
        self.basename = "te_agent_migration_%s_%s" % (
            mode.replace("-", ""),
            timestamp_slug(),
        )
        self.metadata = metadata or {}
        self.steps = []
        self.agents = []
        self.tests = []
        self.unsupported_tests = []
        self.stale_entries = []
        self.diffs = []
        self.decisions = []
        self.errors = []
        self._lock = RLock()

    @property
    def json_path(self):
        return self.output_dir / (self.basename + ".json")

    @property
    def csv_dir(self):
        return self.output_dir / (self.basename + "_csv")

    def record_step(self, event):
        with self._lock:
            self.steps.append(_plain(event.to_dict() if isinstance(event, StepEvent) else event))

    record_event = record_step

    def record_decision(self, prompt, response, agent=None):
        with self._lock:
            self.decisions.append(
                {
                    "timestamp": utc_now(),
                    "agent": agent,
                    "prompt": prompt,
                    "response": response,
                }
            )

    def record_error(self, step, cause, agent=None, data=None):
        with self._lock:
            self.errors.append(
                {
                    "timestamp": utc_now(),
                    "agent": agent,
                    "step": step,
                    "cause": cause,
                    "data": data or {},
                }
            )

    def add_agent(self, row):
        with self._lock:
            self.agents.append(row)

    append_agent = add_agent

    def add_test(self, row):
        with self._lock:
            self.tests.append(row)

    append_test = add_test

    def add_unsupported_test(self, row):
        with self._lock:
            self.unsupported_tests.append(row)

    append_unsupported = add_unsupported_test

    def add_stale_entry(self, row):
        with self._lock:
            self.stale_entries.append(row)

    append_stale = add_stale_entry

    def add_diff(self, row):
        with self._lock:
            self.diffs.append(row)

    append_diff = add_diff

    def append_decision(self, row):
        with self._lock:
            self.decisions.append(row)

    def append_error(self, row):
        with self._lock:
            self.errors.append(row)

    def to_dict(self, final=False):
        with self._lock:
            payload = {
                "schema_version": 1,
                "status": "final" if final else "incremental",
                "started_at": self.started_at,
                "updated_at": utc_now(),
                "mode": self.mode,
                "metadata": self.metadata,
                "steps": self.steps,
                "agents": self.agents,
                "tests": self.tests,
                "unsupported_tests": self.unsupported_tests,
                "stale_entries": self.stale_entries,
                "diffs": self.diffs,
                "decisions": self.decisions,
                "errors": self.errors,
                "totals": {
                    "steps": len(self.steps),
                    "agents": len(self.agents),
                    "tests": len(self.tests),
                    "unsupported_tests": len(self.unsupported_tests),
                    "stale_entries": len(self.stale_entries),
                    "errors": len(self.errors),
                },
            }
            return redact_value(_plain(payload))

    def write_json(self, final=False):
        self.json_path.write_text(
            json.dumps(self.to_dict(final), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.json_path

    @staticmethod
    def _write_rows(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            if not rows:
                handle.write("No rows\n")
                return
            headers = sorted(set(key for row in rows for key in row))
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({header: _cell(row.get(header)) for header in headers})

    def write_csv(self):
        payload = self.to_dict(final=True)
        self.csv_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "schema_version": payload["schema_version"],
            "status": payload["status"],
            "mode": payload["mode"],
            "started_at": payload["started_at"],
            "updated_at": payload["updated_at"],
        }
        summary.update({"metadata.%s" % key: value for key, value in payload["metadata"].items()})
        summary.update({"totals.%s" % key: value for key, value in payload["totals"].items()})
        with (self.csv_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("Field", "Value"))
            for key in sorted(summary):
                writer.writerow((key, _cell(summary[key])))
        for attribute, filename in SECTIONS:
            self._write_rows(self.csv_dir / (filename + ".csv"), payload[attribute])
        return self.csv_dir

    def write_incremental(self):
        return self.write_json(final=False)

    def write_final(self):
        return self.write_json(final=True), self.write_csv()

    write = write_final
