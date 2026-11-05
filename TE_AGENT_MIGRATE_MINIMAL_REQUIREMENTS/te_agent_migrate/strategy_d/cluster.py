"""Strategy D: Enterprise Agent cluster flow (maintenance window).

A cluster cannot move as one unit. The flow takes it apart and builds it again:

    snapshot -> unassign all members -> reset each member into the target group
    -> cluster/assign in the target group -> share back -> assign the saved
    tests -> re-enable the tests that were enabled -> delete the old member
    agents -> restore the name and the tags.

Tests stay in the source account group and keep their IDs. All tests on the
cluster have no agent during the window and may be disabled by the platform.
The old test IDs and the enabled state are saved first and printed in every
failure state line, so an operator can recover by hand.

The flow never calls DELETE on a cluster id. Old members are deleted as
standalone agents only, and only after the new cluster is verified.

Before the unassign (apply only) the flow opens the UI of EVERY member, logs in and reads the UI
settings. A member that fails stops the run before any change (step ``cluster_member_ui_check``).

Tests that the unassign disabled are NOT enabled again by default: ``set_test_enabled`` sends the full
test payload and can overwrite masked secrets. The step ``cluster_restore_tests_enabled`` lists them as
a manual task. ``FlowContext(auto_reenable_tests=True)`` turns the full-payload PUT on;
``auto_reenable_tests='minimal'`` uses ``set_test_enabled_minimal`` (PUT of ``{"enabled": true}``,
experimental, lab check 7).

Dry run (``ctx.apply`` False) sends no write call and opens no agent UI. It does
run the GET-only checks of apply (clean target group, enabled-state snapshot), so
both modes give the same verdict.
"""
from te_agent_migrate.api_client import AmbiguousAgentMatch, ThousandEyesApiClient
from te_agent_migrate.errors import AuthenticationError, UiError, VisibilityTimeout
from te_agent_migrate.models import StepStatus
from te_agent_migrate.strategy_d.context import FlowAborted, FlowResult, attach_stop_info
from te_agent_migrate.strategy_d.standalone import (
    MANUAL_RULES_TEXT,
    _aid_key,
    _desired_groups,
    _format_aids,
    _identity_matches,
    _poll_until,
    _source_aids,
    _delete_type,
    _tags_text,
    _test_aids,
    _translate,
    _unique,
)
from te_agent_migrate.ui_client import boolean_from_snapshot

STEP_PREFLIGHT = 'cluster_preflight_snapshot'
STEP_MEMBER_CHECK = 'cluster_member_ui_check'
STEP_SNAPSHOT_FILE = 'cluster_snapshot_file'
STEP_UNASSIGN = 'cluster_unassign'
STEP_MEMBER_AUTH = 'cluster_member_ui_authenticate'
STEP_MEMBER_SNAPSHOT = 'cluster_member_ui_snapshot_before'
STEP_MEMBER_RESET = 'cluster_member_ui_reset'
STEP_MEMBER_REACHABLE = 'cluster_member_ui_reachable_after_reset'
STEP_MEMBER_AUTH_AFTER = 'cluster_member_ui_authenticate_after_reset'
STEP_MEMBER_TOKEN = 'cluster_member_ui_set_target_token'
STEP_MEMBER_ONLINE = 'cluster_member_api_target_online'
STEP_ASSIGN = 'cluster_assign'
STEP_SHARE_BACK = 'cluster_share_back'
STEP_ASSIGN_TESTS = 'cluster_assign_tests'
STEP_RESTORE_ENABLED = 'cluster_restore_tests_enabled'
STEP_DELETE_OLD = 'cluster_delete_old'
STEP_RENAME = 'cluster_rename'
STEP_RESTORE_TAGS = 'cluster_restore_tags'
STEP_MANUAL_RULES = 'manual_notification_rules'

ASSIGN_CHUNK_SIZE = 100
IDS_PER_LINE = 50
MIN_MEMBERS = 2
MAINTENANCE_TEXT = ('MAINTENANCE WINDOW STARTED: all tests on this cluster have no agent until the new cluster '
                    'is built and the tests are assigned again')


# --------------------------------------------------------------------------- helpers
def _first_value(value):
    if isinstance(value, (list, tuple)):
        return _first_value(value[0]) if value else ''
    if isinstance(value, dict):
        return _first_value(value.get('ipAddress', ''))
    return '' if value is None else str(value).strip()


def _member_id(member):
    value = member.get('memberId', member.get('agentId', member.get('id')))
    return '' if value is None else str(value).strip()


def _member_ip(member):
    return _first_value(member.get('ipAddresses', member.get('ipAddress', [])))


def _member_name(member):
    value = member.get('name', member.get('agentName'))
    return str(value) if value not in (None, '') else _member_ip(member) or _member_id(member)


def _member_state(member):
    value = member.get('state', member.get('agentState'))
    return '' if value is None else str(value)


def _member_online(member):
    return _member_state(member).strip().lower() == 'online'


def _member_ui_ip(item, member):
    """Agent UI address of one member: ``item.ui_ips[memberId]`` when preflight sets it, else the member IP."""
    mapping = getattr(item, 'ui_ips', None)
    if isinstance(mapping, dict):
        value = str(mapping.get(_member_id(member)) or '').strip()
        if value:
            return value
    return _member_ip(member)


def _member_label(member):
    return '%s (%s)' % (_member_name(member), _member_ip(member) or 'no IP')


def _cluster_id(item):
    record = getattr(item, 'record', None)
    return str(getattr(record, 'agent_id', '?'))


def _chunks(values, size):
    values = list(values)
    return [values[i:i + size] for i in range(0, len(values), size)]


def _ids_text(values):
    return '[%s]' % ','.join(str(v) for v in values)


def _tests_total(item):
    return sum(len(v) for v in item.tests_by_aid.values())


def _snapshot_detail(item):
    members = ', '.join('%s=%s' % (_member_id(m), _member_label(m)) for m in item.members)
    return 'cluster %s %r; owner aid %s; shared with aids %s; %d members [%s]; %d tests in %d groups; %d tags' % (
        _cluster_id(item), item.name, item.owner_aid or '?', _format_aids(item.share_aids), len(item.members),
        members, _tests_total(item), len(_test_aids(item)), len(item.tags))


def _assign_text(item):
    parts = []
    for aid in _test_aids(item):
        count = len(item.tests_by_aid[aid])
        parts.append('%d tests in aid %s (%d call%s of up to %d)' % (
            count, aid, len(_chunks(item.tests_by_aid[aid], ASSIGN_CHUNK_SIZE)),
            '' if count <= ASSIGN_CHUNK_SIZE else 's', ASSIGN_CHUNK_SIZE))
    return ', '.join(parts)


def plan_cluster(item, target_aid=None, auto_reenable_tests=False):
    """Return the ordered (step, detail) list of the cluster flow.

    Pure function: no context, no API, no UI. Dry run prints it as planned steps.
    ``target_aid`` is optional and only makes the details concrete. ``auto_reenable_tests`` (False, True or
    'minimal') only changes the text of the step ``cluster_restore_tests_enabled``.
    """
    owner = item.owner_aid or '?'
    target_text = str(target_aid) if target_aid not in (None, '') else '<target>'
    cluster_id = _cluster_id(item)
    members = list(item.members)
    total = len(members)
    wanted = _desired_groups(item, target_aid)
    plan = [(STEP_PREFLIGHT, _snapshot_detail(item))]
    for index, member in enumerate(members, 1):
        plan.append((STEP_MEMBER_CHECK, 'member %d/%d %s: log in to the agent UI at %s and read its settings '
                     'BEFORE any change; stop the run if this fails' % (
                         index, total, _member_label(member), _member_ip(member) or '<no-ip>')))
    plan.append((STEP_UNASSIGN, '%s; unassign all %d members %s from cluster %s in aid %s (never delete the cluster id)'
                 % (MAINTENANCE_TEXT, total, _ids_text(_member_id(m) for m in members), cluster_id, owner)))
    for index, member in enumerate(members, 1):
        tag = 'member %d/%d %s' % (index, total, _member_label(member))
        ip = _member_ip(member) or '<no-ip>'
        plan.append((STEP_MEMBER_AUTH, '%s: log in to the agent UI at %s' % (tag, ip)))
        plan.append((STEP_MEMBER_SNAPSHOT, '%s: read agent UI settings before the reset (browserbot, crash reports)' % tag))
        plan.append((STEP_MEMBER_RESET, '%s: reset agent %s (old member id %s gets a new identity)'
                     % (tag, ip, _member_id(member))))
        plan.append((STEP_MEMBER_REACHABLE, '%s: wait until the agent UI at %s answers again' % (tag, ip)))
        plan.append((STEP_MEMBER_AUTH_AFTER, '%s: log in to the agent UI again after the reset' % tag))
        plan.append((STEP_MEMBER_TOKEN, '%s: submit the token of target aid %s (value hidden)' % (tag, target_text)))
        plan.append((STEP_MEMBER_ONLINE,
                     '%s: wait for a new online agent (%s) in aid %s; stop everything if a new online agent '
                     'appears in source aid %s' % (tag, ip, target_text, owner)))
    if total >= MIN_MEMBERS:
        plan.append((STEP_ASSIGN, 'in aid %s make member 1 the base and assign the other %d new agents; find the '
                     'new cluster id and verify all members are online' % (target_text, total - 1)))
    else:
        plan.append((STEP_ASSIGN, 'a cluster needs at least %d members' % MIN_MEMBERS))
    if len(wanted) > 1:
        plan.append((STEP_SHARE_BACK, 'share the new cluster with aids %s' % _format_aids(wanted)))
    else:
        plan.append((STEP_SHARE_BACK, 'no other group saw the cluster; nothing to share back'))
    assign = _assign_text(item)
    plan.append((STEP_ASSIGN_TESTS, ('assign %s, then verify each test list' % assign) if assign
                 else 'no tests to assign'))
    if assign and auto_reenable_tests == 'minimal':
        plan.append((STEP_RESTORE_ENABLED, 'record the enabled state of %d tests before the unassign; enable again '
                     'every test that was enabled and is disabled now with a minimal PUT of {"enabled": true} '
                     '(experimental, lab check 7)' % _tests_total(item)))
    elif assign and auto_reenable_tests:
        plan.append((STEP_RESTORE_ENABLED, 'record the enabled state of %d tests before the unassign; enable again '
                     'every test that was enabled and is disabled now (PUT of the full test payload)'
                     % _tests_total(item)))
    elif assign:
        plan.append((STEP_RESTORE_ENABLED, 'record the enabled state of %d tests before the unassign; the tool '
                     'does NOT enable tests (auto_reenable_tests is off); it lists, per group, the test ids that '
                     'were enabled before and are disabled now, as a manual task' % _tests_total(item)))
    else:
        plan.append((STEP_RESTORE_ENABLED, 'no tests to re-enable'))
    plan.append((STEP_DELETE_OLD, 'delete the %d old member agents (standalone, never the cluster id) in aid %s '
                 'after the new cluster and the tests are verified; wait until absent' % (total, owner)))
    plan.append((STEP_RENAME, 'set the new cluster name to %r in aid %s and verify it' % (item.name, target_text)))
    if item.tags:
        plan.append((STEP_RESTORE_TAGS, 'restore %d tags: %s' % (len(item.tags), _tags_text(item.tags))))
    else:
        plan.append((STEP_RESTORE_TAGS, 'no tags to restore'))
    plan.append((STEP_MANUAL_RULES, MANUAL_RULES_TEXT))
    return plan


# --------------------------------------------------------------------------- snapshot and state
class _Snapshot(object):
    """What apply saved before the first change: tests per group and their enabled state."""

    def __init__(self, item):
        self.cluster_id = _cluster_id(item)
        self.name = item.name
        self.members = [dict(memberId=_member_id(m), name=_member_name(m), ip=_member_ip(m), state=_member_state(m))
                        for m in item.members]
        self.tests_by_aid = dict((aid, list(item.tests_by_aid[aid])) for aid in _test_aids(item))
        self.test_type = {}      # (aid, test_id) -> test type
        self.enabled = {}        # (aid, test_id) -> True / False
        self.unknown = []        # [(aid, test_id)] enabled state could not be read

    def enabled_ids(self, aid, value):
        return [t for t in self.tests_by_aid.get(aid, []) if self.enabled.get((aid, t)) is value]

    def export(self):
        """Enabled flags and test types for the snapshot file: {'enabled': {aid: {test: bool}}, 'unknown': [...]}"""
        enabled, types = {}, {}
        for (aid, test_id), flag in self.enabled.items():
            enabled.setdefault(aid, {})[test_id] = flag
            types.setdefault(aid, {})[test_id] = self.test_type.get((aid, test_id))
        return {'enabled': enabled, 'test_types': types, 'enabled_unknown': [[a, t] for a, t in self.unknown]}


def _recovery_text(item, snapshot):
    """Old data an operator needs for a manual recovery. Printed in every failure state line."""
    parts = ['old cluster id %s name %r owner aid %s' % (_cluster_id(item), item.name, item.owner_aid or '?'),
             'members %s' % '; '.join('%s %s %s' % (_member_id(m), _member_name(m), _member_ip(m) or 'no-ip')
                                     for m in item.members),
             'shared with aids %s' % _format_aids(item.share_aids)]
    for aid in _test_aids(item):
        ids = item.tests_by_aid[aid]
        parts.append('tests in aid %s: %s' % (aid, _ids_text(ids)))
        if snapshot is not None:
            parts.append('  enabled before in aid %s: %s' % (aid, _ids_text(snapshot.enabled_ids(aid, True))))
            parts.append('  disabled before in aid %s: %s' % (aid, _ids_text(snapshot.enabled_ids(aid, False))))
            unknown = [t for a, t in snapshot.unknown if a == aid]
            if unknown:
                parts.append('  enabled state unknown in aid %s: %s' % (aid, _ids_text(unknown)))
    if snapshot is None and _test_aids(item):
        parts.append('enabled state was not saved yet (no change was made before the snapshot)')
    if item.tags:
        parts.append('tags: %s' % _tags_text(item.tags))
    return '; '.join(parts)


class _Progress(object):
    """What the flow changed so far. ``describe`` becomes the failure state line."""

    def __init__(self, item, owner, target):
        self.item = item
        self.owner = owner
        self.target = str(target)
        self.snapshot = None
        self.unassign_sent = False
        self.unassigned = False
        self.reset_started = []     # member labels, reset command sent
        self.token_sent = []        # member labels, target token submitted
        self.current = ''           # 'member label: sub step'
        self.new_ids = []           # new agent ids online in the target group, member order
        self.cluster_assign_sent = False
        self.cluster_id = None
        self.shared = False
        self.assigned_groups = []
        self.assigned_count = 0
        self.tests_verified = False
        self.enabled_done = False
        self.deleted = []           # standalone ids of old members whose absence is confirmed
        self.delete_sent = []       # standalone ids with a delete sent but absence not confirmed
        self.old_map = None         # member id -> standalone agent id (None = could not be mapped); set after the unassign
        self.old_records = {}       # standalone agent id -> AgentRecord of the old member (owner listing after the unassign)
        self.enabled_manual = False  # the tool did not enable tests; a manual task was printed
        self.renamed = False
        self.done = False

    def describe(self):
        item = self.item
        if not self.unassign_sent:
            return 'nothing changed (the cluster is untouched)'
        total = len(item.members)
        parts = []
        if not self.unassigned:
            parts.append('cluster unassign was sent for %d members but is NOT confirmed; the cluster may be gone '
                         'and the members may be standalone in aid %s' % (total, self.owner))
        else:
            parts.append('cluster %s was unassigned: all %d members are standalone agents in aid %s; the tests '
                         'have no agent and may be disabled' % (_cluster_id(item), total, self.owner))
        labels = [_member_label(m) for m in item.members]
        if self.reset_started:
            parts.append('reset command sent to: %s' % ', '.join(self.reset_started))
        waiting = [label for label in labels if label not in self.reset_started]
        if waiting:
            parts.append('not reset yet (still standalone in the source group): %s' % ', '.join(waiting))
        no_token = [label for label in self.reset_started if label not in self.token_sent]
        if no_token:
            parts.append('reset without a confirmed target token (manual action needed): %s' % ', '.join(no_token))
        if self.token_sent:
            parts.append('target token submitted for: %s' % ', '.join(self.token_sent))
        if self.new_ids:
            parts.append('new agents online in target aid %s: %s' % (self.target, _ids_text(self.new_ids)))
        if self.current:
            parts.append('stopped during: %s' % self.current)
        if not self.cluster_assign_sent:
            parts.append('new cluster is NOT built')
        elif not self.cluster_id:
            parts.append('cluster assign was sent in target aid %s but the new cluster is not confirmed' % self.target)
        else:
            parts.append('new cluster %s exists in target aid %s' % (self.cluster_id, self.target))
            parts.append('shared with the old groups' if self.shared else 'not shared with the old groups yet')
            if self.assigned_groups or self.assigned_count:
                parts.append('tests assigned (%d) in aids %s; %s' % (
                    self.assigned_count, _format_aids(self.assigned_groups),
                    'verified' if self.tests_verified else 'NOT verified'))
            else:
                parts.append('no test assigned yet')
            if self.tests_verified:
                if self.enabled_manual:
                    parts.append('tests NOT enabled again by the tool (manual task printed)')
                else:
                    parts.append('enabled state restored' if self.enabled_done else 'enabled state not restored yet')
        if self.deleted:
            parts.append('old members deleted: %s' % _ids_text(self.deleted))
        if self.delete_sent:
            parts.append('delete sent, not confirmed for old members %s (they may or may not be deleted)'
                         % _ids_text(self.delete_sent))
        if not self.deleted and not self.delete_sent:
            parts.append('old member agents are still present (not deleted)')
        if (self.deleted or self.delete_sent) and not self.renamed:
            parts.append('name or tags may be incomplete')
        return '; '.join(parts)


# --------------------------------------------------------------------------- validation and reads
def _validate(ctx, item):
    """Raise FlowAborted when the cluster cannot run. No API call, no UI call."""
    if getattr(item, 'status', None) != 'ready':
        reasons = '; '.join(getattr(item, 'reasons', []) or [])
        raise FlowAborted('cluster %s is not ready (status=%s)%s' % (
            item.name, getattr(item, 'status', None), ': ' + reasons if reasons else ''))
    kind = getattr(item.inventory, 'kind', None)
    if kind != 'cluster':
        raise FlowAborted('device %s is kind %r; the cluster flow handles kind cluster only' % (item.name, kind))
    record = getattr(item, 'record', None)
    if record is None or not str(record.agent_id).strip():
        raise FlowAborted('cluster %s has no cluster record; run preflight again' % item.name)
    if not getattr(record, 'is_cluster', False):
        raise FlowAborted('record %s of %s is not a cluster (agent type %r); nothing was changed'
                          % (record.agent_id, item.name, getattr(record, 'agent_type', None)))
    if not item.members:
        raise FlowAborted('cluster %s has no members; nothing was changed' % item.name)
    if len(item.members) < MIN_MEMBERS:
        raise FlowAborted('cluster %s has %d member; the flow needs at least %d to rebuild it'
                          % (item.name, len(item.members), MIN_MEMBERS))
    ids, ips = [], []
    for member in item.members:
        label = _member_label(member)
        if not _member_id(member):
            raise FlowAborted('a member of cluster %s has no member id (%s)' % (item.name, label))
        if not _member_ip(member):
            raise FlowAborted('member %s of cluster %s has no IP address; the agent UI cannot be reached'
                              % (label, item.name))
        if not _member_online(member):
            raise FlowAborted('member %s of cluster %s is %s; every member must be online. Nothing was changed'
                              % (label, item.name, _member_state(member) or 'in an unknown state'))
        ids.append(_member_id(member))
        ips.append(_member_ip(member))
    if len(set(ids)) != len(ids) or len(set(ips)) != len(ips):
        raise FlowAborted('cluster %s has duplicate member ids or IPs; nothing was changed' % item.name)
    if ctx.apply and not ctx.target_account_token:
        raise FlowAborted('no target account group token set; apply cannot register the members')
    if str(ctx.target_aid) in ('', 'None'):
        raise FlowAborted('no target account group id set')


def _require_clean_target(ctx, item, ignore_ids):
    """GET only. Abort when the target already has an online agent with a member IP. Returns the target listing."""
    listing = list(ctx.api.list_agents(ctx.target_aid))
    existing = [a for a in listing
                if a.online and a.agent_id not in ignore_ids
                and any(_member_ip(m) in a.ip_addresses for m in item.members)]
    if existing:
        raise FlowAborted('target aid %s already has an online agent with the IP of a member of %s (id %s); '
                          'nothing was changed. Resolve the duplicate first'
                          % (ctx.target_aid, item.name, ', '.join(a.agent_id for a in existing)))
    return listing


def _read_snapshot(ctx, item):
    """Save the enabled state of every test of the cluster. Read-only."""
    snap = _Snapshot(item)
    for aid in _test_aids(item):
        listing = dict((str(t.test_id), t) for t in ctx.api.list_tests(aid))
        for test_id in item.tests_by_aid[aid]:
            test = listing.get(test_id)
            if test is None:
                snap.unknown.append((aid, test_id))
                continue
            if 'enabled' not in test.raw:  # list did not include the flag: read the test itself
                test = ctx.api.get_test(aid, test.test_type, test_id)
            if 'enabled' not in test.raw:
                snap.unknown.append((aid, test_id))
                continue
            snap.test_type[(aid, test_id)] = test.test_type
            snap.enabled[(aid, test_id)] = bool(test.enabled)
    return snap


def _snapshot_summary(snap):
    enabled = sum(1 for v in snap.enabled.values() if v)
    return '%d tests saved: %d enabled, %d disabled, %d unknown' % (
        len(snap.enabled) + len(snap.unknown), enabled, len(snap.enabled) - enabled, len(snap.unknown))


# --------------------------------------------------------------------------- steps
def _map_old_members(item, records, exclude_ids=()):
    """Map each old member to its standalone agent id by IP. Returns {memberId: agent id or None}.

    Never trusts memberId == agent id. Clusters are never candidates. When several records share the
    IP, the one with the member name wins, then the one whose id equals the member id; otherwise the
    member stays unmapped (None).
    """
    mapping, used = {}, set()
    for member in item.members:
        mid, ip, name = _member_id(member), _member_ip(member), _member_name(member).casefold()
        found = [r for r in records if not r.is_cluster and r.agent_id not in exclude_ids
                 and r.agent_id not in used and ip in r.ip_addresses]
        if len(found) > 1:
            named = [r for r in found if name in ((r.name or '').casefold(), (r.hostname or '').casefold())]
            if len(named) == 1:
                found = named
            else:
                same_id = [r for r in found if r.agent_id == mid]
                found = same_id if len(same_id) == 1 else found
        mapping[mid] = found[0].agent_id if len(found) == 1 else None
        if mapping[mid]:
            used.add(mapping[mid])
    return mapping


def _mapping_text(mapping):
    return ', '.join('%s -> %s' % (k, v if v else 'not mapped') for k, v in sorted(mapping.items()))


def _unassign(ctx, item, host, progress, member_ids):
    owner = progress.owner
    cluster_id = _cluster_id(item)
    ctx.console.info(host, MAINTENANCE_TEXT)
    detail = 'unassigned %d members %s from cluster %s in aid %s' % (
        len(member_ids), _ids_text(member_ids), cluster_id, owner)
    with ctx.step(host, STEP_UNASSIGN, detail, changed=True):
        progress.unassign_sent = True
        ctx.api.cluster_unassign(owner, cluster_id, member_ids)
        last = {}

        def probe():
            gone = True
            last['records'] = list(ctx.api.list_agents_with_clusters(owner))
            for rec in last['records']:
                if rec.agent_id == cluster_id and rec.is_cluster:
                    gone = False
            return gone, None

        done, _ = _poll_until(ctx, probe)
        if not done:
            raise FlowAborted('cluster %s is still listed in aid %s after the unassign; no member was reset'
                              % (cluster_id, owner))
        progress.unassigned = True
        # Read the owner group again and map each member IP to its standalone agent id (the first member may
        # keep the old cluster id; the member id is not trusted).
        progress.old_map = _map_old_members(item, last.get('records', []))
        progress.old_records = dict((r.agent_id, r) for r in last.get('records', []) if not r.is_cluster)
        ctx.console.info(host, 'old members as standalone agents in aid %s: %s' % (owner, _mapping_text(progress.old_map)))


def _member_identity(ctx, item, member, progress=None):
    """(names, ips) that identify the new agent of ``member``.

    Names: the inventory member name, and from the standalone record of the old member (found in the owner
    group after the unassign) its name and hostname. IPs: the inventory member IP, the UI address, and all
    IPs of that standalone record. The strict wait still needs an IP AND a name to match.
    """
    names = [_member_name(member)]
    ips = [_member_ip(member), _member_ui_ip(item, member)]
    mapping = (getattr(progress, 'old_map', None) or {})
    record = (getattr(progress, 'old_records', None) or {}).get(mapping.get(_member_id(member)))
    if record is not None:
        names.extend([record.name, record.hostname])
        ips.extend(record.ip_addresses or [])
    return _unique(n for n in names if n), _unique(i for i in ips if i)


def _ignore_ids_for_member(ctx, item, member, old_ids):
    """Ids that are not a new identity for ``member``: every old member id, the cluster id, anything listed now."""
    ids = set(old_ids)
    ip, name = _member_ip(member), _member_name(member)
    for aid in _source_aids(ctx, item):
        for agent in ctx.api.list_agents(aid):
            if _identity_matches(agent, ip, name):
                ids.add(agent.agent_id)
    return ids


def _require_no_new_source_identity(ctx, item, member, ignore_ids, identity=None):
    """Abort when the member registered in the source group again: a new online record with the member IP AND name."""
    names, ips = identity if identity is not None else _member_identity(ctx, item, member)
    for aid in _source_aids(ctx, item):
        unexpected = ThousandEyesApiClient.match_new_agents(ctx.api.list_agents(aid), names, ips, ignore_ids)
        if unexpected:
            raise FlowAborted(
                'wrong group: a new online agent appeared in source aid %s (agent id %s) for member %s after the '
                'target token was submitted; the token went to the wrong group. No further member was touched; '
                'nothing was assigned or deleted; manual action needed'
                % (aid, ', '.join(a.agent_id for a in unexpected), _member_label(member)))


def _wait_member_online(ctx, item, host, member, ignore_ids, taken_ids, target_known=(), identity=None):
    """Strict wait: id not seen before (target group before the first reset, old ids, earlier members), IP and name match.

    ``identity`` is ``(names, ips)`` from ``_member_identity``: the member name, plus the name, hostname and
    all IPs of the old standalone record. Without it only the member name and IP are accepted.
    """
    ip, name = _member_ip(member), _member_name(member)
    names, ips = identity if identity is not None else _member_identity(ctx, item, member)
    known = set(str(i) for i in target_known) | set(ignore_ids) | set(taken_ids)

    def on_poll(attempt, remaining):
        ctx.console.info(host, 'target API poll %s for %s; %.0fs remaining' % (attempt, _member_label(member), remaining))

    def not_visible():
        _require_no_new_source_identity(ctx, item, member, ignore_ids, (names, ips))

    waited = 0
    pause = ctx.poll_seconds if ctx.poll_seconds and ctx.poll_seconds > 0 else 1
    while True:
        try:
            agent = ctx.api.wait_for_agent(
                ctx.target_aid, hostname=name, ip_address=ip,
                timeout_seconds=ctx.timeout_seconds, poll_interval_seconds=ctx.poll_seconds,
                on_poll=on_poll, on_not_visible=not_visible,
                known_ids=known, alt_names=names, alt_ips=ips)
        except AmbiguousAgentMatch as exc:
            raise FlowAborted('more than one new online agent in target aid %s matches member %s (ids %s); the new '
                              'agent cannot be told apart. Nothing was assigned or deleted; manual action needed'
                              % (ctx.target_aid, _member_label(member), ', '.join(exc.agent_ids) or '?'))
        except VisibilityTimeout as exc:
            seen = [a for a in ctx.api.list_agents(ctx.target_aid)
                    if a.online and a.agent_id in known and _identity_matches(a, ip, name)]
            if seen:
                raise FlowAborted('target aid %s returns only known agent id %s for member %s; no new identity appeared'
                                  % (ctx.target_aid, ', '.join(a.agent_id for a in seen), _member_label(member)))
            raise FlowAborted('new agent for member %s did not appear online in target aid %s: %s'
                              % (_member_label(member), ctx.target_aid, exc))
        if agent.agent_id not in known:
            not_visible()
            return agent
        # A known identity is still visible in the target group. Keep waiting.
        if waited >= ctx.timeout_seconds:
            raise FlowAborted('target aid %s returns only known agent id %s for member %s; no new identity appeared'
                              % (ctx.target_aid, agent.agent_id, _member_label(member)))
        not_visible()
        ctx.sleep(pause)
        waited += pause


def _check_member_uis(ctx, item, host):
    """Apply only, BEFORE the unassign: open the UI of every member, log in and read the settings.

    Any failure raises FlowAborted before the first change. Prints one result line per member
    (``cluster_member_ui_check``). Returns {memberId: (browserbot, crash_reports)} for the later steps.
    """
    flags = {}
    total = len(item.members)
    for index, member in enumerate(item.members, 1):
        ip = _member_ui_ip(item, member)
        tag = 'member %d/%d %s' % (index, total, _member_label(member))
        with ctx.step(host, STEP_MEMBER_CHECK, '%s: agent UI at %s: login and settings read ok' % (tag, ip)):
            with _translate('%s: agent UI check failed at %s; nothing was changed' % (tag, ip)):
                with ctx.ui_factory(ip) as ui:
                    ui.authenticate()
                    before = ui.snapshot_state()
            flags[_member_id(member)] = (boolean_from_snapshot(before, 'browserbot', default=True),
                                         boolean_from_snapshot(before, 'crash_reports', default=True))
    return flags


def _migrate_member(ctx, item, host, progress, member, index, total, old_ids, taken_ids, target_known=(),
                    ui_flags=None):
    """UI sequence for one member. Returns the new agent id.

    ``ui_flags`` is the result of ``_check_member_uis``; a member found there is not read a second time.
    """
    ip = _member_ui_ip(item, member)
    label = _member_label(member)
    tag = 'member %d/%d %s' % (index, total, label)
    ignore_ids = None
    with ctx.ui_factory(ip) as ui:
        progress.current = '%s: UI login' % tag
        with ctx.step(host, STEP_MEMBER_AUTH, '%s: logged in to the agent UI at %s' % (tag, ip)):
            with _translate('%s: agent UI login failed' % tag):
                ui.authenticate()
        progress.current = '%s: UI snapshot' % tag
        with ctx.step(host, STEP_MEMBER_SNAPSHOT, '%s: agent UI settings captured' % tag) as captured:
            known_flags = (ui_flags or {}).get(_member_id(member))
            if known_flags is not None:
                browserbot, crash_reports = known_flags
                captured.detail = '%s: agent UI settings kept from the check before the unassign' % tag
            else:
                with _translate('%s: agent UI snapshot failed' % tag):
                    before = ui.snapshot_state()
                browserbot = boolean_from_snapshot(before, 'browserbot', default=True)
                crash_reports = boolean_from_snapshot(before, 'crash_reports', default=True)
        ignore_ids = _ignore_ids_for_member(ctx, item, member, old_ids)

        progress.current = '%s: reset command in flight (reset state unknown)' % tag
        progress.reset_started.append(label)
        with ctx.step(host, STEP_MEMBER_RESET, '%s: agent %s reset' % (tag, ip), changed=True):
            with _translate('%s: agent reset failed' % tag):
                ui.reset_agent()

        progress.current = '%s: waiting for the UI after the reset' % tag
        with ctx.step(host, STEP_MEMBER_REACHABLE, '%s: agent UI at %s answers' % (tag, ip)):
            reachable, _ = _poll_until(ctx, lambda: (bool(ui.is_reachable()), None))
            if not reachable:
                raise FlowAborted('%s: agent UI at %s is unreachable after the reset' % (tag, ip))

        progress.current = '%s: UI login after the reset' % tag
        with ctx.step(host, STEP_MEMBER_AUTH_AFTER, '%s: logged in again after the reset' % tag):
            def try_login():
                try:
                    ui.authenticate()
                except (UiError, AuthenticationError) as exc:
                    return False, exc
                return True, None
            logged_in, last_error = _poll_until(ctx, try_login)
            if not logged_in:
                raise FlowAborted('%s: login after the reset failed (%s)' % (tag, last_error))

        progress.current = '%s: submitting the target token (may or may not be set)' % tag
        with ctx.step(host, STEP_MEMBER_TOKEN,
                      '%s: target token of aid %s submitted (value hidden)' % (tag, ctx.target_aid), changed=True):
            try:
                ui.set_account_group_token(ctx.target_account_token, browserbot=browserbot,
                                           crash_reports=crash_reports)
            except (UiError, AuthenticationError) as exc:
                raise FlowAborted('%s: token submission failed (%s); the agent was reset; the token may or may '
                                  'not be set; manual action needed' % (tag, exc))
        progress.token_sent.append(label)

    progress.current = '%s: waiting for the new agent in target aid %s' % (tag, ctx.target_aid)
    with ctx.step(host, STEP_MEMBER_ONLINE, '%s: new agent online in aid %s' % (tag, ctx.target_aid)):
        agent = _wait_member_online(ctx, item, host, member, ignore_ids, taken_ids, target_known,
                                    _member_identity(ctx, item, member, progress))
    progress.new_ids.append(agent.agent_id)
    progress.current = ''
    return agent.agent_id


def _member_ids_of(record):
    ids = set()
    for member in record.cluster_members:
        value = _member_id(member)
        if value:
            ids.add(value)
    return ids


def _find_new_cluster(ctx, base_id):
    for rec in ctx.api.list_agents_with_clusters(ctx.target_aid):
        if rec.is_cluster and (rec.agent_id == base_id or base_id in _member_ids_of(rec)):
            return rec
    return None


def _cluster_problems(record, new_ids):
    """List of problems that keep the new cluster from being complete and online."""
    problems = []
    if record is None:
        return ['no cluster with base agent in the target group']
    members = dict((_member_id(m), m) for m in record.cluster_members)
    missing = [i for i in new_ids if i not in members]
    if missing:
        problems.append('agents not in the cluster: %s' % _ids_text(missing))
    offline = [i for i in new_ids if i in members and not _member_online(members[i])]
    if offline:
        problems.append('members not online: %s' % _ids_text(offline))
    return problems


def _build_cluster(ctx, item, host, progress, new_ids):
    base, others = new_ids[0], new_ids[1:]
    detail = 'cluster built in aid %s: base agent %s + members %s' % (ctx.target_aid, base, _ids_text(others))
    with ctx.step(host, STEP_ASSIGN, detail, changed=True):
        progress.cluster_assign_sent = True
        ctx.api.cluster_assign(ctx.target_aid, base, others)

        def probe():
            record = _find_new_cluster(ctx, base)
            return (record is not None and not _cluster_problems(record, new_ids)), record

        done, record = _poll_until(ctx, probe)
        if not done:
            raise FlowAborted('new cluster is not complete in target aid %s: %s; old members were NOT deleted'
                              % (ctx.target_aid, '; '.join(_cluster_problems(record, new_ids))))
        progress.cluster_id = record.agent_id
        return record.agent_id


def _share_back(ctx, item, host, progress, new_cluster_id):
    wanted = _desired_groups(item, ctx.target_aid)
    if not wanted:
        raise FlowAborted('refusing to send an empty account group list')
    if len(wanted) == 1:
        ctx.skipped(host, STEP_SHARE_BACK, 'no other group saw the cluster; nothing to share back')
        progress.shared = True
        return
    # The lab must prove whether PUT replaces or adds: always send the FULL list, never an empty one.
    with ctx.step(host, STEP_SHARE_BACK, 'shared cluster %s with aids %s' % (new_cluster_id, _format_aids(wanted)),
                  changed=True):
        ctx.api.set_agent_account_groups(ctx.target_aid, new_cluster_id, wanted)

        def probe():
            record = ctx.api.get_agent(ctx.target_aid, new_cluster_id)
            have = set(str(a) for a in record.shared_aids) | set([str(ctx.target_aid)])
            return (not [a for a in wanted if a not in have]), have

        done, have = _poll_until(ctx, probe)
        if not done:
            missing = [a for a in wanted if a not in have]
            raise FlowAborted('cluster %s is not shared with aids %s after the PUT; old members were NOT deleted'
                              % (new_cluster_id, _format_aids(missing)))
        progress.shared = True


def _assign_tests(ctx, item, host, progress, new_cluster_id):
    aids = _test_aids(item)
    if not aids:
        ctx.skipped(host, STEP_ASSIGN_TESTS, 'no tests to assign')
        progress.tests_verified = True
        return
    detail = 'assigned %d tests in %d groups to cluster %s and verified' % (_tests_total(item), len(aids), new_cluster_id)
    with ctx.step(host, STEP_ASSIGN_TESTS, detail, changed=True):
        for aid in aids:
            ids = item.tests_by_aid[aid]
            chunks = _chunks(ids, ASSIGN_CHUNK_SIZE)
            for number, chunk in enumerate(chunks, 1):
                ctx.api.assign_tests(aid, new_cluster_id, chunk)
                progress.assigned_count += len(chunk)
                if len(chunks) > 1:
                    ctx.console.info(host, 'aid %s: assign call %d/%d done (%d of %d tests)'
                                     % (aid, number, len(chunks), min(number * ASSIGN_CHUNK_SIZE, len(ids)), len(ids)))
            progress.assigned_groups.append(aid)
        for aid in aids:
            needed = set(item.tests_by_aid[aid])

            def probe(aid=aid, needed=needed):
                have = set()
                for record in ctx.api.list_agents_with_clusters(aid):
                    if record.agent_id == new_cluster_id:
                        have = set(str(t) for t in record.test_ids)
                        break
                return needed <= have, have

            done, have = _poll_until(ctx, probe)
            if not done:
                missing = sorted(needed - have)
                raise FlowAborted('aid %s: %d of %d tests are not assigned to cluster %s (%s); old members were '
                                  'NOT deleted' % (aid, len(missing), len(needed), new_cluster_id,
                                                   ','.join(missing[:10])))
        progress.tests_verified = True


def _id_lines(ids):
    """Chunks of at most IDS_PER_LINE ids, each as one comma separated string."""
    ids = list(ids)
    return [','.join(ids[i:i + IDS_PER_LINE]) for i in range(0, len(ids), IDS_PER_LINE)]


def _manual_enable_task(ctx, item, host, progress, snapshot, warnings):
    """Default mode: no write. List, per group, the test ids that were enabled before and are disabled now.

    Reads the tests again (GET). Prints at most IDS_PER_LINE ids on one line and a count per group. The same
    lists are in the snapshot file and in the RECOVERY DATA line. Returns True (a manual task is not a failure).
    """
    if snapshot.unknown:
        text = 'enabled state unknown (not touched; check by hand): %s' % ', '.join(
            'aid %s test %s' % pair for pair in snapshot.unknown)
        warnings.append(text)
        ctx.console.info(host, 'warning: ' + text)
    todo = []   # (aid, [ids disabled now], note)
    for aid in _test_aids(item):
        before = snapshot.enabled_ids(aid, True)
        if not before:
            continue
        try:
            current = dict((str(t.test_id), t) for t in ctx.api.list_tests(aid))
        except Exception as exc:  # recorded and printed, never silent: list every id that was enabled before
            todo.append((aid, list(before), 'could not read the tests now (%s); check each one' % exc))
            continue
        off = [t for t in before if t not in current or not current[t].enabled]
        if off:
            missing = [t for t in off if t not in current]
            note = '' if not missing else '%d not found in the group listing' % len(missing)
            todo.append((aid, off, note))
    if not todo:
        ctx.skipped(host, STEP_RESTORE_ENABLED, 'no test was enabled before and is disabled now; nothing to enable by hand')
        progress.enabled_done = True
        return True
    progress.enabled_manual = True
    total = sum(len(ids) for _, ids, _ in todo)
    for aid, ids, note in todo:
        lines = _id_lines(ids)
        ctx.console.info(host, 'MANUAL TASK: enable by hand in aid %s: %d tests were enabled before and are disabled '
                         'now%s' % (aid, len(ids), '; ' + note if note else ''))
        for number, line in enumerate(lines, 1):
            ctx.console.info(host, 'MANUAL TASK: aid %s test ids (%d/%d): %s' % (aid, number, len(lines), line))
        warnings.append('manual task: %d tests in aid %s were enabled before and are disabled now (enable by hand)'
                        % (len(ids), aid))
    ctx.console.status(StepStatus.CHANGED, host, STEP_RESTORE_ENABLED,
                       'MANUAL TASK, the tool did not enable tests (auto_reenable_tests is off): %d tests in %d groups '
                       'were enabled before and are disabled now; ids are listed above and in the snapshot'
                       % (total, len(todo)))
    return True


def _restore_enabled(ctx, item, host, progress, snapshot, warnings):
    """Enable again each test that was enabled before and is disabled now.

    ``ctx.auto_reenable_tests``: False (default) = manual task only, no write; True = ``set_test_enabled`` (full
    payload PUT); 'minimal' = ``set_test_enabled_minimal`` (PUT of ``{"enabled": true}``, experimental).
    Failure = loud warning, not fatal.
    """
    mode = getattr(ctx, 'auto_reenable_tests', False)
    if not mode:
        return _manual_enable_task(ctx, item, host, progress, snapshot, warnings)
    writer = ctx.api.set_test_enabled_minimal if mode == 'minimal' else ctx.api.set_test_enabled
    wanted = []
    for aid in _test_aids(item):
        wanted.extend((aid, t) for t in snapshot.enabled_ids(aid, True))
    if snapshot.unknown:
        text = 'enabled state unknown (not touched; check by hand): %s' % ', '.join(
            'aid %s test %s' % pair for pair in snapshot.unknown)
        warnings.append(text)
        ctx.console.info(host, 'warning: ' + text)
    if not wanted:
        ctx.skipped(host, STEP_RESTORE_ENABLED, 'no test was enabled before; nothing to re-enable')
        progress.enabled_done = True
        return True
    failed = []
    changed = 0
    for aid in sorted(set(a for a, _ in wanted), key=_aid_key):
        ids = [t for a, t in wanted if a == aid]
        try:
            current = dict((str(t.test_id), t) for t in ctx.api.list_tests(aid))
        except Exception as exc:  # recorded and printed, never silent
            failed.extend((aid, t, 'could not read tests: %s' % exc) for t in ids)
            continue
        for test_id in ids:
            test = current.get(test_id)
            if test is None:
                failed.append((aid, test_id, 'test not found in aid %s' % aid))
                continue
            if test.enabled:
                continue
            try:
                writer(aid, snapshot.test_type[(aid, test_id)], test_id, True)
                changed += 1
            except Exception as exc:
                failed.append((aid, test_id, str(exc)))
        # verify
        pending = [t for t in ids if t not in [f[1] for f in failed if f[0] == aid]]

        def probe(aid=aid, pending=pending):
            now = dict((str(t.test_id), t) for t in ctx.api.list_tests(aid))
            off = [t for t in pending if t in now and not now[t].enabled]
            return not off, off

        try:
            done, off = _poll_until(ctx, probe)
        except Exception as exc:
            done, off = False, [t for t in pending]
            ctx.console.info(host, 'warning: could not verify enabled state in aid %s: %s' % (aid, exc))
        if not done:
            failed.extend((aid, t, 'still disabled after the update') for t in off)
    if failed:
        text = 'tests NOT enabled again (enable by hand): %s' % '; '.join(
            'aid %s test %s (%s)' % item for item in failed)
        warnings.append(text)
        ctx.console.status(StepStatus.FAILED, host, STEP_RESTORE_ENABLED,
                           '%d of %d tests are not enabled again: %s' % (
                               len(failed), len(wanted), ', '.join('%s/%s' % (a, t) for a, t, _ in failed)))
        ctx.console.info(host, 'warning: ' + text)
        return False
    ctx.console.status(StepStatus.CHANGED if changed else StepStatus.OK, host, STEP_RESTORE_ENABLED,
                       '%d tests were enabled before; %d had to be enabled again%s' % (
                           len(wanted), changed, ' (minimal PUT, experimental)' if mode == 'minimal' else ''))
    progress.enabled_done = True
    return True


def _delete_old(ctx, item, host, progress, new_ids, new_cluster_id, warnings=None):
    """Delete the old members as standalone agents. Ids come from the owner group listing (map by IP)."""
    owner = progress.owner
    known_new = set(new_ids) | set([new_cluster_id])
    warnings = warnings if warnings is not None else []
    with ctx.step(host, STEP_DELETE_OLD, 'deleted old member agents in aid %s' % owner, changed=True) as out:
        # Gate: the new cluster is complete and online and the tests are verified.
        if not progress.tests_verified:
            raise FlowAborted('tests are not verified; old members were NOT deleted')
        problems = _cluster_problems(_find_new_cluster(ctx, new_ids[0]), new_ids)
        if problems:
            raise FlowAborted('new cluster is not complete and online (%s); old members were NOT deleted'
                              % '; '.join(problems))
        records = list(ctx.api.list_agents_with_clusters(owner))
        present = dict((r.agent_id, r) for r in records)
        mapping = progress.old_map
        if mapping is None:  # not mapped right after the unassign: map from the current listing, never a new id
            mapping = _map_old_members(item, records, known_new)
            progress.old_map = mapping
        member_text = '; '.join('%s %s' % (_member_id(m), _member_label(m)) for m in item.members)
        unmapped = [_member_label(m) for m in item.members if not mapping.get(_member_id(m))]
        same_as_new = [v for v in mapping.values() if v and v in known_new]
        targets = _unique(v for v in mapping.values() if v and v not in known_new)
        if same_as_new:
            ctx.console.info(host, 'not deleted (same id as a new agent): %s' % _ids_text(same_as_new))
        if unmapped:
            text = ('old members not mapped to a standalone agent id, NOT deleted (delete by hand): %s; members: %s'
                    % (', '.join(unmapped), member_text))
            warnings.append(text)
            ctx.console.info(host, 'warning: ' + text)
        if not targets:
            out.status = StepStatus.SKIPPED
            out.detail = ('no old member agent was deleted (%s)'
                          % ('same id as a new agent' if same_as_new and not unmapped
                             else 'none could be mapped to a standalone agent id'))
            return
        done = []
        for old_id in targets:
            record = present.get(old_id)
            if record is None:
                ctx.console.info(host, 'old member %s is already absent from aid %s' % (old_id, owner))
                continue
            if record.is_cluster:
                raise FlowAborted('refusing to delete id %s in aid %s: it is a cluster, and deleting a cluster '
                                  'deletes its members; delete the old members by hand' % (old_id, owner))
            agent_type = _delete_type(ctx, owner, record, old_id)
            progress.delete_sent.append(old_id)
            ctx.api.delete_agent(owner, old_id, agent_type=agent_type)
            ctx.api.wait_for_agent_absent(owner, agent_id=old_id, timeout_seconds=ctx.timeout_seconds,
                                          poll_interval_seconds=ctx.poll_seconds)
            progress.delete_sent.remove(old_id)
            progress.deleted.append(old_id)
            done.append(old_id)
        if not done:
            out.status = StepStatus.OK  # nothing was sent: every mapped member was already absent
        out.detail = 'deleted old member agents %s in aid %s%s' % (
            _ids_text(done), owner, '; %d not mapped, see warnings' % len(unmapped) if unmapped else '')


def _rename(ctx, item, host, progress, new_cluster_id):
    with ctx.step(host, STEP_RENAME, 'cluster %s name is %r' % (new_cluster_id, item.name)) as outcome:
        current = ctx.api.get_agent(ctx.target_aid, new_cluster_id)
        if current.name != item.name:
            outcome.status = StepStatus.CHANGED  # a write is sent; an equal name is a read-only check (ok)
            ctx.api.update_agent_name(ctx.target_aid, new_cluster_id, item.name)

        def probe():
            return ctx.api.get_agent(ctx.target_aid, new_cluster_id).name == item.name, None

        done, _ = _poll_until(ctx, probe)
        if not done:
            raise FlowAborted('cluster %s did not take the name %r' % (new_cluster_id, item.name))
        progress.renamed = True


def _restore_tags(ctx, item, host, new_cluster_id, warnings):
    if not item.tags:
        ctx.skipped(host, STEP_RESTORE_TAGS, 'no tags to restore')
        return
    done = 0
    for tag in item.tags:
        label = '%s (aid %s)' % (tag.get('key', tag.get('tagId', '?')), tag.get('aid', '?'))
        try:
            ctx.api.assign_tag(tag['aid'], tag['tagId'], [{'id': new_cluster_id, 'type': 'v-agent'}])
            done += 1
        except Exception as exc:  # non-fatal by design; recorded as warning, never silent
            text = 'tag restore failed for %s: %s' % (label, exc)
            warnings.append(text)
            ctx.console.info(host, 'warning: ' + text)
    ctx.console.status(StepStatus.CHANGED if done else StepStatus.OK, host, STEP_RESTORE_TAGS,
                       'restored %d of %d tags on cluster %s' % (done, len(item.tags), new_cluster_id))


# --------------------------------------------------------------------------- flow
def _snapshot_text(ctx):
    if ctx.snapshot_path:
        return ctx.snapshot_path
    if ctx.snapshot_dir:
        return 'not written yet (directory %s)' % ctx.snapshot_dir
    return 'none (no snapshot directory set)'


def _ids_summary(ctx, item, progress):
    """Old and new ids for the stop line: old cluster, new member agents, new cluster, final share set."""
    return 'old cluster id: %s | new agent ids: %s | new cluster id: %s | final share set: %s' % (
        _cluster_id(item), _ids_text(progress.new_ids) if progress.new_ids else 'none yet',
        progress.cluster_id or 'not known yet', _format_aids(_desired_groups(item, ctx.target_aid)))


def _stop_line(ctx, item, progress, exc):
    text = 'stopped; state: %s | %s | snapshot file: %s | RECOVERY DATA: %s' % (
        progress.describe(), _ids_summary(ctx, item, progress), _snapshot_text(ctx),
        _recovery_text(item, progress.snapshot))
    if not isinstance(exc, Exception):
        text += ' | interrupted by %s' % exc.__class__.__name__
    return text


def migrate_cluster(ctx, item):
    """Run the Strategy D cluster flow for one ready cluster. Returns FlowResult.

    A flow that stops re-raises its exception with ``new_agent_id`` (first new member agent, or None),
    ``new_cluster_id`` (None until the new cluster is confirmed), ``snapshot`` and ``state`` set as attributes
    (the runner reads them with ``getattr``).
    """
    host = item.name
    progress = _Progress(item, str(getattr(item, 'owner_aid', '') or ctx.source_aid), ctx.target_aid)
    ctx.snapshot_path = None
    try:
        return _run(ctx, item, host, progress)
    except BaseException as exc:  # includes KeyboardInterrupt and SystemExit: print the state, then re-raise
        state = progress.describe()
        ctx.console.info(host, _stop_line(ctx, item, progress, exc))
        attach_stop_info(exc, new_agent_id=progress.new_ids[0] if progress.new_ids else None,
                         new_cluster_id=progress.cluster_id, snapshot=ctx.snapshot_path, state=state)
        raise


def _run(ctx, item, host, progress):
    with ctx.step(host, STEP_PREFLIGHT, _snapshot_detail(item)):
        _validate(ctx, item)
        member_ids = [_member_id(m) for m in item.members]
        # GET-only guards. Dry run and apply run the same checks.
        target_agents = _require_clean_target(ctx, item, set(member_ids) | set([_cluster_id(item)]))
        target_known = set(a.agent_id for a in target_agents)
        progress.snapshot = _read_snapshot(ctx, item)
        ctx.console.info(host, 'snapshot: ' + _snapshot_summary(progress.snapshot))

    ui_flags = {}
    if ctx.apply:
        # Every member UI must work BEFORE the first change. A wrong password or a blocked port 443 on one
        # member would otherwise leave the cluster taken apart.
        ui_flags = _check_member_uis(ctx, item, host)
        extra = progress.snapshot.export()
        extra['ui_settings'] = dict((mid, {'browserbot': flags[0], 'crash_reports': flags[1]})
                                    for mid, flags in ui_flags.items())
        # The file goes to disk before the first destructive call (the cluster unassign).
        with ctx.step(host, STEP_SNAPSHOT_FILE, 'snapshot file written before the unassign') as written:
            try:
                path = ctx.write_snapshot(item, host, extra)
            except OSError as exc:
                raise FlowAborted('snapshot file could not be written (%s); nothing was changed' % exc)
            if path is None:
                written.status = StepStatus.SKIPPED
                written.detail = 'no snapshot directory set; no snapshot file is written'

    if not ctx.apply:
        ctx.console.info(host, 'dry run check: no online agent with a member IP of %s in target aid %s; %d agents seen there'
                         % (item.name, ctx.target_aid, len(target_agents)))
        if ctx.snapshot_dir:
            ctx.console.info(host, 'dry run: a snapshot file would be written to %s before the unassign' % ctx.snapshot_dir)
        else:
            ctx.console.info(host, 'dry run: no snapshot directory set; no snapshot file would be written')
        for step_name, detail in plan_cluster(item, ctx.target_aid, getattr(ctx, 'auto_reenable_tests', False))[1:]:
            if step_name == STEP_MANUAL_RULES:
                ctx.skipped(host, step_name, detail)
            else:
                ctx.planned(host, step_name, detail)
        return FlowResult(host, True, 'dry-run')

    warnings = []
    _unassign(ctx, item, host, progress, member_ids)

    new_ids = []
    total = len(item.members)
    for index, member in enumerate(item.members, 1):
        new_ids.append(_migrate_member(ctx, item, host, progress, member, index, total,
                                       set(member_ids) | set([_cluster_id(item)]), set(new_ids), target_known,
                                       ui_flags))
    if len(set(new_ids)) != len(new_ids):
        raise FlowAborted('two members resolved to the same new agent id %s; nothing was assigned' % _ids_text(new_ids))

    new_cluster_id = _build_cluster(ctx, item, host, progress, new_ids)
    _share_back(ctx, item, host, progress, new_cluster_id)
    _assign_tests(ctx, item, host, progress, new_cluster_id)
    enabled_ok = _restore_enabled(ctx, item, host, progress, progress.snapshot, warnings)
    if not enabled_ok:
        # Do not hide it: same data as the line printed when the flow stops with an exception.
        ctx.console.info(host, 'RECOVERY DATA (some tests are NOT enabled again; the flow goes on): %s | %s | '
                         'snapshot file: %s' % (_recovery_text(item, progress.snapshot),
                                                _ids_summary(ctx, item, progress), _snapshot_text(ctx)))

    _delete_old(ctx, item, host, progress, new_ids, new_cluster_id, warnings)
    _rename(ctx, item, host, progress, new_cluster_id)
    _restore_tags(ctx, item, host, new_cluster_id, warnings)
    ctx.skipped(host, STEP_MANUAL_RULES, MANUAL_RULES_TEXT)
    progress.done = True
    detail = 'migrated: old cluster %s -> new cluster %s (members %s)' % (
        _cluster_id(item), new_cluster_id, _ids_text(new_ids))
    if not enabled_ok:
        detail += '; SOME TESTS ARE NOT ENABLED AGAIN, see the warnings'
    elif progress.enabled_manual:
        detail += '; TESTS TO ENABLE BY HAND, see the MANUAL TASK lines'
    return FlowResult(host, bool(enabled_ok), detail, new_agent_id=new_ids[0], new_cluster_id=new_cluster_id,
                      warnings=warnings, snapshot=ctx.snapshot_path)
