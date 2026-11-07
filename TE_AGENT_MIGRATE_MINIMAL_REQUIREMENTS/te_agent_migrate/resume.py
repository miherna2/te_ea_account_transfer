"""Finish a migration that stopped half way (a timeout, a closed window, a network drop).

Before a device is reset the tool saves a snapshot of it (tests, groups, tags). When a run stops after the
new agent came online, the snapshot and the live data say what is left:

* the new agent is in the destination group (same IP, a different id than the old one),
* and something is missing: a share, a test, the old agent is still there, the name or a tag is not restored.

The next run finds this by itself, tells the operator, and in ``--apply`` finishes it after one question.
Every step reads first and only does what is missing, so running it twice is safe. It adds tests, it never
removes one, and it deletes the old agent only when that agent is a standalone agent, is offline and is the
device of the snapshot.
"""
import json
import os

from te_agent_migrate.errors import ApiError
from te_agent_migrate.flow.context import FlowAborted, FlowResult, PreflightItem, a2a_targets_of
from te_agent_migrate.flow.standalone import (
    _State, _assign_tests, _delete_old, _desired_groups, _rename, _repoint_a2a, _restore_tags, _share_back,
    _tests_on,
)
from te_agent_migrate.models import InventoryAgent

SNAPSHOT_FOLDERS = ('apply', 'runs')   # under work/: where snapshots are written (current and older versions)
MAX_DEPTH = 4


def _norm(value):
    return str(value if value is not None else '').strip().casefold()


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
                    except OSError:
                        continue
    return [path for _, path in sorted(found, reverse=True)]


def load_snapshots(work_dir):
    """{old agent id: (path, data)}, the newest snapshot of each old agent. Unreadable files are ignored."""
    out = {}
    for path in snapshot_files(work_dir):
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
            old_id = str(data['record']['agent_id'])
            data['target_aid'], data['name'], data['tests_by_aid']
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if (data.get('inventory') or {}).get('kind') == 'cluster':
            continue  # clusters are not finished automatically
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
        return '%s: old agent %s, new agent %s; left to do: %s' % (
            self.name, self.old_id if self.old_record is not None else '%s (gone)' % self.old_id,
            self.new_id, '; '.join(self.pending))


def _ips(record):
    return set(_norm(ip) for ip in (getattr(record, 'ip_addresses', None) or []))


def _tag_on(api, tag, new_id):
    for entry in api.list_tags(tag['aid'], expand_assignments=True):
        if str(entry.get('tagId', entry.get('id'))) == str(tag['tagId']):
            return any(str(a.get('id')) == str(new_id) for a in (entry.get('assignments') or []))
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
    item.a2a_targets = list(snap.get('a2a_targets') or [])
    return item


def _wanted_tests(api, snap, old_record, old_id):
    """Tests the new agent must have, per group: the snapshot, plus anything the old agent still has."""
    wanted = dict((str(aid), [str(t) for t in tests]) for aid, tests in snap['tests_by_aid'].items() if tests)
    if old_record is not None:
        for aid in set(list(snap.get('share_aids') or []) + [str(snap['target_aid'])]):
            held = set()
            try:
                for record in api.list_agents_with_clusters(aid):
                    if str(record.agent_id) == old_id:
                        held = set(str(t) for t in record.test_ids)
            except ApiError:
                continue
            extra = sorted(held - set(wanted.get(str(aid), [])))
            if extra:
                wanted[str(aid)] = wanted.get(str(aid), []) + extra
    return wanted


def find_interrupted(api, source_aid, agents, snapshots, console=None):
    """The inventory devices whose migration stopped half way. Reads only."""
    if not snapshots:
        return []
    try:
        source_records = api.list_agents_with_clusters(source_aid)
    except ApiError:
        return []
    target_cache = {}
    found = []
    for agent in agents:
        if agent.is_cluster or not agent.ip_address:
            continue
        ip, name = _norm(agent.ip_address), _norm(agent.hostname)
        old_here = [r for r in source_records if not r.is_cluster and _norm(r.name) == name and ip in _ips(r)]
        candidates = []
        if old_here:
            if any(r.online for r in old_here):
                continue  # an old agent that is online was not reset: nothing started
            candidates = [(str(r.agent_id), r) for r in old_here if str(r.agent_id) in snapshots]
        else:
            for old_id, (path, data) in snapshots.items():
                if _norm(data['name']) == name and ip in set(_norm(i) for i in data['record'].get('ip_addresses') or []):
                    candidates.append((old_id, None))
        if len(candidates) != 1:
            continue
        old_id, old_record = candidates[0]
        path, snap = snapshots[old_id]
        try:
            interrupted = _inspect(api, snap, path, old_id, old_record, target_cache)
        except ApiError as exc:
            if console is not None:
                console.info('resume', '%s: could not check an earlier run (%s)' % (agent.hostname, exc))
            continue
        if interrupted is not None:
            found.append(interrupted)
    return found


def _inspect(api, snap, path, old_id, old_record, target_cache):
    target = str(snap['target_aid'])
    if target not in target_cache:
        target_cache[target] = api.list_agents(target)
    snap_ips = set(_norm(i) for i in snap['record'].get('ip_addresses') or [])
    news = [r for r in target_cache[target]
            if str(r.agent_id) != old_id and not r.is_cluster and _ips(r) & snap_ips]
    if len(news) != 1:
        return None
    new_record = news[0]
    new_id = str(new_record.agent_id)
    pending = []
    wanted = _wanted_tests(api, snap, old_record, old_id)
    detail = api.get_agent(target, new_id)
    groups = [g for g in _desired_groups(_item_from_snapshot(snap, old_record, wanted, []), target)]
    have = set(str(a) for a in detail.shared_aids) | set([target])
    if [g for g in groups if g not in have]:
        pending.append('share with %s' % ', '.join(g for g in groups if g not in have))
    missing_total = 0
    for aid, tests in sorted(wanted.items()):
        present = _tests_on_api(api, aid, new_id)
        missing_total += len([t for t in tests if t not in present])
    if missing_total:
        pending.append('%d test(s) to assign' % missing_total)
    if old_record is not None:
        pending.append('delete the old agent %s' % old_id)
    if _norm(detail.name) != _norm(snap['name']):
        pending.append('rename to %s' % snap['name'])
    tags = []
    for tag in snap.get('tags') or []:
        try:
            if not _tag_on(api, tag, new_id):
                tags.append(tag)
        except ApiError:
            continue  # unreadable tags are not a reason to call the run unfinished
    if tags:
        pending.append('%d tag(s) to restore' % len(tags))
    if not pending:
        return None
    item = _item_from_snapshot(snap, old_record, wanted, tags)
    return Interrupted(snap['name'], old_id, new_id, snap, path, old_record, new_record, item, pending)


def _tests_on_api(api, aid, agent_id):
    for record in api.list_agents_with_clusters(aid):
        if str(record.agent_id) == str(agent_id):
            return set(str(t) for t in record.test_ids)
    return set()


def resume_device(ctx, interrupted):
    """Finish one device. Returns a FlowResult; raises FlowAborted (with the state in the message) on failure."""
    ctx = ctx.for_target(interrupted.target_aid)
    item = interrupted.item
    new_id, old_id = interrupted.new_id, interrupted.old_id
    state = _State('resuming; new agent %s; old agent %s' % (new_id, old_id))
    warnings = []
    current = ctx.api.get_agent(ctx.target_aid, new_id)
    if not current.online:
        raise FlowAborted('new agent %s is %s, not online; nothing was changed. Start the agent and run again'
                          % (new_id, current.state))
    _share_back(ctx, item, new_id)
    _assign_tests(ctx, item, new_id)
    if interrupted.old_record is not None:
        _repoint_a2a(ctx, item, old_id, new_id, state)
        _delete_old(ctx, item, old_id, new_id, state)
    _rename(ctx, item, new_id)
    _restore_tags(ctx, item, new_id, warnings)
    return FlowResult(item.name, True, 'finished an earlier run: new agent %s' % new_id, new_agent_id=new_id,
                      warnings=warnings, snapshot=interrupted.path)
