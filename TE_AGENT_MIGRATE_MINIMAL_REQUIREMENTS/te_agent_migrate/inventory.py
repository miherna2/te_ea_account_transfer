"""Strict inventory loading (v1 two-column, v2 typed and v3 multi-target formats)."""
import csv
import ipaddress
from pathlib import Path

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import InventoryAgent

LEGACY_COLUMNS = ['hostname', 'ip_address']
V2_COLUMNS = ['type', 'name', 'ip_address']
V2_COLUMNS_WITH_ID = ['type', 'name', 'ip_address', 'agent_id']
# v3: the v2 columns, then these optional columns in this order (any subset).
V3_OPTIONAL_COLUMNS = ['agent_id', 'target_aid', 'share_aids']
V3_COLUMNS = V2_COLUMNS + V3_OPTIONAL_COLUMNS
# Kept for backwards compatibility with code importing the old constant.
EXPECTED_COLUMNS = LEGACY_COLUMNS
VALID_KINDS = ('agent', 'cluster')

HEADER_ERROR = (
    'Inventory header must be one of: hostname,ip_address | '
    'type,name,ip_address | type,name,ip_address,agent_id | '
    'type,name,ip_address,agent_id,target_aid,share_aids '
    '(v3: after ip_address only agent_id, target_aid, share_aids, in this order, each optional; '
    'exact order, no credential columns)'
)


def _header_is_valid(header):
    """True for v1, v2 and v3 headers. v3 = v2 columns plus an ordered subset of the optional columns."""
    if header == LEGACY_COLUMNS:
        return True
    if header[:len(V2_COLUMNS)] != V2_COLUMNS:
        return False
    extra = header[len(V2_COLUMNS):]
    wanted = [name for name in V3_OPTIONAL_COLUMNS if name in extra]
    return extra == wanted


def _clean(value):
    return (value or '').strip()


def _parse_ip(raw_ip, line_number):
    try:
        return str(ipaddress.ip_address(raw_ip))
    except ValueError as exc:
        raise ConfigurationError(
            'Inventory line %d has invalid IP address: %s' % (line_number, raw_ip)
        ) from exc


def _parse_aid_list(text, line_number):
    """Split a ';' separated list of group ids. Digits only. Order is kept, duplicates are dropped."""
    aids = []
    for part in text.split(';'):
        aid = part.strip()
        if not aid:
            continue
        if not aid.isdigit():
            raise ConfigurationError(
                'Inventory line %d has invalid share_aids entry (digits only, separated by ;): %s'
                % (line_number, aid)
            )
        if aid not in aids:
            aids.append(aid)
    return aids


def _parse_row(row, header, line_number):
    """Return an InventoryAgent for one CSV row (already stripped values)."""
    if header == LEGACY_COLUMNS:
        kind = 'agent'
        name = _clean(row.get('hostname'))
        raw_ip = _clean(row.get('ip_address'))
        agent_id = None
        target_aid = None
        share_aids = []
    else:
        kind = _clean(row.get('type')).casefold()
        name = _clean(row.get('name'))
        raw_ip = _clean(row.get('ip_address'))
        agent_id = _clean(row.get('agent_id')) or None
        target_aid = _clean(row.get('target_aid')) or None
        share_aids = _parse_aid_list(_clean(row.get('share_aids')), line_number)
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
    if target_aid is not None and not target_aid.isdigit():
        raise ConfigurationError(
            'Inventory line %d has invalid target_aid (digits only): %s' % (line_number, target_aid)
        )
    if target_aid is not None and share_aids and target_aid not in share_aids:
        raise ConfigurationError(
            'Inventory line %d: share_aids must contain target_aid %s' % (line_number, target_aid)
        )
    return InventoryAgent(
        hostname=name, ip_address=ip_address, kind=kind, agent_id=agent_id,
        target_aid=target_aid, share_aids=share_aids,
    )


def load_inventory(path):
    path = Path(path)
    if not path.is_file():
        raise ConfigurationError('Inventory file does not exist: %s' % path)
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        header = [_clean(name) for name in (reader.fieldnames or [])]
        if not _header_is_valid(header):
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
    targets = []
    for agent in agents:
        if agent.target_aid and agent.target_aid not in targets:
            targets.append(agent.target_aid)
    if targets:
        own = len([a for a in agents if a.target_aid])
        lines.append('  Target groups in the inventory: %d (%d row(s) name their own target)' % (len(targets), own))
    return '\n'.join(lines)
