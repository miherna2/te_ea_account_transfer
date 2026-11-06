"""Finish a migration that stopped after the new agent came online.

Use it when the tool moved the agent (reset, token, share) but stopped before the end,
for example "Read timed out" while it assigned tests. Run it from the project folder.

    python resume_assign.py SNAPSHOT.json NEW_AGENT_ID                (1. read only: shows what is left)
    python resume_assign.py SNAPSHOT.json NEW_AGENT_ID --assign       (2. adds the missing tests)
    python resume_assign.py SNAPSHOT.json NEW_AGENT_ID --finish       (3. deletes the old agent, renames
                                                                          the new one, restores tags)
    python resume_assign.py SNAPSHOT.json NEW_AGENT_ID --finish --unshare 1087761
                                                                       (also stops sharing with that group)

Safety:
  * Without a flag it changes nothing.
  * --assign only ADDS tests, in batches of 5. It never removes a test.
  * --finish refuses to start while any test is missing on the new agent, or the new agent is offline.
  * --finish deletes only the old agent named in the snapshot, only if it is a standalone enterprise
    agent, is not the new agent, and is offline. You must type its id to confirm.
  * --unshare never removes the destination group and never a group that holds tests.
  * Run it again at any time: every step first checks what is already done.

The API token comes from TE_OAUTH_TOKEN or a hidden prompt. Nothing secret is printed.
"""
import getpass
import json
import os
import sys
import time

import te_agent_migrate  # noqa: F401  (adds the libs folder to sys.path)
from te_agent_migrate.api_client import ThousandEyesApiClient

CHUNK = 5
WAIT_SECONDS = 180
POLL_SECONDS = 5


def uniq(values):
    out = []
    for value in values:
        value = str(value)
        if value not in out:
            out.append(value)
    return out


def agent_in(api, aid, agent_id):
    """The agent as seen from group ``aid`` (with its tests in that group), or None."""
    for record in api.list_agents_with_clusters(aid):
        if str(record.agent_id) == str(agent_id):
            return record
    return None


def tests_on(api, aid, agent_id):
    record = agent_in(api, aid, agent_id)
    return set(str(t) for t in record.test_ids) if record else set()


def wanted_tests(api, snap, old_id, old_here):
    """Tests the new agent must have, per group: the snapshot plus anything the old agent still has."""
    wanted = dict((str(k), uniq(v)) for k, v in snap['tests_by_aid'].items() if v)
    if old_here:
        for aid in uniq(list(snap['share_aids']) + [snap['target_aid']]):
            extra = [t for t in sorted(tests_on(api, aid, old_id)) if t not in wanted.get(aid, [])]
            if extra:
                print('note: old agent still has %d test(s) in aid %s that the snapshot does not list; '
                      'they are included' % (len(extra), aid))
                wanted[aid] = wanted.get(aid, []) + extra
    return wanted


def missing_tests(api, wanted, new_id, show=True):
    missing = {}
    for aid, tests in sorted(wanted.items()):
        have = tests_on(api, aid, new_id)
        missing[aid] = [t for t in tests if t not in have]
        if show:
            print('  aid %-8s need %3d, new agent has %3d, missing %3d'
                  % (aid, len(tests), len(tests) - len(missing[aid]), len(missing[aid])))
    return missing


def tag_assigned(api, tag, new_id):
    for item in api.list_tags(tag['aid'], expand_assignments=True):
        if str(item.get('tagId', item.get('id'))) == str(tag['tagId']):
            for assignment in item.get('assignments') or []:
                if str(assignment.get('id')) == str(new_id):
                    return True
    return False


def confirm(question, expected):
    answer = input('%s\nType %s to continue, anything else stops: ' % (question, expected)).strip()
    return answer == expected


def do_assign(api, missing, new_id):
    for aid, tests in sorted(missing.items()):
        for start in range(0, len(tests), CHUNK):
            chunk = tests[start:start + CHUNK]
            try:
                api.assign_tests(aid, new_id, chunk)
            except Exception as exc:
                print('aid %s: batch failed: %s' % (aid, exc))
                print('Stop here. Run it again: it only adds what is still missing.')
                return False
            print('aid %s: assigned %d (%d of %d)' % (aid, len(chunk), min(start + CHUNK, len(tests)), len(tests)))
    return True


def wait_all_present(api, wanted, new_id):
    for attempt in range(1, 13):
        time.sleep(POLL_SECONDS)
        left = missing_tests(api, wanted, new_id, show=False)
        if not any(left.values()):
            return True
        print('waiting for the API to show every test (%d)...' % attempt)
    return False


def finish(api, snap, new_id, new_agent, old_id, old_here, unshare):
    target = str(snap['target_aid'])
    owner = str(snap['owner_aid'])
    name = snap['name']

    # --- 1. delete the old agent
    if old_here is None:
        print('old agent %s: already gone. Nothing to delete.' % old_id)
    else:
        problems = []
        if str(old_id) == str(new_id):
            problems.append('old and new agent id are the same')
        if str(old_here.agent_type or 'enterprise').lower() != 'enterprise':
            problems.append('old agent type is %r, not a standalone enterprise agent' % old_here.agent_type)
        if old_here.online:
            problems.append('old agent is ONLINE; it should be offline after the reset')
        snap_ip = snap['record'].get('ip_addresses') or []
        if not (old_here.name == name or set(old_here.ip_addresses) & set(snap_ip)):
            problems.append('old agent name and IP do not match the snapshot')
        if problems:
            print('STOP, old agent %s was NOT deleted: %s' % (old_id, '; '.join(problems)))
            return 1
        print('old agent %s: name=%r state=%s in aid %s' % (old_id, old_here.name, old_here.state, owner))
        if not confirm('This DELETES old agent %s in aid %s. The new agent %s keeps the tests.'
                       % (old_id, owner, new_id), str(old_id)):
            print('Stopped. Nothing was deleted.')
            return 1
        api.delete_agent(owner, str(old_id), agent_type='enterprise')
        api.wait_for_agent_absent(owner, str(old_id), WAIT_SECONDS, POLL_SECONDS)
        print('old agent %s deleted.' % old_id)

    # --- 2. name of the new agent
    if new_agent.name != name:
        api.update_agent_name(target, new_id, name)
        api.wait_for_agent_name(target, new_id, name, WAIT_SECONDS, POLL_SECONDS)
        print('new agent %s renamed to %r.' % (new_id, name))
    else:
        print('new agent name is already %r.' % name)

    # --- 3. tags (a failure here is only a warning)
    for tag in snap.get('tags') or []:
        label = '%s (aid %s)' % (tag.get('key'), tag.get('aid'))
        try:
            if tag_assigned(api, tag, new_id):
                print('tag %s: already on the new agent.' % label)
                continue
            api.assign_tag(tag['aid'], tag['tagId'], [{'id': new_id, 'type': 'v-agent'}])
            print('tag %s: restored.' % label)
        except Exception as exc:
            print('tag %s: WARNING, not restored: %s' % (label, exc))

    # --- 4. optional: stop sharing with one group
    if unshare:
        unshare = str(unshare)
        held = [a for a, v in snap['tests_by_aid'].items() if v]
        if unshare == target:
            print('STOP: --unshare cannot remove the destination group %s.' % target)
            return 1
        if unshare in held:
            print('STOP: group %s holds tests; it was NOT unshared.' % unshare)
            return 1
        current = api.get_agent(target, new_id)
        keep = [a for a in uniq([target] + list(current.shared_aids)) if a != unshare]
        if unshare not in current.shared_aids:
            print('group %s: the new agent is not shared with it. Nothing to do.' % unshare)
        elif confirm('This STOPS sharing agent %s with aid %s. Final groups: %s.'
                     % (new_id, unshare, ', '.join(keep)), unshare):
            api.set_agent_account_groups(target, new_id, keep)
            for _ in range(12):
                time.sleep(POLL_SECONDS)
                if unshare not in api.get_agent(target, new_id).shared_aids:
                    print('group %s: no longer shared.' % unshare)
                    break
            else:
                print('group %s: STILL shared. Remove it in the web UI (Account Groups list of the agent).'
                      % unshare)
        else:
            print('Stopped. Sharing was not changed.')
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    unshare = None
    if '--unshare' in sys.argv:
        position = sys.argv.index('--unshare')
        unshare = sys.argv[position + 1] if position + 1 < len(sys.argv) else None
        args = [a for a in args if a != unshare]
        if not unshare:
            print('--unshare needs a group id, for example --unshare 1087761')
            return 2
    if len(args) != 2:
        print(__doc__)
        return 2
    do_assign_flag = '--assign' in sys.argv
    do_finish = '--finish' in sys.argv
    with open(args[0], encoding='utf-8') as handle:
        snap = json.load(handle)
    new_id = str(args[1])
    old_id = str(snap['record']['agent_id'])
    target = str(snap['target_aid'])
    owner = str(snap['owner_aid'])
    token = os.environ.get('TE_OAUTH_TOKEN') or getpass.getpass('API token (hidden): ')
    api = ThousandEyesApiClient(token)

    new_agent = api.get_agent(target, new_id)
    print('NEW agent %s: name=%r state=%s' % (new_id, new_agent.name, new_agent.state))
    expected = uniq([target] + list(snap['share_aids']))
    have = set(new_agent.shared_aids) | set([target])
    print('  shared with: %s' % ', '.join(sorted(have)))
    print('  expected   : %s%s' % (', '.join(expected), '  (snapshot)'))
    absent = [a for a in expected if a not in have]
    if absent:
        print('  NOT shared with: %s' % ', '.join(absent))

    old_here = None
    for record in api.list_agents(owner):
        if str(record.agent_id) == old_id:
            old_here = record
    if old_here is None:
        print('OLD agent %s: not found in aid %s (already deleted).' % (old_id, owner))
    else:
        print('OLD agent %s: name=%r state=%s in aid %s (still present)' % (old_id, old_here.name, old_here.state, owner))

    print('Tests (snapshot: %d in %d groups):' % (
        sum(len(v) for v in snap['tests_by_aid'].values()), len([v for v in snap['tests_by_aid'].values() if v])))
    wanted = wanted_tests(api, snap, old_id, old_here)
    missing = missing_tests(api, wanted, new_id)
    all_there = not any(missing.values())

    for tag in snap.get('tags') or []:
        try:
            state = 'on the new agent' if tag_assigned(api, tag, new_id) else 'NOT on the new agent'
        except Exception as exc:
            state = 'unknown (%s)' % exc
        print('Tag %s (aid %s): %s' % (tag.get('key'), tag.get('aid'), state))

    if not (do_assign_flag or do_finish):
        print('\nread only: nothing was changed.')
        if not all_there:
            print('Next: add --assign to add the missing tests.')
        else:
            print('All tests are on the new agent. Next: add --finish (delete old agent, rename, tags).')
        return 0

    if do_assign_flag and not all_there:
        if not do_assign(api, missing, new_id):
            return 1
        if not wait_all_present(api, wanted, new_id):
            print('NOT complete: the API still shows missing tests. Send me this window.')
            return 1
        print('OK: the new agent has all tests in every group.')
        all_there = True

    if do_finish:
        if not all_there:
            print('STOP: tests are missing on the new agent. Run with --assign first. Nothing was deleted.')
            return 1
        if not new_agent.online:
            print('STOP: the new agent is %s, not online. Nothing was deleted.' % new_agent.state)
            return 1
        if absent:
            print('warning: the new agent is not shared with %s (all its tests are there, so this does not '
                  'block the finish).' % ', '.join(absent))
        return finish(api, snap, new_id, new_agent, old_id, old_here, unshare)
    return 0


if __name__ == '__main__':
    sys.exit(main())
