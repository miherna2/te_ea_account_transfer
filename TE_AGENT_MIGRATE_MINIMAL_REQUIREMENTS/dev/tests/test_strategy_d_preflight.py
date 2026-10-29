import unittest

from te_agent_migrate.errors import ApiError
from te_agent_migrate.models import AccountGroup, AgentRecord, InventoryAgent, StepStatus, TestRecord
from te_agent_migrate.strategy_d.context import FlowContext
from te_agent_migrate.strategy_d import preflight as pf

SRC = '100'
TGT = '200'
OTHER = '300'
THIRD = '400'


# ------------------------------------------------------------------- raw data
# The real list call returns NO accountGroups and no owner field. Only
# GET /agents/{id} returns accountGroups. The fakes below follow that.
def row(agent_id, name, ip='', state='online', tests=None):
    return {
        'agentId': str(agent_id), 'agentName': name, 'agentType': 'enterprise',
        'agentState': state, 'ipAddresses': [ip] if ip else [], 'testIds': list(tests or []),
    }


def member(member_id, name, ip, state='online'):
    return {'memberId': member_id, 'name': name, 'ipAddresses': [ip], 'agentState': state}


def cluster_row(agent_id, name, members, tests=None, state=None):
    raw = {
        'agentId': str(agent_id), 'agentName': name, 'agentType': 'enterprise-cluster',
        'clusterMembers': members, 'testIds': list(tests or []),
    }
    if state:
        raw['agentState'] = state
    return raw


def detail(raw, shares=(), owner=SRC, owner_key='ownerAid', accountgroups=True):
    """What GET /agents/{id} returns for a row."""
    data = dict(raw)
    if accountgroups:
        data['accountGroups'] = [{'aid': str(a), 'accountGroupName': 'g%s' % a} for a in shares]
    if owner is not None:
        data[owner_key] = owner
    return data


def test_row(test_id, enabled=True, agents=None, test_type='http-server', **extra):
    raw = {'testId': str(test_id), 'testName': 't%s' % test_id, 'type': test_type}
    if enabled is not None:
        raw['enabled'] = enabled
    if agents is not None:
        raw['agents'] = [{'agentId': str(a)} for a in agents]
    raw.update(extra)
    return raw


class FakeApi(object):
    def __init__(self, agents=None, details=None, tests=None, tags=None, groups=None):
        self.agents = agents or {}
        self.details = details or {}
        self.tests = tests or {}
        self.tags = tags or {}
        self.groups = groups
        self.calls = []

    @staticmethod
    def _value(value):
        if isinstance(value, Exception):
            raise value
        return value

    def list_account_groups(self):
        self.calls.append(('groups',))
        value = self._value(self.groups if self.groups is not None else [])
        return [AccountGroup(str(a), 'g%s' % a) for a in value]

    def list_agents_with_clusters(self, aid):
        self.calls.append(('agents', str(aid)))
        return [AgentRecord.from_api(dict(r), str(aid)) for r in self._value(self.agents.get(str(aid), []))]

    def get_agent(self, aid, agent_id):
        self.calls.append(('get_agent', str(aid), str(agent_id)))
        if str(agent_id) not in self.details:
            raise ApiError('404 not found')
        return AgentRecord.from_api(dict(self._value(self.details[str(agent_id)])), str(aid))

    def list_tests(self, aid):
        self.calls.append(('tests', str(aid)))
        return [TestRecord.from_api(dict(t), str(aid)) for t in self._value(self.tests.get(str(aid), []))]

    def list_tags(self, aid, expand_assignments=False):
        self.calls.append(('tags', str(aid)))
        assert expand_assignments
        return list(self._value(self.tags.get(str(aid), [])))

    def __getattr__(self, name):
        # any write or unexpected call must fail the test
        raise AssertionError('unexpected api call: %s' % name)


class FakeConsole(object):
    def __init__(self):
        self.tasks = []
        self.statuses = []
        self.infos = []

    def task(self, name):
        self.tasks.append(name)

    def status(self, status, agent, step, detail=''):
        self.statuses.append((status, agent, step, detail))

    def info(self, scope, message):
        self.infos.append((scope, message))


def make_ctx(api, apply=False):
    return FlowContext(api, None, FakeConsole(), SRC, TGT, apply=apply)


def inv(name, ip='', kind='agent', agent_id=None):
    return InventoryAgent(name, ip, kind, agent_id)


def world(raw, shares=(), owner=SRC, group_tests=None, tests=None, **kw):
    """One device owned by SRC, visible in SRC and every group in ``shares``.

    List rows carry no accountGroups. ``group_tests`` gives the test ids the
    device has in each other group.
    """
    agents = {SRC: [raw]}
    for aid in shares:
        if str(aid) == SRC:
            continue
        other = dict(raw)
        other['testIds'] = list((group_tests or {}).get(aid, []))
        agents.setdefault(str(aid), []).append(other)
    details = {raw['agentId']: detail(raw, shares=shares, owner=owner)}
    return FakeApi(agents=agents, details=details, tests=tests, groups=kw.pop('groups', [SRC, TGT, OTHER, THIRD]),
                   **kw)


class PreflightBase(unittest.TestCase):
    def run_one(self, api, entry, apply=False, all_aids=None):
        return pf.run_preflight(make_ctx(api, apply), [entry], all_aids=all_aids)[0]


class FakeRealismTests(PreflightBase):
    def test_list_rows_have_no_account_groups_and_detail_has(self):
        api = world(row(1, 'EA1', '10.0.0.1'), shares=[OTHER])
        listed = api.list_agents_with_clusters(SRC)[0]
        self.assertEqual(listed.shared_aids, [])
        self.assertNotIn('accountGroups', listed.raw)
        self.assertEqual(api.get_agent(SRC, '1').shared_aids, [OTHER])


class MatchingTests(PreflightBase):
    def test_standalone_match_needs_ip_and_name_case_insensitive(self):
        api = world(row(1, 'Site-A-EA01', '10.0.0.1', tests=['5', '3']))
        item = self.run_one(api, inv('  site-a-ea01 ', ' 10.0.0.1 '))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.record.agent_id, '1')
        self.assertEqual(item.tests_by_aid, {SRC: ['3', '5']})
        self.assertEqual(item.owner_aid, SRC)

    def test_ip_matches_name_differs_is_blocked(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        item = self.run_one(api, inv('other-name', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('name differs', item.reasons[0])
        self.assertIn('10.0.0.1', item.reasons[0])
        self.assertIsNone(item.record)
        self.assertNotIn(('get_agent', SRC, '1'), api.calls)

    def test_name_matches_ip_differs_is_blocked(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        item = self.run_one(api, inv('ea1', '10.9.9.9'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('IP differs', item.reasons[0])
        self.assertIsNone(item.record)

    def test_ip_and_name_on_different_records_is_blocked(self):
        api = FakeApi(agents={SRC: [row(1, 'A', '10.0.0.1'), row(2, 'B', '10.0.0.2')]})
        item = self.run_one(api, inv('A', '10.0.0.2'))
        self.assertEqual(item.status, 'blocked')
        self.assertIsNone(item.record)

    def test_two_candidates_is_blocked(self):
        api = FakeApi(agents={SRC: [row(1, 'A', '10.0.0.1'), row(2, 'a', '10.0.0.1')]})
        item = self.run_one(api, inv('A', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('ambiguous', item.reasons[0])
        self.assertIn('1', item.reasons[0])
        self.assertIn('2', item.reasons[0])
        self.assertIsNone(item.record)

    def test_no_match_is_blocked(self):
        api = FakeApi(agents={SRC: [row(1, 'A', '10.0.0.1')]})
        item = self.run_one(api, inv('nope', '10.1.1.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('no agent', item.reasons[0])
        self.assertIsNone(item.record)

    def test_inventory_without_ip_is_blocked(self):
        api = FakeApi(agents={SRC: [row(1, 'A', '10.0.0.1')]})
        item = self.run_one(api, inv('A', ''))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('both name and IP', item.reasons[0])

    def test_agent_id_with_ip_and_name_ok(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        item = self.run_one(api, inv('EA1', '10.0.0.1', agent_id='1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.warnings, [])

    def test_agent_id_with_only_name_agreeing_is_ready_with_warning(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        item = self.run_one(api, inv('EA1', '10.9.9.9', agent_id='1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertTrue(any('inventory IP' in w for w in item.warnings))
        self.assertEqual(item.ui_ip, '10.9.9.9')

    def test_agent_id_with_only_ip_agreeing_is_ready_with_warning(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        item = self.run_one(api, inv('renamed', '10.0.0.1', agent_id='1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertTrue(any('inventory name' in w for w in item.warnings))

    def test_agent_id_with_neither_ip_nor_name_is_blocked(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        item = self.run_one(api, inv('whatever', '9.9.9.9', agent_id='1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('neither', item.reasons[0])

    def test_agent_id_not_found_is_blocked(self):
        # no live agent has this id, and none has this name and IP: nothing to fall back to
        api = FakeApi(agents={SRC: [row(1, 'A', '10.0.0.1')]})
        item = self.run_one(api, inv('B', '10.0.0.9', agent_id='77'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('77', item.reasons[0])

    def test_kind_mismatch_agent_vs_cluster(self):
        c = cluster_row(9, 'CL', [member('m1', 'm1', '10.0.0.5')])
        api = FakeApi(agents={SRC: [c]})
        item = self.run_one(api, inv('CL', '10.0.0.5', kind='agent'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('cluster', item.reasons[0])

    def test_kind_mismatch_cluster_vs_agent(self):
        api = FakeApi(agents={SRC: [row(1, 'EA1', '10.0.0.1')]})
        item = self.run_one(api, inv('EA1', kind='cluster'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('standalone agent', item.reasons[0])

    def test_kind_mismatch_by_agent_id(self):
        api = FakeApi(agents={SRC: [row(1, 'EA1', '10.0.0.1')]})
        item = self.run_one(api, inv('EA1', kind='cluster', agent_id='1'))
        self.assertEqual(item.status, 'blocked')

    def test_cluster_by_name_and_by_id(self):
        members = [member('m1', 'n1', '10.0.0.5'), member('m2', 'n2', '10.0.0.6')]
        for entry in (inv('sedc - cl', kind='cluster'), inv('x', kind='cluster', agent_id='9')):
            api = world(cluster_row(9, 'SEDC - CL', members, tests=['1', '2']))
            item = self.run_one(api, entry)
            self.assertEqual(item.status, 'ready', item.reasons)
            self.assertEqual(item.record.agent_id, '9')

    def test_cluster_name_ambiguous_blocked(self):
        members = [member('m1', 'n1', '10.0.0.5')]
        api = FakeApi(agents={SRC: [cluster_row(9, 'CL', members), cluster_row(10, 'cl', members)]})
        item = self.run_one(api, inv('CL', kind='cluster'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('ambiguous', item.reasons[0])


class SharingTests(PreflightBase):
    def test_share_set_comes_from_get_agent_and_scan(self):
        api = world(row(1, 'EA1', '10.0.0.1', tests=['10', '11']), shares=[OTHER, THIRD],
                    group_tests={OTHER: ['30'], THIRD: []})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.owner_aid, SRC)
        self.assertTrue(item.owner_proven)
        self.assertEqual(item.share_aids, [SRC, OTHER, THIRD])
        self.assertEqual(item.tests_by_aid, {SRC: ['10', '11'], OTHER: ['30'], THIRD: []})
        self.assertEqual(item.share_sources['get_agent'], [SRC, OTHER, THIRD])
        self.assertEqual(item.share_sources['scan'], [SRC, OTHER, THIRD])
        self.assertIn(('get_agent', SRC, '1'), api.calls)

    def test_owner_listed_in_account_groups_is_fine(self):
        api = world(row(1, 'EA1', '10.0.0.1'), shares=[SRC, OTHER])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.share_aids, [SRC, OTHER])

    def test_share_aids_not_only_source_when_list_has_no_account_groups(self):
        # regression for the review blocker: list rows never carry accountGroups
        api = world(row(1, 'EA1', '10.0.0.1'), shares=[OTHER])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertIn(OTHER, item.share_aids)

    def test_get_agent_without_account_groups_blocks(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw]}, details={'1': detail(raw, accountgroups=False)}, groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertTrue(any('cannot read sharing; lab check 1' in r for r in item.reasons))

    def test_get_agent_failure_blocks(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw]}, details={}, groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('cannot read sharing; lab check 1', item.reasons[0])
        self.assertIn('404', item.reasons[0])

    def test_scan_and_get_agent_disagree_scan_has_more(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(
            agents={SRC: [raw], OTHER: [raw], THIRD: [raw]},
            details={'1': detail(raw, shares=[OTHER])}, groups=[SRC, TGT, OTHER, THIRD])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('disagree', item.reasons[0])
        self.assertIn('[100, 300]', item.reasons[0])
        self.assertIn('[100, 300, 400]', item.reasons[0])
        self.assertEqual(item.share_aids, [])

    def test_scan_and_get_agent_disagree_get_agent_has_more(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(
            agents={SRC: [raw], OTHER: []},
            details={'1': detail(raw, shares=[OTHER])}, groups=[SRC, TGT, OTHER])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('disagree', item.reasons[0])

    def test_scan_uses_all_aids_parameter(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(
            agents={SRC: [raw], THIRD: [raw]},
            details={'1': detail(raw, shares=[])}, groups=[SRC, TGT])  # list_account_groups not used
        item = self.run_one(api, inv('EA1', '10.0.0.1'), all_aids=[SRC, TGT, THIRD])
        self.assertEqual(item.status, 'blocked')
        self.assertIn('disagree', item.reasons[0])
        self.assertNotIn(('groups',), api.calls)

    def test_list_account_groups_failure_is_warning_not_block(self):
        api = world(row(1, 'EA1', '10.0.0.1'), shares=[OTHER], groups=ApiError('403'))
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertTrue(any('account group list not readable' in w for w in item.warnings))
        self.assertEqual(item.share_aids, [SRC, OTHER])

    def test_unreadable_shared_group_blocks(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw], OTHER: ApiError('403 forbidden')},
                      details={'1': detail(raw, shares=[OTHER])}, groups=[SRC, TGT, OTHER])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertTrue(any(OTHER in r and 'cannot read agents of shared group' in r for r in item.reasons))

    def test_unreadable_unrelated_group_is_warning(self):
        api = world(row(1, 'EA1', '10.0.0.1'), shares=[OTHER])
        api.agents[THIRD] = ApiError('403 forbidden')
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertTrue(any(THIRD in w and 'could not be scanned' in w for w in item.warnings))

    def test_account_groups_unparseable_blocks(self):
        raw = row(1, 'EA1', '10.0.0.1')
        data = dict(raw, accountGroups=[{'name': 'no id'}], ownerAid=SRC)
        api = FakeApi(agents={SRC: [raw]}, details={'1': data}, groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertTrue(any('cannot read sharing' in r for r in item.reasons))

    def test_agent_shared_with_target_is_blocked_as_rerun(self):
        # H1: the target is in the share set and the target listing has the same id
        api = world(row(1, 'EA1', '10.0.0.1'), shares=[TGT])
        for assume in (False, True):
            item = pf.run_preflight(make_ctx(api), [inv('EA1', '10.0.0.1')], assume_owner_source=assume)[0]
            self.assertEqual(item.status, 'blocked', item.reasons)
            self.assertIn(pf.REASON_IN_TARGET, item.reasons[0])
            self.assertIn('unshare', item.reasons[0])

    def test_target_in_share_set_without_target_listing_is_blocked(self):
        # accountGroups names the target but the target listing does not show the agent
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw]}, details={'1': detail(raw, shares=[TGT])}, groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertTrue(any(pf.REASON_IN_TARGET in r for r in item.reasons), item.reasons)

    def test_device_in_target_listing_only_is_blocked(self):
        # re-run: the new agent (same id here) is owned by the target and shared back to the source
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw], TGT: [dict(raw)]}, details={'1': detail(raw, shares=[])},
                      groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'), apply=True)
        self.assertEqual(item.status, 'blocked')
        self.assertIn(pf.REASON_IN_TARGET, item.reasons[0])
        self.assertNotIn(('get_agent', SRC, '1'), api.calls)

    def test_cluster_id_in_target_listing_is_blocked(self):
        members = [member('m1', 'n1', '10.0.0.5'), member('m2', 'n2', '10.0.0.6')]
        c = cluster_row(9, 'CL', members)
        api = FakeApi(agents={SRC: [c], TGT: [dict(c)]}, details={'9': detail(c, shares=[])}, groups=[SRC, TGT])
        item = self.run_one(api, inv('CL', kind='cluster'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn(pf.REASON_IN_TARGET, item.reasons[0])

    def test_other_agent_in_target_listing_does_not_block(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        api.agents[TGT] = [row(2, 'EA2', '10.0.0.2')]
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)


class OwnerTests(PreflightBase):
    def test_owner_unproven_without_owner_fields_blocks(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw]}, details={'1': detail(raw, shares=[], owner=None)}, groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('owner group not proven to be the source group', item.reasons[0])
        self.assertFalse(item.owner_proven)
        self.assertEqual(item.owner_aid, '')

    def test_owner_other_group_blocks(self):
        api = world(row(1, 'EA1', '10.0.0.1'), owner=OTHER)
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('owner group not proven to be the source group', item.reasons[0])
        self.assertIn('ownerAid=%s' % OTHER, item.reasons[0])

    def test_owner_account_group_id_field_is_accepted(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw]},
                      details={'1': detail(raw, owner=SRC, owner_key='ownerAccountGroupId')}, groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertTrue(item.owner_proven)

    def test_owner_integer_value_is_accepted(self):
        api = world(row(1, 'EA1', '10.0.0.1'), owner=int(SRC))
        self.assertEqual(self.run_one(api, inv('EA1', '10.0.0.1')).status, 'ready')

    def test_conflicting_owner_fields_block(self):
        raw = row(1, 'EA1', '10.0.0.1')
        data = detail(raw, owner=SRC)
        data['ownerAccountGroupId'] = OTHER
        api = FakeApi(agents={SRC: [raw]}, details={'1': data}, groups=[SRC, TGT])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('owner group not proven', item.reasons[0])

    def test_listing_in_source_group_alone_is_not_proof(self):
        # shared with everyone, listed in source, but no owner field anywhere
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw], OTHER: [raw]},
                      details={'1': detail(raw, shares=[SRC, OTHER], owner=None)}, groups=[SRC, TGT, OTHER])
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('owner group not proven', item.reasons[0])

    def test_prove_owner_rule(self):
        self.assertEqual(pf.prove_owner('1', {}, {})[0], False)
        self.assertEqual(pf.prove_owner('1', {'ownerAid': '1'})[0], True)
        self.assertEqual(pf.prove_owner('1', {'ownerAid': 1}, {})[0], True)
        self.assertEqual(pf.prove_owner('1', {'ownerAid': {'aid': '1'}})[0], True)
        self.assertEqual(pf.prove_owner('1', {'ownerAid': '1'}, {'ownerAid': '2'})[0], False)
        self.assertEqual(pf.prove_owner('1', {'ownerAid': ''})[0], False)


class StatusTests(PreflightBase):
    def test_cluster_copies_members(self):
        members = [member('m1', 'n1', '10.0.0.5'), member('m2', 'n2', '10.0.0.6')]
        api = world(cluster_row(9, 'SEDC - CL', members, tests=['1', '2']))
        item = self.run_one(api, inv('SEDC - CL', kind='cluster'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual([m['memberId'] for m in item.members], ['m1', 'm2'])
        self.assertEqual(item.members[0]['state'], 'online')
        self.assertEqual(item.members[0]['ipAddresses'], ['10.0.0.5'])
        self.assertEqual(item.tests_by_aid, {SRC: ['1', '2']})
        self.assertEqual(item.ui_ip, '')

    def test_cluster_offline_member_is_excluded(self):
        members = [member('m1', 'n1', '10.0.0.5'), member('m2', 'n2', '10.0.0.6', 'offline')]
        api = FakeApi(agents={SRC: [cluster_row(9, 'CL', members)]})
        item = self.run_one(api, inv('CL', kind='cluster'))
        self.assertEqual(item.status, 'excluded')
        self.assertIn('n2', item.reasons[0])
        self.assertIn('offline', item.reasons[0])
        self.assertEqual(len(item.members), 2)

    def test_standalone_offline_is_excluded_and_skips_further_calls(self):
        api = FakeApi(agents={SRC: [row(1, 'EA1', '10.0.0.1', state='offline', tests=['4'])]})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'excluded')
        self.assertIn('offline', item.reasons[0])
        self.assertEqual(item.tests_by_aid, {SRC: ['4']})
        self.assertNotIn(('tags', SRC), api.calls)
        self.assertNotIn(('agents', TGT), api.calls)
        self.assertFalse([c for c in api.calls if c[0] == 'get_agent'])

    def test_target_name_collision_blocked(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        api.agents[TGT] = [row(50, 'ea1', '10.7.7.7')]
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('target group', item.reasons[0])
        self.assertIn('50', item.reasons[0])

    def test_target_other_name_ok(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        api.agents[TGT] = [row(50, 'different', '10.7.7.7')]
        self.assertEqual(self.run_one(api, inv('EA1', '10.0.0.1')).status, 'ready')

    def test_unreadable_source_or_target_listing_blocks(self):
        api = FakeApi(agents={SRC: ApiError('500')})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('source group', item.reasons[0])
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw], TGT: ApiError('500')})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('target group', item.reasons[0])

    def test_duplicate_selection_second_blocked(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        items = pf.run_preflight(make_ctx(api), [inv('EA1', '10.0.0.1'), inv('EA1 again', '10.0.0.1', agent_id='1')])
        self.assertEqual([i.status for i in items], ['ready', 'blocked'])
        self.assertIn('duplicate', items[1].reasons[0])

    def test_source_equals_target_blocks(self):
        api = world(row(1, 'A', '10.0.0.1'))
        ctx = FlowContext(api, None, FakeConsole(), SRC, SRC)
        self.assertEqual(pf.run_preflight(ctx, [inv('A', '10.0.0.1')])[0].status, 'blocked')


class UiIpAndAttributesTests(PreflightBase):
    def test_ui_ip_is_inventory_ip_trimmed(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        item = self.run_one(api, inv('EA1', ' 10.0.0.1 '))
        self.assertEqual(item.ui_ip, '10.0.0.1')

    def test_all_items_have_extra_attributes_even_blocked(self):
        api = FakeApi(agents={SRC: []})
        item = self.run_one(api, inv('nope', '10.1.1.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertEqual(item.ui_ip, '10.1.1.1')
        self.assertEqual(item.warnings, [])
        self.assertEqual(item.test_enabled, {})
        self.assertFalse(item.owner_proven)

    def test_preflight_item_class_not_extended(self):
        # attributes are set on the instance, PreflightItem stays the orchestrator's class
        self.assertFalse(hasattr(pf.PreflightItem, 'ui_ip'))
        self.assertFalse(hasattr(pf.PreflightItem, 'test_enabled'))


class TestsPerGroupTests(PreflightBase):
    def test_test_enabled_per_group_one_list_call_per_group(self):
        api = world(
            row(1, 'EA1', '10.0.0.1', tests=['10', '11', '12']), shares=[OTHER], group_tests={OTHER: ['30']},
            tests={
                SRC: [test_row(10, True), test_row(11, False), test_row(12, None), test_row(99, True)],
                OTHER: [test_row(30, False)],
            })
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.test_enabled, {SRC: {'10': True, '11': False, '12': None}, OTHER: {'30': False}})
        self.assertEqual(api.calls.count(('tests', SRC)), 1)
        self.assertEqual(api.calls.count(('tests', OTHER)), 1)
        self.assertTrue(any('could not be read' in w and '12' in w for w in item.warnings))
        self.assertIn('warning: ' + item.warnings[0], item.reasons)

    def test_test_listing_missing_a_test_gives_none_and_warning(self):
        api = world(row(1, 'EA1', '10.0.0.1', tests=['10']), tests={SRC: []})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready')
        self.assertEqual(item.test_enabled, {SRC: {'10': None}})
        self.assertEqual(len(item.warnings), 1)

    def test_unreadable_test_listing_is_warning_only(self):
        api = world(row(1, 'EA1', '10.0.0.1', tests=['10']), tests={SRC: ApiError('500')})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.test_enabled, {SRC: {'10': None}})
        self.assertTrue(any('could not read tests' in w for w in item.warnings))

    def test_a2a_test_targeting_device_blocks(self):
        api = world(
            row(1, 'EA1', '10.0.0.1'), shares=[OTHER],
            tests={OTHER: [test_row(70, agents=[5], test_type='agent-to-agent', targetAgentId='1'),
                           test_row(71, agents=[5], test_type='agent-to-agent', targetAgentId='2')]})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('agent-to-agent', item.reasons[0])
        self.assertIn('70', item.reasons[0])
        self.assertNotIn('71', item.reasons[0])

    def test_a2a_test_with_other_target_does_not_block(self):
        api = world(row(1, 'EA1', '10.0.0.1'),
                    tests={SRC: [test_row(70, agents=[5], test_type='agent-to-agent', targetAgentId=2)]})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.warnings, [])

    def test_a2a_test_without_readable_target_is_warning(self):
        api = world(row(1, 'EA1', '10.0.0.1'),
                    tests={SRC: [test_row(70, agents=[5], test_type='agent-to-agent')]})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertTrue(any('targetAgentId' in w for w in item.warnings))

    def test_last_agent_test_is_warning(self):
        api = world(row(1, 'EA1', '10.0.0.1', tests=['10', '11']),
                    tests={SRC: [test_row(10, agents=[1]), test_row(11, agents=[1, 2])]})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(len(item.warnings), 1)
        self.assertIn('only agent', item.warnings[0])
        self.assertIn('10', item.warnings[0])
        self.assertNotIn(', 11', item.warnings[0])

    def test_last_agent_warning_for_standalone_says_tests_stay_enabled(self):
        api = world(row(1, 'EA1', '10.0.0.1', tests=['10']), tests={SRC: [test_row(10, agents=[1])]})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertIn('only agent', item.warnings[0])
        self.assertIn('not disabled', item.warnings[0])
        self.assertNotIn('will be disabled', item.warnings[0])

    def test_last_agent_warning_for_cluster_says_tests_will_be_disabled(self):
        members = [member('m1', 'n1', '10.0.0.5'), member('m2', 'n2', '10.0.0.6')]
        api = world(cluster_row(9, 'CL', members, tests=['10']), tests={SRC: [test_row(10, agents=[9])]})
        item = self.run_one(api, inv('CL', kind='cluster'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertIn('only agent', item.warnings[0])
        self.assertIn('will be disabled', item.warnings[0])

    def test_owner_assumed_flag_is_set_only_for_the_operator_statement(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw]}, details={'1': detail(raw, shares=[], owner=None)}, groups=[SRC, TGT])
        assumed = pf.run_preflight(make_ctx(api), [inv('EA1', '10.0.0.1')], assume_owner_source=True)[0]
        self.assertEqual(assumed.status, 'ready', assumed.reasons)
        self.assertTrue(assumed.owner_assumed)
        proven = self.run_one(world(row(1, 'EA1', '10.0.0.1')), inv('EA1', '10.0.0.1'))
        self.assertTrue(proven.owner_proven)
        self.assertFalse(proven.owner_assumed)

    def test_unreadable_agent_list_of_tests_is_warning(self):
        api = world(row(1, 'EA1', '10.0.0.1', tests=['10']), tests={SRC: [test_row(10)]})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready')
        self.assertTrue(any('no readable agent list' in w for w in item.warnings))


class FamilyTests(PreflightBase):
    def test_mixed_ip_families_blocked(self):
        members = [member('m1', 'n1', '10.0.0.5'), member('m2', 'n2', '2001:db8::5')]
        api = FakeApi(agents={SRC: [cluster_row(9, 'CL', members)]})
        item = self.run_one(api, inv('CL', kind='cluster'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('IP family', item.reasons[0])
        self.assertIn('IPv4', item.reasons[0])
        self.assertIn('IPv6', item.reasons[0])
        self.assertFalse([c for c in api.calls if c[0] == 'get_agent'])

    def test_same_family_ok_and_dual_stack_member_ok(self):
        members = [member('m1', 'n1', '2001:db8::5'), member('m2', 'n2', '2001:db8::6')]
        api = world(cluster_row(9, 'CL', members))
        self.assertEqual(self.run_one(api, inv('CL', kind='cluster')).status, 'ready')
        dual = dict(member('m2', 'n2', '10.0.0.6'), ipAddresses=['10.0.0.6', '2001:db8::6'])
        api = world(cluster_row(9, 'CL', [member('m1', 'n1', '2001:db8::5'), dual]))
        self.assertEqual(self.run_one(api, inv('CL', kind='cluster')).status, 'ready')

    def test_member_without_ip_blocked(self):
        members = [member('m1', 'n1', '10.0.0.5'), dict(member('m2', 'n2', ''), ipAddresses=[])]
        api = FakeApi(agents={SRC: [cluster_row(9, 'CL', members)]})
        item = self.run_one(api, inv('CL', kind='cluster'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('no readable IP', item.reasons[0])


class TagsTests(PreflightBase):
    def test_tags_collected_per_group(self):
        api = world(
            row(1, 'EA1', '10.0.0.1'), shares=[OTHER],
            tags={
                SRC: [
                    {'id': 't1', 'key': 'site', 'value': 'A',
                     'assignments': [{'id': '1', 'type': 'v-agent'}, {'id': '2', 'type': 'v-agent'}]},
                    {'id': 't2', 'key': 'test-tag', 'value': 'x',
                     'assignments': [{'id': '1', 'type': 'test'}]},
                ],
                OTHER: [
                    {'id': 't9', 'key': 'env', 'value': 'prod',
                     'assignments': [{'id': 1, 'type': 'enterprise-agent'}]},
                ],
            },
        )
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.tags, [
            {'aid': SRC, 'tagId': 't1', 'key': 'site', 'value': 'A'},
            {'aid': OTHER, 'tagId': 't9', 'key': 'env', 'value': 'prod'},
        ])

    def test_tag_api_error_is_tolerated(self):
        api = world(row(1, 'EA1', '10.0.0.1'), tags={SRC: ApiError('boom')})
        item = self.run_one(api, inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready')
        self.assertEqual(item.tags, [])
        self.assertTrue(item.reasons[0].startswith('warning:'))
        self.assertIn('boom', item.reasons[0])
        self.assertIn('boom', item.warnings[0])


class RunTests(PreflightBase):
    def test_group_listings_cached_and_only_get_calls(self):
        api = world(row(1, 'A', '10.0.0.1'))
        api.agents[SRC].append(row(2, 'B', '10.0.0.2'))
        api.details['2'] = detail(row(2, 'B', '10.0.0.2'))
        pf.run_preflight(make_ctx(api, apply=True), [inv('A', '10.0.0.1'), inv('B', '10.0.0.2')])
        self.assertEqual(api.calls.count(('agents', SRC)), 1)
        self.assertEqual(api.calls.count(('agents', TGT)), 1)
        self.assertEqual(api.calls.count(('agents', OTHER)), 1)
        self.assertEqual(api.calls.count(('tags', SRC)), 1)
        self.assertEqual(api.calls.count(('tests', SRC)), 1)
        self.assertEqual(api.calls.count(('groups',)), 1)
        self.assertEqual(api.calls.count(('get_agent', SRC, '1')), 1)
        self.assertEqual(api.calls.count(('get_agent', SRC, '2')), 1)

    def test_same_result_dry_run_and_apply(self):
        def build():
            return world(row(1, 'A', '10.0.0.1', tests=['1']), shares=[OTHER], tests={SRC: [test_row(1)]})
        a = pf.run_preflight(make_ctx(build(), apply=False), [inv('A', '10.0.0.1')])[0]
        b = pf.run_preflight(make_ctx(build(), apply=True), [inv('A', '10.0.0.1')])[0]
        self.assertEqual(
            (a.status, a.tests_by_aid, a.share_aids, a.test_enabled, a.warnings, a.ui_ip),
            (b.status, b.tests_by_aid, b.share_aids, b.test_enabled, b.warnings, b.ui_ip))

    def test_has_blockers_and_summarize(self):
        ready = pf.PreflightItem(inv('a'), status='ready')
        excl = pf.PreflightItem(inv('b'), status='excluded')
        blocked = pf.PreflightItem(inv('c'), status='blocked')
        self.assertFalse(pf.has_blockers([]))
        self.assertFalse(pf.has_blockers([ready, excl]))
        self.assertTrue(pf.has_blockers([ready, excl, blocked]))
        self.assertEqual(pf.summarize([ready, excl, blocked, blocked]),
                         {'ready': 1, 'blocked': 2, 'excluded': 1})


class PrintTests(PreflightBase):
    def test_print_preflight_labels_owner_shares_and_warnings(self):
        api = world(
            row(1, 'GOOD', '10.0.0.1', tests=['1', '2']), shares=[OTHER], group_tests={OTHER: ['3']},
            tests={SRC: [test_row(1, agents=[1]), test_row(2, agents=[1, 7])], OTHER: [test_row(3, agents=[1, 7])]})
        api.agents[SRC].append(row(2, 'OFF', '10.0.0.2', state='offline'))
        ctx = make_ctx(api)
        items = pf.run_preflight(ctx, [inv('GOOD', '10.0.0.1'), inv('OFF', '10.0.0.2'), inv('MISSING', '10.5.5.5')])
        pf.print_preflight(ctx, items)
        self.assertEqual(ctx.console.tasks, ['Preflight'])
        got = dict((s[1], s) for s in ctx.console.statuses)
        self.assertEqual(got['GOOD'][0], StepStatus.OK)
        self.assertIn('tests=3', got['GOOD'][3])
        self.assertIn('shares=2', got['GOOD'][3])
        self.assertNotIn('warning', got['GOOD'][3])
        self.assertEqual(got['OFF'][0], StepStatus.SKIPPED)
        self.assertIn('offline', got['OFF'][3])
        self.assertEqual(got['MISSING'][0], StepStatus.FAILED)
        self.assertIn('no agent', got['MISSING'][3])
        infos = ctx.console.infos
        self.assertIn(('GOOD', 'owner=100 shares=[100, 300]'), infos)
        self.assertTrue(any(s == 'GOOD' and m.startswith('warning:') and 'only agent' in m for s, m in infos))
        self.assertFalse([i for i in infos if i[0] == 'MISSING'])

    def test_print_preflight_shows_disagreeing_share_sets_and_unproven_owner(self):
        raw = row(1, 'EA1', '10.0.0.1')
        api = FakeApi(agents={SRC: [raw], OTHER: [raw]},
                      details={'1': detail(raw, shares=[], owner=None)}, groups=[SRC, TGT, OTHER])
        ctx = make_ctx(api)
        items = pf.run_preflight(ctx, [inv('EA1', '10.0.0.1')])
        pf.print_preflight(ctx, items)
        self.assertEqual(ctx.console.statuses[0][0], StepStatus.FAILED)
        text = ' '.join(m for _, m in ctx.console.infos)
        self.assertIn('owner=not proven', text)
        self.assertIn('disagree', text)

    def test_print_preflight_without_info_method_does_not_fail(self):
        api = world(row(1, 'EA1', '10.0.0.1'))
        ctx = make_ctx(api)
        ctx.console = type('C', (object,), {
            'task': lambda self, n: None, 'status': lambda self, *a, **k: None})()
        pf.print_preflight(ctx, pf.run_preflight(ctx, [inv('EA1', '10.0.0.1')]))


if __name__ == '__main__':
    unittest.main()
