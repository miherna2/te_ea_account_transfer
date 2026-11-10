"""The tool finds the account groups by itself.

* Source group: the group where the devices of the inventory are (name and IP). If one group sees all of
  them it is proposed (``[Y/n]``). If several see all of them (a group that only sees a shared agent shows it
  too) the operator picks among those few. If none does, the operator picks from the full list.
* Destination group of a device, in this order: the ``target`` column of its row (a group id or an exact
  group name), then ``work/input/prefix_map.csv`` (the part of the device name before `` - ``), and
  otherwise one question for the whole run.
"""
import os
import time

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.scope import load_prefix_map, prefix_key, prefix_of


def _norm(value):
    return str(value if value is not None else '').strip().casefold()


def _ip_set(record):
    return set(_norm(ip) for ip in (getattr(record, 'ip_addresses', None) or []))


def sees(record, agent):
    """True when ``record`` (an agent or cluster listed in some group) is the device of the inventory row."""
    if agent.agent_id and str(record.agent_id) != str(agent.agent_id):
        return False
    if _norm(record.name) != _norm(agent.hostname):
        return False
    if record.is_cluster:
        return agent.is_cluster or bool(agent.auto_kind)
    if agent.is_cluster:
        return False
    return (not agent.ip_address) or _norm(agent.ip_address) in _ip_set(record)


def scan_groups(api, groups, agents, console=None):
    """Return ({aid: number of inventory rows that group sees}, [aids that could not be read])."""
    seen = {}
    unreadable = []
    started = time.monotonic()
    for position, group in enumerate(groups, start=1):
        if console is not None:
            console.info('source', 'reading group %d/%d: %s [%s]; elapsed %.1fs'
                         % (position, len(groups), group.name, group.aid, time.monotonic() - started))
        try:
            records = api.list_agent_identities(group.aid)
        except Exception:  # a group that this token cannot read is only skipped
            unreadable.append(group.aid)
            continue
        count = sum(1 for agent in agents if any(sees(record, agent) for record in records))
        if count:
            seen[group.aid] = count
    if console is not None:
        console.info('source', 'discovery finished: %d groups checked in %.1fs'
                     % (len(groups), time.monotonic() - started))
    return seen, unreadable


def detect_source(api, groups, agents, prompter, console):
    """Choose the source group. Asks at most one short question; never guesses silently."""
    console.info('source', 'looking for the %d device(s) in %d account group(s)' % (len(agents), len(groups)))
    seen, unreadable = scan_groups(api, groups, agents, console)
    if unreadable:
        console.info('source', '%d group(s) could not be read and were skipped' % len(unreadable))
    total = len(agents)
    full = [group for group in groups if seen.get(group.aid) == total]
    if len(full) == 1:
        group = full[0]
        if prompter.ask_yes('Source account group: %s [%s] (all %d device(s) are there). Use it?'
                            % (group.name, group.aid, total)):
            prompter.record('Select source account group', '%s [%s] detected' % (group.name, group.aid))
            return group
        return prompter.choose_account_group('Select source account group', groups)
    if len(full) > 1:
        console.info('source', '%d groups see all the devices (the owner and the groups it is shared with). '
                               'Pick the group that owns them.' % len(full))
        return prompter.choose_account_group('Select source account group', full)
    if seen:
        parts = ['%s [%s]: %d of %d' % (next(g.name for g in groups if g.aid == aid), aid, count, total)
                 for aid, count in sorted(seen.items(), key=lambda item: -item[1])[:5]]
        console.info('source', 'no single group sees all the devices (%s)' % '; '.join(parts))
    else:
        console.info('source', 'none of the devices was found by name and IP in any group')
    return prompter.choose_account_group('Select source account group', groups)


def _unique_group(groups, text):
    """The one group with this name (case ignored), or None; ConfigurationError when it is ambiguous."""
    found = [group for group in groups if _norm(group.name) == _norm(text)]
    if len(found) > 1:
        raise ConfigurationError('More than one account group is named %r (%s); use the group id'
                                 % (text, ', '.join(group.aid for group in found)))
    return found[0] if found else None


def resolve_targets(agents, groups, prefix_map_path=None, console=None):
    """Fill ``target_aid`` of the rows that name their group or that a prefix map can place.

    Raises ConfigurationError for a group name that does not exist (nothing has been changed yet).
    Rows that stay without a target use the one group that the operator picks for the run.
    """
    by_aid = set(group.aid for group in groups)
    entries = []
    if prefix_map_path and os.path.isfile(str(prefix_map_path)):
        entries = [entry for entry in load_prefix_map(prefix_map_path)]
    lookup = {}
    for entry in entries:
        lookup.setdefault(prefix_key(entry.prefix), entry)
    from_map = 0
    for agent in agents:
        if agent.target_name:
            group = _unique_group(groups, agent.target_name)
            if group is None:
                raise ConfigurationError(
                    'The destination %r of %s is not an account group that the token can access'
                    % (agent.target_name, agent.hostname))
            agent.target_aid = group.aid
            continue
        if agent.target_aid or not lookup:
            continue
        entry = lookup.get(prefix_key(prefix_of(agent.hostname)))
        if entry is None or entry.status != 'OK':
            continue
        aid = (entry.aid or '').strip()
        if not aid and entry.group_name:
            group = _unique_group(groups, entry.group_name)
            aid = group.aid if group else ''
        if aid and aid in by_aid:
            agent.target_aid = aid
            from_map += 1
    if from_map and console is not None:
        console.info('target', 'prefix_map.csv gave the destination group of %d device(s)' % from_map)
