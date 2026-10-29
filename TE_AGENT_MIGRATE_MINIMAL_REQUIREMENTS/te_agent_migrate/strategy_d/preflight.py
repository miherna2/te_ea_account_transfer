"""Read-only preflight for Strategy D.

Resolves every selected agent or cluster in the source group, proves who owns
it and who it is shared with, checks that it is online and that its name is
free in the target group, and snapshots what a later flow needs to restore:
share groups, tests per group (with their enabled flag), tags and cluster
members.

Only GET calls go through ``ctx.api``. The behaviour is the same in dry-run and
apply mode. Nothing is written, nothing is reset.

Entry point: ``run_preflight(ctx, inventory_agents, all_aids=None)``.
``all_aids`` is the list of account groups whose agent listing is scanned for
the cross-check below. When it is ``None`` the preflight calls
``ctx.api.list_account_groups()``. The source and target groups are always
scanned. A scanned group that cannot be read is a warning, except when the
agent is known to be shared with it: then the item is blocked.

Matching rules (nothing is guessed)
-----------------------------------
* Standalone agent, inventory row has no ``agent_id``: the record must match
  BOTH the inventory IP and the inventory name (trimmed, case-insensitive).
  IP alone or name alone is never accepted. Zero matches or more than one ->
  blocked with a reason that says what matched and what differed.
* Standalone agent with ``agent_id``: match by id, and the IP or the name of the
  inventory row must agree with that record. Otherwise blocked. If only one of
  the two agrees, the item stays ready and gets a warning.
* Cluster: by ``agent_id`` (cluster id) or by exact cluster name. Never by IP.

Sharing rules
-------------
The agent LIST call does not return ``accountGroups`` (0 of 1012 rows in the
real data). Only ``GET /agents/{id}`` does. Two sources are read and merged:

1. ``ctx.api.get_agent(source_aid, agent_id)`` -> ``accountGroups``.
2. A scan of the agent listing of every reachable group (see ``all_aids``):
   the groups whose listing contains the same agent id (cluster id for
   clusters).

The owner is always part of the share set, so the source group is added to
both sets before they are compared. If the sets agree, that union is
``share_aids``. If they differ, the item is blocked and the reason lists both
sets. If ``get_agent`` fails or returns no ``accountGroups`` field, the item is
blocked ("cannot read sharing; lab check 1").

Owner proof rule
----------------
The owner is NEVER assumed to be the source group. The owner is proven only
when ``GET /agents/{id}`` (or, as a second witness, the list row) carries
``ownerAid`` or ``ownerAccountGroupId``, at least one of these fields is
present, and every value found equals ``ctx.source_aid``. The group in which
the agent was listed, the query aid and the share set are NOT proof. If no
owner field exists, or any value differs, the item is blocked with
"owner group not proven to be the source group".

Extra attributes set on each PreflightItem (``PreflightItem`` itself is not
changed, these are plain attributes, set on every item including blocked ones)
-------------------------------------------------------------------------
``ui_ip``        inventory IP of a standalone agent, the address of the agent
                 UI. ``''`` for clusters. Cluster member IPs are in
                 ``item.members`` (``ipAddresses``).
``warnings``     list of str, never blocking. When the status is ``ready`` each
                 one is also copied to ``reasons`` as ``'warning: ...'``.
``test_enabled`` ``{aid: {test_id: bool or None}}`` enabled flag of every test
                 assigned to the device, per group. ``None`` = not readable.
``owner_proven`` True when the owner proof rule passed.
``share_sources`` ``{'get_agent': [aid...], 'scan': [aid...]}`` for reports.
``owner_assumed`` True when the owner was NOT proven by the API but assumed by the operator
                 (``--owner-is-source``); the dry-run file records ``assumed`` or ``proven``.

Target group rules (re-run guard, every device, with or without ``--owner-is-source``)
-----------------------------------------------------------------------------------
A device is blocked with "agent is already in the target group (re-run, or owned by the target)"
when its id (cluster id for clusters) is listed in the target group, or when the target group is in
its share set. A device that was legitimately shared with the target before the move is blocked
too: the operator must unshare it first.
"""
import ipaddress

from te_agent_migrate.errors import ApiError
from te_agent_migrate.models import StepStatus
from te_agent_migrate.output import mask_secrets
from te_agent_migrate.strategy_d.context import PreflightItem

STATUS_READY = 'ready'
STATUS_BLOCKED = 'blocked'
STATUS_EXCLUDED = 'excluded'

AGENT_TAG_TYPES = ('v-agent', 'agent', 'enterprise-agent', 'cluster')
OWNER_KEYS = ('ownerAid', 'ownerAccountGroupId')
REASON_OWNER = 'owner group not proven to be the source group'
REASON_SHARING = 'cannot read sharing; lab check 1'
REASON_IN_TARGET = 'agent is already in the target group (re-run, or owned by the target)'
REASON_IN_TARGET_HINT = ('if the device was shared with the target group before the move, '
                         'the operator must unshare it first')
MAX_IDS_SHOWN = 10


# --------------------------------------------------------------------- helpers
def _norm_name(value):
    return str(value if value is not None else '').strip().casefold()


def _norm_ip(value):
    text = str(value if value is not None else '').strip()
    if not text:
        return ''
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text.casefold()


def _is_online(state):
    return str(state if state is not None else '').strip().lower() == 'online'


def _member_state(member):
    value = member.get('agentState', member.get('state'))
    return '' if value is None else str(value)


def _member_label(member):
    name = member.get('name', member.get('agentName'))
    if name:
        return str(name)
    ips = member.get('ipAddresses', member.get('ipAddress'))
    if isinstance(ips, list) and ips:
        first = ips[0]
        return str(first.get('ipAddress', first)) if isinstance(first, dict) else str(first)
    if ips:
        return str(ips)
    ident = member.get('memberId', member.get('agentId', member.get('id')))
    return str(ident) if ident is not None else 'unknown member'


def _members(record):
    result = []
    for member in record.cluster_members:
        item = dict(member)
        if 'memberId' not in item:
            item['memberId'] = member.get('agentId', member.get('id'))
        if 'name' not in item:
            item['name'] = member.get('agentName')
        if 'ipAddresses' not in item:
            item['ipAddresses'] = member.get('ipAddress', [])
        item['state'] = _member_state(member) or item.get('state')
        result.append(item)
    return result


def _ip_strings(value):
    """Flat list of ip strings from a str, a list of str or a list of dict."""
    if isinstance(value, dict):
        value = value.get('ipAddress', value.get('ip'))
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_ip_strings(item))
        return out
    text = str(value).strip() if value is not None else ''
    return [text] if text else []


def _ids_text(ids):
    ids = sorted(str(i) for i in ids)
    shown = ', '.join(ids[:MAX_IDS_SHOWN])
    if len(ids) > MAX_IDS_SHOWN:
        shown += ', ... (+%d more)' % (len(ids) - MAX_IDS_SHOWN)
    return shown


def _aids_text(aids):
    return '[%s]' % ', '.join(sorted(str(a) for a in aids))


def _err(exc):
    return mask_secrets(str(exc))


class _Groups(object):
    """Per-run cache of read-only listings keyed by account group id.

    Errors are cached too, so a group that fails is not retried for every item.
    """

    def __init__(self, api):
        self.api = api
        self._agents = {}
        self._tags = {}
        self._tests = {}

    @staticmethod
    def _cached(store, key, loader):
        if key not in store:
            try:
                store[key] = ('ok', list(loader()))
            except ApiError as exc:
                store[key] = ('err', exc)
        kind, value = store[key]
        if kind == 'err':
            raise value
        return value

    def agents(self, aid):
        aid = str(aid)
        return self._cached(self._agents, aid, lambda: self.api.list_agents_with_clusters(aid))

    def tags(self, aid):
        aid = str(aid)
        return self._cached(self._tags, aid, lambda: self.api.list_tags(aid, expand_assignments=True))

    def tests(self, aid):
        aid = str(aid)
        return self._cached(self._tests, aid, lambda: self.api.list_tests(aid))


# ------------------------------------------------------------------ resolution
def _mismatch(inv, record):
    actual = 'cluster' if record.is_cluster else 'standalone agent'
    wanted = 'cluster' if inv.is_cluster else 'standalone agent'
    return 'inventory says %s but %s (id %s) is a %s' % (wanted, record.name, record.agent_id, actual)


def _ip_matches(inv_ip, record):
    ip = _norm_ip(inv_ip)
    return bool(ip) and ip in [_norm_ip(x) for x in record.ip_addresses]


def _name_matches(inv_name, record):
    name = _norm_name(inv_name)
    return bool(name) and _norm_name(record.name) == name


def _resolve(inv, records):
    """Return (record, blocked_reason, warnings). Never guesses."""
    want_cluster = bool(inv.is_cluster)
    label = 'cluster' if want_cluster else 'agent'

    if inv.agent_id:
        wanted = str(inv.agent_id).strip()
        found = [r for r in records if str(r.agent_id) == wanted]
        if not found:
            return None, 'no %s with agent_id %s in the source group' % (label, wanted), []
        if len(found) > 1:
            return None, 'agent_id %s matches %d records in the source group' % (wanted, len(found)), []
        record = found[0]
        if bool(record.is_cluster) != want_cluster:
            return record, _mismatch(inv, record), []
        if want_cluster:
            return record, None, []
        by_ip = _ip_matches(inv.ip_address, record)
        by_name = _name_matches(inv.hostname, record)
        if not by_ip and not by_name:
            return record, ('agent_id %s is %r (%s) but neither inventory IP %s nor inventory name %r agree'
                            % (wanted, record.name, ', '.join(record.ip_addresses) or 'no ip',
                               inv.ip_address or '-', inv.hostname)), []
        warnings = []
        if not by_ip:
            warnings.append('inventory IP %s is not an IP of agent %s (%s); matched by agent_id and name only'
                            % (inv.ip_address or '-', record.agent_id, ', '.join(record.ip_addresses) or 'no ip'))
        if not by_name:
            warnings.append('inventory name %r differs from agent name %r; matched by agent_id and IP only'
                            % (inv.hostname, record.name))
        return record, None, warnings

    if want_cluster:
        name = _norm_name(inv.hostname)
        named = [r for r in records if _norm_name(r.name) == name]
        clusters = [r for r in named if r.is_cluster]
        if len(clusters) == 1:
            return clusters[0], None, []
        if len(clusters) > 1:
            return None, 'cluster name %r matches %d clusters in the source group (ambiguous)' % (
                inv.hostname, len(clusters)), []
        if named:
            return named[0], _mismatch(inv, named[0]), []
        return None, 'no cluster named %r in the source group' % inv.hostname, []

    # standalone agent without agent_id: IP AND name must match the same record
    ip_text = str(inv.ip_address if inv.ip_address is not None else '').strip()
    if not ip_text or not _norm_name(inv.hostname):
        return None, ('inventory row needs both name and IP to select a standalone agent '
                      '(name %r, IP %r); give an agent_id otherwise' % (inv.hostname, ip_text)), []
    both = [r for r in records if _ip_matches(inv.ip_address, r) and _name_matches(inv.hostname, r)]
    standalone = [r for r in both if not r.is_cluster]
    if len(standalone) == 1:
        return standalone[0], None, []
    if len(standalone) > 1:
        return None, 'selection by IP %s and name %r matches %d agents in the source group (ambiguous): %s' % (
            inv.ip_address, inv.hostname, len(standalone),
            ', '.join(sorted(str(r.agent_id) for r in standalone))), []
    if both:
        return both[0], _mismatch(inv, both[0]), []
    by_ip = [r for r in records if _ip_matches(inv.ip_address, r)]
    by_name = [r for r in records if _name_matches(inv.hostname, r)]
    parts = []
    if by_ip:
        parts.append('IP %s matches %s but the name differs from %r' % (
            inv.ip_address, ', '.join('%r (id %s)' % (r.name, r.agent_id) for r in by_ip), inv.hostname))
    if by_name:
        parts.append('name %r matches %s but the IP differs from %s' % (
            inv.hostname, ', '.join('id %s (%s)' % (r.agent_id, ', '.join(r.ip_addresses) or 'no ip')
                                    for r in by_name), inv.ip_address))
    if not parts:
        parts.append('no agent with IP %s and name %r in the source group' % (inv.ip_address, inv.hostname))
    return None, 'no agent matches both IP and name: ' + '; '.join(parts), []


# ------------------------------------------------------------------------ tags
def _tags_for(groups, aids, agent_id, warnings):
    found = []
    agent_key = str(agent_id)
    for aid in aids:
        try:
            tags = groups.tags(aid)
        except ApiError as exc:
            warnings.append('could not read tags in group %s: %s' % (aid, _err(exc)))
            continue
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            for assignment in tag.get('assignments') or []:
                if not isinstance(assignment, dict):
                    continue
                kind = assignment.get('type')
                if kind is not None and str(kind).lower() not in AGENT_TAG_TYPES:
                    continue
                if str(assignment.get('id')) != agent_key:
                    continue
                found.append({
                    'aid': str(aid),
                    'tagId': str(tag.get('tagId', tag.get('id', ''))),
                    'key': tag.get('key'),
                    'value': tag.get('value'),
                })
                break
    return found


# ------------------------------------------------------------- owner and share
def _owner_values(raw):
    values = []
    if not isinstance(raw, dict):
        return values
    for key in OWNER_KEYS:
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get('aid', value.get('accountGroupId', value.get('id')))
        if value is None or isinstance(value, bool) or not str(value).strip():
            continue
        values.append((key, str(value).strip()))
    return values


def prove_owner(source_aid, *raws):
    """Return (proven, detail). See the module docstring for the rule."""
    found = []
    for raw in raws:
        found.extend(_owner_values(raw))
    if not found:
        return False, 'no %s field in the agent data' % ' or '.join(OWNER_KEYS)
    wrong = [(k, v) for k, v in found if v != str(source_aid)]
    if wrong:
        return False, 'agent data says %s' % ', '.join('%s=%s' % (k, v) for k, v in wrong)
    return True, ''


def _has_accountgroups(raw):
    return isinstance(raw, dict) and isinstance(raw.get('accountGroups'), list)


def _sharing_check(ctx, record, groups, scan_aids, assume_owner_source=False):
    """Prove owner and share set. Return dict with 'blocked' (list of reasons) and results."""
    source = str(ctx.source_aid)
    agent_id = str(record.agent_id)
    out = {'blocked': [], 'warnings': [], 'share_aids': [], 'owner_proven': False, 'owner_assumed': False,
           'sources': {'get_agent': [], 'scan': []}, 'listing': {}}

    try:
        detail = ctx.api.get_agent(source, agent_id)
    except ApiError as exc:
        out['blocked'].append('%s: GET /agents/%s failed: %s' % (REASON_SHARING, agent_id, _err(exc)))
        out['blocked'].append('%s (no agent detail)' % REASON_OWNER)
        return out
    if str(detail.agent_id) != agent_id:
        out['blocked'].append('GET /agents/%s returned a different agent id (%s)' % (agent_id, detail.agent_id))
        return out

    proven, why = prove_owner(source, detail.raw, record.raw)
    if not proven and assume_owner_source:
        found = _owner_values(detail.raw) + _owner_values(record.raw)
        if not found:
            # No owner field in the API data. The operator states that the source
            # group owns the device (--owner-is-source). A field that names another
            # group still blocks.
            proven = True
            out['owner_assumed'] = True
            out['warnings'].append('owner assumed to be the source group %s by the operator '
                                   '(no owner field in the API data)' % source)
    out['owner_proven'] = proven
    if not proven:
        out['blocked'].append('%s (%s)' % (REASON_OWNER, why))

    if not _has_accountgroups(detail.raw):
        out['blocked'].append('%s: GET /agents/%s returned no accountGroups' % (REASON_SHARING, agent_id))
        return out
    if detail.raw['accountGroups'] and not detail.shared_aids:
        out['blocked'].append('%s: accountGroups of agent %s could not be parsed' % (REASON_SHARING, agent_id))
        return out

    detail_set = set(str(a) for a in detail.shared_aids) | set([source])

    # scan every reachable group for the same agent id
    scan_set = set()
    unreadable = []
    for aid in sorted(set(scan_aids) | detail_set):
        try:
            listing = groups.agents(aid)
        except ApiError as exc:
            unreadable.append(aid)
            if aid in detail_set:
                out['blocked'].append('cannot read agents of shared group %s, tests would be lost: %s'
                                      % (aid, _err(exc)))
            else:
                out['warnings'].append('group %s could not be scanned for sharing: %s' % (aid, _err(exc)))
            continue
        same = [r for r in listing if str(r.agent_id) == agent_id]
        if same:
            scan_set.add(aid)
            out['listing'][aid] = same[0]
    scan_set.add(source)

    out['sources'] = {'get_agent': sorted(detail_set), 'scan': sorted(scan_set)}
    if out['blocked']:
        return out
    if detail_set != scan_set:
        out['blocked'].append(
            'sharing sources disagree for agent %s: GET /agents/%s accountGroups (owner included) = %s, '
            'group listing scan = %s' % (agent_id, agent_id, _aids_text(detail_set), _aids_text(scan_set)))
        return out
    out['share_aids'] = sorted(detail_set)
    return out


# ------------------------------------------------------------ tests per group
def _test_agent_ids(test):
    agents = test.raw.get('agents')
    if not isinstance(agents, list):
        return None
    ids = set()
    for item in agents:
        value = item.get('agentId', item.get('id')) if isinstance(item, dict) else item
        if value is not None and str(value):
            ids.add(str(value))
    return ids


def _is_a2a(test):
    return 'agent-to-agent' in str(test.test_type).lower()


def _target_agent_id(test):
    value = test.raw.get('targetAgentId')
    if value is None or isinstance(value, bool) or not str(value).strip():
        return None
    return str(value).strip()


def _test_checks(groups, share_aids, tests_by_aid, agent_id, warnings, blocked, is_cluster=False):
    """Read tests once per group. Return test_enabled; fill warnings and blocked."""
    agent_id = str(agent_id)
    test_enabled = {}
    for aid in share_aids:
        wanted = tests_by_aid.get(aid, [])
        try:
            listing = groups.tests(aid)
        except ApiError as exc:
            warnings.append('could not read tests in group %s (enabled flags, agent targets and last-agent '
                            'tests not checked): %s' % (aid, _err(exc)))
            test_enabled[aid] = dict((t, None) for t in wanted)
            continue
        by_id = dict((str(t.test_id), t) for t in listing)

        enabled = {}
        unknown = []
        for test_id in wanted:
            test = by_id.get(str(test_id))
            if test is None or 'enabled' not in test.raw or not isinstance(test.raw.get('enabled'), bool):
                enabled[test_id] = None
                unknown.append(test_id)
            else:
                enabled[test_id] = test.raw['enabled']
        test_enabled[aid] = enabled
        if unknown:
            warnings.append('%d test(s) in group %s could not be read (enabled flag unknown): %s'
                            % (len(unknown), aid, _ids_text(unknown)))

        # agent-to-agent tests that name this device as target
        targeting = []
        unreadable_targets = 0
        for test in listing:
            if not _is_a2a(test):
                continue
            target = _target_agent_id(test)
            if target is None:
                unreadable_targets += 1
            elif target == agent_id:
                targeting.append(str(test.test_id))
        if targeting:
            blocked.append('%d agent-to-agent test(s) in group %s use this device as target agent '
                           '(the agent id changes, update them first): %s'
                           % (len(targeting), aid, _ids_text(targeting)))
        if unreadable_targets:
            warnings.append('%d agent-to-agent test(s) in group %s have no readable targetAgentId; cannot '
                            'prove that none targets this device' % (unreadable_targets, aid))

        # single-agent tests: the device is their last agent
        last = []
        unreadable_agents = 0
        for test_id in wanted:
            test = by_id.get(str(test_id))
            if test is None:
                continue
            ids = _test_agent_ids(test)
            if ids is None:
                unreadable_agents += 1
            elif ids == set([agent_id]):
                last.append(str(test_id))
        if last and is_cluster:
            warnings.append('%d test(s) in group %s have this device as their only agent and will be '
                            'disabled when it is removed: %s' % (len(last), aid, _ids_text(last)))
        elif last:
            warnings.append('%d test(s) in group %s have this device as their only agent; the old agent '
                            'stays until the new one is verified, so these tests are not disabled: %s'
                            % (len(last), aid, _ids_text(last)))
        if unreadable_agents:
            warnings.append('%d test(s) in group %s have no readable agent list; last-agent check skipped '
                            'for them' % (unreadable_agents, aid))
    return test_enabled


# -------------------------------------------------------------- cluster family
def _family_problem(members):
    """Return a reason when the cluster members do not share one IP family, else ''."""
    common = None
    details = []
    for member in members:
        families = set()
        for text in _ip_strings(member.get('ipAddresses')):
            try:
                families.add(ipaddress.ip_address(text).version)
            except ValueError:
                continue
        label = _member_label(member)
        if not families:
            return 'cluster member %s has no readable IP address, IP family cannot be checked' % label
        details.append('%s=%s' % (label, '/'.join('IPv%d' % v for v in sorted(families))))
        common = families if common is None else (common & families)
    if common is not None and not common:
        return 'cluster members do not use the same IP family (%s)' % ', '.join(details)
    return ''


# ------------------------------------------------------------------- main flow
def _item(inv, status, reasons=None, record=None, owner_aid='', share_aids=None, tests_by_aid=None,
          tags=None, members=None, warnings=None, test_enabled=None, owner_proven=False, share_sources=None,
          owner_assumed=False):
    item = PreflightItem(
        inv, record=record, status=status, reasons=reasons, owner_aid=owner_aid,
        share_aids=share_aids, tests_by_aid=tests_by_aid, tags=tags, members=members,
    )
    item.ui_ip = '' if inv.is_cluster else str(inv.ip_address if inv.ip_address is not None else '').strip()
    item.warnings = list(warnings or [])
    item.test_enabled = dict((str(k), dict(v)) for k, v in (test_enabled or {}).items())
    item.owner_proven = bool(owner_proven)
    item.owner_assumed = bool(owner_assumed)
    item.share_sources = dict(share_sources or {'get_agent': [], 'scan': []})
    if status == STATUS_READY:
        for text in item.warnings:
            item.reasons.append('warning: ' + text)
    return item


def _blocked(inv, reasons, record=None, warnings=None, **extra):
    return _item(inv, STATUS_BLOCKED, reasons=reasons, record=record, warnings=warnings, **extra)


def _check_one(ctx, inv, groups, seen_ids, scan_aids, run_warnings, assume_owner_source=False):
    source = str(ctx.source_aid)
    target = str(ctx.target_aid)

    if source == target:
        return _blocked(inv, ['source and target account group are the same (%s)' % source])

    try:
        source_records = groups.agents(source)
    except ApiError as exc:
        return _blocked(inv, ['cannot read agents of the source group %s: %s' % (source, _err(exc))])

    record, problem, warnings = _resolve(inv, source_records)
    warnings = list(warnings) + list(run_warnings)
    if problem:
        return _blocked(inv, [problem], record=record, warnings=warnings)

    agent_id = str(record.agent_id)
    if agent_id in seen_ids:
        return _blocked(
            inv,
            ['duplicate selection: %s (id %s) was already selected as %r'
             % (record.name, agent_id, seen_ids[agent_id])],
            record=record, warnings=warnings,
        )
    seen_ids[agent_id] = inv.hostname

    reasons = []
    members = _members(record) if record.is_cluster else []

    # offline devices are excluded, reported, never migrated (plan section 10)
    if record.is_cluster:
        down = [m for m in members if not _is_online(_member_state(m))]
        if down:
            reasons.append('cluster has offline member(s): %s' % ', '.join(
                '%s (%s)' % (_member_label(m), _member_state(m) or 'unknown') for m in down))
        elif not members and not _is_online(record.state):
            reasons.append('cluster is %s and has no member data' % (record.state or 'unknown'))
    elif not _is_online(record.state):
        reasons.append('agent is %s' % (record.state or 'unknown'))
    if reasons:
        return _item(
            inv, STATUS_EXCLUDED, reasons=reasons, record=record, owner_aid='',
            share_aids=[source], tests_by_aid={source: record.test_ids}, members=members,
            warnings=warnings,
        )

    # cluster members must share one IP family (plan section 14)
    if record.is_cluster and members:
        family = _family_problem(members)
        if family:
            return _blocked(inv, [family], record=record, warnings=warnings, members=members)

    # name collision in the target group
    try:
        target_agents = groups.agents(target)
    except ApiError as exc:
        return _blocked(inv, ['cannot read agents of the target group %s: %s' % (target, _err(exc))],
                        record=record, warnings=warnings, members=members)
    taken = [t for t in target_agents
             if _norm_name(t.name) == _norm_name(record.name) and str(t.agent_id) != agent_id]
    if taken:
        return _blocked(
            inv,
            ['name %r is already used in the target group %s by agent id %s'
             % (record.name, target, taken[0].agent_id)],
            record=record, warnings=warnings, members=members,
        )

    # the device is already listed in the target group: a re-run, or owned by the target
    if any(str(t.agent_id) == agent_id for t in target_agents):
        return _blocked(
            inv, ['%s; %s' % (REASON_IN_TARGET, REASON_IN_TARGET_HINT)],
            record=record, warnings=warnings, members=members,
        )

    # owner proof and share set (GET /agents/{id} + scan of every reachable group)
    sharing = _sharing_check(ctx, record, groups, scan_aids, assume_owner_source)
    warnings.extend(sharing['warnings'])
    shared_to_target = (target in sharing['sources'].get('get_agent', [])
                        or target in sharing['share_aids'])
    if shared_to_target:
        return _blocked(
            inv, list(sharing['blocked']) + ['%s; %s' % (REASON_IN_TARGET, REASON_IN_TARGET_HINT)],
            record=record, warnings=warnings, members=members,
            owner_proven=sharing['owner_proven'], share_sources=sharing['sources'],
            owner_assumed=sharing['owner_assumed'],
        )
    if sharing['blocked']:
        return _blocked(
            inv, sharing['blocked'], record=record, warnings=warnings, members=members,
            owner_proven=sharing['owner_proven'], share_sources=sharing['sources'],
            owner_assumed=sharing['owner_assumed'],
        )
    share_aids = sharing['share_aids']

    tests_by_aid = {}
    for aid in share_aids:
        listed = record if aid == source else sharing['listing'].get(aid)
        tests_by_aid[aid] = list(listed.test_ids) if listed is not None else []

    blocked = []
    test_enabled = _test_checks(groups, share_aids, tests_by_aid, agent_id, warnings, blocked,
                                is_cluster=bool(record.is_cluster))
    if blocked:
        return _blocked(
            inv, blocked, record=record, warnings=warnings, members=members, owner_aid=source,
            share_aids=share_aids, tests_by_aid=tests_by_aid, test_enabled=test_enabled,
            owner_proven=True, share_sources=sharing['sources'], owner_assumed=sharing['owner_assumed'],
        )

    tags = _tags_for(groups, share_aids, agent_id, warnings)

    return _item(
        inv, STATUS_READY, record=record, owner_aid=source, share_aids=share_aids,
        tests_by_aid=tests_by_aid, tags=tags, members=members, warnings=warnings,
        test_enabled=test_enabled, owner_proven=True, share_sources=sharing['sources'],
        owner_assumed=sharing['owner_assumed'],
    )


def _scan_groups(ctx, all_aids):
    """Return (sorted aid list, run-level warnings)."""
    warnings = []
    aids = set([str(ctx.source_aid), str(ctx.target_aid)])
    if all_aids is not None:
        aids.update(str(a) for a in all_aids)
    else:
        try:
            aids.update(str(g.aid) for g in ctx.api.list_account_groups())
        except ApiError as exc:
            warnings.append('account group list not readable (%s); sharing scan limited to the source, the '
                            'target and the groups named by the agent' % _err(exc))
    return sorted(aids), warnings


def run_preflight(ctx, inventory_agents, all_aids=None, assume_owner_source=False):
    """Return one PreflightItem per inventory entry, in inventory order. GET only.

    ``all_aids``: account group ids to scan for sharing (default: every group from
    ``ctx.api.list_account_groups()``; the source and target are always scanned).
    ``assume_owner_source``: operator statement (``--owner-is-source``). When the API data has
    no owner field, the source group is taken as owner and a warning is added. A field that
    names another group still blocks.
    """
    groups = _Groups(ctx.api)
    scan_aids, run_warnings = _scan_groups(ctx, all_aids)
    seen_ids = {}
    items = []
    for inv in inventory_agents:
        items.append(_check_one(ctx, inv, groups, seen_ids, scan_aids, run_warnings, assume_owner_source))
    return items


# ---------------------------------------------------------------------- output
def _total_tests(item):
    return sum(len(v) for v in item.tests_by_aid.values())


def _detail(item):
    if item.status == STATUS_BLOCKED:
        return 'blocked: ' + '; '.join(item.reasons)
    kind = 'cluster' if item.inventory.is_cluster else 'agent'
    parts = ['%s' % kind, 'tests=%d' % _total_tests(item), 'shares=%d' % len(item.share_aids)]
    if item.inventory.is_cluster:
        parts.append('members=%d' % len(item.members))
    text = ', '.join(parts)
    # warnings are printed as info lines, not here
    others = [r for r in item.reasons if not r.startswith('warning:')]
    if item.status == STATUS_EXCLUDED:
        text = 'excluded (offline): ' + '; '.join(others) + ' [' + text + ']'
    elif others:
        text += ' | ' + '; '.join(others)
    return text


def _owner_text(item):
    if getattr(item, 'owner_proven', False) and item.owner_aid:
        return item.owner_aid
    return 'not proven'


def print_preflight(ctx, items):
    ctx.console.task('Preflight')
    info = getattr(ctx.console, 'info', None)
    for item in items:
        if item.status == STATUS_BLOCKED:
            status = StepStatus.FAILED
        elif item.status == STATUS_EXCLUDED:
            status = StepStatus.SKIPPED
        else:
            status = StepStatus.OK
        ctx.console.status(status, item.name, '', _detail(item))
        if info is None:
            continue
        sources = getattr(item, 'share_sources', None) or {}
        if item.share_aids:
            share_text = _aids_text(item.share_aids)
        elif sources.get('get_agent') or sources.get('scan'):
            share_text = 'disagree (get_agent=%s, scan=%s)' % (
                _aids_text(sources.get('get_agent', [])), _aids_text(sources.get('scan', [])))
        else:
            share_text = 'unknown'
        if item.record is not None:
            info(item.name, 'owner=%s shares=%s' % (_owner_text(item), share_text))
        for text in getattr(item, 'warnings', []):
            info(item.name, 'warning: ' + text)


def summarize(items):
    counts = {STATUS_READY: 0, STATUS_BLOCKED: 0, STATUS_EXCLUDED: 0}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def has_blockers(items):
    """True when any item is blocked. Excluded (offline) items do not block."""
    return any(item.status == STATUS_BLOCKED for item in items)
