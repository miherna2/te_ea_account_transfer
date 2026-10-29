"""Strict inventory loading (v1 two-column and v2 typed formats)."""
import csv
import ipaddress
from pathlib import Path

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import InventoryAgent

LEGACY_COLUMNS = ['hostname', 'ip_address']
V2_COLUMNS = ['type', 'name', 'ip_address']
V2_COLUMNS_WITH_ID = ['type', 'name', 'ip_address', 'agent_id']
# Kept for backwards compatibility with code importing the old constant.
EXPECTED_COLUMNS = LEGACY_COLUMNS
VALID_KINDS = ('agent', 'cluster')

HEADER_ERROR = (
    'Inventory header must be one of: hostname,ip_address | '
    'type,name,ip_address | type,name,ip_address,agent_id '
    '(exact order, no credential columns)'
)


def _clean(value):
    return (value or '').strip()


def _parse_ip(raw_ip, line_number):
    try:
        return str(ipaddress.ip_address(raw_ip))
    except ValueError as exc:
        raise ConfigurationError(
            'Inventory line %d has invalid IP address: %s' % (line_number, raw_ip)
        ) from exc


def _parse_row(row, header, line_number):
    """Return an InventoryAgent for one CSV row (already stripped values)."""
    if header == LEGACY_COLUMNS:
        kind = 'agent'
        name = _clean(row.get('hostname'))
        raw_ip = _clean(row.get('ip_address'))
        agent_id = None
    else:
        kind = _clean(row.get('type')).casefold()
        name = _clean(row.get('name'))
        raw_ip = _clean(row.get('ip_address'))
        agent_id = _clean(row.get('agent_id')) or None
        if kind not in VALID_KINDS:
            raise ConfigurationError(
                'Inventory line %d has invalid type %r (expected agent or cluster)'
                % (line_number, _clean(row.get('type')))
            )
    if not name:
        raise ConfigurationError(
            'Inventory line %d is missing a name' % line_number
        )
    if kind == 'agent' and not raw_ip:
        raise ConfigurationError(
            'Inventory line %d contains an empty value (agent rows need name and ip_address)'
            % line_number
        )
    ip_address = _parse_ip(raw_ip, line_number) if raw_ip else ''
    if agent_id is not None and not agent_id.isdigit():
        raise ConfigurationError(
            'Inventory line %d has invalid agent_id (digits only): %s'
            % (line_number, agent_id)
        )
    return InventoryAgent(
        hostname=name, ip_address=ip_address, kind=kind, agent_id=agent_id
    )


def load_inventory(path):
    path = Path(path)
    if not path.is_file():
        raise ConfigurationError('Inventory file does not exist: %s' % path)
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        header = [_clean(name) for name in (reader.fieldnames or [])]
        if header not in (LEGACY_COLUMNS, V2_COLUMNS, V2_COLUMNS_WITH_ID):
            if not header:
                raise ConfigurationError('Inventory is empty')
            raise ConfigurationError(HEADER_ERROR)
        # Re-key rows with the cleaned header names.
        reader.fieldnames = header
        agents = []
        seen_names = {}
        seen_ips = {}
        seen_ids = {}
        for row in reader:
            line_number = reader.line_num
            values = [v for k, v in row.items() if k is not None]
            if not any(_clean(v) for v in values if not isinstance(v, list)):
                continue  # blank line / all-empty row
            if None in row:
                raise ConfigurationError(
                    'Inventory line %d has more columns than the header' % line_number
                )
            agent = _parse_row(row, header, line_number)
            name_key = agent.hostname.casefold()
            if name_key in seen_names:
                raise ConfigurationError(
                    'Inventory line %d duplicates name %r (first seen on line %d)'
                    % (line_number, agent.hostname, seen_names[name_key])
                )
            if agent.ip_address and agent.ip_address in seen_ips:
                raise ConfigurationError(
                    'Inventory line %d duplicates IP address %s (first seen on line %d)'
                    % (line_number, agent.ip_address, seen_ips[agent.ip_address])
                )
            if agent.agent_id and agent.agent_id in seen_ids:
                raise ConfigurationError(
                    'Inventory line %d duplicates agent_id %s (first seen on line %d)'
                    % (line_number, agent.agent_id, seen_ids[agent.agent_id])
                )
            seen_names[name_key] = line_number
            if agent.ip_address:
                seen_ips[agent.ip_address] = line_number
            if agent.agent_id:
                seen_ids[agent.agent_id] = line_number
            agents.append(agent)
    if not agents:
        raise ConfigurationError('Inventory is empty')
    return agents


def describe_inventory(agents):
    """Short multi-line summary of an inventory for confirmation prompts."""
    agents = list(agents)
    clusters = [a for a in agents if a.is_cluster]
    standalone = [a for a in agents if not a.is_cluster]
    lines = [
        'Inventory entries: %d' % len(agents),
        '  Standalone agents: %d' % len(standalone),
        '  Clusters: %d' % len(clusters),
    ]
    return '\n'.join(lines)
