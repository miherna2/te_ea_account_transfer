import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from te_agent_migrate.api_client import AmbiguousAgentMatch, ThousandEyesApiClient
from te_agent_migrate.errors import ApiError, UiError, VisibilityTimeout
from te_agent_migrate.http import RetryPolicy
from te_agent_migrate.models import AgentRecord, InventoryAgent, LocalSnapshot, StepStatus, TestRecord
from te_agent_migrate.output import MigrationConsole
from te_agent_migrate.strategy_d import cluster as cluster_flow
from te_agent_migrate.strategy_d.cluster import _Progress, _delete_old, migrate_cluster, plan_cluster
from te_agent_migrate.strategy_d.context import FlowAborted, FlowContext, PreflightItem, ReadOnlyApi

SOURCE = '1'
TARGET = '9'
OTHER = '2'
NAME = 'SEDC-CLUSTER-01'
CLUSTER_ID = '11'
MEMBERS = [('11', 'EA-A', '10.0.0.5'), ('12', 'EA-B', '10.0.0.6'), ('13', 'EA-C', '10.0.0.7')]
NEW_IDS = {'10.0.0.5': '91', '10.0.0.6': '92', '10.0.0.7': '93'}
FAKE_TOKEN = 'FAKEFAKEFAKEFAKE0123456789ABCDEF'  # synthetic, 32 chars, not a real credential

WRITE_CALLS = ('cluster_unassign', 'cluster_assign', 'set_agent_account_groups', 'assign_tests',
               'set_test_enabled', 'set_test_enabled_minimal', 'delete_agent', 'update_agent_name', 'assign_tag')
DEFAULT_TESTS = {SOURCE: ['101', '102'], OTHER: ['201']}


class FakeConsole(object):
    def __init__(self):
        self.statuses = []
        self.infos = []

    def status(self, status, agent, step, detail=''):
        self.statuses.append((status, agent, step, detail))

    def info(self, scope, message):
        self.infos.append((scope, message))

    def text(self):
        return '\n'.join(' '.join(str(p) for p in row) for row in self.statuses + self.infos)

    def steps(self, status=None):
        return [row[2] for row in self.statuses if status is None or row[0] == status]


def bare(rec):
    """The real list endpoints return no accountGroups."""
    return AgentRecord(rec.agent_id, rec.name, rec.hostname, list(rec.ip_addresses), rec.state, rec.aid,
                       agent_type=rec.agent_type, cluster_members=list(rec.cluster_members),
                       test_ids=list(rec.test_ids))


def member_dicts(count, state='online'):
    return [{'memberId': mid, 'name': name, 'ipAddresses': [ip], 'state': state} for mid, name, ip in MEMBERS[:count]]


class World(object):
    """Shared state and ordered event log for the fake API and the fake agent UI."""

    def __init__(self, count=2, tests=None, disabled=()):
        self.events = []
        self.count = count
        self.defs = MEMBERS[:count]
        self.agents = {SOURCE: [], OTHER: [], TARGET: []}   # standalone agents per group
        self.cluster_exists = True
        self.new_cluster = None
        self.shares = {}
        self.assigned = {}
        self.new_name = 'EA-A-registered'
        self.tests = {}
        for aid, ids in (DEFAULT_TESTS if tests is None else tests).items():
            self.tests[aid] = dict((t, {'type': 'http-server', 'enabled': t not in disabled}) for t in ids)
        # switches
        self.wrong_group_ips = set()
        self.ui_fail = {}                 # (ip, method) -> exception
        self.ui_fail_after_unassign = {}  # (ip, method) -> exception, only after the cluster unassign was sent
        self.unassign_ignored = False
        self.disable_on_unassign = True
        self.share_put_ignored = False
        self.assign_ignored = False
        self.new_member_offline = None
        self.enable_fail = set()
        self.tag_fail = False
        self.ghost_cluster_id = None
        self.token_seen = []
        self.never_online = set()
        self.absent_timeout = False
        self.interrupt_at = None          # api or ui method name that raises KeyboardInterrupt
        self.standalone_ids = {}          # member id -> standalone agent id after the unassign (default: same)
        self.unmappable = set()           # member ids that have no standalone record after the unassign
        self.assign_returns_list = False  # cluster_assign answers with a JSON array
        self.extra_on_register = {}       # ip -> [AgentRecord] added to the target when that device registers
        self.ui_alias = {}                # UI address -> device IP, when they differ
        self.hostnames = {}               # device IP -> hostname (differs from the member name)
        self.old_extra_ips = {}           # device IP -> more IPs of the old standalone record
        self.new_ips = {}                 # device IP -> IPs the new agent registers with (default: [IP])
        self.ui_settings = {}             # device IP -> (browserbot, crash_reports) strings of the UI
        self.list_tests_fail = False      # list_tests raises after the unassign

    def old_standalone(self):
        return [AgentRecord(self.standalone_ids.get(mid, mid), name, self.hostnames.get(ip, name),
                            [ip] + list(self.old_extra_ips.get(ip, [])), 'online', SOURCE, agent_type='enterprise')
                for mid, name, ip in self.defs if mid not in self.unmappable]

    def register_new_agent(self, ip):
        ip = self.ui_alias.get(ip, ip)
        self.agents[TARGET].extend(self.extra_on_register.get(ip, []))
        if ip in self.never_online:
            return
        group = SOURCE if ip in self.wrong_group_ips else TARGET
        name = [n for _, n, i in self.defs if i == ip][0]
        host = self.hostnames.get(ip, name)
        self.agents[group].append(AgentRecord(NEW_IDS[ip], host + '-new', host, list(self.new_ips.get(ip, [ip])),
                                              'online', group, agent_type='enterprise'))
        for rec in self.agents[SOURCE]:
            if ip in rec.ip_addresses and rec.agent_id != NEW_IDS[ip]:
                rec.state = 'offline'

    def cluster_record(self, aid):
        if self.new_cluster is None:
            return None
        base = self.new_cluster['base']
        members = []
        for mid in [base] + self.new_cluster['others']:
            ip = [i for i, n in NEW_IDS.items() if n == mid][0]
            state = 'offline' if self.new_member_offline == mid else 'online'
            members.append({'memberId': mid, 'name': 'm' + mid, 'ipAddresses': [ip], 'agentState': state})
        return AgentRecord(base, self.new_name, self.new_name, [m['ipAddresses'][0] for m in members], 'online', aid,
                           agent_type='enterprise-cluster', cluster_members=members,
                           shared_aids=self.shares.get(base, []), test_ids=self.assigned.get((aid, base), []))


class FakeApi(object):
    def __init__(self, world):
        self.w = world
        self.calls = []
        self.wait_calls = []

    def _log(self, name, *args):
        self.calls.append((name,) + args)
        self.w.events.append(('api', name) + args)

    # reads
    def _maybe_interrupt(self, name):
        if self.w.interrupt_at == name:
            raise KeyboardInterrupt()

    def list_agents(self, aid, agent_types='ENTERPRISE'):
        self._log('list_agents', aid)
        return [bare(r) for r in self.w.agents.get(aid, [])]

    def list_agents_with_clusters(self, aid):
        self._log('list_agents_with_clusters', aid)
        w = self.w
        result = []
        cluster_members = set()
        if w.new_cluster:
            cluster_members = set([w.new_cluster['base']] + w.new_cluster['others'])
        for rec in w.agents.get(aid, []):
            if rec.agent_id not in cluster_members:
                result.append(rec)
        if aid == SOURCE and w.cluster_exists:
            result.append(AgentRecord(CLUSTER_ID, NAME, NAME, [i for _, _, i in w.defs], 'online', SOURCE,
                                      agent_type='enterprise-cluster', cluster_members=member_dicts(w.count),
                                      shared_aids=[SOURCE, OTHER]))
        if aid == SOURCE and w.ghost_cluster_id and w.new_cluster:
            result = [r for r in result if r.agent_id != w.ghost_cluster_id]
            result.append(AgentRecord(w.ghost_cluster_id, 'ghost', 'ghost', [], 'online', SOURCE,
                                      agent_type='enterprise-cluster', cluster_members=member_dicts(1)))
        if w.new_cluster and (aid == TARGET or aid in w.shares.get(w.new_cluster['base'], [])):
            result.append(w.cluster_record(aid))
        return [bare(r) for r in result]

    def wait_for_agent(self, aid, hostname, ip_address, timeout_seconds, poll_interval_seconds=5.0,
                       on_poll=None, on_not_visible=None, known_ids=None, alt_names=None, alt_ips=None):
        self.wait_calls.append(dict(hostname=hostname, ip=ip_address, known_ids=known_ids,
                                    alt_names=list(alt_names or []), alt_ips=list(alt_ips or [])))
        for attempt in range(1, 4):
            if on_poll:
                on_poll(attempt, 1.0)
            if known_ids is None:
                agent = ThousandEyesApiClient.match_agent(self.list_agents(aid), hostname, ip_address,
                                                          online_only=True)
            else:
                found = ThousandEyesApiClient.match_new_agents(
                    self.list_agents(aid), [hostname] + list(alt_names or []), [ip_address] + list(alt_ips or []),
                    known_ids)
                if len(found) > 1:
                    raise AmbiguousAgentMatch('ambiguous', sorted(a.agent_id for a in found))
                agent = found[0] if found else None
            if agent is not None:
                self._log('wait_for_agent', aid, agent.agent_id)
                return agent
            if on_not_visible:
                on_not_visible()
        raise VisibilityTimeout('not visible')

    def get_agent(self, aid, agent_id):
        self._log('get_agent', aid, agent_id)
        if self.w.new_cluster and agent_id == self.w.new_cluster['base']:
            return self.w.cluster_record(aid)
        raise ApiError('agent %s not found' % agent_id)

    def list_tests(self, aid):
        self._log('list_tests', aid)
        if self.w.list_tests_fail and any(e[:2] == ('api', 'cluster_unassign') for e in self.w.events):
            raise ApiError('list tests failed')
        return [TestRecord(t, 'test-' + t, v['type'], aid, v['enabled'],
                           raw={'testId': t, 'enabled': v['enabled'], 'type': v['type']})
                for t, v in sorted(self.w.tests.get(aid, {}).items())]

    def get_test(self, aid, test_type, test_id):
        self._log('get_test', aid, test_type, test_id)
        v = self.w.tests[aid][test_id]
        return TestRecord(test_id, 'test-' + test_id, v['type'], aid, v['enabled'], raw={'enabled': v['enabled']})

    # writes
    def cluster_unassign(self, aid, cluster_id, member_ids):
        self._log('cluster_unassign', aid, cluster_id, list(member_ids))
        self._maybe_interrupt('cluster_unassign')
        if self.w.unassign_ignored:
            return {}
        self.w.cluster_exists = False
        self.w.agents[SOURCE].extend(self.w.old_standalone())
        if self.w.disable_on_unassign:
            for group in self.w.tests.values():
                for v in group.values():
                    v['enabled'] = False
        return {}

    def cluster_assign(self, aid, base_agent_id, agent_ids):
        self._log('cluster_assign', aid, base_agent_id, list(agent_ids))
        self.w.new_cluster = {'base': base_agent_id, 'others': list(agent_ids)}
        self._maybe_interrupt('cluster_assign')
        return {'_list': [{'agentId': base_agent_id}]} if self.w.assign_returns_list else {}

    def set_agent_account_groups(self, aid, agent_id, ids):
        self._log('set_agent_account_groups', aid, agent_id, list(ids))
        if not ids:
            raise ApiError('empty list refused')
        if not self.w.share_put_ignored:
            self.w.shares[agent_id] = list(ids)

    def assign_tests(self, aid, agent_id, test_ids):
        self._log('assign_tests', aid, agent_id, list(test_ids))
        if len(test_ids) > 100:
            raise ApiError('too many tests in one call')
        if not self.w.assign_ignored:
            self.w.assigned.setdefault((aid, agent_id), [])
            self.w.assigned[(aid, agent_id)] = self.w.assigned[(aid, agent_id)] + list(test_ids)

    def set_test_enabled(self, aid, test_type, test_id, enabled):
        self._log('set_test_enabled', aid, test_type, test_id, enabled)
        if test_id in self.w.enable_fail:
            raise ApiError('HTTP 400 on test %s' % test_id)
        self.w.tests[aid][test_id]['enabled'] = enabled

    def set_test_enabled_minimal(self, aid, test_type, test_id, enabled):
        self._log('set_test_enabled_minimal', aid, test_type, test_id, enabled)
        if test_id in self.w.enable_fail:
            raise ApiError('HTTP 400 on test %s' % test_id)
        self.w.tests[aid][test_id]['enabled'] = enabled

    def delete_agent(self, aid, agent_id, agent_type=None):
        self._log('delete_agent', aid, agent_id, agent_type)
        for group in self.w.agents.values():
            group[:] = [r for r in group if r.agent_id != agent_id]

    def wait_for_agent_absent(self, aid, agent_id, timeout_seconds, poll_interval_seconds=5.0, on_poll=None):
        self._log('wait_for_agent_absent', aid, agent_id)
        if self.w.absent_timeout:
            raise VisibilityTimeout('still present')

    def update_agent_name(self, aid, agent_id, name):
        self._log('update_agent_name', aid, agent_id, name)
        self.w.new_name = name

    def assign_tag(self, aid, tag_id, assignments):
        self._log('assign_tag', aid, tag_id, list(assignments))
        if self.w.tag_fail:
            raise ApiError('tag 403')


class FakeUi(object):
    def __init__(self, world, ip):
        self.w = world
        self.ip = ip

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def _event(self, name, *extra):
        self.w.events.append(('ui', name, self.ip) + extra)
        failure = self.w.ui_fail.get((self.ip, name))
        if failure is None and any(e[:2] == ('api', 'cluster_unassign') for e in self.w.events):
            failure = self.w.ui_fail_after_unassign.get((self.ip, name))
        if failure is not None:
            raise failure

    def authenticate(self):
        self._event('authenticate')

    def snapshot_state(self):
        self._event('snapshot_state')
        browserbot, crash = self.w.ui_settings.get(self.w.ui_alias.get(self.ip, self.ip), ('no', 'yes'))
        return LocalSnapshot(self.ip, 'now', {'app_config': {'browserbot': browserbot, 'crash_reports': crash}})

    def reset_agent(self):
        self._event('reset_agent')
        if self.w.interrupt_at == 'reset_agent':
            raise KeyboardInterrupt()

    def is_reachable(self):
        self._event('is_reachable')
        return True

    def set_account_group_token(self, account_token, browserbot, crash_reports):
        self._event('set_account_group_token', browserbot, crash_reports)
        self.w.token_seen.append(account_token)
        self.w.register_new_agent(self.ip)


def make_item(count=2, status='ready', kind='cluster', tests=None, shares=None, tags=None, members=None,
              record=True, cluster_flag=True):
    inventory = InventoryAgent(NAME, '', kind=kind, agent_id=CLUSTER_ID)
    member_list = member_dicts(count) if members is None else members
    rec = AgentRecord(CLUSTER_ID, NAME, NAME, [m['ipAddresses'][0] for m in member_list if m.get('ipAddresses')],
                      'online', SOURCE, agent_type='enterprise-cluster' if cluster_flag else 'enterprise',
                      cluster_members=member_list) if record else None
    return PreflightItem(
        inventory, record=rec, status=status, reasons=['member offline'] if status != 'ready' else [],
        owner_aid=SOURCE,
        share_aids=[SOURCE, OTHER] if shares is None else shares,
        tests_by_aid=dict(DEFAULT_TESTS) if tests is None else tests,
        tags=[{'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A'}] if tags is None else tags,
        members=member_list)


class Harness(object):
    def __init__(self, apply=True, token=FAKE_TOKEN, count=2, tests=None, disabled=(), snapshot_dir=None,
                 auto_reenable=True):
        self.world = World(count=count, tests=tests, disabled=disabled)
        self.api = FakeApi(self.world)
        self.console = FakeConsole()
        self.opened = []      # UI sessions opened after the unassign (the member resets)
        self.checked = []     # UI sessions opened before the unassign (the member UI check)
        self.slept = []

        def factory(host):
            if any(e[:2] == ('api', 'cluster_unassign') for e in self.world.events):
                self.opened.append(host)
            else:
                self.checked.append(host)
            return FakeUi(self.world, host)

        self.ctx = FlowContext(self.api, factory, self.console, SOURCE, TARGET, target_account_token=token,
                               apply=apply, timeout_seconds=2, poll_seconds=1, sleep=self.slept.append,
                               snapshot_dir=snapshot_dir, auto_reenable_tests=auto_reenable)

    def writes(self):
        return [c for c in self.api.calls if c[0] in WRITE_CALLS]

    def indexes(self, *prefix):
        return [i for i, e in enumerate(self.world.events) if e[:len(prefix)] == prefix]

    def first(self, *prefix):
        found = self.indexes(*prefix)
        if not found:
            raise AssertionError('event %r not found in %r' % (prefix, self.world.events))
        return found[0]

    def last(self, *prefix):
        found = self.indexes(*prefix)
        if not found:
            raise AssertionError('event %r not found in %r' % (prefix, self.world.events))
        return found[-1]

    def stop_line(self):
        lines = [msg for _, msg in self.console.infos if msg.startswith('stopped; state:')]
        return lines[-1] if lines else ''


class ClusterHappyPathTests(unittest.TestCase):
    def test_result_and_step_order(self):
        h = Harness()
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(result.new_cluster_id, '91')
        self.assertEqual(result.new_agent_id, '91')
        self.assertEqual(result.warnings, [])
        per_member = ['cluster_member_ui_authenticate', 'cluster_member_ui_snapshot_before',
                      'cluster_member_ui_reset', 'cluster_member_ui_reachable_after_reset',
                      'cluster_member_ui_authenticate_after_reset', 'cluster_member_ui_set_target_token',
                      'cluster_member_api_target_online']
        self.assertEqual(h.console.steps(), (
            ['cluster_preflight_snapshot'] + ['cluster_member_ui_check'] * 2 + ['cluster_snapshot_file'] +
            ['cluster_unassign'] + per_member * 2 +
            ['cluster_assign', 'cluster_share_back', 'cluster_assign_tests', 'cluster_restore_tests_enabled',
             'cluster_delete_old', 'cluster_rename', 'cluster_restore_tags', 'manual_notification_rules']))
        self.assertEqual(h.console.steps(StepStatus.FAILED), [])
        self.assertEqual(h.opened, ['10.0.0.5', '10.0.0.6'])
        self.assertEqual(h.checked, ['10.0.0.5', '10.0.0.6'])

    def test_unassign_before_reset_and_snapshot_before_unassign(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        unassign = h.first('api', 'cluster_unassign')
        self.assertLess(h.first('api', 'list_tests'), unassign)
        for ip in ('10.0.0.5', '10.0.0.6'):
            self.assertLess(h.first('ui', 'authenticate', ip), unassign)       # the check before the unassign
            self.assertLess(unassign, h.last('ui', 'authenticate', ip))       # the login for the reset after it
            self.assertLess(unassign, h.first('ui', 'reset_agent', ip))
        self.assertIn(('cluster_unassign', SOURCE, CLUSTER_ID, ['11', '12']), h.api.calls)
        self.assertTrue(any('MAINTENANCE WINDOW STARTED' in msg for _, msg in h.console.infos))

    def test_members_are_processed_in_order_one_after_the_other(self):
        h = Harness(count=3)
        migrate_cluster(h.ctx, make_item(count=3))
        resets = [h.first('ui', 'reset_agent', ip) for ip in ('10.0.0.5', '10.0.0.6', '10.0.0.7')]
        tokens = [h.first('ui', 'set_account_group_token', ip, False, True) for ip in ('10.0.0.5', '10.0.0.6', '10.0.0.7')]
        self.assertEqual(resets, sorted(resets))
        self.assertLess(tokens[0], resets[1])
        self.assertLess(tokens[1], resets[2])
        self.assertLess(tokens[2], h.first('api', 'cluster_assign'))

    def test_cluster_assign_base_is_first_member(self):
        h = Harness(count=3)
        migrate_cluster(h.ctx, make_item(count=3))
        self.assertIn(('cluster_assign', TARGET, '91', ['92', '93']), h.api.calls)

    def test_assign_tests_before_delete_and_delete_is_last_destructive_call(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        delete_at = h.first('api', 'delete_agent')
        self.assertLess(h.last('api', 'assign_tests'), delete_at)
        self.assertLess(h.last('api', 'set_test_enabled'), delete_at)
        self.assertLess(h.first('api', 'cluster_assign'), h.first('api', 'set_agent_account_groups'))
        self.assertLess(h.first('api', 'set_agent_account_groups'), h.first('api', 'assign_tests'))
        self.assertLess(h.last('ui', 'set_account_group_token'), delete_at)
        after = [e[1] for e in h.world.events[delete_at:] if e[0] == 'api' and e[1] in WRITE_CALLS]
        self.assertEqual(sorted(set(after)), ['assign_tag', 'delete_agent', 'update_agent_name'])
        self.assertLess(delete_at, h.first('api', 'update_agent_name'))
        self.assertLess(h.first('api', 'update_agent_name'), h.first('api', 'assign_tag'))

    def test_delete_old_members_as_enterprise_in_owner_group_and_wait_absent(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        deletes = [c for c in h.api.calls if c[0] == 'delete_agent']
        self.assertEqual(deletes, [('delete_agent', SOURCE, '11', 'enterprise'), ('delete_agent', SOURCE, '12', 'enterprise')])
        self.assertIn(('wait_for_agent_absent', SOURCE, '11'), h.api.calls)
        self.assertIn(('wait_for_agent_absent', SOURCE, '12'), h.api.calls)
        self.assertEqual([c for c in deletes if c[3] == 'enterprise-cluster'], [])

    def test_share_back_sends_full_list_target_first(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        puts = [c for c in h.api.calls if c[0] == 'set_agent_account_groups']
        self.assertEqual(puts, [('set_agent_account_groups', TARGET, '91', [TARGET, SOURCE, OTHER])])

    def test_tests_assigned_per_group_with_group_aid(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        assigns = [c for c in h.api.calls if c[0] == 'assign_tests']
        self.assertEqual(assigns, [('assign_tests', SOURCE, '91', ['101', '102']), ('assign_tests', OTHER, '91', ['201'])])

    def test_rename_and_tags_use_new_cluster_id(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item(tags=[{'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A'},
                                               {'aid': OTHER, 'tagId': 't2', 'key': 'role', 'value': 'edge'}]))
        self.assertIn(('update_agent_name', TARGET, '91', NAME), h.api.calls)
        self.assertEqual([c for c in h.api.calls if c[0] == 'assign_tag'], [
            ('assign_tag', SOURCE, 't1', [{'id': '91', 'type': 'v-agent'}]),
            ('assign_tag', OTHER, 't2', [{'id': '91', 'type': 'v-agent'}])])

    def test_no_rename_call_when_name_already_equal(self):
        h = Harness()
        h.world.new_name = NAME
        migrate_cluster(h.ctx, make_item())
        self.assertEqual([c for c in h.api.calls if c[0] == 'update_agent_name'], [])

    def test_tag_failure_is_not_fatal(self):
        h = Harness()
        h.world.tag_fail = True
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('tag restore failed', result.warnings[0])
        self.assertEqual(h.console.steps(StepStatus.FAILED), [])

    def test_manual_notification_rules_skipped_line(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        rows = [r for r in h.console.statuses if r[2] == 'manual_notification_rules']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], StepStatus.SKIPPED)
        self.assertIn('by hand', rows[0][3])

    def test_ui_gets_snapshot_values(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        self.assertIn(('ui', 'set_account_group_token', '10.0.0.5', False, True), h.world.events)
        self.assertIn(('ui', 'set_account_group_token', '10.0.0.6', False, True), h.world.events)
        self.assertEqual(h.world.token_seen, [FAKE_TOKEN, FAKE_TOKEN])

    def test_reset_that_keeps_the_old_id_aborts_before_assign_and_delete(self):
        h = Harness()
        h.world.defs = [('91', 'EA-A', '10.0.0.5'), ('12', 'EA-B', '10.0.0.6')]
        members = [{'memberId': '91', 'name': 'EA-A', 'ipAddresses': ['10.0.0.5'], 'state': 'online'},
                   {'memberId': '12', 'name': 'EA-B', 'ipAddresses': ['10.0.0.6'], 'state': 'online'}]
        # the "new" agent has the id of the old member: no new identity, nothing may be deleted
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item(members=members))
        self.assertIn('no new identity', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'delete_agent', 'assign_tests')], [])

    def test_delete_step_skips_ids_equal_to_new_ids(self):
        h = Harness()
        item = make_item()
        progress = _Progress(item, SOURCE, TARGET)
        progress.tests_verified = True
        # the new base agent has the same id (11) as old member 11; old member 12 is a standalone agent in the owner group
        h.world.new_cluster = {'base': '11', 'others': ['92']}
        h.world.agents[SOURCE] = [AgentRecord('12', 'EA-B', 'EA-B', ['10.0.0.6'], 'offline', SOURCE,
                                              agent_type='enterprise')]
        h.world.agents[TARGET] = [AgentRecord('11', 'n', 'n', ['10.0.0.5'], 'online', TARGET),
                                  AgentRecord('92', 'n2', 'n2', ['10.0.0.6'], 'online', TARGET)]
        h.world.cluster_exists = False
        NEW_IDS['10.0.0.5'] = '11'
        try:
            _delete_old(h.ctx, item, item.name, progress, ['11', '92'], '11')
        finally:
            NEW_IDS['10.0.0.5'] = '91'
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'],
                         [('delete_agent', SOURCE, '12', 'enterprise')])


class ClusterEnabledAndChunkingTests(unittest.TestCase):
    def test_enabled_state_is_restored_for_tests_that_were_enabled(self):
        h = Harness(disabled=('102',))
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        sets = [c for c in h.api.calls if c[0] == 'set_test_enabled']
        self.assertEqual(sets, [('set_test_enabled', SOURCE, 'http-server', '101', True),
                                ('set_test_enabled', OTHER, 'http-server', '201', True)])
        self.assertTrue(h.world.tests[SOURCE]['101']['enabled'])
        self.assertTrue(h.world.tests[OTHER]['201']['enabled'])
        self.assertFalse(h.world.tests[SOURCE]['102']['enabled'])   # was disabled before: stays disabled
        self.assertEqual(h.console.steps(StepStatus.FAILED), [])

    def test_already_enabled_tests_are_not_touched(self):
        h = Harness()
        h.world.disable_on_unassign = False
        migrate_cluster(h.ctx, make_item())
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_test_enabled'], [])

    def test_enable_failure_is_loud_lists_ids_and_does_not_block_delete(self):
        h = Harness()
        h.world.enable_fail = set(['101'])
        result = migrate_cluster(h.ctx, make_item())
        self.assertFalse(result.ok)
        self.assertIn('cluster_restore_tests_enabled', h.console.steps(StepStatus.FAILED))
        self.assertTrue(any('101' in w for w in result.warnings))
        self.assertTrue(h.world.tests[OTHER]['201']['enabled'])
        self.assertIn(('delete_agent', SOURCE, '11', 'enterprise'), h.api.calls)
        self.assertIn('NOT ENABLED AGAIN', result.detail)

    def test_snapshot_without_enabled_flag_reads_the_test(self):
        h = Harness()
        original = h.api.list_tests

        def no_flag(aid):
            rows = original(aid)
            for r in rows:
                r.raw.pop('enabled')
            return rows

        h.api.list_tests = no_flag
        h.world.disable_on_unassign = False
        # the restore step lists tests again and then sees no 'enabled' key; tests are really enabled
        migrate_cluster(h.ctx, make_item())
        self.assertTrue(any(c[0] == 'get_test' for c in h.api.calls))

    def test_large_test_list_is_assigned_in_chunks_of_100(self):
        ids = ['%d' % (1000 + n) for n in range(572)]
        h = Harness(tests={SOURCE: ids}, disabled=())
        result = migrate_cluster(h.ctx, make_item(tests={SOURCE: ids}))
        self.assertTrue(result.ok)
        assigns = [c for c in h.api.calls if c[0] == 'assign_tests']
        self.assertEqual([len(c[3]) for c in assigns], [100, 100, 100, 100, 100, 72])
        self.assertEqual([t for c in assigns for t in c[3]], ids)
        self.assertEqual(set(c[1] for c in assigns), set([SOURCE]))
        self.assertEqual(h.world.assigned[(SOURCE, '91')], ids)

    def test_exactly_100_tests_is_one_call(self):
        ids = ['%d' % (1000 + n) for n in range(100)]
        h = Harness(tests={SOURCE: ids})
        migrate_cluster(h.ctx, make_item(tests={SOURCE: ids}))
        self.assertEqual(len([c for c in h.api.calls if c[0] == 'assign_tests']), 1)

    def test_chunk_failure_state_line_says_how_many_were_assigned(self):
        ids = ['%d' % (1000 + n) for n in range(250)]
        h = Harness(tests={SOURCE: ids})
        original = h.api.assign_tests

        def flaky(aid, agent_id, test_ids):
            if test_ids[0] == ids[200]:
                raise ApiError('HTTP 429 persists')
            original(aid, agent_id, test_ids)

        h.api.assign_tests = flaky
        with self.assertRaises(ApiError):
            migrate_cluster(h.ctx, make_item(tests={SOURCE: ids}))
        self.assertIn('tests assigned (200)', h.stop_line())
        self.assertIn('NOT verified', h.stop_line())
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])


class ClusterShareTests(unittest.TestCase):
    def test_empty_share_list_is_never_sent(self):
        h = Harness(tests={})
        result = migrate_cluster(h.ctx, make_item(shares=[], tests={}))
        self.assertTrue(result.ok)
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_agent_account_groups'], [])
        self.assertIn(('cluster_share_back', StepStatus.SKIPPED), [(r[2], r[0]) for r in h.console.statuses])

    def test_every_put_is_non_empty_and_starts_with_target(self):
        for shares, tests in (([SOURCE], {SOURCE: ['101']}), ([], {OTHER: ['201']}), ([SOURCE, OTHER], {})):
            h = Harness(tests=tests)
            migrate_cluster(h.ctx, make_item(shares=shares, tests=tests))
            for call in h.api.calls:
                if call[0] == 'set_agent_account_groups':
                    self.assertTrue(call[3])
                    self.assertEqual(call[3][0], TARGET)
                    self.assertEqual(len(call[3]), len(set(call[3])))

    def test_groups_with_tests_are_added_to_share_list(self):
        h = Harness(tests={OTHER: ['201']})
        migrate_cluster(h.ctx, make_item(shares=[SOURCE], tests={OTHER: ['201']}))
        puts = [c for c in h.api.calls if c[0] == 'set_agent_account_groups']
        self.assertEqual(puts[0][3], [TARGET, SOURCE, OTHER])

    def test_share_not_applied_aborts_before_tests_and_delete(self):
        h = Harness()
        h.world.share_put_ignored = True
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('NOT deleted', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] in ('assign_tests', 'delete_agent')], [])


class ClusterSafetyTests(unittest.TestCase):
    def test_wrong_group_on_member_2_stops_further_members(self):
        h = Harness(count=3)
        h.world.wrong_group_ips = set(['10.0.0.6'])
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item(count=3))
        self.assertIn('wrong group', str(caught.exception))
        self.assertEqual(h.opened, ['10.0.0.5', '10.0.0.6'])          # member 3 was never opened
        self.assertEqual(h.indexes('ui', 'reset_agent', '10.0.0.7'), [])
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'assign_tests', 'delete_agent',
                                                               'set_agent_account_groups')], [])
        self.assertIn('cluster_member_api_target_online', h.console.steps(StepStatus.FAILED))
        self.assertIn('wrong group', h.stop_line() + str(caught.exception))
        self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_wrong_group_on_member_1_never_touches_member_2(self):
        h = Harness()
        h.world.wrong_group_ips = set(['10.0.0.5'])
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(h.opened, ['10.0.0.5'])
        self.assertEqual(h.indexes('ui', 'reset_agent', '10.0.0.6'), [])

    def test_member_2_ui_failure_leaves_clear_state_and_no_delete(self):
        h = Harness()
        h.world.ui_fail[('10.0.0.6', 'reset_agent')] = UiError('reset not confirmed')
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertEqual([c for c in h.api.calls if c[0] in ('delete_agent', 'cluster_assign', 'assign_tests')], [])
        state = h.stop_line()
        self.assertIn('cluster %s was unassigned' % CLUSTER_ID, state)
        self.assertIn('reset command sent to: EA-A (10.0.0.5), EA-B (10.0.0.6)', state)
        self.assertIn('target token submitted for: EA-A (10.0.0.5)', state)
        self.assertIn('new agents online in target aid 9: [91]', state)
        self.assertIn('member 2/2 EA-B (10.0.0.6)', state)
        self.assertIn('new cluster is NOT built', state)
        self.assertIn('old member agents are still present', state)
        self.assertIn('cluster_member_ui_reset', h.console.steps(StepStatus.FAILED))

    def test_failure_state_line_prints_old_data_for_manual_recovery(self):
        h = Harness(disabled=('102',))
        h.world.ui_fail_after_unassign[('10.0.0.6', 'authenticate')] = UiError('login refused')
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        state = h.stop_line()
        self.assertIn('RECOVERY DATA', state)
        self.assertIn('old cluster id 11', state)
        self.assertIn('tests in aid 1: [101,102]', state)
        self.assertIn('tests in aid 2: [201]', state)
        self.assertIn('enabled before in aid 1: [101]', state)
        self.assertIn('disabled before in aid 1: [102]', state)
        self.assertIn('enabled before in aid 2: [201]', state)
        self.assertIn('members 11 EA-A 10.0.0.5; 12 EA-B 10.0.0.6', state)
        self.assertIn('tags: site=A (aid 1)', state)

    def test_member_1_not_reset_state_when_login_fails(self):
        h = Harness()
        h.world.ui_fail_after_unassign[('10.0.0.5', 'authenticate')] = UiError('login refused')
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        state = h.stop_line()
        self.assertIn('not reset yet (still standalone in the source group): EA-A (10.0.0.5), EA-B (10.0.0.6)', state)
        self.assertNotIn('reset command sent to', state)
        self.assertEqual(h.indexes('ui', 'reset_agent', '10.0.0.5'), [])

    def test_token_ui_error_becomes_flow_aborted_with_state(self):
        h = Harness()
        h.world.ui_fail[('10.0.0.5', 'set_account_group_token')] = UiError('rejected')
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('manual action needed', str(caught.exception))
        self.assertIn('reset without a confirmed target token', h.stop_line())

    def test_unassign_not_confirmed_aborts_before_any_reset(self):
        h = Harness()
        h.world.unassign_ignored = True
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('still listed', str(caught.exception))
        self.assertEqual(h.opened, [])
        self.assertIn('NOT confirmed', h.stop_line())
        self.assertTrue(h.slept)

    def test_new_member_offline_aborts_before_tests_and_delete(self):
        h = Harness()
        h.world.new_member_offline = '92'
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('not online', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] in ('assign_tests', 'delete_agent')], [])
        self.assertIn('cluster_assign', h.console.steps(StepStatus.FAILED))

    def test_missing_tests_after_assign_abort_before_delete(self):
        h = Harness()
        h.world.assign_ignored = True
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('NOT deleted', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])
        self.assertIn('cluster_assign_tests', h.console.steps(StepStatus.FAILED))

    def test_delete_refuses_an_old_id_that_is_a_cluster(self):
        h = Harness()
        h.world.ghost_cluster_id = '12'
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('it is a cluster', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent' and c[2] == '12'], [])
        for call in h.api.calls:
            if call[0] == 'delete_agent':
                self.assertEqual(call[3], 'enterprise')

    def test_cluster_id_is_never_deleted_with_cluster_type(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        for call in h.api.calls:
            if call[0] == 'delete_agent':
                self.assertNotEqual(call[3], 'enterprise-cluster')
                self.assertIsNotNone(call[3])

    def test_existing_online_target_agent_with_member_ip_blocks_everything(self):
        h = Harness()
        h.world.agents[TARGET].append(AgentRecord('55', 'other-name', 'other-name', ['10.0.0.6'], 'online', TARGET))
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(h.opened, [])
        self.assertEqual(h.writes(), [])
        self.assertIn('nothing changed', h.stop_line())

    def test_member_never_online_aborts_without_assign_or_delete(self):
        h = Harness()
        h.world.never_online = set(['10.0.0.6'])
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('did not appear online', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'delete_agent', 'assign_tests')], [])


class ClusterTokenTests(unittest.TestCase):
    def test_token_never_in_console_output(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        self.assertEqual(h.world.token_seen, [FAKE_TOKEN, FAKE_TOKEN])
        self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_fake_token_has_32_characters_and_is_alphanumeric(self):
        self.assertEqual(len(FAKE_TOKEN), 32)
        self.assertTrue(FAKE_TOKEN.isalnum())

    def test_token_not_printed_on_failure(self):
        for setup in (lambda w: w.wrong_group_ips.add('10.0.0.6'),
                      lambda w: w.ui_fail.__setitem__(('10.0.0.6', 'reset_agent'), UiError('x'))):
            h = Harness()
            setup(h.world)
            with self.assertRaises(FlowAborted):
                migrate_cluster(h.ctx, make_item())
            self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_token_not_printed_in_dry_run(self):
        h = Harness(apply=False)
        migrate_cluster(h.ctx, make_item())
        self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_apply_without_token_aborts_before_any_call(self):
        h = Harness(token='')
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(h.api.calls, [])
        self.assertEqual(h.opened, [])


class ClusterValidationTests(unittest.TestCase):
    def assert_refused(self, item, text, apply=True):
        h = Harness(apply=apply)
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, item)
        self.assertIn(text, str(caught.exception))
        self.assertEqual(h.api.calls, [])
        self.assertEqual(h.opened, [])
        self.assertEqual(h.world.events, [])
        self.assertEqual(h.console.steps(StepStatus.FAILED), ['cluster_preflight_snapshot'])
        self.assertIn('nothing changed', h.stop_line())

    def test_offline_member_refused(self):
        for apply in (True, False):
            members = member_dicts(2)
            members[1]['state'] = 'offline'
            self.assert_refused(make_item(members=members), 'must be online', apply)

    def test_member_without_state_refused(self):
        members = member_dicts(2)
        members[0]['state'] = ''
        self.assert_refused(make_item(members=members), 'unknown state')

    def test_member_without_ip_refused(self):
        for apply in (True, False):
            members = member_dicts(2)
            members[1]['ipAddresses'] = []
            self.assert_refused(make_item(members=members), 'no IP address', apply)

    def test_no_members_refused(self):
        self.assert_refused(make_item(members=[]), 'no members')

    def test_single_member_refused(self):
        self.assert_refused(make_item(members=member_dicts(1)), 'at least 2')

    def test_item_not_ready_refused(self):
        for status in ('blocked', 'excluded'):
            self.assert_refused(make_item(status=status), 'not ready')

    def test_standalone_kind_refused(self):
        self.assert_refused(make_item(kind='agent'), 'cluster flow handles kind cluster only')

    def test_record_not_a_cluster_refused(self):
        self.assert_refused(make_item(cluster_flag=False), 'is not a cluster')

    def test_missing_record_refused(self):
        self.assert_refused(make_item(record=False), 'no cluster record')

    def test_duplicate_member_ip_refused(self):
        members = member_dicts(2)
        members[1]['ipAddresses'] = ['10.0.0.5']
        self.assert_refused(make_item(members=members), 'duplicate')


class ClusterDryRunTests(unittest.TestCase):
    READ_CALLS = ('list_agents', 'list_tests', 'get_test', 'list_agents_with_clusters', 'get_agent')

    def test_dry_run_makes_only_get_calls_and_opens_no_ui(self):
        h = Harness(apply=False)
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, 'dry-run')
        self.assertIsNone(result.new_cluster_id)
        self.assertIsNone(result.snapshot)
        self.assertTrue(h.api.calls)
        self.assertEqual([c for c in h.api.calls if c[0] not in self.READ_CALLS], [])
        self.assertEqual(h.writes(), [])
        self.assertEqual([e for e in h.world.events if e[0] == 'ui'], [])
        self.assertEqual(h.opened, [])
        self.assertEqual(h.slept, [])

    def test_dry_run_runs_the_enabled_state_snapshot_and_prints_it(self):
        h = Harness(apply=False, disabled=('102',))
        migrate_cluster(h.ctx, make_item())
        self.assertIn(('list_tests', SOURCE), h.api.calls)
        self.assertIn(('list_tests', OTHER), h.api.calls)
        self.assertTrue(any('snapshot: 3 tests saved: 2 enabled, 1 disabled, 0 unknown' in m for _, m in h.console.infos))

    def test_dry_run_prints_preflight_ok_and_every_step_as_would_changed(self):
        h = Harness(apply=False, token='')
        migrate_cluster(h.ctx, make_item())
        rows = h.console.statuses
        self.assertEqual(rows[0][2:3], ('cluster_preflight_snapshot',))
        self.assertEqual(rows[0][0], StepStatus.OK)
        for row in rows[1:-1]:
            self.assertEqual(row[0], StepStatus.CHANGED, row)
            self.assertTrue(row[3].startswith('would:'), row)
        self.assertEqual(rows[-1][2], 'manual_notification_rules')
        self.assertEqual(rows[-1][0], StepStatus.SKIPPED)
        self.assertEqual([r[2] for r in rows], [s for s, _ in plan_cluster(make_item())])
        self.assertEqual(h.console.steps(StepStatus.FAILED), [])

    def test_dry_run_fails_like_apply_when_target_has_an_online_agent_with_a_member_ip(self):
        verdicts = []
        for apply in (False, True):
            h = Harness(apply=apply)
            h.world.agents[TARGET].append(AgentRecord('55', 'x', 'x', ['10.0.0.6'], 'online', TARGET))
            with self.assertRaises(FlowAborted):
                migrate_cluster(h.ctx, make_item())
            verdicts.append(h.console.steps(StepStatus.FAILED))
            self.assertEqual(h.writes(), [])
            self.assertEqual(h.opened, [])
        self.assertEqual(verdicts[0], ['cluster_preflight_snapshot'])
        self.assertEqual(verdicts[0], verdicts[1])

    def test_dry_run_cannot_write_even_if_the_flow_tried(self):
        h = Harness(apply=False)

        def bad_check(ctx, item, ignore_ids):
            ctx.api.cluster_unassign(SOURCE, CLUSTER_ID, ['11', '12'])

        with mock.patch.object(cluster_flow, '_require_clean_target', bad_check):
            with self.assertRaises(RuntimeError) as caught:
                migrate_cluster(h.ctx, make_item())
        self.assertIn('write call in dry run: cluster_unassign', str(caught.exception))
        self.assertEqual(h.writes(), [])
        self.assertIsInstance(h.ctx.api, ReadOnlyApi)

    def test_dry_run_writes_no_snapshot_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, 'snaps')
        h = Harness(apply=False, snapshot_dir=target)
        migrate_cluster(h.ctx, make_item())
        self.assertFalse(os.path.exists(target))
        self.assertTrue(any('would be written' in m for _, m in h.console.infos))

    def test_dry_run_details_are_concrete(self):
        h = Harness(apply=False)
        migrate_cluster(h.ctx, make_item())
        rows = dict((r[2], r[3]) for r in h.console.statuses)
        self.assertIn('unassign all 2 members [11,12]', rows['cluster_unassign'])
        self.assertIn('MAINTENANCE WINDOW STARTED', rows['cluster_unassign'])
        self.assertEqual(rows['cluster_share_back'], 'would: share the new cluster with aids [9,1,2]')
        self.assertIn('2 tests in aid 1', rows['cluster_assign_tests'])
        self.assertIn('1 tests in aid 2', rows['cluster_assign_tests'])
        self.assertIn('would: delete the 2 old member agents', rows['cluster_delete_old'])

    def test_dry_run_shows_chunk_count(self):
        ids = ['%d' % (1000 + n) for n in range(572)]
        text = dict(plan_cluster(make_item(tests={SOURCE: ids}), TARGET))['cluster_assign_tests']
        self.assertIn('572 tests in aid 1 (6 calls of up to 100)', text)

    def test_dry_run_with_real_console_has_no_failures(self):
        h = Harness(apply=False)
        stream = io.StringIO()
        console = MigrationConsole(stream=stream, use_color=False)
        h.ctx.console = console
        migrate_cluster(h.ctx, make_item())
        self.assertFalse(console.has_failures())
        self.assertIn('changed:  [%s] => would: ' % NAME, stream.getvalue())
        self.assertIn('skipped:  [%s] => restore agent notification rules' % NAME, stream.getvalue())


class ClusterStrictWaitTests(unittest.TestCase):
    def test_wait_gets_target_snapshot_old_ids_and_earlier_members(self):
        h = Harness()
        h.world.agents[TARGET].append(AgentRecord('50', 'someone', 'someone', ['10.7.7.7'], 'offline', TARGET))
        migrate_cluster(h.ctx, make_item())
        first, second = h.api.wait_calls
        self.assertEqual(first['ip'], '10.0.0.5')
        self.assertTrue(set(['50', '11', '12']) <= first['known_ids'])
        self.assertNotIn('91', first['known_ids'])
        self.assertIn('91', second['known_ids'])      # the agent taken by member 1 is not a candidate for member 2

    def test_foreign_agent_with_member_ip_but_other_name_is_never_taken(self):
        h = Harness()
        h.world.never_online = set(['10.0.0.5'])   # the real member 1 never registers; a foreign box shows up
        h.world.extra_on_register = {'10.0.0.5': [AgentRecord('77', 'other-customer', 'other-customer', ['10.0.0.5'],
                                                              'online', TARGET, agent_type='enterprise')]}
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('did not appear online', str(caught.exception))
        self.assertEqual(h.opened, ['10.0.0.5'])
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'delete_agent', 'assign_tests',
                                                               'set_agent_account_groups')], [])

    def test_foreign_agent_with_member_name_but_other_ip_is_ignored(self):
        h = Harness()
        h.world.never_online = set(['10.0.0.5'])
        h.world.agents[TARGET].append(AgentRecord('77', 'EA-A', 'EA-A', ['10.200.0.1'], 'offline', TARGET))
        h.world.extra_on_register = {'10.0.0.6': []}
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(h.opened, ['10.0.0.5'])
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'delete_agent')], [])

    def test_two_qualifying_agents_abort_and_list_ids(self):
        h = Harness()
        h.world.extra_on_register = {'10.0.0.5': [AgentRecord('78', 'EA-A-clone', 'EA-A-clone', ['10.0.0.5'],
                                                              'online', TARGET, agent_type='enterprise')]}
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        text = str(caught.exception)
        self.assertIn('more than one new online agent', text)
        self.assertIn('78', text)
        self.assertIn('91', text)
        self.assertEqual(h.opened, ['10.0.0.5'])
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'delete_agent', 'assign_tests')], [])
        self.assertIn('cluster_member_api_target_online', h.console.steps(StepStatus.FAILED))


class ClusterOldMemberMappingTests(unittest.TestCase):
    def deletes(self, h):
        return [c for c in h.api.calls if c[0] == 'delete_agent']

    def test_delete_uses_standalone_ids_from_the_listing_not_member_ids(self):
        h = Harness()
        h.world.standalone_ids = {'11': '211', '12': '212'}
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(self.deletes(h), [('delete_agent', SOURCE, '211', 'enterprise'),
                                           ('delete_agent', SOURCE, '212', 'enterprise')])
        self.assertIn(('wait_for_agent_absent', SOURCE, '211'), h.api.calls)
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent' and c[2] in ('11', '12')], [])
        self.assertTrue(any('11 -> 211' in m and '12 -> 212' in m for _, m in h.console.infos))

    def test_first_member_that_keeps_the_old_cluster_id_is_deleted_as_standalone(self):
        h = Harness()
        h.world.standalone_ids = {'11': CLUSTER_ID, '12': '212'}   # first member keeps the cluster id
        migrate_cluster(h.ctx, make_item())
        self.assertEqual(self.deletes(h), [('delete_agent', SOURCE, '11', 'enterprise'),
                                           ('delete_agent', SOURCE, '212', 'enterprise')])
        self.assertNotIn('enterprise-cluster', [c[3] for c in self.deletes(h)])

    def test_unmapped_member_is_skipped_with_warning_and_the_rest_deleted(self):
        h = Harness()
        h.world.unmappable = set(['12'])
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(self.deletes(h), [('delete_agent', SOURCE, '11', 'enterprise')])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('EA-B (10.0.0.6)', result.warnings[0])
        self.assertIn('12 -> not mapped', ' '.join(m for _, m in h.console.infos))
        rows = [r for r in h.console.statuses if r[2] == 'cluster_delete_old']
        self.assertEqual(rows[0][0], StepStatus.CHANGED)
        self.assertIn('1 not mapped', rows[0][3])

    def test_nothing_mapped_prints_skipped_and_warning_with_member_list(self):
        h = Harness()
        h.world.unmappable = set(['11', '12'])
        result = migrate_cluster(h.ctx, make_item())
        self.assertEqual(self.deletes(h), [])
        rows = [r for r in h.console.statuses if r[2] == 'cluster_delete_old']
        self.assertEqual([r[0] for r in rows], [StepStatus.SKIPPED])
        self.assertNotIn('deleted', rows[0][3].replace('was deleted', ''))
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('11 EA-A (10.0.0.5)', result.warnings[0])
        self.assertIn('12 EA-B (10.0.0.6)', result.warnings[0])
        self.assertTrue(any(m.startswith('warning:') for _, m in h.console.infos))
        # the rest of the flow still runs
        self.assertIn('cluster_rename', h.console.steps())

    def test_same_ip_twice_prefers_the_member_name(self):
        h = Harness()
        original = h.world.old_standalone

        def with_twin():
            return original() + [AgentRecord('300', 'twin', 'twin', ['10.0.0.5'], 'online', SOURCE,
                                             agent_type='enterprise')]
        h.world.old_standalone = with_twin
        migrate_cluster(h.ctx, make_item())
        self.assertEqual([c[2] for c in self.deletes(h)], ['11', '12'])

    def test_ambiguous_ip_without_name_match_stays_unmapped(self):
        h = Harness()
        original = h.world.old_standalone

        def twins():
            return [r for r in original() if r.agent_id != '11'] + [
                AgentRecord('301', 'twin-a', 'twin-a', ['10.0.0.5'], 'online', SOURCE, agent_type='enterprise'),
                AgentRecord('302', 'twin-b', 'twin-b', ['10.0.0.5'], 'online', SOURCE, agent_type='enterprise')]
        h.world.old_standalone = twins
        result = migrate_cluster(h.ctx, make_item())
        self.assertEqual([c[2] for c in self.deletes(h)], ['12'])
        self.assertEqual(len(result.warnings), 1)

    def test_listing_is_read_right_after_the_unassign_before_any_reset(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        unassign = h.first('api', 'cluster_unassign')
        reads = [i for i in h.indexes('api', 'list_agents_with_clusters', SOURCE) if i > unassign]
        self.assertTrue(any(i < h.last('ui', 'authenticate', '10.0.0.5') for i in reads))

    def test_delete_type_unknown_is_read_from_the_agent_or_refused(self):
        h = Harness()
        original = h.world.old_standalone

        def untyped():
            rows = original()
            for r in rows:
                r.agent_type = None
            return rows
        h.world.old_standalone = untyped
        with self.assertRaises(ApiError):
            migrate_cluster(h.ctx, make_item())   # get_agent of an old member is not available in the fake
        self.assertEqual(self.deletes(h), [])


class ClusterDeleteStateTests(unittest.TestCase):
    def test_absent_wait_timeout_keeps_unconfirmed_state(self):
        h = Harness()
        h.world.absent_timeout = True
        with self.assertRaises(VisibilityTimeout):
            migrate_cluster(h.ctx, make_item())
        line = h.stop_line()
        self.assertIn('delete sent, not confirmed for old members [11]', line)
        self.assertNotIn('old members deleted', line)
        self.assertIn('cluster_delete_old', h.console.steps(StepStatus.FAILED))

    def test_confirmed_deletes_are_listed_after_a_later_failure(self):
        h = Harness()
        h.api.update_agent_name = lambda *a, **k: (_ for _ in ()).throw(ApiError('rename failed'))
        with self.assertRaises(ApiError):
            migrate_cluster(h.ctx, make_item())
        line = h.stop_line()
        self.assertIn('old members deleted: [11,12]', line)
        self.assertNotIn('delete sent, not confirmed', line)


class ClusterInterruptTests(unittest.TestCase):
    def test_keyboard_interrupt_after_unassign_sent_prints_state_and_recovery_data(self):
        h = Harness(disabled=('102',))
        h.world.interrupt_at = 'cluster_unassign'
        with self.assertRaises(KeyboardInterrupt):
            migrate_cluster(h.ctx, make_item())
        line = h.stop_line()
        self.assertIn('NOT confirmed', line)
        self.assertIn('RECOVERY DATA', line)
        self.assertIn('enabled before in aid 1: [101]', line)
        self.assertIn('interrupted by KeyboardInterrupt', line)
        self.assertEqual(h.console.steps(StepStatus.FAILED), ['cluster_unassign'])
        self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_keyboard_interrupt_during_a_member_reset(self):
        h = Harness()
        h.world.interrupt_at = 'reset_agent'
        with self.assertRaises(KeyboardInterrupt):
            migrate_cluster(h.ctx, make_item())
        line = h.stop_line()
        self.assertIn('reset command sent to: EA-A (10.0.0.5)', line)
        self.assertIn('RECOVERY DATA', line)
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'delete_agent')], [])

    def test_keyboard_interrupt_while_building_the_cluster(self):
        h = Harness()
        h.world.interrupt_at = 'cluster_assign'
        with self.assertRaises(KeyboardInterrupt):
            migrate_cluster(h.ctx, make_item())
        self.assertIn('cluster assign was sent', h.stop_line())

    def test_system_exit_is_reraised(self):
        h = Harness()

        def boom(*args, **kwargs):
            raise SystemExit(2)
        h.api.cluster_unassign = boom
        with self.assertRaises(SystemExit):
            migrate_cluster(h.ctx, make_item())
        self.assertIn('interrupted by SystemExit', h.stop_line())

    def test_interrupt_in_dry_run_reraises(self):
        h = Harness(apply=False)

        def boom(*args, **kwargs):
            raise KeyboardInterrupt()
        h.api.list_agents = boom
        with self.assertRaises(KeyboardInterrupt):
            migrate_cluster(h.ctx, make_item())
        self.assertIn('state:', h.stop_line())


class ClusterWriteShapeTests(unittest.TestCase):
    def test_cluster_assign_that_returns_an_array_is_fine(self):
        h = Harness()
        h.world.assign_returns_list = True
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(result.new_cluster_id, '91')


class ClusterSnapshotFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, 'snaps')

    def files(self):
        return sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []

    def test_file_is_written_before_the_unassign_with_modes_and_no_token(self):
        h = Harness(snapshot_dir=self.dir, disabled=('102',))
        seen = []
        original = FakeApi.cluster_unassign

        def spy(api, aid, cluster_id, member_ids):
            seen.append(list(self.files()))
            return original(api, aid, cluster_id, member_ids)

        with mock.patch.object(FakeApi, 'cluster_unassign', spy):
            result = migrate_cluster(h.ctx, make_item(tags=[
                {'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A', 'authToken': FAKE_TOKEN}]))
        self.assertEqual(len(seen[0]), 1)
        path = os.path.join(self.dir, self.files()[0])
        self.assertEqual(result.snapshot, path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        with open(path) as handle:
            text = handle.read()
        self.assertNotIn(FAKE_TOKEN, text)
        data = json.loads(text)
        self.assertEqual(data['record']['agent_id'], CLUSTER_ID)
        self.assertEqual([m['memberId'] for m in data['members']], ['11', '12'])
        self.assertEqual(data['share_aids'], [SOURCE, OTHER])
        self.assertEqual(data['tests_by_aid'], {SOURCE: ['101', '102'], OTHER: ['201']})
        self.assertEqual(data['extra']['enabled'], {SOURCE: {'101': True, '102': False}, OTHER: {'201': True}})
        self.assertEqual(data['tags'], [{'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A'}])
        self.assertRegex(self.files()[0], r'^\d{8}T\d{6}Z_SEDC-CLUSTER-01\.json$')

    def test_without_directory_no_file_and_info_line(self):
        h = Harness()
        result = migrate_cluster(h.ctx, make_item())
        self.assertIsNone(result.snapshot)
        self.assertTrue(any('no snapshot directory set' in m for _, m in h.console.infos))

    def test_failure_state_line_names_the_file(self):
        h = Harness(snapshot_dir=self.dir)
        h.world.ui_fail[('10.0.0.6', 'reset_agent')] = UiError('x')
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertIn(os.path.join(self.dir, self.files()[0]), h.stop_line())

    def test_failure_before_the_file_says_not_written(self):
        h = Harness(snapshot_dir=self.dir)
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item(status='blocked'))
        self.assertIn('not written yet', h.stop_line())

    def test_unwritable_directory_stops_before_the_unassign(self):
        blocker = os.path.join(self.tmp, 'afile')
        open(blocker, 'w').close()
        h = Harness(snapshot_dir=os.path.join(blocker, 'sub'))
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('snapshot file could not be written', str(caught.exception))
        self.assertEqual(h.writes(), [])
        self.assertIn('cluster_snapshot_file', h.console.steps(StepStatus.FAILED))
        self.assertEqual(h.indexes('api', 'cluster_unassign'), [])


class ClusterUiIpTests(unittest.TestCase):
    def test_ui_ips_mapping_overrides_the_member_ip_for_the_ui_only(self):
        h = Harness()
        item = make_item()
        item.ui_ips = {'11': '10.50.0.5'}
        h.world.ui_alias = {'10.50.0.5': '10.0.0.5'}
        migrate_cluster(h.ctx, item)
        self.assertEqual(h.opened, ['10.50.0.5', '10.0.0.6'])
        self.assertEqual(h.api.wait_calls[0]['ip'], '10.0.0.5')

    def test_without_the_attribute_member_ips_are_used(self):
        h = Harness()
        migrate_cluster(h.ctx, make_item())
        self.assertEqual(h.opened, ['10.0.0.5', '10.0.0.6'])


class ClusterMemberUiCheckTests(unittest.TestCase):
    """H2: every member UI is checked BEFORE the unassign (apply only)."""

    def test_check_runs_for_every_member_before_the_unassign_and_prints_ok(self):
        h = Harness(count=3)
        migrate_cluster(h.ctx, make_item(count=3))
        unassign = h.first('api', 'cluster_unassign')
        self.assertEqual(h.checked, ['10.0.0.5', '10.0.0.6', '10.0.0.7'])
        for ip in h.checked:
            self.assertLess(h.first('ui', 'authenticate', ip), unassign)
            self.assertLess(h.first('ui', 'snapshot_state', ip), unassign)
            self.assertEqual(len(h.indexes('ui', 'snapshot_state', ip)), 1)   # the flags are kept, not read twice
        rows = [r for r in h.console.statuses if r[2] == 'cluster_member_ui_check']
        self.assertEqual([r[0] for r in rows], [StepStatus.OK] * 3)
        self.assertIn('member 2/3 EA-B (10.0.0.6)', rows[1][3])
        self.assertLess(h.console.steps().index('cluster_member_ui_check'), h.console.steps().index('cluster_unassign'))

    def test_member_2_login_failure_stops_before_any_change(self):
        h = Harness()
        h.world.ui_fail[('10.0.0.6', 'authenticate')] = UiError('login refused')
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('member 2/2 EA-B (10.0.0.6)', str(caught.exception))
        self.assertIn('nothing was changed', str(caught.exception))
        self.assertEqual(h.writes(), [])
        self.assertEqual(h.indexes('api', 'cluster_unassign'), [])
        self.assertEqual(h.indexes('ui', 'reset_agent'), [])
        failed = [r for r in h.console.statuses if r[0] == StepStatus.FAILED]
        self.assertEqual([r[2] for r in failed], ['cluster_member_ui_check'])
        oks = [r for r in h.console.statuses if r[2] == 'cluster_member_ui_check' and r[0] == StepStatus.OK]
        self.assertEqual(len(oks), 1)
        self.assertIn('nothing changed', h.stop_line())
        self.assertNotIn('MAINTENANCE WINDOW STARTED', h.console.text())
        self.assertEqual(h.ctx.snapshot_path, None)

    def test_snapshot_state_failure_and_factory_failure_also_stop(self):
        h = Harness()
        h.world.ui_fail[('10.0.0.5', 'snapshot_state')] = UiError('no settings')
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(h.indexes('api', 'cluster_unassign'), [])

        h = Harness()

        def broken(host):
            raise UiError('connection refused on 443')

        h.ctx.ui_factory = broken
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('443', str(caught.exception))
        self.assertEqual(h.writes(), [])

    def test_failed_check_writes_no_snapshot_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        h = Harness(snapshot_dir=tmp)
        h.world.ui_fail[('10.0.0.6', 'authenticate')] = UiError('login refused')
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(os.listdir(tmp), [])

    def test_each_member_keeps_its_own_ui_flags_for_the_token_step(self):
        h = Harness()
        h.world.ui_settings = {'10.0.0.5': ('yes', 'no'), '10.0.0.6': ('no', 'no')}
        migrate_cluster(h.ctx, make_item())
        self.assertIn(('ui', 'set_account_group_token', '10.0.0.5', True, False), h.world.events)
        self.assertIn(('ui', 'set_account_group_token', '10.0.0.6', False, False), h.world.events)

    def test_ui_flags_are_in_the_snapshot_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        h = Harness(snapshot_dir=tmp)
        h.world.ui_settings = {'10.0.0.5': ('yes', 'no')}
        migrate_cluster(h.ctx, make_item())
        files = os.listdir(tmp)
        self.assertEqual(len(files), 1)
        with open(os.path.join(tmp, files[0])) as handle:
            data = json.load(handle)
        self.assertEqual(data['extra']['ui_settings']['11'], {'browserbot': True, 'crash_reports': False})
        self.assertIn('enabled', data['extra'])

    def test_ui_ips_mapping_is_used_for_the_check(self):
        h = Harness()
        item = make_item()
        item.ui_ips = {'11': '10.50.0.5'}
        h.world.ui_alias = {'10.50.0.5': '10.0.0.5'}
        migrate_cluster(h.ctx, item)
        self.assertEqual(h.checked, ['10.50.0.5', '10.0.0.6'])

    def test_dry_run_opens_no_ui_and_prints_the_check_as_planned(self):
        h = Harness(apply=False)
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(h.checked, [])
        self.assertEqual(h.opened, [])
        self.assertEqual(h.world.events and [e for e in h.world.events if e[0] == 'ui'], [])
        rows = [r for r in h.console.statuses if r[2] == 'cluster_member_ui_check']
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row[0], StepStatus.CHANGED)
            self.assertTrue(row[3].startswith('would:'))
        self.assertEqual(h.writes(), [])

    def test_plan_has_one_check_per_member_before_the_unassign(self):
        names = [n for n, _ in plan_cluster(make_item(count=3))]
        self.assertEqual(names.count('cluster_member_ui_check'), 3)
        self.assertLess(max(i for i, n in enumerate(names) if n == 'cluster_member_ui_check'),
                        names.index('cluster_unassign'))


class ClusterMemberIdentityTests(unittest.TestCase):
    """H3: the member wait accepts the old standalone name, hostname and IPs."""

    def test_member_with_a_hostname_other_than_its_name_still_matches(self):
        h = Harness()
        h.world.hostnames = {'10.0.0.5': 'host-a.corp', '10.0.0.6': 'host-b.corp'}
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        first = h.api.wait_calls[0]
        self.assertEqual(first['hostname'], 'EA-A')
        self.assertIn('host-a.corp', first['alt_names'])
        self.assertIn('EA-A', first['alt_names'])
        self.assertIn('10.0.0.5', first['alt_ips'])
        self.assertIn('host-b.corp', h.api.wait_calls[1]['alt_names'])

    def test_all_ips_of_the_old_record_are_accepted(self):
        h = Harness()
        h.world.old_extra_ips = {'10.0.0.5': ['10.0.5.5']}
        h.world.new_ips = {'10.0.0.5': ['10.0.5.5']}   # the new agent registers with the second address only
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertIn('10.0.5.5', h.api.wait_calls[0]['alt_ips'])

    def test_foreign_agent_with_the_right_hostname_but_another_ip_is_rejected(self):
        h = Harness()
        h.world.hostnames = {'10.0.0.5': 'host-a.corp'}
        h.world.never_online = set(['10.0.0.5'])
        h.world.extra_on_register = {'10.0.0.5': [AgentRecord('77', 'host-a.corp-zz', 'host-a.corp', ['10.99.0.1'],
                                                              'online', TARGET, agent_type='enterprise')]}
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertIn('did not appear online', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] in ('cluster_assign', 'delete_agent', 'assign_tests')], [])

    def test_foreign_agent_with_a_known_ip_but_a_foreign_name_is_rejected(self):
        h = Harness()
        h.world.hostnames = {'10.0.0.5': 'host-a.corp'}
        h.world.never_online = set(['10.0.0.5'])
        h.world.extra_on_register = {'10.0.0.5': [AgentRecord('77', 'stranger', 'stranger', ['10.0.0.5'],
                                                              'online', TARGET, agent_type='enterprise')]}
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())

    def test_unmapped_member_falls_back_to_the_member_name_and_ip(self):
        h = Harness()
        h.world.unmappable = set(['11'])
        migrate_cluster(h.ctx, make_item())
        first = h.api.wait_calls[0]
        self.assertEqual(first['alt_names'], ['EA-A'])
        self.assertEqual(first['alt_ips'], ['10.0.0.5'])

    def test_source_side_check_needs_ip_and_name(self):
        h = Harness()
        item = make_item()
        member = item.members[0]
        ignore = set(['11', '12'])
        h.world.agents[SOURCE].append(AgentRecord('70', 'unrelated', 'unrelated', ['10.0.0.5'], 'online', SOURCE))
        h.world.agents[SOURCE].append(AgentRecord('71', 'EA-A', 'EA-A', ['10.7.7.7'], 'online', SOURCE))
        cluster_flow._require_no_new_source_identity(h.ctx, item, member, ignore)
        h.world.agents[SOURCE].append(AgentRecord('72', 'EA-A-ff', 'x', ['10.0.0.5'], 'online', SOURCE))
        with self.assertRaises(FlowAborted) as caught:
            cluster_flow._require_no_new_source_identity(h.ctx, item, member, ignore)
        self.assertIn('72', str(caught.exception))


class ClusterReenableModeTests(unittest.TestCase):
    """H4: default = manual task, True = full PUT, 'minimal' = minimal PUT."""

    def test_default_context_is_manual(self):
        ctx = FlowContext(FakeApi(World()), None, FakeConsole(), SOURCE, TARGET)
        self.assertIs(ctx.auto_reenable_tests, False)

    def test_context_accepts_true_false_and_minimal(self):
        for given, expected in ((True, True), (False, False), ('minimal', 'minimal'), (None, False)):
            ctx = FlowContext(FakeApi(World()), None, FakeConsole(), SOURCE, TARGET, auto_reenable_tests=given)
            self.assertEqual(ctx.auto_reenable_tests, expected)
            self.assertIs(type(ctx.auto_reenable_tests), type(expected))

    def test_manual_mode_never_writes_a_test_and_lists_the_ids(self):
        h = Harness(auto_reenable=False, disabled=('102',))
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual([c for c in h.api.calls if c[0] in ('set_test_enabled', 'set_test_enabled_minimal')], [])
        self.assertFalse(h.world.tests[SOURCE]['101']['enabled'])     # still disabled: it is a manual task
        rows = [r for r in h.console.statuses if r[2] == 'cluster_restore_tests_enabled']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], StepStatus.CHANGED)
        self.assertIn('MANUAL TASK', rows[0][3])
        self.assertIn('2 tests in 2 groups', rows[0][3])
        text = '\n'.join(m for _, m in h.console.infos if m.startswith('MANUAL TASK'))
        self.assertIn('aid 1: 1 tests were enabled before and are disabled now', text)
        self.assertIn('aid 1 test ids (1/1): 101', text)
        self.assertIn('aid 2 test ids (1/1): 201', text)
        self.assertNotIn('102', text)                                 # was disabled before: not part of the task
        self.assertTrue(any('manual task' in w for w in result.warnings))
        self.assertIn('TESTS TO ENABLE BY HAND', result.detail)
        self.assertEqual(h.console.steps(StepStatus.FAILED), [])
        self.assertIn('cluster_delete_old', h.console.steps())

    def test_manual_mode_prints_50_ids_per_line(self):
        ids = ['%d' % n for n in range(1000, 1120)]
        h = Harness(auto_reenable=False, tests={SOURCE: ids})
        migrate_cluster(h.ctx, make_item(tests={SOURCE: ids}, shares=[SOURCE]))
        lines = [m for _, m in h.console.infos if m.startswith('MANUAL TASK: aid 1 test ids')]
        self.assertEqual(len(lines), 3)
        self.assertEqual([len(l.split(': ')[-1].split(',')) for l in lines], [50, 50, 20])
        self.assertIn('(1/3)', lines[0])
        self.assertTrue(any('aid 1: 120 tests were enabled before' in m for _, m in h.console.infos))
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_test_enabled'], [])

    def test_manual_mode_with_nothing_to_enable_is_skipped(self):
        h = Harness(auto_reenable=False)
        h.world.disable_on_unassign = False
        result = migrate_cluster(h.ctx, make_item())
        rows = [r for r in h.console.statuses if r[2] == 'cluster_restore_tests_enabled']
        self.assertEqual([r[0] for r in rows], [StepStatus.SKIPPED])
        self.assertNotIn('MANUAL TASK', h.console.text())
        self.assertTrue(result.ok)

    def test_manual_mode_when_the_tests_cannot_be_read_lists_every_id(self):
        h = Harness(auto_reenable=False)
        h.world.list_tests_fail = True
        result = migrate_cluster(h.ctx, make_item())
        text = '\n'.join(m for _, m in h.console.infos if m.startswith('MANUAL TASK'))
        self.assertIn('could not read the tests now', text)
        self.assertIn('101,102', text)
        self.assertTrue(result.ok)

    def test_manual_mode_the_snapshot_file_has_the_enabled_state(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        h = Harness(auto_reenable=False, disabled=('102',), snapshot_dir=tmp)
        migrate_cluster(h.ctx, make_item())
        with open(h.ctx.snapshot_path) as handle:
            data = json.load(handle)
        self.assertEqual(data['extra']['enabled'][SOURCE], {'101': True, '102': False})

    def test_full_mode_uses_set_test_enabled(self):
        h = Harness(auto_reenable=True, disabled=('102',))
        migrate_cluster(h.ctx, make_item())
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_test_enabled'],
                         [('set_test_enabled', SOURCE, 'http-server', '101', True),
                          ('set_test_enabled', OTHER, 'http-server', '201', True)])
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_test_enabled_minimal'], [])
        rows = [r for r in h.console.statuses if r[2] == 'cluster_restore_tests_enabled']
        self.assertEqual(rows[0][0], StepStatus.CHANGED)

    def test_minimal_mode_uses_set_test_enabled_minimal_only(self):
        h = Harness(auto_reenable='minimal', disabled=('102',))
        result = migrate_cluster(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_test_enabled'], [])
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_test_enabled_minimal'],
                         [('set_test_enabled_minimal', SOURCE, 'http-server', '101', True),
                          ('set_test_enabled_minimal', OTHER, 'http-server', '201', True)])
        self.assertTrue(h.world.tests[SOURCE]['101']['enabled'])
        rows = [r for r in h.console.statuses if r[2] == 'cluster_restore_tests_enabled']
        self.assertIn('minimal PUT', rows[0][3])

    def test_minimal_mode_failure_is_loud(self):
        h = Harness(auto_reenable='minimal')
        h.world.enable_fail = set(['101'])
        result = migrate_cluster(h.ctx, make_item())
        self.assertFalse(result.ok)
        self.assertIn('cluster_restore_tests_enabled', h.console.steps(StepStatus.FAILED))

    def test_dry_run_plan_text_follows_the_mode(self):
        texts = {}
        for mode in (False, True, 'minimal'):
            plan = dict(plan_cluster(make_item(), TARGET, mode))
            texts[mode] = plan['cluster_restore_tests_enabled']
        self.assertIn('does NOT enable tests', texts[False])
        self.assertIn('full test payload', texts[True])
        self.assertIn('{"enabled": true}', texts['minimal'])
        h = Harness(apply=False, auto_reenable='minimal')
        migrate_cluster(h.ctx, make_item())
        row = [r for r in h.console.statuses if r[2] == 'cluster_restore_tests_enabled'][0]
        self.assertIn('minimal PUT', row[3])
        self.assertEqual([c for c in h.api.calls if 'enabled' in c[0]], [])

    def test_api_wrapper_blocks_the_minimal_writer_in_dry_run(self):
        api = ReadOnlyApi(FakeApi(World()))
        with self.assertRaises(RuntimeError):
            api.set_test_enabled_minimal(SOURCE, 'http-server', '1', True)


class ClusterStopInfoTests(unittest.TestCase):
    """M2 and M5."""

    def test_stop_line_has_ids_share_set_and_snapshot(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        h = Harness(snapshot_dir=tmp)
        h.world.absent_timeout = True
        with self.assertRaises(VisibilityTimeout) as caught:
            migrate_cluster(h.ctx, make_item())
        line = h.stop_line()
        self.assertIn('old cluster id: 11', line)
        self.assertIn('new agent ids: [91,92]', line)
        self.assertIn('new cluster id: 91', line)
        self.assertIn('final share set: [9,1,2]', line)
        self.assertIn('snapshot file: ' + h.ctx.snapshot_path, line)
        self.assertIn('RECOVERY DATA', line)
        exc = caught.exception
        self.assertEqual(exc.new_agent_id, '91')
        self.assertEqual(exc.new_cluster_id, '91')
        self.assertEqual(exc.snapshot, h.ctx.snapshot_path)
        self.assertIn('delete sent, not confirmed', exc.state)

    def test_flow_aborted_gets_the_attributes_before_the_cluster_exists(self):
        h = Harness()
        h.world.ui_fail[('10.0.0.6', 'reset_agent')] = UiError('reset not confirmed')
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(caught.exception.new_agent_id, '91')
        self.assertIsNone(caught.exception.new_cluster_id)
        self.assertIsNone(caught.exception.snapshot)
        self.assertIn('new cluster is NOT built', caught.exception.state)
        self.assertIn('new cluster id: not known yet', h.stop_line())
        self.assertIn('new agent ids: [91]', h.stop_line())

    def test_attributes_are_set_on_keyboard_interrupt(self):
        h = Harness()
        h.world.interrupt_at = 'cluster_assign'
        with self.assertRaises(KeyboardInterrupt) as caught:
            migrate_cluster(h.ctx, make_item())
        self.assertEqual(caught.exception.new_cluster_id, None)
        self.assertEqual(caught.exception.new_agent_id, '91')
        self.assertIn('interrupted by KeyboardInterrupt', h.stop_line())

    def test_preflight_failure_has_empty_attributes(self):
        h = Harness()
        with self.assertRaises(FlowAborted) as caught:
            migrate_cluster(h.ctx, make_item(status='blocked'))
        self.assertIsNone(caught.exception.new_agent_id)
        self.assertIsNone(caught.exception.new_cluster_id)
        self.assertIn('nothing changed', caught.exception.state)

    def test_attach_never_raises_on_an_object_without_attributes(self):
        from te_agent_migrate.strategy_d.context import attach_stop_info

        class Slots(Exception):
            __slots__ = ()

        exc = Slots()
        self.assertIs(attach_stop_info(exc, new_agent_id='1'), exc)

    def test_enable_failure_prints_recovery_data_and_does_not_hide_it(self):
        h = Harness(auto_reenable=True)
        h.world.enable_fail = set(['101'])
        result = migrate_cluster(h.ctx, make_item())
        self.assertFalse(result.ok)
        lines = [m for _, m in h.console.infos if m.startswith('RECOVERY DATA')]
        self.assertEqual(len(lines), 1)
        self.assertIn('some tests are NOT enabled again', lines[0])
        self.assertIn('old cluster id 11', lines[0])
        self.assertIn('enabled before in aid 1: [101,102]', lines[0])
        self.assertIn('new cluster id: 91', lines[0])
        self.assertIn('snapshot file:', lines[0])
        self.assertNotIn(FAKE_TOKEN, lines[0])

    def test_clean_enable_prints_no_recovery_line(self):
        h = Harness(auto_reenable=True)
        migrate_cluster(h.ctx, make_item())
        self.assertEqual([m for _, m in h.console.infos if m.startswith('RECOVERY DATA')], [])


class ClusterChangedOkWordsTests(unittest.TestCase):
    """L5."""

    def test_writes_are_changed_and_checks_are_ok(self):
        h = Harness(auto_reenable=True, disabled=('102',))
        migrate_cluster(h.ctx, make_item())
        by_step = {}
        for status, _, step, _ in h.console.statuses:
            by_step.setdefault(step, set()).add(status)
        for step in ('cluster_unassign', 'cluster_member_ui_reset', 'cluster_member_ui_set_target_token',
                     'cluster_assign', 'cluster_share_back', 'cluster_assign_tests',
                     'cluster_restore_tests_enabled', 'cluster_delete_old', 'cluster_rename',
                     'cluster_restore_tags'):
            self.assertEqual(by_step[step], set([StepStatus.CHANGED]), step)
        for step in ('cluster_preflight_snapshot', 'cluster_member_ui_check', 'cluster_member_ui_authenticate',
                     'cluster_member_ui_snapshot_before', 'cluster_member_ui_reachable_after_reset',
                     'cluster_member_ui_authenticate_after_reset', 'cluster_member_api_target_online'):
            self.assertEqual(by_step[step], set([StepStatus.OK]), step)

    def test_rename_is_ok_when_the_name_is_right_and_tags_ok_when_none_restored(self):
        h = Harness()
        h.world.new_name = NAME
        migrate_cluster(h.ctx, make_item(tags=[]))
        by_step = dict((r[2], r[0]) for r in h.console.statuses)
        self.assertEqual(by_step['cluster_rename'], StepStatus.OK)
        self.assertEqual(by_step['cluster_restore_tags'], StepStatus.SKIPPED)

    def test_reenable_step_is_ok_when_nothing_had_to_be_enabled(self):
        h = Harness(auto_reenable=True)
        h.world.disable_on_unassign = False
        migrate_cluster(h.ctx, make_item())
        row = [r for r in h.console.statuses if r[2] == 'cluster_restore_tests_enabled'][0]
        self.assertEqual(row[0], StepStatus.OK)


class PlanTests(unittest.TestCase):
    def test_plan_order_and_names(self):
        plan = plan_cluster(make_item(count=3))
        names = [p[0] for p in plan]
        self.assertEqual(names[:5], ['cluster_preflight_snapshot'] + ['cluster_member_ui_check'] * 3 +
                         ['cluster_unassign'])
        self.assertEqual(names[5:26].count('cluster_member_ui_reset'), 3)
        self.assertEqual(names[26:], ['cluster_assign', 'cluster_share_back', 'cluster_assign_tests',
                                      'cluster_restore_tests_enabled', 'cluster_delete_old', 'cluster_rename',
                                      'cluster_restore_tags', 'manual_notification_rules'])
        for step, detail in plan:
            self.assertTrue(detail)

    def test_plan_is_pure_and_target_optional(self):
        item = make_item()
        self.assertEqual(plan_cluster(item), plan_cluster(item))
        self.assertIn('[9,1,2]', dict(plan_cluster(item, target_aid=TARGET))['cluster_share_back'])
        online = [d for s, d in plan_cluster(item) if s == 'cluster_member_api_target_online'][0]
        self.assertIn('<target>', online)

    def test_plan_without_tags_or_tests(self):
        plan = dict(plan_cluster(make_item(tests={}, tags=[], shares=[])))
        self.assertEqual(plan['cluster_assign_tests'], 'no tests to assign')
        self.assertEqual(plan['cluster_restore_tests_enabled'], 'no tests to re-enable')
        self.assertEqual(plan['cluster_restore_tags'], 'no tags to restore')
        self.assertIn('nothing to share', plan['cluster_share_back'])


class RealConsoleIntegrationTests(unittest.TestCase):
    def test_runs_with_real_console_and_counts_no_failures(self):
        h = Harness()
        stream = io.StringIO()
        console = MigrationConsole(stream=stream, use_color=False)
        h.ctx.console = console
        migrate_cluster(h.ctx, make_item())
        self.assertFalse(console.has_failures())
        self.assertIn('ok:', stream.getvalue())
        self.assertIn('MAINTENANCE WINDOW STARTED', stream.getvalue())
        self.assertNotIn(FAKE_TOKEN, stream.getvalue())

    def test_failure_state_line_survives_real_console(self):
        h = Harness()
        h.world.ui_fail[('10.0.0.6', 'reset_agent')] = UiError('x')
        stream = io.StringIO()
        h.ctx.console = MigrationConsole(stream=stream, use_color=False)
        with self.assertRaises(FlowAborted):
            migrate_cluster(h.ctx, make_item())
        out = stream.getvalue()
        self.assertIn('RECOVERY DATA', out)
        self.assertIn('tests in aid 1: [101,102]', out)
        self.assertTrue(h.ctx.console.has_failures())


# --------------------------------------------------------------------------- api_client.set_test_enabled
class _Response(object):
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.content = b'{}' if payload is not None else b''

    def json(self):
        return self._payload


class _Session(object):
    def __init__(self, responses):
        self.headers = {}
        self.calls = []
        self._responses = list(responses)

    def request(self, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, 'params': kwargs.get('params'), 'json': kwargs.get('json')})
        return self._responses.pop(0)

    def close(self):
        pass


class SetTestEnabledTests(unittest.TestCase):
    def make(self, payload):
        session = _Session([_Response(200, payload), _Response(200, dict(payload, enabled=True))])
        client = ThousandEyesApiClient('synthetic-token', session=session, sleep=lambda s: None,
                                       retry_policy=RetryPolicy(attempts=1, initial_backoff_seconds=0.0))
        return client, session

    def test_put_has_only_enabled_changed_and_no_response_only_fields(self):
        payload = {
            'testId': '101', 'testName': 'synthetic', 'type': 'http-server', 'enabled': False,
            'url': 'https://example.com', 'interval': 300, 'createdDate': '2026-01-01', '_links': {'self': {}},
            'agents': [{'agentId': '91', 'agentName': 'x'}],
            'tags': [{'tagId': 't1', 'key': 'k'}], 'alertRules': [{'ruleId': 'r1', 'ruleName': 'n'}],
            'sharedWithAccounts': [{'aid': '2', 'name': 'other'}], 'labels': [{'labelId': 'l'}],
        }
        client, session = self.make(payload)
        record = client.set_test_enabled('1', 'http-server', '101', True)
        self.assertTrue(record.enabled)
        self.assertEqual([c['method'] for c in session.calls], ['GET', 'PUT'])
        put = session.calls[1]
        self.assertTrue(put['url'].endswith('tests/http-server/101'))
        self.assertEqual(put['params'], {'aid': '1'})
        body = put['json']
        self.assertIs(body['enabled'], True)
        for field in ('testId', 'type', 'createdDate', '_links', 'labels'):
            self.assertNotIn(field, body)
        self.assertEqual(body['url'], 'https://example.com')
        self.assertEqual(body['interval'], 300)
        self.assertEqual(body['testName'], 'synthetic')
        self.assertEqual(body['agents'], [{'agentId': '91'}])
        self.assertEqual(body['tags'], ['t1'])
        self.assertEqual(body['alertRules'], ['r1'])
        self.assertEqual(body['sharedWithAccounts'], [{'aid': 2}])

    def test_can_disable_and_never_sends_other_methods(self):
        client, session = self.make({'testId': '5', 'testName': 'n', 'type': 'dns-server', 'enabled': True})
        client.set_test_enabled('1', 'dns-server', '5', False)
        self.assertIs(session.calls[1]['json']['enabled'], False)
        self.assertNotIn('DELETE', [c['method'] for c in session.calls])

    def test_put_failure_raises_api_error(self):
        session = _Session([_Response(200, {'testId': '5', 'type': 'dns-server', 'enabled': False}),
                            _Response(400, {})])
        client = ThousandEyesApiClient('synthetic-token', session=session, sleep=lambda s: None,
                                       retry_policy=RetryPolicy(attempts=1, initial_backoff_seconds=0.0))
        with self.assertRaises(ApiError):
            client.set_test_enabled('1', 'dns-server', '5', True)


if __name__ == '__main__':
    unittest.main()
