"""Timestamped JSON and Excel reporting for migration runs."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, cast

from openpyxl import Workbook

from te_agent_migrate.models import StepEvent
from te_agent_migrate.redaction import redact_value

SHEETS = [
    "Summary",
    "Agents",
    "Tests",
    "Unsupported Tests",
    "Stale Entries",
    "Diffs",
    "Decisions",
    "Errors",
]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "value"):
        return value.value
    return value


class RunReport:
    """Accumulates run state and writes redacted incremental/final artifacts."""

    def __init__(self, output_dir: Path, mode: str, metadata: dict[str, Any] | None = None) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.started_at = utc_now()
        self.basename = f"te_agent_migration_{mode.replace('-', '')}_{timestamp_slug()}"
        self.metadata = metadata or {}
        self.steps: list[dict[str, Any]] = []
        self.agents: list[dict[str, Any]] = []
        self.tests: list[dict[str, Any]] = []
        self.unsupported_tests: list[dict[str, Any]] = []
        self.stale_entries: list[dict[str, Any]] = []
        self.diffs: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self._lock = RLock()

    @property
    def json_path(self) -> Path:
        return self.output_dir / f"{self.basename}.json"

    @property
    def xlsx_path(self) -> Path:
        return self.output_dir / f"{self.basename}.xlsx"

    def record_step(self, event: StepEvent | dict[str, Any]) -> None:
        with self._lock:
            self.steps.append(_plain(event.to_dict() if isinstance(event, StepEvent) else event))

    def record_event(self, event: StepEvent | dict[str, Any]) -> None:
        self.record_step(event)

    def record_decision(self, prompt: str, response: str, agent: str | None = None) -> None:
        with self._lock:
            self.decisions.append(
                {"timestamp": utc_now(), "agent": agent, "prompt": prompt, "response": response}
            )

    def record_error(
        self, step: str, cause: str, agent: str | None = None, data: dict[str, Any] | None = None
    ) -> None:
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

    def add_agent(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.agents.append(row)

    append_agent = add_agent

    def add_test(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.tests.append(row)

    append_test = add_test

    def add_unsupported_test(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.unsupported_tests.append(row)

    append_unsupported = add_unsupported_test

    def add_stale_entry(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.stale_entries.append(row)

    append_stale = add_stale_entry

    def add_diff(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.diffs.append(row)

    append_diff = add_diff

    def append_decision(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.decisions.append(row)

    def append_error(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.errors.append(row)

    def to_dict(self, final: bool = False) -> dict[str, Any]:
        with self._lock:
            return cast(
                dict[str, Any],
                redact_value(
                    {
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
                    },
                ),
            )

    def write_json(self, final: bool = False) -> Path:
        self.json_path.write_text(
            json.dumps(self.to_dict(final), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return self.json_path

    def write_excel(self) -> Path:
        payload = self.to_dict(final=True)
        wb = Workbook()
        default = wb.active
        if default is None:
            raise RuntimeError("openpyxl did not create a default worksheet")
        default.title = SHEETS[0]
        for name in SHEETS[1:]:
            wb.create_sheet(name)
        self._write_key_values(wb["Summary"], payload)
        self._write_rows(wb["Agents"], payload["agents"])
        self._write_rows(wb["Tests"], payload["tests"])
        self._write_rows(wb["Unsupported Tests"], payload["unsupported_tests"])
        self._write_rows(wb["Stale Entries"], payload["stale_entries"])
        self._write_rows(wb["Diffs"], payload["diffs"])
        self._write_rows(wb["Decisions"], payload["decisions"])
        self._write_rows(wb["Errors"], payload["errors"])
        wb.save(self.xlsx_path)
        return self.xlsx_path

    def write_incremental(self) -> Path:
        return self.write_json(final=False)

    def write_final(self) -> tuple[Path, Path]:
        return self.write_json(final=True), self.write_excel()

    def write(self) -> tuple[Path, Path]:
        return self.write_final()

    @staticmethod
    def _write_key_values(sheet: Any, payload: dict[str, Any]) -> None:
        rows = {
            "schema_version": payload["schema_version"],
            "status": payload["status"],
            "mode": payload["mode"],
            "started_at": payload["started_at"],
            "updated_at": payload["updated_at"],
            **{f"metadata.{k}": v for k, v in payload["metadata"].items()},
            **{f"totals.{k}": v for k, v in payload["totals"].items()},
        }
        sheet.append(["Field", "Value"])
        for key, value in rows.items():
            sheet.append([key, _cell(value)])

    @staticmethod
    def _write_rows(sheet: Any, rows: list[dict[str, Any]]) -> None:
        if not rows:
            sheet.append(["No rows"])
            return
        headers = sorted({key for row in rows for key in row})
        sheet.append(headers)
        for row in rows:
            sheet.append([_cell(row.get(header, "")) for header in headers])


def _cell(value: Any) -> Any:
    value = _plain(value)
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True)
    return value
