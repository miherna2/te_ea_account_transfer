"""Inventory loading. Forgiving about the file, strict about the devices.

The file needs only the device name and its IP address. Everything else is optional:

    name,ip_address                      the minimum
    name,ip_address,target               one destination group per device (id or exact group name)
    type,name,ip_address,agent_id,target_aid    the older, fuller format

The tool accepts:
  * column names in many spellings (``name``, ``hostname``, ``agent``; ``ip``, ``ip_address``; ``target``,
    ``target_aid``, ``destination``; ``type``; ``agent_id``). Other columns are ignored, except a column
    that looks like a credential (password, token, secret): that is refused;
  * ``,`` ``;`` or a tab between the columns, and an Excel byte order mark (UTF-8 or UTF-16);
  * a file without a header line when each line is ``name,ip``;
  * no ``type`` column: a row with an IP is a standalone agent, a row without an IP is looked up as a cluster
    (``auto_kind`` is set so preflight can also find a cluster whose IP is given).

``share_aids`` is accepted from old files and ignored: the groups that see a device are read from the API.
"""
import csv
import io
import ipaddress
import re
from pathlib import Path

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.models import InventoryAgent

VALID_KINDS = ('agent', 'cluster')
DELIMITERS = (',', ';', '\t')

# canonical column -> accepted spellings (compared after lower case and "_" for spaces and "-")
ALIASES = {
    'name': ('name', 'hostname', 'host', 'host_name', 'agent', 'agent_name', 'device', 'device_name',
             'cluster', 'cluster_name'),
    'ip_address': ('ip_address', 'ip', 'ipaddress', 'ip_addr', 'address', 'agent_ip', 'management_ip'),
    'type': ('type', 'kind', 'agent_type'),
    'agent_id': ('agent_id', 'id', 'agentid'),
    'target': ('target', 'target_aid', 'target_group', 'target_account_group', 'destination',
               'destination_aid', 'destination_group', 'dest', 'dest_aid', 'to'),
    'share_aids': ('share_aids',),
}
SPELLING = dict((spelling, canonical) for canonical, spellings in ALIASES.items() for spelling in spellings)
CREDENTIAL_WORDS = ('password', 'passwd', 'secret', 'token', 'credential', 'apikey', 'api_key')
AGENT_WORDS = ('agent', 'standalone', 'enterprise', 'enterprise-agent', 'ea', 'appliance')
CLUSTER_WORDS = ('cluster', 'enterprise-cluster')

HEADER_HELP = (
    'The inventory needs a column with the device name and a column with its IP address, for example:\n'
    '    name,ip_address\n'
    '    MyAgent-01,10.1.1.5\n'
    'Optional: a target column (destination group id or name), a type column (agent or cluster).'
)


def _clean(value):
    return (value or '').strip()


def _key(value):
    """Normal form of a column name."""
    text = re.sub(r'[\s\-]+', '_', _clean(value).casefold())
    return text.strip('_')


def _read_text(path):
    """File text, with the byte order mark of Excel (UTF-8 or UTF-16) removed."""
    raw = Path(path).read_bytes()
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ConfigurationError('Inventory file %s is not text in UTF-8 or UTF-16 (save it as CSV UTF-8)' % path)


def _delimiter(lines):
    """The separator that the first non-empty line uses most. Comma when there is none."""
    first = next((line for line in lines if line.strip()), '')
    counts = [(first.count(delimiter), -index, delimiter) for index, delimiter in enumerate(DELIMITERS)]
    best = max(counts)
    return best[2] if best[0] > 0 else ','


def _parse_ip(raw_ip, line_number):
    try:
        return str(ipaddress.ip_address(raw_ip))
    except ValueError as exc:
        raise ConfigurationError(
            'Inventory line %d has invalid IP address: %s' % (line_number, raw_ip)
        ) from exc


def _parse_aid_list(text, line_number):
    """Old ``share_aids`` column: ids separated by ';'. Kept for old files; the tool ignores the groups."""
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


def _is_ip(text):
    try:
        ipaddress.ip_address(_clean(text))
        return True
    except ValueError:
        return False


def _columns(first_row):
    """Canonical column names for a header row, or None when the row is not a header."""
    names = [SPELLING.get(_key(cell)) for cell in first_row]
    recognized = [name for name in names if name]
    if 'name' in recognized:
        return names
    return None


def _check_header(first_row):
    for cell in first_row:
        key = _key(cell)
        if any(word in key for word in CREDENTIAL_WORDS):
            raise ConfigurationError(
                'Inventory column %r looks like a credential. Never put passwords or tokens in the inventory; '
                'the tool asks for them.' % _clean(cell)
            )


def _kind_of(raw_type, ip_address, line_number):
    """(kind, auto) of a row. Empty type = decided from the IP; preflight can still correct it."""
    text = _clean(raw_type).casefold()
    if not text:
        return ('agent', True) if ip_address else ('cluster', True)
    if text in AGENT_WORDS:
        return 'agent', False
    if text in CLUSTER_WORDS:
        return 'cluster', False
    raise ConfigurationError(
        'Inventory line %d has invalid type %r (expected agent or cluster, or leave it empty)'
        % (line_number, _clean(raw_type))
    )


def _parse_row(values, line_number):
    """InventoryAgent for one row. ``values`` maps canonical column names to text."""
    name = _clean(values.get('name'))
    if not name:
        raise ConfigurationError('Inventory line %d is missing a name' % line_number)
    raw_ip = _clean(values.get('ip_address'))
    ip_address = _parse_ip(raw_ip, line_number) if raw_ip else ''
    kind, auto = _kind_of(values.get('type'), ip_address, line_number)
    if kind == 'agent' and not ip_address:
        raise ConfigurationError(
            'Inventory line %d: an agent needs name and ip_address (%s has no IP address)' % (line_number, name)
        )
    agent_id = _clean(values.get('agent_id')) or None
    if agent_id is not None and not agent_id.isdigit():
        raise ConfigurationError(
            'Inventory line %d has invalid agent_id (digits only): %s' % (line_number, agent_id)
        )
    target = _clean(values.get('target'))
    target_aid = target if target.isdigit() else None
    target_name = target if target and not target.isdigit() else None
    agent = InventoryAgent(
        hostname=name, ip_address=ip_address, kind=kind, agent_id=agent_id, target_aid=target_aid,
        share_aids=_parse_aid_list(_clean(values.get('share_aids')), line_number),
    )
    agent.auto_kind = auto
    agent.target_name = target_name  # a group NAME: the caller turns it into an id once it knows the groups
    return agent


def _rows(path):
    """(line_number, canonical values) of every non-empty line of the file."""
    text = _read_text(path)
    lines = text.splitlines()
    delimiter = _delimiter(lines)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    columns = None
    out = []
    first = True
    for row in reader:
        cells = [_clean(cell) for cell in row]
        if not any(cells):
            continue
        if first:
            first = False
            _check_header(cells)
            columns = _columns(cells)
            if columns is not None:
                continue
            # No header: each line is  name,ip[,target]
            if len(cells) >= 2 and _is_ip(cells[1]):
                columns = ['name', 'ip_address', 'target']
            else:
                raise ConfigurationError(HEADER_HELP)
        if len(cells) > len(columns):
            raise ConfigurationError('Inventory line %d has more columns than the header' % reader.line_num)
        values = {}
        for index, cell in enumerate(cells):
            if columns[index]:
                values[columns[index]] = cell
        out.append((reader.line_num, values))
    if columns is None:
        raise ConfigurationError('Inventory is empty')
    if 'ip_address' not in columns and not all(
            _clean(v.get('type')).casefold() in CLUSTER_WORDS for _, v in out):
        raise ConfigurationError(HEADER_HELP)
    return out


def load_inventory(path):
    path = Path(path)
    if not path.is_file():
        raise ConfigurationError('Inventory file does not exist: %s' % path)
    agents = []
    seen_names = {}
    seen_ips = {}
    seen_ids = {}
    for line_number, values in _rows(path):
        agent = _parse_row(values, line_number)
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


def capture_inventory(path, ask, say=print):
    """Ask for the devices on the console and save them as the inventory file. Returns the path.

    ``ask(prompt)`` returns a line. Each line is ``name ip`` (and optionally a destination group id or
    name after them), separated by spaces, commas, semicolons or tabs. An empty line ends the list.
    """
    say('The inventory file %s does not exist.' % path)
    say('Type one device per line as:  name ip   (an empty line ends the list)')
    rows = []
    while True:
        line = _clean(ask('device %d: ' % (len(rows) + 1)))
        if not line:
            if rows:
                break
            say('Type at least one device, for example:  MyAgent-01 10.1.1.5')
            continue
        parts = [part for part in re.split(r'[\s,;]+', line) if part]
        if len(parts) < 2 or not _is_ip(parts[1]):
            say('Not understood. Use:  name ip   (for example: MyAgent-01 10.1.1.5)')
            continue
        if any(row[0].casefold() == parts[0].casefold() for row in rows):
            say('%s is already in the list.' % parts[0])
            continue
        rows.append((parts[0], str(ipaddress.ip_address(parts[1])), ' '.join(parts[2:])))
    with_target = any(row[2] for row in rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['name', 'ip_address'] + (['target'] if with_target else []))
        for row in rows:
            writer.writerow(list(row[:2]) + ([row[2]] if with_target else []))
    say('Saved %d device(s) in %s' % (len(rows), path))
    return path


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
