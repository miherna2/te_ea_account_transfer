"""Strict two-column inventory loading."""

from __future__ import annotations

import csv
import ipaddress
from pathlib import Path

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import InventoryAgent

EXPECTED_COLUMNS = ["hostname", "ip_address"]


def load_inventory(path: Path) -> list[InventoryAgent]:
    if not path.is_file():
        raise ConfigurationError(f"Inventory file does not exist: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise ConfigurationError(
                "Inventory header must be exactly: hostname,ip_address (no credential columns)"
            )
        agents: list[InventoryAgent] = []
        seen_hosts: set[str] = set()
        seen_ips: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            hostname = (row.get("hostname") or "").strip()
            raw_ip = (row.get("ip_address") or "").strip()
            if not hostname or not raw_ip:
                raise ConfigurationError(f"Inventory line {line_number} contains an empty value")
            try:
                ip_address = str(ipaddress.ip_address(raw_ip))
            except ValueError as exc:
                raise ConfigurationError(
                    f"Inventory line {line_number} has invalid IP address: {raw_ip}"
                ) from exc
            host_key = hostname.casefold()
            if host_key in seen_hosts or ip_address in seen_ips:
                raise ConfigurationError(
                    f"Inventory line {line_number} duplicates a hostname or IP address"
                )
            seen_hosts.add(host_key)
            seen_ips.add(ip_address)
            agents.append(InventoryAgent(hostname=hostname, ip_address=ip_address))
    if not agents:
        raise ConfigurationError("Inventory is empty")
    return agents
