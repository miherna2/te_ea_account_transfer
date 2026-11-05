"""Check, and if asked finish, the test assignment of an agent that the tool already moved.

Usage (run from the project folder):
    python resume_assign.py SNAPSHOT.json NEW_AGENT_ID            (read only)
    python resume_assign.py SNAPSHOT.json NEW_AGENT_ID --assign   (adds missing tests)

It only ADDS tests to the new agent, in small batches. It never deletes, renames or unshares.
The API token comes from TE_OAUTH_TOKEN or a hidden prompt. Nothing secret is printed.
"""
import getpass
import json
import os
import sys
import time

import te_agent_migrate  # noqa: F401  (adds the libs folder to sys.path)
from te_agent_migrate.api_client import ThousandEyesApiClient

CHUNK = 10


def have_tests(api, aid, new_id):
    for record in api.list_agents_with_clusters(aid):
        if str(record.agent_id) == str(new_id):
            return set(str(t) for t in record.test_ids)
    return set()


def report(api, tests_by_aid, new_id):
    missing_by_aid = {}
    for aid, wanted in sorted(tests_by_aid.items()):
        have = have_tests(api, aid, new_id)
        missing = [t for t in wanted if str(t) not in have]
        missing_by_aid[aid] = missing
        print('aid %s: need %d, new agent has %d of them, missing %d'
              % (aid, len(wanted), len(wanted) - len(missing), len(missing)))
    return missing_by_aid


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(args) != 2:
        print(__doc__)
        return 2
    do_assign = '--assign' in sys.argv
    with open(args[0], encoding='utf-8') as handle:
        snap = json.load(handle)
    new_id = args[1]
    target = str(snap['target_aid'])
    tests_by_aid = dict((str(k), list(v)) for k, v in snap['tests_by_aid'].items() if v)
    token = os.environ.get('TE_OAUTH_TOKEN') or getpass.getpass('API token (hidden): ')
    api = ThousandEyesApiClient(token)

    agent = api.get_agent(target, new_id)
    print('new agent %s: state=%s shared_with=%s' % (new_id, agent.state, sorted(agent.shared_aids)))
    print('snapshot: old agent %s, %d tests in %d groups'
          % (snap['record']['agent_id'], sum(len(v) for v in tests_by_aid.values()), len(tests_by_aid)))
    missing_by_aid = report(api, tests_by_aid, new_id)
    if not do_assign:
        print('read only: nothing was changed. Add --assign to add the missing tests.')
        return 0

    for aid, missing in sorted(missing_by_aid.items()):
        for start in range(0, len(missing), CHUNK):
            chunk = missing[start:start + CHUNK]
            try:
                api.assign_tests(aid, new_id, chunk)
                print('aid %s: assigned %d tests (%d of %d)'
                      % (aid, len(chunk), min(start + CHUNK, len(missing)), len(missing)))
            except Exception as exc:
                print('aid %s: batch failed: %s' % (aid, exc))
                print('Stop here. Run it again; it only adds what is still missing.')
                return 1
    for attempt in range(12):
        time.sleep(5)
        left = report(api, tests_by_aid, new_id)
        if not any(left.values()):
            print('OK: the new agent has all tests in every group.')
            return 0
        print('waiting (%d)...' % (attempt + 1))
    print('NOT complete. Send me this window.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
