"""Build a migration scope (inventory v3) from an operator workbook.

The workbook has a sheet "Summary" (one row per agent) and a sheet
"Agent_Tests" (one row per agent and test). This module:

* reads the .xlsx file with the standard library only (zipfile + xml);
* derives a prefix map: agent name prefix -> target account group;
* builds a plan for each device and writes inventory v3 for READY devices;
* writes a report of all devices with status and reason.

No network call is made. Run it as::

    python -m te_agent_migrate.scope --xlsx FILE.xlsx --out work/input

Exit codes: 0 = no device needs review, 3 = some devices need review (edit
prefix_map.csv and run again), 1 = read error.
"""
import argparse
import csv
import ipaddress
import os
import posixpath
import re
import sys
import zipfile
import zlib
import xml.etree.ElementTree as ElementTree
from collections import Counter, OrderedDict, namedtuple
from decimal import Decimal, InvalidOperation

from te_agent_migrate.errors import ConfigurationError

SUMMARY_SHEET = 'Summary'
TESTS_SHEET = 'Agent_Tests'

STATUS_READY = 'READY'
STATUS_EXCLUDED_OFFLINE = 'EXCLUDED_OFFLINE'
STATUS_EXCLUDED_LOGIN = 'EXCLUDED_LOGIN'
STATUS_NEEDS_REVIEW = 'NEEDS_REVIEW'
PLAN_STATUSES = (STATUS_READY, STATUS_EXCLUDED_OFFLINE, STATUS_EXCLUDED_LOGIN, STATUS_NEEDS_REVIEW)

MAP_OK = 'OK'
MAP_REVIEW = 'REVIEW'
MAP_STATUSES = (MAP_OK, MAP_REVIEW)

PREFIX_MAP_COLUMNS = ['prefix', 'group_name', 'aid', 'status']
INVENTORY_COLUMNS = ['type', 'name', 'ip_address', 'agent_id', 'target_aid', 'share_aids']
REPORT_COLUMNS = [
    'name', 'ip_address', 'agent_id', 'prefix', 'status', 'reason', 'state', 'login_state',
    'source_aid', 'target_aid', 'target_name', 'share_aids', 'warnings', 'test_count',
]

PREFIX_MAP_FILE = 'prefix_map.csv'
INVENTORY_FILE = 'inventory.csv'
REPORT_FILE = 'scope_report.csv'
DEFAULT_OUT_DIR = os.path.join('work', 'input')

EXIT_OK = 0
EXIT_READ_ERROR = 1
EXIT_NEEDS_REVIEW = 3

WARNING_TARGET_NOT_IN_GROUPS = 'Target group is not in AccountGroups. It was added to share_aids.'

_MAX_MEMBER_BYTES = 300 * 1024 * 1024
_NAME_SEPARATOR = ' - '
_GROUP_PATTERN = re.compile(r'^(.*?)\s*\[\s*(\d+)\s*\]\s*$')
_IP_SPLIT_PATTERN = re.compile(r'[;,\s]+')

PrefixEntry = namedtuple('PrefixEntry', ['prefix', 'group_name', 'aid', 'status'])


# --------------------------------------------------------------------------- xlsx reader
def _local(tag):
    """Return an XML tag name without its namespace."""
    if not isinstance(tag, str):
        return ''
    return tag.rsplit('}', 1)[-1]


def _attr(element, name):
    """Return an attribute by local name (ignores the namespace), or None."""
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def _read_member(archive, name):
    """Return the bytes of one archive member. Refuse huge members and DOCTYPE or ENTITY."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise ConfigurationError('Workbook part is missing: %s' % name)
    if info.file_size > _MAX_MEMBER_BYTES:
        raise ConfigurationError('Workbook part is too large: %s' % name)
    data = archive.read(name)
    head = data[:4096].upper()
    if b'<!DOCTYPE' in head or b'<!ENTITY' in head:
        raise ConfigurationError('Workbook part has a DOCTYPE or ENTITY declaration: %s' % name)
    return data


def _parse_xml(data, name):
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ConfigurationError('Workbook part is not valid XML (%s): %s' % (name, exc))


def render_number(text):
    """Render a numeric cell as text. Whole numbers have no '.0' and no exponent."""
    text = (text or '').strip()
    if not text:
        return ''
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if number.is_finite() and number == number.to_integral_value():
        return str(int(number))
    return text


def _column_index(reference):
    """Return the zero-based column of a cell reference such as 'AB12'."""
    value = 0
    for char in reference:
        if not char.isalpha():
            break
        value = value * 26 + (ord(char.upper()) - 64)
    return value - 1


def _text_of_string_item(item):
    """Return the text of a shared string item or an inline string item."""
    parts = []
    for child in item:
        name = _local(child.tag)
        if name == 't':
            parts.append(child.text or '')
        elif name == 'r':
            for run_child in child:
                if _local(run_child.tag) == 't':
                    parts.append(run_child.text or '')
    return ''.join(parts)


def _read_shared_strings(archive):
    if 'xl/sharedStrings.xml' not in archive.namelist():
        return []
    root = _parse_xml(_read_member(archive, 'xl/sharedStrings.xml'), 'xl/sharedStrings.xml')
    return [_text_of_string_item(item) for item in root if _local(item.tag) == 'si']


def _find_sheet_part(archive, sheet_name):
    """Return the archive path of the worksheet with this name."""
    workbook = _parse_xml(_read_member(archive, 'xl/workbook.xml'), 'xl/workbook.xml')
    sheets = [(element.get('name') or '', _attr(element, 'id'))
              for element in workbook.iter() if _local(element.tag) == 'sheet']
    relation_id = None
    for name, found_id in sheets:
        if name == sheet_name:
            relation_id = found_id
            break
    if relation_id is None:
        for name, found_id in sheets:
            if name.strip().casefold() == sheet_name.casefold():
                relation_id = found_id
                break
    if relation_id is None:
        names = [name for name, _found in sheets]
        raise ConfigurationError(
            'Workbook has no sheet named %r (sheets found: %d)' % (sheet_name, len(names))
        )
    rels_name = 'xl/_rels/workbook.xml.rels'
    rels = _parse_xml(_read_member(archive, rels_name), rels_name)
    for element in rels.iter():
        if _local(element.tag) == 'Relationship' and element.get('Id') == relation_id:
            target = element.get('Target') or ''
            if target.startswith('/'):
                return target.lstrip('/')
            return posixpath.normpath(posixpath.join('xl', target))
    raise ConfigurationError('Workbook relation not found for sheet %r' % sheet_name)


def _cell_text(cell, shared):
    cell_type = cell.get('t')
    if cell_type == 'inlineStr':
        for child in cell:
            if _local(child.tag) == 'is':
                return _text_of_string_item(child)
        return ''
    value = ''
    for child in cell:
        if _local(child.tag) == 'v':
            value = child.text or ''
            break
    if cell_type == 's':
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ''
    if cell_type in ('str', 'd'):
        return value
    if cell_type == 'b':
        return 'TRUE' if value.strip() == '1' else 'FALSE'
    if cell_type == 'e':
        return ''
    return render_number(value)


def read_sheet(path, sheet_name):
    """Return the rows of one sheet as a list of lists of text. Empty cells are ''."""
    try:
        with zipfile.ZipFile(str(path)) as archive:
            shared = _read_shared_strings(archive)
            part = _find_sheet_part(archive, sheet_name)
            root = _parse_xml(_read_member(archive, part), part)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError):
        raise ConfigurationError('File is not a valid .xlsx workbook: %s' % path)
    except (IOError, OSError) as exc:
        raise ConfigurationError('Cannot read workbook %s: %s' % (path, exc))
    rows = []
    for row in root.iter():
        if _local(row.tag) != 'row':
            continue
        values = []
        position = 0
        for cell in row:
            if _local(cell.tag) != 'c':
                continue
            reference = cell.get('r')
            if reference:
                position = max(_column_index(reference), position)
            while len(values) < position:
                values.append('')
            text = _cell_text(cell, shared)
            if position < len(values):
                values[position] = text
            else:
                values.append(text)
            position += 1
        row_number = row.get('r')
        if row_number and row_number.isdigit():
            while len(rows) < int(row_number) - 1:
                rows.append([])
        rows.append(values)
    return rows


def _normal_header(text):
    return re.sub(r'[^0-9a-z]', '', (text or '').casefold())


def _header_map(header_row):
    mapping = {}
    for index, text in enumerate(header_row):
        key = _normal_header(text)
        if key and key not in mapping:
            mapping[key] = index
    return mapping


def _pick(row, mapping, key):
    index = mapping.get(key)
    if index is None or index >= len(row):
        return ''
    return (row[index] or '').strip()


def _require_columns(mapping, required, sheet_name):
    missing = [label for key, label in required if key not in mapping]
    if missing:
        raise ConfigurationError(
            'Sheet %r is missing column(s): %s' % (sheet_name, ', '.join(missing))
        )


# --------------------------------------------------------------------------- rows
class ScopeRow(object):
    """One agent from the Summary sheet."""

    def __init__(self, name, agent_id='', source_name='', state='', hostname='', ips=None,
                 groups=None, login_state='', notes='', test_count=0, test_owner_groups=None,
                 source_aid='', bad_group_entries=0):
        self.name = name
        self.agent_id = agent_id
        self.source_name = source_name
        self.source_aid = source_aid
        self.state = state
        self.hostname = hostname
        self.ips = list(ips or [])
        self.groups = list(groups or [])
        self.login_state = login_state
        self.notes = notes
        self.test_count = test_count
        self.test_owner_groups = list(test_owner_groups or [])
        self.bad_group_entries = bad_group_entries

    def __repr__(self):
        return 'ScopeRow(%r)' % (self.name,)


def parse_group_list(text):
    """Parse 'Name [123]; Other [456]'. Return (list of (name, aid), count of bad entries)."""
    groups = []
    seen = set()
    bad = 0
    for part in (text or '').split(';'):
        part = part.strip()
        if not part:
            continue
        match = _GROUP_PATTERN.match(part)
        if not match or not match.group(1).strip():
            bad += 1
            continue
        aid = match.group(2)
        if aid in seen:
            continue
        seen.add(aid)
        groups.append((match.group(1).strip(), aid))
    return groups, bad


def _parse_ips(text):
    return [part for part in _IP_SPLIT_PATTERN.split(text or '') if part]


def _parse_count(text):
    try:
        return int(Decimal((text or '').strip()))
    except (InvalidOperation, ValueError):
        return 0


def read_scope_workbook(path):
    """Read the Summary sheet. Return a list of ScopeRow (rows with an AgentName only)."""
    table = read_sheet(path, SUMMARY_SHEET)
    if not table:
        raise ConfigurationError('Sheet %r is empty' % SUMMARY_SHEET)
    mapping = _header_map(table[0])
    _require_columns(mapping, [
        ('agentname', 'AgentName'),
        ('expectedcurrentprimarygroup', 'ExpectedCurrentPrimaryGroup'),
        ('state', 'State'),
        ('accountgroups', 'AccountGroups'),
    ], SUMMARY_SHEET)
    rows = []
    for values in table[1:]:
        name = _pick(values, mapping, 'agentname')
        if not name:
            continue
        groups, bad = parse_group_list(_pick(values, mapping, 'accountgroups'))
        source_text = _pick(values, mapping, 'expectedcurrentprimarygroup')
        source_name, source_aid = source_text, ''
        match = _GROUP_PATTERN.match(source_text)
        if match and match.group(1).strip():
            source_name, source_aid = match.group(1).strip(), match.group(2)
        owners = [part.strip() for part in _pick(values, mapping, 'testownergroups').split(';')
                  if part.strip()]
        rows.append(ScopeRow(
            name=name,
            agent_id=_pick(values, mapping, 'agentid'),
            source_name=source_name,
            source_aid=source_aid,
            state=_pick(values, mapping, 'state'),
            hostname=_pick(values, mapping, 'hostname'),
            ips=_parse_ips(_pick(values, mapping, 'privateips')),
            groups=groups,
            login_state=_pick(values, mapping, 'loginstate'),
            notes=_pick(values, mapping, 'notes'),
            test_count=_parse_count(_pick(values, mapping, 'uniquetests')),
            test_owner_groups=owners,
            bad_group_entries=bad,
        ))
    return rows


def read_scope_tests(path):
    """Read the Agent_Tests sheet.

    Return a dict with these keys:

    * ``agents``: agent name -> Counter of test type (one count for each row);
    * ``groups``: group aid -> Counter of test type (one count for each row);
    * ``group_test_ids``: group aid -> set of distinct test ids;
    * ``group_unique_types``: group aid -> Counter of test type, each distinct
      test id counted once (use it to size a lab).
    """
    table = read_sheet(path, TESTS_SHEET)
    if not table:
        raise ConfigurationError('Sheet %r is empty' % TESTS_SHEET)
    mapping = _header_map(table[0])
    _require_columns(mapping, [
        ('agentname', 'AgentName'), ('aid', 'AID'), ('testid', 'TestId'), ('testtype', 'TestType'),
    ], TESTS_SHEET)
    agents = OrderedDict()
    groups = OrderedDict()
    group_test_ids = OrderedDict()
    group_unique_types = OrderedDict()
    for values in table[1:]:
        name = _pick(values, mapping, 'agentname')
        aid = _pick(values, mapping, 'aid')
        test_id = _pick(values, mapping, 'testid')
        test_type = _pick(values, mapping, 'testtype') or 'unknown'
        if not name and not aid and not test_id:
            continue
        if name:
            agents.setdefault(name, Counter())[test_type] += 1
        if aid:
            groups.setdefault(aid, Counter())[test_type] += 1
            ids = group_test_ids.setdefault(aid, set())
            unique_types = group_unique_types.setdefault(aid, Counter())
            if test_id and test_id not in ids:
                ids.add(test_id)
                unique_types[test_type] += 1
    return {
        'agents': agents,
        'groups': groups,
        'group_test_ids': group_test_ids,
        'group_unique_types': group_unique_types,
    }


# --------------------------------------------------------------------------- prefix map
def prefix_of(name):
    """Return the prefix of an agent name: the text before ' - '.

    A name without ' - ' uses the text before its first '-'. A name with neither has no prefix.
    """
    name = name or ''
    position = name.find(_NAME_SEPARATOR)
    if position < 0:
        position = name.find('-')
    if position < 0:
        return ''
    return name[:position].strip()


def prefix_key(prefix):
    """Normal form of a prefix for comparison: spaces collapsed, case ignored."""
    return ' '.join((prefix or '').split()).casefold()


def _group_rank(name):
    lowered = (name or '').casefold()
    if lowered.startswith('div_'):
        return 0
    if lowered.startswith('bu_'):
        return 1
    return 2


def source_aid_of(row):
    """Return the aid of the source group of a row, or '' when it is not known."""
    if row.source_aid:
        return row.source_aid
    wanted = row.source_name.strip().casefold()
    if wanted:
        for name, aid in row.groups:
            if name.casefold() == wanted:
                return aid
    return ''


def _derive_entry(prefix, members):
    names = OrderedDict()
    for row in members:
        for name, aid in row.groups:
            names.setdefault(aid, name)
    sources = set(source_aid_of(row) for row in members)
    sources.discard('')
    # Count the devices of this prefix that are in each group (the source is not a target).
    seen = Counter()
    for row in members:
        for aid in set(aid for _name, aid in row.groups):
            if aid not in sources:
                seen[aid] += 1
    # A Div_ or BU_ group is a division group. One device without it (an anomaly) must
    # not hide it: the plan adds a warning for that device.
    division = [aid for aid in names if aid in seen and _group_rank(names[aid]) < 2]
    if division:
        best = min(_group_rank(names[aid]) for aid in division)
        tier = [aid for aid in division if _group_rank(names[aid]) == best]
        if len(tier) == 1:
            return PrefixEntry(prefix, names[tier[0]], tier[0], MAP_OK)
        counts = sorted((seen[aid] for aid in tier), reverse=True)
        if counts[0] > counts[1] and counts[0] * 2 > len(members):
            top = [aid for aid in tier if seen[aid] == counts[0]][0]
            return PrefixEntry(prefix, names[top], top, MAP_OK)
    # No division group: use a group that every device of the prefix is in.
    common = [aid for aid in names if seen.get(aid) == len(members)]
    if len(common) == 1:
        return PrefixEntry(prefix, names[common[0]], common[0], MAP_OK)
    # Fallback: the group that is most often first in the list (without the source).
    firsts = Counter()
    for row in members:
        source = source_aid_of(row)
        for _name, aid in row.groups:
            if aid != source:
                firsts[aid] += 1
                break
    if not firsts:
        return PrefixEntry(prefix, '', '', MAP_REVIEW)
    top = max(firsts.values())
    for aid in names:
        if firsts.get(aid) == top:
            return PrefixEntry(prefix, names[aid], aid, MAP_REVIEW)
    return PrefixEntry(prefix, '', '', MAP_REVIEW)


def derive_prefix_map(rows):
    """Return an ordered list of PrefixEntry (prefix, group_name, aid, status)."""
    order = []
    members = {}
    for row in rows:
        prefix = prefix_of(row.name)
        if not prefix:
            continue
        key = prefix_key(prefix)
        if key not in members:
            members[key] = (prefix, [])
            order.append(key)
        members[key][1].append(row)
    return [_derive_entry(members[key][0], members[key][1]) for key in order]


def write_prefix_map(path, entries):
    """Write prefix_map.csv. Never overwrite a file that exists. Return True when written."""
    path = str(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    try:
        handle = open(path, 'x', newline='', encoding='utf-8')
    except FileExistsError:
        return False
    with handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(PREFIX_MAP_COLUMNS)
        for entry in entries:
            writer.writerow([entry[0], entry[1], entry[2], entry[3]])
    return True


def load_prefix_map(path):
    """Read prefix_map.csv. Return a list of PrefixEntry. Raise ConfigurationError if it is not valid."""
    path = str(path)
    if not os.path.isfile(path):
        raise ConfigurationError('Prefix map file does not exist: %s' % path)
    entries = []
    seen = {}
    with open(path, newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        header = [(name or '').strip() for name in (reader.fieldnames or [])]
        if header != PREFIX_MAP_COLUMNS:
            raise ConfigurationError(
                'Prefix map header must be: %s' % ','.join(PREFIX_MAP_COLUMNS)
            )
        reader.fieldnames = header
        for row in reader:
            line = reader.line_num
            prefix = (row.get('prefix') or '').strip()
            group_name = (row.get('group_name') or '').strip()
            aid = (row.get('aid') or '').strip()
            status = (row.get('status') or '').strip().upper()
            if not (prefix or group_name or aid or status):
                continue
            if prefix.startswith('#'):
                continue
            if not prefix:
                raise ConfigurationError('Prefix map line %d has no prefix' % line)
            if status not in MAP_STATUSES:
                raise ConfigurationError(
                    'Prefix map line %d has invalid status (use OK or REVIEW)' % line
                )
            if aid and not aid.isdigit():
                raise ConfigurationError('Prefix map line %d has invalid aid (digits only)' % line)
            key = prefix_key(prefix)
            if key in seen:
                raise ConfigurationError(
                    'Prefix map line %d repeats a prefix (first seen on line %d)' % (line, seen[key])
                )
            seen[key] = line
            entries.append(PrefixEntry(prefix, group_name, aid, status))
    return entries


# --------------------------------------------------------------------------- plan
class DevicePlan(object):
    """The plan for one device."""

    def __init__(self, name):
        self.name = name
        self.prefix = ''
        self.ip = ''
        self.agent_id = ''
        self.source_aid = ''
        self.source_name = ''
        self.target_aid = ''
        self.target_name = ''
        self.share_aids = []
        self.warnings = []
        self.status = STATUS_NEEDS_REVIEW
        self.reason = ''
        self.state = ''
        self.login_state = ''
        self.test_count = 0

    @property
    def ready(self):
        return self.status == STATUS_READY

    def __repr__(self):
        return 'DevicePlan(%r, %s)' % (self.name, self.status)


def _first_valid_ip(ips):
    for raw in ips:
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return ''


def _entry_lookup(prefix_map):
    lookup = {}
    for entry in prefix_map or []:
        entry = PrefixEntry(*tuple(entry))
        lookup.setdefault(prefix_key(entry.prefix), entry)
    return lookup


def _resolve_target(entry, rows):
    """Return (aid, name) of an OK map entry. The aid may be '' if it cannot be found."""
    aid = (entry.aid or '').strip()
    name = (entry.group_name or '').strip()
    if aid:
        if not name:
            for row in rows:
                for group_name, group_aid in row.groups:
                    if group_aid == aid:
                        return aid, group_name
        return aid, name
    if name:
        wanted = name.casefold()
        for row in rows:
            for group_name, group_aid in row.groups:
                if group_name.casefold() == wanted:
                    return group_aid, group_name
    return '', name


def build_plan(rows, prefix_map):
    """Return a list of DevicePlan, one for each row, in row order."""
    rows = list(rows)
    lookup = _entry_lookup(prefix_map)
    plans = []
    for row in rows:
        plan = DevicePlan(row.name)
        plan.prefix = prefix_of(row.name)
        plan.ip = _first_valid_ip(row.ips)
        plan.state = row.state
        plan.login_state = row.login_state
        plan.test_count = row.test_count
        plan.source_name = row.source_name
        plan.source_aid = source_aid_of(row)
        agent_id = row.agent_id.strip()
        if agent_id and agent_id.isdigit():
            plan.agent_id = agent_id
        elif agent_id:
            plan.warnings.append('AgentId is not a number. It was not used.')
        if row.source_name and not plan.source_aid:
            plan.warnings.append('Source group is not in AccountGroups.')
        if row.bad_group_entries:
            plan.warnings.append(
                'AccountGroups has %d entry(ies) without an aid. They were ignored.'
                % row.bad_group_entries
            )
        review = ''
        entry = lookup.get(prefix_key(plan.prefix)) if plan.prefix else None
        if not plan.prefix:
            review = 'Agent name has no prefix (expected "<prefix> - <host>").'
        elif entry is None:
            review = 'Prefix is not in the prefix map.'
        elif entry.status != MAP_OK:
            review = 'Prefix map status is REVIEW.'
            if entry.group_name:
                review += ' Suggested group: %s.' % entry.group_name
        else:
            target_aid, target_name = _resolve_target(entry, rows)
            if not target_aid:
                review = 'Prefix map entry has no usable group (set aid).'
            elif target_aid == plan.source_aid:
                review = 'Target group is the source group.'
            else:
                plan.target_aid = target_aid
                plan.target_name = target_name
        group_aids = [aid for _name, aid in row.groups]
        if plan.target_aid:
            shares = [plan.target_aid]
            if plan.target_aid not in group_aids:
                plan.warnings.append(WARNING_TARGET_NOT_IN_GROUPS)
        else:
            shares = []
        for aid in group_aids:
            if aid not in shares:
                shares.append(aid)
        plan.share_aids = shares
        if row.state.strip().casefold() != 'online':
            plan.status = STATUS_EXCLUDED_OFFLINE
            plan.reason = 'State is %r, not online.' % row.state.strip()
        elif row.login_state.strip().casefold() != 'good':
            plan.status = STATUS_EXCLUDED_LOGIN
            plan.reason = 'Login state is %r, not good.' % row.login_state.strip()
        elif review:
            plan.status = STATUS_NEEDS_REVIEW
            plan.reason = review
        elif not plan.ip:
            plan.status = STATUS_NEEDS_REVIEW
            plan.reason = 'No valid private IP address.'
        else:
            plan.status = STATUS_READY
        plans.append(plan)
    _check_duplicates(plans)
    return plans


def _check_duplicates(plans):
    """Make READY rows unique by name and IP (an inventory refuses duplicates)."""
    names = set()
    ips = set()
    ids = set()
    for plan in plans:
        if not plan.ready:
            continue
        name_key = plan.name.casefold()
        if name_key in names:
            plan.status = STATUS_NEEDS_REVIEW
            plan.reason = 'Agent name is used twice in the workbook.'
            continue
        if plan.ip in ips:
            plan.status = STATUS_NEEDS_REVIEW
            plan.reason = 'IP address is used twice in the workbook.'
            continue
        names.add(name_key)
        ips.add(plan.ip)
        if plan.agent_id:
            if plan.agent_id in ids:
                plan.agent_id = ''
                plan.warnings.append('AgentId is used twice in the workbook. It was not used.')
            else:
                ids.add(plan.agent_id)


# --------------------------------------------------------------------------- output files
def write_inventory(path, plans):
    """Write inventory v3 for READY devices only. Return the number of rows written."""
    count = 0
    with open(str(path), 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(INVENTORY_COLUMNS)
        for plan in plans:
            if not plan.ready:
                continue
            writer.writerow([
                'agent', plan.name, plan.ip, plan.agent_id, plan.target_aid,
                ';'.join(plan.share_aids),
            ])
            count += 1
    return count


def write_scope_report(path, plans):
    """Write a CSV report of all devices with status and reason."""
    with open(str(path), 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(REPORT_COLUMNS)
        for plan in plans:
            writer.writerow([
                plan.name, plan.ip, plan.agent_id, plan.prefix, plan.status, plan.reason,
                plan.state, plan.login_state, plan.source_aid, plan.target_aid, plan.target_name,
                ';'.join(plan.share_aids), ' | '.join(plan.warnings), plan.test_count,
            ])


def archive_existing(path):
    """Move an existing file to <stem>.old.csv (or <stem>.old.N.csv). Return the new path or None."""
    path = str(path)
    if not os.path.exists(path):
        return None
    base, extension = os.path.splitext(path)
    target = '%s.old%s' % (base, extension)
    number = 2
    while os.path.exists(target):
        target = '%s.old.%d%s' % (base, number, extension)
        number += 1
    os.rename(path, target)
    return target


# --------------------------------------------------------------------------- summary and CLI
def count_by_status(plans):
    counts = OrderedDict((status, 0) for status in PLAN_STATUSES)
    for plan in plans:
        counts[plan.status] += 1
    return counts


def format_summary(plans, entries=None):
    """Return summary lines. They never contain host names or IP addresses."""
    counts = count_by_status(plans)
    lines = ['SCOPE SUMMARY ' + '*' * 56]
    lines.append('  devices in workbook ........ %d' % len(plans))
    for status in PLAN_STATUSES:
        lines.append('  %-27s %d' % (status + ' ' + '.' * max(2, 24 - len(status)), counts[status]))
    review_prefixes = []
    for plan in plans:
        if plan.status == STATUS_NEEDS_REVIEW:
            label = plan.prefix or '(no prefix)'
            if label not in review_prefixes:
                review_prefixes.append(label)
    if entries is not None:
        used = set(prefix_key(p.prefix) for p in plans if p.prefix)
        in_use = [e for e in entries if prefix_key(e.prefix) in used]
        reviewed = len([e for e in in_use if e.status != MAP_OK])
        lines.append('  prefixes in map ............ %d (%d with status REVIEW)' % (len(in_use), reviewed))
        if len(entries) != len(in_use):
            lines.append('  map entries not used ....... %d' % (len(entries) - len(in_use)))
    target_warnings = [p for p in plans if WARNING_TARGET_NOT_IN_GROUPS in p.warnings]
    ready_warnings = [p for p in target_warnings if p.ready]
    lines.append(
        '  target not in AccountGroups  %d (%d in READY devices)'
        % (len(target_warnings), len(ready_warnings))
    )
    other = len([p for p in plans for w in p.warnings if w != WARNING_TARGET_NOT_IN_GROUPS])
    lines.append('  other warnings ............. %d' % other)
    if review_prefixes:
        lines.append('')
        lines.append('Prefixes that need review (%d):' % len(review_prefixes))
        for label in review_prefixes:
            lines.append('  - %s' % label)
        lines.append(
            'Action: edit prefix_map.csv (set group_name, aid and status OK), then run again.'
        )
    return lines


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m te_agent_migrate.scope',
        description='Build inventory v3 and a prefix map from a scope workbook. No network call.',
    )
    parser.add_argument('--xlsx', required=True, help='Path to the scope workbook (.xlsx).')
    parser.add_argument('--out', default=DEFAULT_OUT_DIR,
                        help='Output directory (default: %s).' % DEFAULT_OUT_DIR)
    return parser


def main(argv=None, stream=None):
    stream = stream or sys.stdout
    args = _build_parser().parse_args(argv)

    def say(text=''):
        stream.write(text + '\n')

    out_dir = args.out
    try:
        rows = read_scope_workbook(args.xlsx)
        if not rows:
            raise ConfigurationError('Sheet %r has no agent rows.' % SUMMARY_SHEET)
        derived = derive_prefix_map(rows)
        map_path = os.path.join(out_dir, PREFIX_MAP_FILE)
        created = write_prefix_map(map_path, derived)
        entries = load_prefix_map(map_path)
        plans = build_plan(rows, entries)
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        inventory_path = os.path.join(out_dir, INVENTORY_FILE)
        moved = None
        written = 0
        if any(plan.ready for plan in plans):
            moved = archive_existing(inventory_path)
            written = write_inventory(inventory_path, plans)
        write_scope_report(os.path.join(out_dir, REPORT_FILE), plans)
    except (ConfigurationError, IOError, OSError, ValueError) as exc:
        say('ERROR: %s' % exc)
        return EXIT_READ_ERROR

    for line in format_summary(plans, entries):
        say(line)
    say('')
    say('Prefix map: %s (%s)' % (map_path, 'created' if created else 'kept, not changed'))
    if written:
        say('Inventory: %s (%d READY row(s))' % (inventory_path, written))
        if moved:
            say('Previous inventory moved to: %s' % moved)
    else:
        say('Inventory: not written (no READY device).')
    say('Report: %s' % os.path.join(out_dir, REPORT_FILE))
    known = set(prefix_key(e.prefix) for e in entries)
    missing = set(prefix_key(prefix_of(r.name)) for r in rows if prefix_of(r.name)) - known
    if missing:
        say('Note: %d prefix(es) in the workbook are not in the existing prefix map.' % len(missing))
    if any(plan.status == STATUS_NEEDS_REVIEW for plan in plans):
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
