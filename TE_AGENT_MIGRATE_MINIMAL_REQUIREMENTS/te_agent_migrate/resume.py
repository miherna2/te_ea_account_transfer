"""Finish a migration that stopped half way (a timeout, a closed window, a network drop).

Before a device is reset the tool saves a snapshot of it (tests, groups, tags). When a run stops after the
new agent came online, the snapshot and the live data say what is left:

* the new agent is in the destination group (same IP, a different id than the old one),
* and something is missing: a share, a test, old-agent retirement, the name or a tag.

The next run finds this by itself, tells the operator, and in ``--apply`` finishes it after one question.
Every step reads first and only does what is missing, so running it twice is safe. It adds tests, it never
removes one. It disables and marks the verified old registration instead of deleting it.
"""
import ipaddress
import json
import os

from te_agent_migrate.errors import ApiError
from te_agent_migrate.flow.context import FlowAborted, FlowResult, PreflightItem
from te_agent_migrate.flow.preflight import source_interface_problem
from te_agent_migrate.flow.standalone import (
    _State, _assign_tests, _retire_old, _desired_groups, _rename, _repoint_a2a, _restore_tags, _share_back,
    _verify_enabled, _retirement_complete, retired_agent_name,
)
from te_agent_migrate.models import InventoryAgent

SNAPSHOT_FOLDERS = ('apply', 'runs')   # under work/: where snapshots are written (current and older versions)
MAX_DEPTH = 4


def _norm(value):
    return str(value if value is not None else '').strip().casefold()


def _ip(value):
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return ''


def _scalar(value):
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ''


def _snapshot_problem(snap, old_id):
    """Validate only the saved fields that recovery uses to identify a device or send a write."""
    if not isinstance(snap, dict) or not isinstance(snap.get('record'), dict):
        return 'record is not an object'
    record = snap['record']
    if not _scalar(old_id).isdigit() or _scalar(record.get('agent_id')) != str(old_id):
        return 'old agent id is missing or inconsistent'
    source, target = _scalar(snap.get('source_aid')), _scalar(snap.get('target_aid'))
    if not source.isdigit() or not target.isdigit() or source == target:
        return 'source and destination must be distinct account group ids'
    if snap.get('owner_aid') and _scalar(snap['owner_aid']) != source:
        return 'owner does not match the source account group'
    if not isinstance(snap.get('name'), str) or not snap['name'].strip():
        return 'device name is missing'
    ips = record.get('ip_addresses')
    if not isinstance(ips, list) or not ips or any(not _ip(value) for value in ips):
        return 'recorded IP addresses are missing or invalid'
    if not isinstance(snap.get('inventory', {}), dict):
        return 'inventory is not an object'
    shares, tests = snap.get('share_aids', []), snap.get('tests_by_aid')
    if not isinstance(shares, list) or any(not _scalar(aid).isdigit() for aid in shares):
        return 'share_aids is not a list of account group ids'
    if not isinstance(tests, dict):
        return 'tests_by_aid is not an object'
    for aid, ids in tests.items():
        if not _scalar(aid).isdigit() or not isinstance(ids, list) or any(not _scalar(t) for t in ids):
            return 'tests_by_aid contains invalid account group or test ids'
    tags, targets = snap.get('tags', []), snap.get('a2a_targets', [])
    if not isinstance(tags, list) or any(not isinstance(t, dict) or not _scalar(t.get('aid')).isdigit()
                                         or not _scalar(t.get('tagId')) for t in tags):
        return 'tags contains invalid assignments'
    if not isinstance(targets, list) or any(
            not isinstance(t, dict) or not _scalar(t.get('aid')).isdigit() or not _scalar(t.get('test_id'))
            or _scalar(t.get('old_target_agent_id')) != str(old_id) for t in targets):
        return 'a2a_targets contains invalid dependencies'
    enabled = snap.get('test_enabled')
    if enabled is not None and (not isinstance(enabled, dict) or any(
            not _scalar(aid).isdigit() or not isinstance(flags, dict)
            or any(not _scalar(t) or (flag is not None and not isinstance(flag, bool))
                   for t, flag in flags.items()) for aid, flags in enabled.items())):
        return 'test_enabled contains invalid saved states'
    replacement = snap.get('replacement')
    if replacement is not None and (
            not isinstance(replacement, dict)
            or not _scalar(replacement.get('agent_id')).isdigit()
            or _scalar(replacement.get('agent_id')) == str(old_id)
            or _scalar(replacement.get('target_aid')) != target
            or not _scalar(replacement.get('name'))
            or not isinstance(replacement.get('ip_addresses'), list)
            or not replacement['ip_addresses']
            or any(not _ip(ip) for ip in replacement['ip_addresses'])):
        return 'replacement checkpoint is invalid or belongs to another destination'
    return None


def _refuse(name, path, reason):
    raise FlowAborted('%s: cannot safely resume snapshot %s: %s; no recovery changes were made'
                      % (name, path, reason))


def snapshot_files(work_dir):
    """Paths of the snapshot files under work/apply and work/runs, newest first."""
    found = []
    for folder in SNAPSHOT_FOLDERS:
        base = os.path.join(str(work_dir), folder)
        if not os.path.isdir(base):
            continue
        base_depth = base.rstrip(os.sep).count(os.sep)
        for directory, subdirs, files in os.walk(base):
            if directory.count(os.sep) - base_depth >= MAX_DEPTH:
                del subdirs[:]
            if os.path.basename(directory) != 'snapshots':
                continue
            for name in files:
                if name.endswith('.json'):
                    path = os.path.join(directory, name)
                    try:
                        found.append((os.path.getmtime(path), path))
                    except OSError as exc:
                        raise FlowAborted('cannot inspect saved snapshot %s (%s); recovery must be checked manually'
                                          % (path, type(exc).__name__))
    return [path for _, path in sorted(found, reverse=True)]


def load_snapshots(work_dir, snapshot_dir=None):
    """{old agent id: (path, data)}, newest first. Damaged saved recovery state blocks a new reset."""
    out = {}
    try:
        paths = snapshot_files(work_dir)
        if snapshot_dir is not None and os.path.isdir(str(snapshot_dir)):
            paths.extend(os.path.join(str(snapshot_dir), name) for name in os.listdir(str(snapshot_dir))
                         if name.endswith('.json'))
        paths = sorted(set(os.path.abspath(path) for path in paths), key=os.path.getmtime, reverse=True)
    except OSError as exc:
        raise FlowAborted('cannot inspect saved recovery state (%s); no new migration is allowed'
                          % type(exc).__name__)
    for path in paths:
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
            old_id = str(data['record']['agent_id'])
            data['target_aid'], data['name'], data['tests_by_aid']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FlowAborted('cannot read saved snapshot %s (%s); restore or inspect it before starting a new migration'
                              % (path, type(exc).__name__))
        inventory = data.get('inventory')
        if isinstance(inventory, dict) and inventory.get('kind') == 'cluster':
            continue  # clusters are not finished automatically
        problem = _snapshot_problem(data, old_id)
        if problem:
            _refuse('saved recovery state', path, problem)
        out.setdefault(old_id, (path, data))
    return out


class Interrupted(object):
    """One half-finished migration and what is left to do."""

    def __init__(self, name, old_id, new_id, snapshot, path, old_record, new_record, item, pending):
        self.name = name
        self.old_id = old_id
        self.new_id = new_id
        self.snapshot = snapshot
        self.path = path
        self.old_record = old_record
        self.new_record = new_record
        self.item = item
        self.pending = pending
        self.target_aid = str(snapshot['target_aid'])

    def describe(self):
        return '%s: source %s -> destination %s; old agent %s, new agent %s; left to do: %s' % (
            self.name, self.snapshot['source_aid'], self.target_aid,
            self.old_id if self.old_record is not None else '%s (gone)' % self.old_id,
            self.new_id, '; '.join(self.pending))


def _ips(record):
    return set(_ip(ip) for ip in (getattr(record, 'ip_addresses', None) or []) if _ip(ip))


def _identity_matches(record, snap, ip):
    old, inventory = snap['record'], snap.get('inventory') or {}
    names = set(_norm(value) for value in (snap['name'], old.get('name'), old.get('hostname'),
                                          inventory.get('hostname')) if _scalar(value))
    return (not record.is_cluster and _ip(ip) in _ips(record)
            and bool(names & set([_norm(record.name), _norm(record.hostname)])))


def _old_identity_matches(record, snap, ip):
    old_id = str(snap['record']['agent_id'])
    if str(record.agent_id) != old_id:
        return False
    if _identity_matches(record, snap, ip):
        return True
    return (not record.is_cluster and str(record.agent_type or '').casefold() == 'enterprise'
            and record.raw.get('enabled') is False and record.name == retired_agent_name(old_id)
            and _ip(ip) in _ips(record) and _ip(ip) in set(_ip(value) for value in snap['record']['ip_addresses']))


def _live_shares(record, target, label='new'):
    """Read the complete current share list before any authoritative share PUT."""
    groups = (getattr(record, 'raw', None) or {}).get('accountGroups')
    if not isinstance(groups, list):
        raise FlowAborted('%s agent accountGroups is unreadable; no recovery changes were made' % label)
    have = set([str(target)])
    for group in groups:
        value = group.get('aid', group.get('accountGroupId', group.get('id'))) if isinstance(group, dict) else group
        if not _scalar(value).isdigit():
            raise FlowAborted('%s agent accountGroups contains an invalid group; no recovery changes were made' % label)
        have.add(_scalar(value))
    return have


def _replacement_matches(record, snap, ip):
    saved = snap.get('replacement')
    if saved is None:
        return _identity_matches(record, snap, ip)
    return (str(record.agent_id) == str(saved['agent_id'])
            and str(record.agent_type).casefold() == 'enterprise'
            and _ip(ip) in _ips(record)
            and _ip(ip) in set(_ip(value) for value in saved['ip_addresses'])
            and (_identity_matches(record, snap, ip)
                 or _norm(record.name) == _norm(saved['name'])
                 or (saved.get('hostname') and _norm(record.hostname) == _norm(saved['hostname']))))


def _tag_on(api, tag, new_id):
    for entry in api.list_tags(tag['aid'], expand_assignments=True):
        if str(entry.get('tagId', entry.get('id'))) == str(tag['tagId']):
            return any(str(a.get('id')) == str(new_id) and _norm(a.get('type')) == 'v-agent'
                       for a in (entry.get('assignments') or []))
    return False


def _item_from_snapshot(snap, old_record, wanted, tags):
    inventory = snap.get('inventory') or {}
    old = snap['record']
    ip = (snap.get('ui_ip') or inventory.get('ip_address') or (old.get('ip_addresses') or [''])[0])
    agent = InventoryAgent(snap['name'], ip, kind='agent', agent_id=str(old['agent_id']),
                           target_aid=snap['target_aid'])
    item = PreflightItem(agent, record=old_record, status='ready', owner_aid=snap.get('owner_aid') or '',
                         share_aids=snap.get('share_aids') or [], tests_by_aid=wanted, tags=tags,
                         target_aid=snap['target_aid'])
    item.ui_ip = ip
    item.test_enabled = snap.get('test_enabled') or {}
    item.a2a_targets = list(snap.get('a2a_targets') or [])
    if old_record is not None:
        item.share_aids = sorted(set(str(a) for a in item.share_aids)
                                 | _live_shares(old_record, snap['source_aid'], 'old'))
    return item


def _wanted_tests(api, snap, old_record, old_id):
    """Tests the new agent must have, per group: the snapshot, plus anything the old agent still has."""
    wanted = dict((str(aid), [str(t) for t in tests]) for aid, tests in snap['tests_by_aid'].items() if tests)
    if old_record is not None:
        aids = set(str(a) for a in list(snap.get('share_aids') or [])
                   + list(snap['tests_by_aid']) + [snap['source_aid'], snap['target_aid']])
        aids.update(_live_shares(old_record, snap['source_aid'], 'old'))
        for aid in sorted(aids):
            held = set()
            try:
                for record in api.list_agent_identities(aid, assignment_ids=[old_id]):
                    if str(record.agent_id) == old_id:
                        if not record.test_ids_known:
                            raise FlowAborted('old agent assignments are unreadable in group %s' % aid)
                        held = set(str(t) for t in record.test_ids)
            except ApiError as exc:
                raise FlowAborted('cannot read old agent tests in group %s (%s); no recovery changes were made'
                                  % (aid, exc))
            extra = sorted(held - set(wanted.get(str(aid), [])))
            if extra:
                wanted[str(aid)] = wanted.get(str(aid), []) + extra
    return wanted


def find_interrupted(api, source_aid, agents, snapshots, console=None, target_aid=None, completed_ids=None):
    """Read unfinished migrations, and add verified completed replacement ids to the optional set."""
    if not snapshots:
        return []
    for old_id, (path, snap) in snapshots.items():
        problem = _snapshot_problem(snap, old_id)
        if problem:
            _refuse('saved recovery state', path, problem)
    try:
        source_records = api.list_agent_identities(source_aid)
    except ApiError as exc:
        raise FlowAborted('cannot inspect earlier migrations in source group %s (%s)' % (source_aid, exc))
    source_by_id = dict((str(r.agent_id), r) for r in source_records)
    target_cache = {}
    found = []
    for agent in agents:
        if agent.is_cluster or not agent.ip_address:
            continue
        ip, name = _ip(agent.ip_address), _norm(agent.hostname)
        candidates = []
        for old_id, (path, snap) in snapshots.items():
            old_record = source_by_id.get(str(old_id))
            live_match = (old_record is not None and _norm(old_record.name) == name
                          and ip in _ips(old_record))
            old = snap.get('record') if isinstance(snap, dict) else None
            recorded_ips = old.get('ip_addresses') if isinstance(old, dict) else None
            relevant = live_match or (isinstance(snap, dict) and _norm(snap.get('name')) == name
                                     and isinstance(recorded_ips, list)
                                     and ip in set(_ip(i) for i in recorded_ips))
            if not relevant:
                continue
            if old_record is not None and old_record.online and snap.get('replacement') is None:
                continue  # this saved snapshot never reached a reset
            problem = _snapshot_problem(snap, old_id)
            if problem:
                _refuse(agent.hostname, path, problem)
            selected_target = agent.target_aid or target_aid
            if str(snap['source_aid']) != str(source_aid):
                _refuse(agent.hostname, path, 'snapshot source differs from the selected source')
            if selected_target is not None and str(snap['target_aid']) != str(selected_target):
                _refuse(agent.hostname, path, 'snapshot destination differs from the selected destination')
            if agent.agent_id and str(agent.agent_id) != str(old_id):
                _refuse(agent.hostname, path, 'inventory agent_id differs from the snapshot old agent id')
            if old_record is not None and (old_record.is_cluster or not _old_identity_matches(old_record, snap, ip)):
                _refuse(agent.hostname, path, 'old agent identity could not be verified')
            candidates.append((str(old_id), old_record, path, snap))
        if not candidates:
            continue
        if len(candidates) != 1:
            _refuse(agent.hostname, candidates[0][2], 'ambiguous earlier migrations match this device')
        old_id, old_record, path, snap = candidates[0]
        try:
            interrupted = _inspect(api, snap, path, old_id, old_record, target_cache, ip)
        except ApiError as exc:
            _refuse(agent.hostname, path, 'could not check the earlier run (%s)' % exc)
        if interrupted is not None:
            if interrupted.pending:
                found.append(interrupted)
            else:
                if completed_ids is not None:
                    completed_ids.add(interrupted.new_id)
                if console is not None:
                    console.info('resume', '%s: earlier migration from source %s to destination %s is complete '
                                 '(new agent %s); it will not be reset again'
                                 % (interrupted.name, snap['source_aid'], snap['target_aid'], interrupted.new_id))
                    unknown = _unknown_enabled(interrupted.item)
                    if unknown:
                        console.info('resume', _enabled_warning(unknown))
    return found


def _inspect(api, snap, path, old_id, old_record, target_cache, ip):
    target = str(snap['target_aid'])
    if target not in target_cache:
        target_cache[target] = api.list_agent_identities(target, agent_types='enterprise')
    same_ip = [r for r in target_cache[target] if str(r.agent_id) != old_id and ip in _ips(r)]
    news = [r for r in same_ip if _replacement_matches(r, snap, ip)]
    if not news and not same_ip and old_record is not None and snap.get('replacement') is None:
        return None
    if not news:
        _refuse(snap['name'], path, 'new agent identity could not be verified in the destination')
    if len(news) != 1:
        _refuse(snap['name'], path, 'ambiguous new agent identities in the destination')
    new_record = news[0]
    new_id = str(new_record.agent_id)
    pending = []
    if old_record is not None:
        old_record = _read_old(api, snap, old_id, ip)
    wanted = _wanted_tests(api, snap, old_record, old_id)
    detail = api.get_agent(target, new_id)
    if str(detail.agent_id) != new_id or not _replacement_matches(detail, snap, ip):
        _refuse(snap['name'], path, 'new agent detail does not match its recorded identity')
    if not detail.online or detail.raw.get('enabled') is not True:
        pending.append('replacement must be online and enabled')
    groups = [g for g in _desired_groups(_item_from_snapshot(snap, old_record, wanted, []), target)]
    have = _live_shares(detail, target)
    if [g for g in groups if g not in have]:
        pending.append('share with %s' % ', '.join(g for g in groups if g not in have))
    missing_total = 0
    for aid, tests in sorted(wanted.items()):
        present = _tests_on_api(api, aid, new_id)
        missing_total += len([t for t in tests if t not in present])
    if missing_total:
        pending.append('%d test(s) to assign' % missing_total)
    dependencies = _pending_a2a(api, snap.get('a2a_targets') or [], new_id)
    if dependencies:
        pending.append('%d agent-to-agent target(s) to repair' % len(dependencies))
    if _enabled_needs_review(api, snap):
        pending.append('saved test enabled/disabled state needs manual review')
    if old_record is not None and not _retirement_complete(api, str(snap['source_aid']), old_record):
        pending.append('finish old agent %s retirement: disabled, tagged and named %s (keep its registration)'
                       % (old_id, retired_agent_name(old_id)))
    if _norm(detail.name) != _norm(snap['name']):
        pending.append('rename to %s' % snap['name'])
    tags = []
    for tag in snap.get('tags') or []:
        try:
            if not _tag_on(api, tag, new_id):
                tags.append(tag)
        except ApiError as exc:
            _refuse(snap['name'], path, 'could not verify saved tags (%s)' % exc)
    if tags:
        pending.append('%d tag(s) to restore' % len(tags))
    item = _item_from_snapshot(snap, old_record, wanted, tags)
    return Interrupted(snap['name'], old_id, new_id, snap, path, old_record, new_record, item, pending)


def _tests_on_api(api, aid, agent_id):
    for record in api.list_agent_identities(aid, assignment_ids=[agent_id]):
        if str(record.agent_id) == str(agent_id):
            if not record.test_ids_known:
                raise FlowAborted('replacement assignments are unreadable in group %s' % aid)
            return set(str(t) for t in record.test_ids)
    return set()


def _pending_a2a(api, entries, new_id):
    """Read dependencies before any recovery write; a changed target is never overwritten."""
    pending = []
    for entry in entries:
        try:
            test = api.get_test(entry['aid'], 'agent-to-agent', entry['test_id'])
        except ApiError as exc:
            raise FlowAborted('cannot read agent-to-agent test %s in group %s (%s); no recovery changes were made'
                              % (entry['test_id'], entry['aid'], exc))
        target = _scalar(test.raw.get('targetAgentId'))
        if str(test.test_id) != str(entry['test_id']) or 'agent-to-agent' not in str(test.test_type).lower():
            raise FlowAborted('agent-to-agent test identity is unreadable; no recovery changes were made')
        if not target:
            raise FlowAborted('agent-to-agent target is unreadable; no recovery changes were made')
        if target == str(new_id):
            continue
        if target != str(entry['old_target_agent_id']):
            raise FlowAborted('agent-to-agent target changed for test %s in group %s; no recovery changes were made'
                              % (entry['test_id'], entry['aid']))
        pending.append(dict(entry))
    return pending


def _enabled_needs_review(api, snap):
    """A saved bool mismatch remains unfinished even after name, tags and old cleanup are done."""
    for aid, flags in (snap.get('test_enabled') or {}).items():
        expected = dict((str(t), value) for t, value in flags.items() if isinstance(value, bool))
        if not expected:
            continue
        try:
            tests = dict((str(t.test_id), t) for t in api.list_tests(str(aid)))
        except ApiError as exc:
            raise FlowAborted('cannot verify saved test enabled state in group %s (%s)' % (aid, exc))
        for test_id, value in expected.items():
            test = tests.get(test_id)
            actual = test.raw.get('enabled') if test is not None else None
            if not isinstance(actual, bool) or actual != value:
                return True
    return False


def _unknown_enabled(item):
    return sum(1 for aid, tests in item.tests_by_aid.items() for test_id in tests
               if not isinstance(item.test_enabled.get(aid, {}).get(test_id), bool))


def _enabled_warning(count):
    return ('%d test(s) have no saved enabled state in this snapshot; verify their enabled/disabled '
            'state manually. Recovery does not replay test settings' % count)


def _verify_source_interfaces(api, item, old_id):
    """The association POST cannot preserve an explicit interface on the old source."""
    for aid, wanted in item.tests_by_aid.items():
        if not wanted:
            continue
        try:
            listing = dict((str(test.test_id), test) for test in api.list_tests(aid))
            for test_id in wanted:
                summary = listing.get(str(test_id))
                if summary is None or not _scalar(summary.test_type):
                    raise FlowAborted('test %s in group %s has no readable test detail type; '
                                      'source interfaces cannot be verified' % (test_id, aid))
                detail = api.get_test(aid, summary.test_type, str(test_id))
                if str(detail.test_id) != str(test_id):
                    raise FlowAborted('test %s in group %s returned a different identity; '
                                      'source interfaces cannot be verified' % (test_id, aid))
                problem = source_interface_problem(detail, [old_id], require_selected=False)
                if problem:
                    raise FlowAborted('%s in group %s; no recovery changes were made' % (problem, aid))
        except ApiError as exc:
            raise FlowAborted('cannot inspect test source interfaces in group %s (%s); '
                              'no recovery changes were made' % (aid, type(exc).__name__))


def _read_old(api, snap, old_id, ip):
    old = api.get_agent(snap['source_aid'], old_id)
    if (str(old.agent_id) != str(old_id) or str(old.agent_type or '').casefold() != 'enterprise'
            or not _old_identity_matches(old, snap, ip)):
        raise FlowAborted('old agent identity changed; recovery stopped without changing it')
    if old.online and snap.get('replacement') is None:
        raise FlowAborted('online old agent requires a saved replacement checkpoint; recovery stopped')
    return old


def _check_old(ctx, interrupted):
    if interrupted.old_record is not None:
        return _read_old(ctx.api, interrupted.snapshot, interrupted.old_id, interrupted.item.ui_ip)


def resume_device(ctx, interrupted):
    """Finish one device. Returns a FlowResult; raises FlowAborted (with the state in the message) on failure."""
    problem = _snapshot_problem(interrupted.snapshot, interrupted.old_id)
    if problem or str(ctx.source_aid) != str(interrupted.snapshot.get('source_aid')):
        _refuse(interrupted.name, interrupted.path, problem or 'selected source changed')
    ctx = ctx.for_target(interrupted.target_aid)
    item = interrupted.item
    new_id, old_id = interrupted.new_id, interrupted.old_id
    state = _State('resuming; new agent %s; old agent %s' % (new_id, old_id))
    warnings = []
    current = ctx.api.get_agent(ctx.target_aid, new_id)
    if str(current.agent_id) != str(new_id) or not _replacement_matches(current, interrupted.snapshot, item.ui_ip):
        raise FlowAborted('new agent identity changed; no recovery changes were made')
    if not current.online or current.raw.get('enabled') is not True:
        raise FlowAborted('new agent %s is %s, not confirmed online and enabled; nothing was changed'
                          % (new_id, current.state))
    have = _live_shares(current, ctx.target_aid)
    old_record = _check_old(ctx, interrupted)
    item.tests_by_aid = _wanted_tests(ctx.api, interrupted.snapshot, old_record, old_id)
    if old_record is not None:
        item.record = old_record
        item.share_aids = sorted(set(str(a) for a in item.share_aids)
                                 | _live_shares(old_record, ctx.source_aid, 'old'))
    item.a2a_targets = _pending_a2a(ctx.api, interrupted.snapshot.get('a2a_targets') or [], new_id)
    _verify_source_interfaces(ctx.api, item, old_id)
    unknown = _unknown_enabled(item)
    if unknown:
        warning = _enabled_warning(unknown)
        warnings.append(warning)
        ctx.console.info(item.name, warning)
    _verify_enabled(ctx, item, new_id)
    wanted = set(_desired_groups(item, ctx.target_aid))
    item.share_aids = sorted(set(str(a) for a in item.share_aids) | have)
    if wanted - have:
        _share_back(ctx, item, new_id)
    _assign_tests(ctx, item, new_id)
    _repoint_a2a(ctx, item, old_id, new_id, state)
    _verify_enabled(ctx, item, new_id)
    if interrupted.old_record is not None:
        _check_old(ctx, interrupted)
        _retire_old(ctx, item, old_id, new_id, state)
    _rename(ctx, item, new_id)
    _restore_tags(ctx, item, new_id, warnings)
    _verify_enabled(ctx, item, new_id)
    detail = 'finished an earlier run: new agent %s' % new_id
    if interrupted.old_record is not None:
        detail += '; old registration retained disabled and marked for manual deletion'
    return FlowResult(item.name, True, detail, new_agent_id=new_id,
                      warnings=warnings, snapshot=interrupted.path)
