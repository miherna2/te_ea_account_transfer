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
from te_agent_migrate.models import AgentRecord, InventoryAgent, LocalSnapshot, StepStatus
from te_agent_migrate.output import MigrationConsole
from te_agent_migrate.strategy_d import standalone
from te_agent_migrate.strategy_d.context import FlowAborted, FlowContext, PreflightItem, ReadOnlyApi
from te_agent_migrate.strategy_d.standalone import migrate_standalone, plan_standalone

SOURCE = '1'
TARGET = '9'
OTHER = '2'
IP = '10.0.0.5'
NAME = 'SITE-A-EA01'
OLD_ID = '11'
NEW_ID = '99'
FAKE_TOKEN = 'FAKEFAKEFAKEFAKE0123456789ABCDEF'  # synthetic, 32 chars, not a real credential

WRITE_CALLS = ('set_agent_account_groups', 'assign_tests', 'delete_agent', 'update_agent_name', 'assign_tag')
UI_IP = '10.9.9.9'


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


class World(object):
    """Shared state and ordered event log for the fake API and the fake UI."""

    def __init__(self):
        self.events = []
        self.agents = {SOURCE: [], OTHER: [], TARGET: []}
        self.old = AgentRecord(OLD_ID, NAME, NAME, [IP], 'online', SOURCE, shared_aids=[SOURCE, OTHER],
                               agent_type='enterprise')
        self.agents[SOURCE].append(self.old)
        self.shares = {}
        self.assigned = {}
        self.names = {}
        self.wrong_group = False
        self.share_put_ignored = False
        self.assign_ignored = False
        self.new_offline = False
        self.tag_fail = False
        self.delete_fail = False
        self.reachable = [True]
        self.never_online = False
        self.absent_timeout = False
        self.interrupt_at = None          # api or ui method name that raises KeyboardInterrupt
        self.new_name = NAME + '-abc'
        self.extra_on_register = []       # AgentRecords added to the target together with the new agent
        self.list_result = False          # writes return a JSON array wrapper

    def register_new_agent(self):
        group = SOURCE if self.wrong_group else TARGET
        if self.never_online:
            return
        state = 'offline' if self.new_offline else 'online'
        rec = AgentRecord(NEW_ID, self.new_name, NAME, [IP], state, group, agent_type='enterprise')
        self.agents[group].append(rec)
        self.names[NEW_ID] = rec.name
        self.old.state = 'offline'
        self.agents[TARGET].extend(self.extra_on_register)

    def new_record(self, aid):
        for group in self.agents.values():
            for rec in group:
                if rec.agent_id == NEW_ID:
                    state = 'offline' if self.new_offline else 'online'
                    return AgentRecord(NEW_ID, self.names[NEW_ID], NAME, [IP], state, aid, agent_type='enterprise',
                                       shared_aids=self.shares.get(NEW_ID, []),
                                       test_ids=self.assigned.get((aid, NEW_ID), []))
        raise ApiError('agent %s not found' % NEW_ID)


class FakeApi(object):
    def __init__(self, world):
        self.w = world
        self.calls = []

    def _log(self, name, *args):
        self.calls.append((name,) + args)
        self.w.events.append(('api', name) + args)

    def _bare(self, rec):
        """The real list endpoints return no accountGroups."""
        return AgentRecord(rec.agent_id, rec.name, rec.hostname, list(rec.ip_addresses), rec.state, rec.aid,
                           agent_type=rec.agent_type, test_ids=list(rec.test_ids))

    def _maybe_interrupt(self, name):
        if self.w.interrupt_at == name:
            raise KeyboardInterrupt()

    def list_agents(self, aid, agent_types='ENTERPRISE'):
        self._log('list_agents', aid)
        return [self._bare(r) for r in self.w.agents.get(aid, [])]

    def list_agents_with_clusters(self, aid):
        result = []
        for rec in self.w.agents.get(aid, []):
            if rec.agent_id == NEW_ID:
                result.append(self._bare(self.w.new_record(aid)))
            else:
                result.append(self._bare(rec))
        if aid in (SOURCE, OTHER) and NEW_ID in self.w.shares and aid in self.w.shares[NEW_ID]:
            if not any(r.agent_id == NEW_ID for r in result):
                result.append(self._bare(self.w.new_record(aid)))
        return result

    def wait_for_agent(self, aid, hostname, ip_address, timeout_seconds, poll_interval_seconds=5.0,
                       on_poll=None, on_not_visible=None, known_ids=None, alt_names=None, alt_ips=None):
        self.wait_args = dict(known_ids=known_ids, alt_names=alt_names, alt_ips=alt_ips)
        for attempt in range(1, 4):
            if on_poll:
                on_poll(attempt, 1.0)
            if known_ids is None:
                agent = ThousandEyesApiClient.match_agent(self.list_agents(aid), hostname, ip_address, online_only=True)
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
        self._maybe_interrupt('get_agent')
        if agent_id == NEW_ID:
            return self.w.new_record(aid)
        return self.w.old

    def set_agent_account_groups(self, aid, agent_id, ids):
        self._log('set_agent_account_groups', aid, agent_id, list(ids))
        if not ids:
            raise ApiError('empty list refused')
        if not self.w.share_put_ignored:
            self.w.shares[agent_id] = list(ids)
        return {'_list': []} if self.w.list_result else {}

    def assign_tests(self, aid, agent_id, test_ids):
        self._log('assign_tests', aid, agent_id, list(test_ids))
        self._maybe_interrupt('assign_tests')
        if not self.w.assign_ignored:
            self.w.assigned[(aid, agent_id)] = list(test_ids)

    def delete_agent(self, aid, agent_id, agent_type=None):
        self._log('delete_agent', aid, agent_id, agent_type)
        if self.w.delete_fail:
            raise ApiError('delete failed')
        for group in self.w.agents.values():
            group[:] = [r for r in group if r.agent_id != agent_id]

    def wait_for_agent_absent(self, aid, agent_id, timeout_seconds, poll_interval_seconds=5.0, on_poll=None):
        self._log('wait_for_agent_absent', aid, agent_id)
        if self.w.absent_timeout or any(r.agent_id == agent_id for r in self.list_agents(aid)):
            raise VisibilityTimeout('still present')

    def update_agent_name(self, aid, agent_id, name):
        self._log('update_agent_name', aid, agent_id, name)
        self.w.names[agent_id] = name

    def wait_for_agent_name(self, aid, agent_id, expected_name, timeout_seconds, poll_interval_seconds=5.0,
                            on_poll=None):
        self._log('wait_for_agent_name', aid, agent_id, expected_name)
        return self.w.new_record(aid)

    def assign_tag(self, aid, tag_id, assignments):
        self._log('assign_tag', aid, tag_id, list(assignments))
        if self.w.tag_fail:
            raise ApiError('tag 403')


class FakeUi(object):
    def __init__(self, world):
        self.w = world

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def authenticate(self):
        self.w.events.append(('ui', 'authenticate'))

    def snapshot_state(self):
        self.w.events.append(('ui', 'snapshot_state'))
        return LocalSnapshot(IP, 'now', {'app_config': {'browserbot': 'no', 'crash_reports': 'yes'}})

    def reset_agent(self):
        self.w.events.append(('ui', 'reset_agent'))
        if self.w.interrupt_at == 'reset_agent':
            raise KeyboardInterrupt()

    def is_reachable(self):
        self.w.events.append(('ui', 'is_reachable'))
        if self.w.interrupt_at == 'is_reachable':
            raise KeyboardInterrupt()
        if len(self.w.reachable) > 1:
            return self.w.reachable.pop(0)
        return self.w.reachable[0]

    def set_account_group_token(self, account_token, browserbot, crash_reports):
        self.w.events.append(('ui', 'set_account_group_token', browserbot, crash_reports))
        self.w.token_seen = account_token
        self.w.register_new_agent()


def make_item(status='ready', kind='agent', tests=None, shares=None, tags=None, ips=None, record=True,
              agent_type='enterprise', ui_ip=None):
    inventory = InventoryAgent(NAME, IP, kind=kind)
    rec = AgentRecord(OLD_ID, NAME, NAME, [IP] if ips is None else ips, 'online', SOURCE,
                      agent_type=agent_type) if record else None
    item = _make_item(inventory, rec, status, kind, tests, shares, tags)
    if ui_ip is not None:
        item.ui_ip = ui_ip
    return item


def _make_item(inventory, rec, status, kind, tests, shares, tags):
    return PreflightItem(
        inventory, record=rec, status=status, reasons=['offline'] if status != 'ready' else [],
        owner_aid=SOURCE,
        share_aids=[SOURCE, OTHER] if shares is None else shares,
        tests_by_aid={SOURCE: ['101', '102'], OTHER: ['201']} if tests is None else tests,
        tags=[{'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A'}] if tags is None else tags)


class Harness(object):
    def __init__(self, apply=True, token=FAKE_TOKEN, snapshot_dir=None):
        self.world = World()
        self.api = FakeApi(self.world)
        self.console = FakeConsole()
        self.opened = []
        self.slept = []

        def factory(host):
            self.opened.append(host)
            return FakeUi(self.world)

        self.ctx = FlowContext(self.api, factory, self.console, SOURCE, TARGET, target_account_token=token,
                               apply=apply, timeout_seconds=2, poll_seconds=1, sleep=self.slept.append,
                               snapshot_dir=snapshot_dir)

    def writes(self):
        return [c for c in self.api.calls if c[0] in WRITE_CALLS]

    def stop_line(self):
        lines = [msg for _, msg in self.console.infos if msg.startswith('stopped; state:')]
        return lines[-1] if lines else ''

    def event_index(self, *prefix):
        for index, event in enumerate(self.world.events):
            if event[:len(prefix)] == prefix:
                return index
        raise AssertionError('event %r not found in %r' % (prefix, self.world.events))


class StandaloneApplyTests(unittest.TestCase):
    def test_happy_path_result_and_step_order(self):
        h = Harness()
        result = migrate_standalone(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(result.new_agent_id, NEW_ID)
        self.assertEqual(result.warnings, [])
        self.assertEqual(h.console.steps(), [
            'preflight_snapshot', 'ui_authenticate', 'ui_snapshot_before', 'ui_reset',
            'ui_reachable_after_reset', 'ui_authenticate_after_reset', 'ui_set_target_token',
            'api_target_online', 'api_share_back', 'api_assign_tests', 'api_delete_old', 'api_rename',
            'api_restore_tags', 'manual_notification_rules'])
        self.assertEqual(h.opened, [IP])
        self.assertEqual(h.console.steps(StepStatus.FAILED), [])

    def test_delete_old_after_share_and_assign_verified(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        delete_at = h.event_index('api', 'delete_agent')
        self.assertLess(h.event_index('api', 'set_agent_account_groups'), delete_at)
        self.assertLess(h.event_index('api', 'assign_tests'), delete_at)
        self.assertLess(h.event_index('ui', 'set_account_group_token'), delete_at)
        # the last assign comes before the delete
        assigns = [i for i, e in enumerate(h.world.events) if e[:2] == ('api', 'assign_tests')]
        self.assertTrue(assigns and max(assigns) < delete_at)
        self.assertLess(delete_at, h.event_index('api', 'update_agent_name'))
        self.assertLess(h.event_index('api', 'update_agent_name'), h.event_index('api', 'assign_tag'))

    def test_delete_uses_owner_aid_and_enterprise_type_then_waits_absent(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        self.assertIn(('delete_agent', SOURCE, OLD_ID, 'enterprise'), h.api.calls)
        self.assertIn(('wait_for_agent_absent', SOURCE, OLD_ID), h.api.calls)
        self.assertLess(h.event_index('api', 'delete_agent'), h.event_index('api', 'wait_for_agent_absent'))

    def test_share_back_sends_full_list_target_first_and_never_empty(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        puts = [c for c in h.api.calls if c[0] == 'set_agent_account_groups']
        self.assertEqual(puts, [('set_agent_account_groups', TARGET, NEW_ID, [TARGET, SOURCE, OTHER])])
        for call in puts:
            self.assertTrue(call[3])

    def test_no_other_groups_skips_share_and_never_sends_empty(self):
        h = Harness()
        item = make_item(shares=[], tests={})
        result = migrate_standalone(h.ctx, item)
        self.assertTrue(result.ok)
        self.assertEqual([c for c in h.api.calls if c[0] == 'set_agent_account_groups'], [])
        self.assertEqual([c for c in h.api.calls if c[0] == 'assign_tests'], [])
        self.assertIn(('api_share_back', StepStatus.SKIPPED), [(r[2], r[0]) for r in h.console.statuses])

    def test_assign_tests_per_group_with_group_aid(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        assigns = [c for c in h.api.calls if c[0] == 'assign_tests']
        self.assertEqual(assigns, [('assign_tests', SOURCE, NEW_ID, ['101', '102']),
                                   ('assign_tests', OTHER, NEW_ID, ['201'])])

    def test_empty_test_list_for_a_group_is_not_assigned(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item(tests={SOURCE: ['101'], OTHER: []}))
        assigns = [c for c in h.api.calls if c[0] == 'assign_tests']
        self.assertEqual(assigns, [('assign_tests', SOURCE, NEW_ID, ['101'])])

    def test_ui_gets_snapshot_values_and_token_never_printed(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        self.assertEqual(h.world.token_seen, FAKE_TOKEN)
        self.assertIn(('ui', 'set_account_group_token', False, True), h.world.events)
        self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_fake_token_has_32_characters_and_is_alphanumeric(self):
        self.assertEqual(len(FAKE_TOKEN), 32)
        self.assertTrue(FAKE_TOKEN.isalnum())

    def test_token_not_printed_on_failure_either(self):
        h = Harness()
        h.world.wrong_group = True
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item())
        self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_rename_when_name_differs(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        self.assertIn(('update_agent_name', TARGET, NEW_ID, NAME), h.api.calls)
        self.assertIn(('wait_for_agent_name', TARGET, NEW_ID, NAME), h.api.calls)

    def test_tags_restored_as_v_agent_in_their_own_group(self):
        h = Harness()
        tags = [{'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A'},
                {'aid': OTHER, 'tagId': 't2', 'key': 'role', 'value': 'edge'}]
        migrate_standalone(h.ctx, make_item(tags=tags))
        tag_calls = [c for c in h.api.calls if c[0] == 'assign_tag']
        self.assertEqual(tag_calls, [
            ('assign_tag', SOURCE, 't1', [{'id': NEW_ID, 'type': 'v-agent'}]),
            ('assign_tag', OTHER, 't2', [{'id': NEW_ID, 'type': 'v-agent'}])])

    def test_tag_failure_is_not_fatal(self):
        h = Harness()
        h.world.tag_fail = True
        result = migrate_standalone(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('tag restore failed', result.warnings[0])
        self.assertTrue(any('warning: tag restore failed' in msg for _, msg in h.console.infos))
        self.assertIn('manual_notification_rules', h.console.steps(StepStatus.SKIPPED))
        self.assertEqual(h.console.steps(StepStatus.FAILED), [])

    def test_manual_notification_rules_always_skipped_line(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        rows = [r for r in h.console.statuses if r[2] == 'manual_notification_rules']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], StepStatus.SKIPPED)
        self.assertIn('by hand', rows[0][3])


class StandaloneSafetyTests(unittest.TestCase):
    def test_wrong_group_detection_stops_before_delete_or_share(self):
        h = Harness()
        h.world.wrong_group = True
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('wrong group', str(caught.exception))
        self.assertEqual(h.writes(), [])
        self.assertIn('api_target_online', h.console.steps(StepStatus.FAILED))
        self.assertTrue(any('state:' in msg for _, msg in h.console.infos))

    def test_unreachable_after_reset_aborts_with_state(self):
        h = Harness()
        h.world.reachable = [False]
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('agent was reset; no token set; manual action needed', str(caught.exception))
        self.assertNotIn(('ui', 'set_account_group_token', False, True), h.world.events)
        self.assertEqual(h.writes(), [])
        self.assertEqual(h.console.steps(StepStatus.FAILED), ['ui_reachable_after_reset'])
        self.assertTrue(h.slept)  # polled with the injected sleep, never a real one

    def test_slow_reset_recovers_when_ui_comes_back(self):
        h = Harness()
        h.world.reachable = [False, True]
        result = migrate_standalone(h.ctx, make_item())
        self.assertTrue(result.ok)

    def test_share_not_applied_aborts_before_delete(self):
        h = Harness()
        h.world.share_put_ignored = True
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('NOT deleted', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] in ('delete_agent', 'assign_tests')], [])

    def test_missing_tests_after_assign_abort_before_delete(self):
        h = Harness()
        h.world.assign_ignored = True
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('NOT deleted', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])
        self.assertIn('api_assign_tests', h.console.steps(StepStatus.FAILED))

    def test_new_agent_not_online_before_delete_aborts(self):
        h = Harness()
        h.world.new_offline = True
        # wait_for_agent only returns online agents; script the fake to return the offline one anyway
        original = h.api.wait_for_agent

        def forced(aid, hostname, ip_address, timeout_seconds, **kwargs):
            return h.world.new_record(aid)

        h.api.wait_for_agent = forced
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('not online', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])
        h.api.wait_for_agent = original

    def test_never_delete_when_old_equals_new(self):
        h = Harness()

        def same(aid, hostname, ip_address, timeout_seconds, **kwargs):
            h.world.register_new_agent()
            rec = h.world.new_record(aid)
            rec.agent_id = OLD_ID
            return rec

        # old and new share one id: deleting would remove the migrated agent
        h.api.wait_for_agent = same
        original_get = h.api.get_agent
        h.api.get_agent = lambda aid, agent_id: h.world.new_record(aid)
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('only the old agent id', str(caught.exception))
        self.assertEqual(h.writes(), [])
        h.api.get_agent = original_get

    def test_target_never_online_aborts_without_writes(self):
        h = Harness()
        h.world.never_online = True
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('did not appear online', str(caught.exception))
        self.assertEqual(h.writes(), [])

    def test_existing_online_target_agent_blocks_reset(self):
        h = Harness()
        h.world.agents[TARGET].append(AgentRecord('55', NAME, NAME, [IP], 'online', TARGET))
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item())
        self.assertNotIn(('ui', 'reset_agent'), h.world.events)
        self.assertEqual(h.opened, [])

    def test_delete_failure_propagates_api_error_and_keeps_state_info(self):
        h = Harness()
        h.world.delete_fail = True
        with self.assertRaises(ApiError):
            migrate_standalone(h.ctx, make_item())
        self.assertIn('api_delete_old', h.console.steps(StepStatus.FAILED))
        self.assertIn('delete sent, not confirmed', h.stop_line())
        self.assertNotIn('api_rename', h.console.steps())

    def test_token_ui_error_becomes_flow_aborted_with_state(self):
        h = Harness()

        class BadUi(FakeUi):
            def set_account_group_token(self, *args, **kwargs):
                raise UiError('rejected')

        h.ctx.ui_factory = lambda host: BadUi(h.world)
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('manual action needed', str(caught.exception))

    def test_apply_without_token_aborts_before_any_call(self):
        h = Harness(token='')
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item())
        self.assertEqual(h.opened, [])
        self.assertEqual(h.writes(), [])


class StandaloneValidationTests(unittest.TestCase):
    def test_item_not_ready_raises(self):
        for status in ('blocked', 'excluded'):
            h = Harness()
            with self.assertRaises(FlowAborted) as caught:
                migrate_standalone(h.ctx, make_item(status=status))
            self.assertIn('not ready', str(caught.exception))
            self.assertEqual(h.opened, [])
            self.assertEqual(h.api.calls, [])

    def test_cluster_item_raises(self):
        h = Harness()
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item(kind='cluster'))

    def test_missing_ip_raises_in_apply_and_dry_run(self):
        for apply in (True, False):
            h = Harness(apply=apply)
            with self.assertRaises(FlowAborted) as caught:
                migrate_standalone(h.ctx, make_item(ips=[]))
            self.assertIn('no IP', str(caught.exception))
            self.assertEqual(h.opened, [])

    def test_missing_record_raises(self):
        h = Harness()
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item(record=False))

    def test_failure_is_printed_as_failed_step(self):
        h = Harness()
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item(status='blocked'))
        self.assertEqual(h.console.steps(StepStatus.FAILED), ['preflight_snapshot'])


class StandaloneDryRunTests(unittest.TestCase):
    def test_dry_run_has_no_writes_no_ui_and_only_get_calls(self):
        h = Harness(apply=False)
        result = migrate_standalone(h.ctx, make_item())
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, 'dry-run')
        self.assertIsNone(result.new_agent_id)
        self.assertIsNone(result.snapshot)
        self.assertEqual(h.opened, [])
        self.assertEqual(h.world.events, [('api', 'list_agents', TARGET)])
        self.assertEqual(h.api.calls, [('list_agents', TARGET)])
        self.assertEqual(h.writes(), [])
        self.assertEqual(h.slept, [])

    def test_dry_run_prints_every_step_as_would_changed(self):
        h = Harness(apply=False, token='')
        migrate_standalone(h.ctx, make_item())
        rows = dict((r[2], r) for r in h.console.statuses)
        self.assertEqual(rows['preflight_snapshot'][0], StepStatus.OK)
        for step in ('ui_authenticate', 'ui_snapshot_before', 'ui_reset', 'ui_reachable_after_reset',
                     'ui_authenticate_after_reset', 'ui_set_target_token', 'api_target_online', 'api_share_back',
                     'api_assign_tests', 'api_delete_old', 'api_rename', 'api_restore_tags'):
            self.assertEqual(rows[step][0], StepStatus.CHANGED, step)
            self.assertTrue(rows[step][3].startswith('would:'), step)
        self.assertEqual(rows['manual_notification_rules'][0], StepStatus.SKIPPED)
        self.assertEqual(len(h.console.statuses), 14)

    def test_dry_run_with_real_console_prints_changed_would_lines(self):
        h = Harness(apply=False)
        stream = io.StringIO()
        console = MigrationConsole(stream=stream, use_color=False)
        h.ctx.console = console
        migrate_standalone(h.ctx, make_item())
        self.assertIn('changed:  [%s] => would: ' % NAME, stream.getvalue())
        self.assertFalse(console.has_failures())

    def test_dry_run_details_are_concrete(self):
        h = Harness(apply=False)
        migrate_standalone(h.ctx, make_item())
        rows = dict((r[2], r[3]) for r in h.console.statuses)
        self.assertEqual(rows['api_share_back'], 'would: share agent back to aids [9,1,2]')
        self.assertIn('assign 2 tests in aid 1', rows['api_assign_tests'])
        self.assertIn('1 tests in aid 2', rows['api_assign_tests'])
        self.assertIn('would: delete old agent 11 in aid 1', rows['api_delete_old'])

    def test_dry_run_fails_like_apply_when_target_already_has_the_agent(self):
        verdicts = []
        for apply in (False, True):
            h = Harness(apply=apply)
            h.world.agents[TARGET].append(AgentRecord('55', NAME, NAME, [IP], 'online', TARGET))
            with self.assertRaises(FlowAborted):
                migrate_standalone(h.ctx, make_item())
            verdicts.append(h.console.steps(StepStatus.FAILED))
            self.assertEqual(h.opened, [])
            self.assertEqual(h.writes(), [])
        self.assertEqual(verdicts[0], ['preflight_snapshot'])
        self.assertEqual(verdicts[0], verdicts[1])

    def test_dry_run_token_not_in_output(self):
        h = Harness(apply=False)
        migrate_standalone(h.ctx, make_item())
        self.assertNotIn(FAKE_TOKEN, h.console.text())

    def test_dry_run_cannot_write_even_if_the_flow_tried(self):
        h = Harness(apply=False)

        def bad_check(ctx, item, ip, old_id):
            ctx.api.delete_agent(SOURCE, OLD_ID, agent_type='enterprise')

        with mock.patch.object(standalone, '_require_clean_target', bad_check):
            with self.assertRaises(RuntimeError) as caught:
                migrate_standalone(h.ctx, make_item())
        self.assertIn('write call in dry run: delete_agent', str(caught.exception))
        self.assertEqual(h.api.calls, [])
        self.assertEqual(h.console.steps(StepStatus.FAILED), ['preflight_snapshot'])

    def test_dry_run_context_wraps_api_read_only(self):
        h = Harness(apply=False)
        self.assertIsInstance(h.ctx.api, ReadOnlyApi)
        self.assertIs(Harness(apply=True).ctx.api.__class__, FakeApi)


class StandaloneStrictWaitTests(unittest.TestCase):
    def test_wait_gets_snapshot_of_target_ids_names_and_ips(self):
        h = Harness()
        h.world.agents[TARGET].append(AgentRecord('50', 'someone-else', 'someone-else', ['10.7.7.7'], 'online', TARGET))
        migrate_standalone(h.ctx, make_item())
        args = h.api.wait_args
        self.assertEqual(args['known_ids'], set(['50', OLD_ID]))
        self.assertIn(NAME, args['alt_names'])
        self.assertIn(IP, args['alt_ips'])

    def test_new_agent_with_suffix_name_is_accepted(self):
        h = Harness()
        h.world.new_name = NAME.lower() + '-9f3a'
        result = migrate_standalone(h.ctx, make_item())
        self.assertEqual(result.new_agent_id, NEW_ID)

    def test_foreign_agent_with_same_ip_but_other_name_never_gets_shares_or_tests(self):
        h = Harness()

        def register():  # only a foreign box shows up; the real agent never registers
            h.world.agents[TARGET].append(AgentRecord('77', 'other-customer-box', 'other-customer-box', [IP],
                                                      'online', TARGET, agent_type='enterprise'))
            h.world.old.state = 'offline'

        h.world.register_new_agent = register
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('did not appear online', str(caught.exception))
        self.assertEqual(h.writes(), [])

    def test_foreign_agent_with_same_name_but_other_ip_is_ignored(self):
        h = Harness()

        def register():
            h.world.agents[TARGET].append(AgentRecord('77', NAME, NAME, ['10.200.0.1'], 'online', TARGET,
                                                      agent_type='enterprise'))
        h.world.register_new_agent = register
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item())
        self.assertEqual(h.writes(), [])

    def test_agent_already_in_target_before_the_reset_is_never_taken(self):
        h = Harness()
        # offline duplicate with the same IP and name: allowed before the reset (only ONLINE ones block it)
        h.world.agents[TARGET].append(AgentRecord('55', NAME, NAME, [IP], 'offline', TARGET, agent_type='enterprise'))
        result = migrate_standalone(h.ctx, make_item())
        self.assertEqual(result.new_agent_id, NEW_ID)
        self.assertIn('55', h.api.wait_args['known_ids'])
        self.assertNotIn(('assign_tests', TARGET, '55', ['101', '102']), h.api.calls)

    def test_two_qualifying_agents_abort_and_list_ids(self):
        h = Harness()
        h.world.extra_on_register = [AgentRecord('78', NAME + '-zz', NAME, [IP], 'online', TARGET,
                                                 agent_type='enterprise')]
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        text = str(caught.exception)
        self.assertIn('more than one new online agent', text)
        self.assertIn('78', text)
        self.assertIn(NEW_ID, text)
        self.assertEqual(h.writes(), [])
        self.assertIn('api_target_online', h.console.steps(StepStatus.FAILED))


class StandaloneUiIpTests(unittest.TestCase):
    def test_ui_ip_attribute_is_the_ui_address(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item(ui_ip=UI_IP))
        self.assertEqual(h.opened, [UI_IP])

    def test_missing_ui_ip_falls_back_to_first_record_ip(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        self.assertEqual(h.opened, [IP])

    def test_empty_ui_ip_falls_back(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item(ui_ip=''))
        self.assertEqual(h.opened, [IP])

    def test_ui_ip_without_record_ip_is_enough_and_plan_uses_it(self):
        item = make_item(ips=[], ui_ip=UI_IP)
        plan = dict(plan_standalone(item))
        self.assertIn(UI_IP, plan['ui_authenticate'])
        h = Harness(apply=False)
        self.assertTrue(migrate_standalone(h.ctx, item).ok)


class StandaloneInterruptTests(unittest.TestCase):
    def test_keyboard_interrupt_mid_flow_prints_state_and_reraises(self):
        for point in ('is_reachable', 'assign_tests'):
            h = Harness()
            h.world.interrupt_at = point
            with self.assertRaises(KeyboardInterrupt):
                migrate_standalone(h.ctx, make_item())
            line = h.stop_line()
            self.assertIn('state:', line)
            self.assertIn('interrupted by KeyboardInterrupt', line)
            self.assertEqual(len(h.console.steps(StepStatus.FAILED)), 1)
            self.assertNotIn(FAKE_TOKEN, h.console.text())
            self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])

    def test_interrupt_during_reset_says_reset_state_is_unknown(self):
        h = Harness()
        h.world.interrupt_at = 'reset_agent'
        with self.assertRaises(KeyboardInterrupt):
            migrate_standalone(h.ctx, make_item())
        self.assertIn('reset command sent; agent state unknown', h.stop_line())

    def test_system_exit_is_reraised_too(self):
        h = Harness()

        def boom(*args, **kwargs):
            raise SystemExit(3)
        h.api.wait_for_agent = boom
        with self.assertRaises(SystemExit):
            migrate_standalone(h.ctx, make_item())
        self.assertIn('interrupted by SystemExit', h.stop_line())

    def test_interrupt_in_dry_run_also_reraises(self):
        h = Harness(apply=False)

        def boom(*args, **kwargs):
            raise KeyboardInterrupt()
        h.api.list_agents = boom
        with self.assertRaises(KeyboardInterrupt):
            migrate_standalone(h.ctx, make_item())
        self.assertIn('state:', h.stop_line())


class StandaloneDeleteStateTests(unittest.TestCase):
    def test_delete_ok_then_absent_wait_times_out_keeps_unconfirmed_state(self):
        h = Harness()
        h.world.absent_timeout = True
        with self.assertRaises(VisibilityTimeout):
            migrate_standalone(h.ctx, make_item())
        self.assertIn(('delete_agent', SOURCE, OLD_ID, 'enterprise'), h.api.calls)
        line = h.stop_line()
        self.assertIn('delete sent, not confirmed', line)
        self.assertNotIn('old agent deleted', line)
        self.assertIn('api_delete_old', h.console.steps(StepStatus.FAILED))
        self.assertNotIn('api_rename', h.console.steps())

    def test_state_after_confirmed_delete_says_deleted(self):
        h = Harness()
        # the rename step fails after the delete was confirmed
        h.api.update_agent_name = lambda *a, **k: (_ for _ in ()).throw(ApiError('rename failed'))
        h.world.names[NEW_ID] = 'wrong'
        with self.assertRaises(ApiError):
            migrate_standalone(h.ctx, make_item())
        self.assertIn('old agent deleted', h.stop_line())
        self.assertNotIn('delete sent, not confirmed', h.stop_line())

    def test_delete_passes_the_real_agent_type(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item(agent_type='enterprise'))
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'],
                         [('delete_agent', SOURCE, OLD_ID, 'enterprise')])

    def test_unknown_type_is_read_from_the_agent_before_delete(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item(agent_type=None))
        self.assertIn(('delete_agent', SOURCE, OLD_ID, 'enterprise'), h.api.calls)

    def test_unknown_type_everywhere_refuses_to_delete(self):
        h = Harness()
        h.world.old.agent_type = None
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item(agent_type=None))
        self.assertIn('type of old agent', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])

    def test_cluster_type_is_never_deleted(self):
        h = Harness()
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item(agent_type='enterprise-cluster'))
        self.assertIn('it is a cluster', str(caught.exception))
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])


class StandaloneWriteShapeTests(unittest.TestCase):
    def test_share_put_that_returns_a_list_wrapper_is_fine(self):
        h = Harness()
        h.world.list_result = True
        result = migrate_standalone(h.ctx, make_item())
        self.assertTrue(result.ok)


class StandaloneSnapshotFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, 'snaps')

    def files(self):
        return sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []

    def test_snapshot_written_before_reset_with_modes_and_no_token(self):
        h = Harness(snapshot_dir=self.dir)
        result = migrate_standalone(h.ctx, make_item(ui_ip=UI_IP, tags=[
            {'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A', 'secretField': FAKE_TOKEN}]))
        self.assertEqual(len(self.files()), 1)
        path = os.path.join(self.dir, self.files()[0])
        self.assertEqual(result.snapshot, path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        with open(path) as handle:
            text = handle.read()
        self.assertNotIn(FAKE_TOKEN, text)
        data = json.loads(text)
        self.assertEqual(data['name'], NAME)
        self.assertEqual(data['record']['agent_id'], OLD_ID)
        self.assertEqual(data['share_aids'], [SOURCE, OTHER])
        self.assertEqual(data['tests_by_aid'], {SOURCE: ['101', '102'], OTHER: ['201']})
        self.assertEqual(data['tags'], [{'aid': SOURCE, 'tagId': 't1', 'key': 'site', 'value': 'A'}])
        self.assertEqual(data['ui_ip'], UI_IP)
        self.assertEqual(data['extra']['ui_settings'], {'browserbot': False, 'crash_reports': True})
        self.assertNotIn('token', ' '.join(data.keys()).lower())
        self.assertRegex(self.files()[0], r'^\d{8}T\d{6}Z_SITE-A-EA01\.json$')
        # on disk before the reset
        self.assertTrue(any('snapshot file written' in m for _, m in h.console.infos))

    def test_snapshot_exists_when_reset_is_called(self):
        h = Harness(snapshot_dir=self.dir)
        seen = []
        original = FakeUi.reset_agent

        def spy(ui):
            seen.append(self.files())
            original(ui)

        with mock.patch.object(FakeUi, 'reset_agent', spy):
            migrate_standalone(h.ctx, make_item())
        self.assertEqual(len(seen[0]), 1)

    def test_enabled_flags_are_saved_when_preflight_provides_them(self):
        h = Harness(snapshot_dir=self.dir)
        item = make_item()
        item.test_enabled = {SOURCE: {'101': True, '102': None}, OTHER: {'201': False}}
        migrate_standalone(h.ctx, item)
        with open(os.path.join(self.dir, self.files()[0])) as handle:
            data = json.load(handle)
        self.assertEqual(data['test_enabled'], {SOURCE: {'101': True, '102': None}, OTHER: {'201': False}})

    def test_name_is_sanitized(self):
        h = Harness(snapshot_dir=self.dir)
        item = make_item()
        item.inventory.hostname = '../../etc/pass wd;rm -rf *'
        item.record.name = item.inventory.hostname
        h.ctx.write_snapshot(item, 'x')
        name = self.files()[0]
        self.assertNotIn('/', name)
        self.assertRegex(name, r'^[A-Za-z0-9._-]+$')
        self.assertTrue(os.path.abspath(os.path.join(self.dir, name)).startswith(os.path.abspath(self.dir)))
        self.assertFalse(name.split('_', 1)[1].startswith('.'))

    def test_without_directory_no_file_and_info_line(self):
        h = Harness(snapshot_dir=None)
        result = migrate_standalone(h.ctx, make_item())
        self.assertIsNone(result.snapshot)
        self.assertTrue(any('no snapshot directory set' in m for _, m in h.console.infos))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_failure_state_line_names_the_snapshot_file(self):
        h = Harness(snapshot_dir=self.dir)
        h.world.reachable = [False]
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item())
        path = os.path.join(self.dir, self.files()[0])
        self.assertIn(path, h.stop_line())

    def test_failure_before_the_snapshot_says_not_written(self):
        h = Harness(snapshot_dir=self.dir)
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item(status='blocked'))
        self.assertIn('not written yet', h.stop_line())
        self.assertEqual(self.files(), [])

    def test_failure_without_directory_says_none(self):
        h = Harness()
        h.world.reachable = [False]
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, make_item())
        self.assertIn('snapshot file: none (no snapshot directory set)', h.stop_line())

    def test_unwritable_directory_stops_before_the_reset(self):
        blocker = os.path.join(self.tmp, 'afile')
        open(blocker, 'w').close()
        h = Harness(snapshot_dir=os.path.join(blocker, 'sub'))
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('snapshot file could not be written', str(caught.exception))
        self.assertNotIn(('ui', 'reset_agent'), h.world.events)

    def test_dry_run_writes_no_file(self):
        h = Harness(apply=False, snapshot_dir=self.dir)
        migrate_standalone(h.ctx, make_item())
        self.assertFalse(os.path.exists(self.dir))
        self.assertTrue(any('would be written' in m for _, m in h.console.infos))

    def test_two_runs_in_the_same_second_do_not_overwrite(self):
        h = Harness(snapshot_dir=self.dir)
        item = make_item()
        with mock.patch('te_agent_migrate.strategy_d.context.datetime') as fake_dt:
            fake_dt.now.return_value.strftime.return_value = '20260101T000000Z'
            h.ctx.write_snapshot(item, 'x')
            h.ctx.write_snapshot(item, 'x')
        self.assertEqual(len(self.files()), 2)


class ReadOnlyApiTests(unittest.TestCase):
    def test_reads_pass_writes_raise(self):
        w = World()
        api = FakeApi(w)
        ro = ReadOnlyApi(api)
        self.assertEqual(len(ro.list_agents(SOURCE)), 1)
        for name in ('delete_agent', 'update_agent_name', 'assign_tests', 'set_agent_account_groups',
                     'cluster_assign', 'cluster_unassign', 'assign_tag', 'set_test_enabled', 'update_test',
                     'create_test', 'create_tag', 'delete_test'):
            with self.assertRaises(RuntimeError) as caught:
                getattr(ro, name)('1', '2', '3')
            self.assertEqual(str(caught.exception), 'write call in dry run: %s' % name)
        self.assertEqual(api.calls, [('list_agents', SOURCE)])

    def test_request_write_methods_raise_and_get_passes(self):
        class Raw(object):
            def __init__(self):
                self.calls = []

            def request(self, method, path, **kwargs):
                self.calls.append((method, path))
                return {}

        raw = Raw()
        ro = ReadOnlyApi(raw)
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'post', 'delete'):
            with self.assertRaises(RuntimeError) as caught:
                ro.request(method, 'agents/1')
            self.assertIn('write call in dry run', str(caught.exception))
        self.assertEqual(ro.request('GET', 'agents'), {})
        self.assertEqual(raw.calls, [('GET', 'agents')])

    def test_wait_helpers_are_reads(self):
        w = World()
        ro = ReadOnlyApi(FakeApi(w))
        ro.wait_for_agent_absent(TARGET, 'nope', 1)


class PlanTests(unittest.TestCase):
    def test_plan_order_and_names(self):
        plan = plan_standalone(make_item())
        self.assertEqual([p[0] for p in plan], [
            'preflight_snapshot', 'ui_authenticate', 'ui_snapshot_before', 'ui_reset',
            'ui_reachable_after_reset', 'ui_authenticate_after_reset', 'ui_set_target_token',
            'api_target_online', 'api_share_back', 'api_assign_tests', 'api_delete_old', 'api_rename',
            'api_restore_tags', 'manual_notification_rules'])
        for step, detail in plan:
            self.assertTrue(detail)

    def test_plan_is_pure_and_target_optional(self):
        item = make_item()
        first = plan_standalone(item)
        second = plan_standalone(item)
        self.assertEqual(first, second)
        with_target = dict(plan_standalone(item, target_aid=TARGET))
        self.assertIn('[9,1,2]', with_target['api_share_back'])

    def test_plan_without_tags_or_tests(self):
        plan = dict(plan_standalone(make_item(tests={}, tags=[], shares=[])))
        self.assertEqual(plan['api_assign_tests'], 'no tests to assign')
        self.assertEqual(plan['api_restore_tags'], 'no tags to restore')
        self.assertIn('nothing to share', plan['api_share_back'])


class StandaloneFixWaveTests(unittest.TestCase):
    """G2 fixes: M2 stop line and exception attributes, L2 source-side check, L5 changed/ok words."""

    def test_stop_line_has_old_and_new_ids_share_set_and_snapshot(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        h = Harness(snapshot_dir=os.path.join(tmp, 'snaps'))
        h.world.delete_fail = True
        with self.assertRaises(ApiError) as caught:
            migrate_standalone(h.ctx, make_item())
        line = h.stop_line()
        self.assertIn('old agent id: %s' % OLD_ID, line)
        self.assertIn('new agent id: %s' % NEW_ID, line)
        self.assertIn('final share set: [%s,%s,%s]' % (TARGET, SOURCE, OTHER), line)
        self.assertIn('snapshot file: ' + h.ctx.snapshot_path, line)
        exc = caught.exception
        self.assertEqual(exc.new_agent_id, NEW_ID)
        self.assertIsNone(exc.new_cluster_id)
        self.assertEqual(exc.snapshot, h.ctx.snapshot_path)
        self.assertIn('delete sent, not confirmed', exc.state)

    def test_stop_line_before_the_new_agent_is_known(self):
        h = Harness()
        h.world.reachable = [False]
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertIn('new agent id: not known yet', h.stop_line())
        self.assertIn('old agent id: %s' % OLD_ID, h.stop_line())
        self.assertIsNone(caught.exception.new_agent_id)
        self.assertIsNone(caught.exception.snapshot)
        self.assertIn('manual action needed', caught.exception.state)

    def test_attributes_are_set_on_keyboard_interrupt_too(self):
        h = Harness()
        h.world.interrupt_at = 'assign_tests'
        with self.assertRaises(KeyboardInterrupt) as caught:
            migrate_standalone(h.ctx, make_item())
        self.assertEqual(caught.exception.new_agent_id, NEW_ID)
        self.assertIn('interrupted by KeyboardInterrupt', h.stop_line())

    def test_flow_aborted_has_default_attributes(self):
        exc = FlowAborted('x')
        self.assertIsNone(exc.new_agent_id)
        self.assertIsNone(exc.new_cluster_id)
        self.assertIsNone(exc.snapshot)
        self.assertIsNone(exc.state)

    def test_source_check_needs_ip_and_name(self):
        h = Harness()
        item = make_item()
        prior = set([OLD_ID])
        check = lambda: standalone._require_no_new_source_identity(h.ctx, item, IP, prior)
        # same IP, other name: not this device
        h.world.agents[SOURCE].append(AgentRecord('70', 'unrelated', 'unrelated', [IP], 'online', SOURCE))
        check()
        # same name, other IP: not this device
        h.world.agents[SOURCE].append(AgentRecord('71', NAME, NAME, ['10.7.7.7'], 'online', SOURCE))
        check()
        # offline record with IP and name: not a live registration
        h.world.agents[SOURCE].append(AgentRecord('72', NAME, NAME, [IP], 'offline', SOURCE))
        check()
        # IP and name together: wrong group
        h.world.agents[SOURCE].append(AgentRecord('73', NAME + '-ab12', 'other', [IP], 'online', SOURCE))
        with self.assertRaises(FlowAborted) as caught:
            check()
        self.assertIn('73', str(caught.exception))
        self.assertNotIn('70', str(caught.exception))

    def test_source_check_ignores_known_ids(self):
        h = Harness()
        h.world.agents[SOURCE].append(AgentRecord('73', NAME, NAME, [IP], 'online', SOURCE))
        standalone._require_no_new_source_identity(h.ctx, make_item(), IP, set([OLD_ID, '73']))

    def test_apply_marks_writes_changed_and_reads_ok(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item())
        by_step = dict((r[2], r[0]) for r in h.console.statuses)
        for step in ('ui_reset', 'ui_set_target_token', 'api_share_back', 'api_assign_tests', 'api_delete_old',
                     'api_rename', 'api_restore_tags'):
            self.assertEqual(by_step[step], StepStatus.CHANGED, step)
        for step in ('preflight_snapshot', 'ui_authenticate', 'ui_snapshot_before', 'ui_reachable_after_reset',
                     'ui_authenticate_after_reset', 'api_target_online'):
            self.assertEqual(by_step[step], StepStatus.OK, step)

    def test_rename_is_ok_when_the_name_is_already_right(self):
        h = Harness()
        h.world.new_name = NAME
        migrate_standalone(h.ctx, make_item())
        self.assertEqual([c for c in h.api.calls if c[0] == 'update_agent_name'], [])
        self.assertEqual(dict((r[2], r[0]) for r in h.console.statuses)['api_rename'], StepStatus.OK)

    def test_tags_step_is_ok_when_nothing_is_restored(self):
        h = Harness()
        migrate_standalone(h.ctx, make_item(tags=[]))
        rows = [r for r in h.console.statuses if r[2] == 'api_restore_tags']
        self.assertEqual([r[0] for r in rows], [StepStatus.SKIPPED])

    def test_dry_run_keeps_would_lines(self):
        h = Harness(apply=False)
        migrate_standalone(h.ctx, make_item())
        rows = [r for r in h.console.statuses if r[2] == 'ui_reset']
        self.assertEqual(rows[0][0], StepStatus.CHANGED)
        self.assertTrue(rows[0][3].startswith('would:'))
        self.assertEqual(h.writes(), [])


class RealConsoleIntegrationTests(unittest.TestCase):
    def test_runs_with_real_console_and_counts_no_failures(self):
        h = Harness()
        stream = io.StringIO()
        console = MigrationConsole(stream=stream, use_color=False)
        h.ctx.console = console
        migrate_standalone(h.ctx, make_item())
        self.assertFalse(console.has_failures())
        self.assertIn('ok:', stream.getvalue())
        self.assertNotIn(FAKE_TOKEN, stream.getvalue())


if __name__ == '__main__':
    unittest.main()
