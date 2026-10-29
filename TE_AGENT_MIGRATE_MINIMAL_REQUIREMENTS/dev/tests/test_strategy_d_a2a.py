"""Tests for the opt-in repoint of agent-to-agent tests (``--repoint-agent-to-agent``).

Synthetic data only. The API, the UI, the flows and the prompts are fakes. No network is used.
The API shape (``tests/agent-to-agent/{id}``, field ``targetAgentId``) is an ASSUMPTION that the lab must check.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from te_agent_migrate import cli
from te_agent_migrate.api_client import A2aTargetChanged
from te_agent_migrate.errors import ApiError
from te_agent_migrate.models import InventoryAgent, StepStatus
from te_agent_migrate.strategy_d import preflight as pf
from te_agent_migrate.strategy_d import runner
from te_agent_migrate.strategy_d.context import FlowAborted, FlowContext, PreflightItem, ReadOnlyApi
from te_agent_migrate.strategy_d.standalone import migrate_standalone, plan_standalone

from dev.tests import test_api_client_v7 as api_tests
from dev.tests import test_cli as cli_tests
from dev.tests import test_strategy_d_preflight as pf_tests
from dev.tests import test_strategy_d_standalone as sa

SECRET_MARKER = 'MARKER-SECRET-SCRIPT-0001'  # synthetic marker, not a credential
STEP = 'repoint a2a targets'
A2A_CALL = 'set_agent_to_agent_target'


# ---------------------------------------------------------------- API client
def a2a_raw(target, test_id='70', **extra):
    raw = {
        'testId': test_id, 'testName': 'AAAA - a2a', 'type': 'agent-to-agent', 'enabled': True,
        'targetAgentId': target, 'interval': 300,
        'agents': [{'agentId': '5', 'agentName': 'AAAA - HOST05', 'location': 'x'}],
        'tags': [{'tagId': 't1', 'key': 'site'}], 'alertRules': [{'ruleId': 'r1', 'ruleName': 'n'}],
        'sharedWithAccounts': [{'aid': '200', 'name': 'Group B'}],
        'createdDate': '2020-01-01', 'modifiedBy': 'someone', '_links': {'self': {'href': 'x'}},
        'transactionScript': SECRET_MARKER,
    }
    raw.update(extra)
    return raw


def ok(payload=None):
    return api_tests.FakeResponse(200, {} if payload is None else payload)


class ApiSetTargetTests(unittest.TestCase):
    def call(self, responses, expected_old='11', new='99'):
        client, session, sleeps = api_tests.make_client(responses)
        try:
            result = client.set_agent_to_agent_target('70', new, '100', expected_old)
        finally:
            self.session, self.sleeps = session, sleeps
        return result

    def methods(self):
        return [c['method'] for c in self.session.calls]

    def test_changes_only_the_target_with_get_put_get(self):
        before = a2a_raw('11')
        after = a2a_raw('99')
        self.assertEqual(self.call([ok(before), ok({}), ok(after)]), 'changed')
        self.assertEqual(self.methods(), ['GET', 'PUT', 'GET'])
        put = self.session.calls[1]
        self.assertTrue(put['url'].endswith('tests/agent-to-agent/70'))
        self.assertEqual(put['params'], {'aid': '100'})
        body = put['json']
        self.assertEqual(body['targetAgentId'], '99')
        # response-only and expanded fields are reduced like in set_test_enabled
        for dropped in ('createdDate', 'modifiedBy', '_links', 'testId', 'type'):
            self.assertNotIn(dropped, body)
        self.assertEqual(body['agents'], [{'agentId': '5'}])
        self.assertEqual(body['tags'], ['t1'])
        self.assertEqual(body['alertRules'], ['r1'])
        self.assertEqual(body['sharedWithAccounts'], [{'aid': 200}])
        # every other field is sent unchanged
        self.assertEqual(body['interval'], 300)
        self.assertEqual(body['enabled'], True)
        self.assertEqual(body['testName'], 'AAAA - a2a')

    def test_already_pointing_at_the_new_id_writes_nothing(self):
        self.assertEqual(self.call([ok(a2a_raw('99'))]), 'already')
        self.assertEqual(self.methods(), ['GET'])

    def test_other_target_than_expected_raises_and_writes_nothing(self):
        with self.assertRaises(A2aTargetChanged) as caught:
            self.call([ok(a2a_raw('55'))])
        self.assertEqual(self.methods(), ['GET'])
        self.assertIn('55', str(caught.exception))
        self.assertIn('11', str(caught.exception))
        self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_unreadable_target_raises_and_writes_nothing(self):
        for value in (None, '', {'agentId': '11'}, ['11'], True):
            with self.assertRaises(ApiError):
                self.call([ok(a2a_raw(value))])
            self.assertEqual(self.methods(), ['GET'])

    def test_wrong_test_type_raises_and_writes_nothing(self):
        with self.assertRaises(ApiError):
            self.call([ok(a2a_raw('11', type='http-server'))])
        self.assertEqual(self.methods(), ['GET'])

    def test_same_old_and_new_id_is_refused_before_any_call(self):
        with self.assertRaises(ApiError):
            self.call([], expected_old='99', new='99')
        self.assertEqual(self.methods(), [])

    def test_read_back_with_old_target_raises(self):
        with self.assertRaises(ApiError) as caught:
            self.call([ok(a2a_raw('11')), ok({}), ok(a2a_raw('11'))])
        self.assertIn('after the PUT', str(caught.exception))

    def test_read_back_with_other_agents_or_enabled_raises(self):
        with self.assertRaises(ApiError):
            self.call([ok(a2a_raw('11')), ok({}), ok(a2a_raw('99', enabled=False))])
        with self.assertRaises(ApiError):
            self.call([ok(a2a_raw('11')), ok({}), ok(a2a_raw('99', agents=[]))])

    def test_put_failure_is_not_retried_and_hides_values(self):
        bad = api_tests.FakeResponse(500, {}, headers={'x-request-id': 'rid-1'})
        with self.assertRaises(ApiError) as caught:
            self.call([ok(a2a_raw('11')), bad])
        self.assertEqual(self.methods(), ['GET', 'PUT'])
        self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_put_retries_on_429_only(self):
        limited = api_tests.FakeResponse(429, {}, headers={'retry-after': '2'})
        self.assertEqual(self.call([ok(a2a_raw('11')), limited, ok({}), ok(a2a_raw('99'))]), 'changed')
        self.assertEqual(self.methods(), ['GET', 'PUT', 'PUT', 'GET'])
        self.assertEqual(self.sleeps, [2.0])

    def test_put_with_an_array_body_is_accepted(self):
        self.assertEqual(self.call([ok(a2a_raw('11')), api_tests.FakeResponse(200, [1]), ok(a2a_raw('99'))]),
                         'changed')

    def test_existing_set_test_enabled_still_sends_the_same_body(self):
        client, session, _ = api_tests.make_client([ok(a2a_raw('11')), ok({})])
        client.set_test_enabled('100', 'agent-to-agent', '70', False)
        body = session.calls[1]['json']
        self.assertEqual(body['enabled'], False)
        self.assertEqual(body['targetAgentId'], '11')
        self.assertEqual(body['agents'], [{'agentId': '5'}])


class ReadOnlyApiTests(unittest.TestCase):
    def test_dry_run_wrapper_refuses_the_write_and_allows_reads(self):
        inner = mock.Mock()
        wrapped = ReadOnlyApi(inner)
        with self.assertRaises(RuntimeError) as caught:
            wrapped.set_agent_to_agent_target('70', '99', '100', '11')
        self.assertIn('write call in dry run: set_agent_to_agent_target', str(caught.exception))
        inner.set_agent_to_agent_target.assert_not_called()
        wrapped.get_test('100', 'agent-to-agent', '70')
        inner.get_test.assert_called_once_with('100', 'agent-to-agent', '70')

    def test_method_is_in_the_explicit_write_list(self):
        from te_agent_migrate.strategy_d.context import WRITE_API_METHODS
        self.assertIn(A2A_CALL, WRITE_API_METHODS)


# ----------------------------------------------------------------- preflight
def a2a_test(test_id, target=None, **extra):
    if target is not None:
        extra['targetAgentId'] = target
    return pf_tests.test_row(test_id, agents=[5], test_type='agent-to-agent', **extra)


def repoint_ctx(api, repoint=True):
    return FlowContext(api, None, pf_tests.FakeConsole(), pf_tests.SRC, pf_tests.TGT, apply=False,
                       repoint_a2a=repoint)


def run_pf(api, entry, repoint=True):
    return pf.run_preflight(repoint_ctx(api, repoint), [entry], all_aids=None)[0]


class PreflightRepointTests(unittest.TestCase):
    def standalone_world(self, tests):
        return pf_tests.world(pf_tests.row(1, 'EA1', '10.0.0.1'), shares=[pf_tests.OTHER], tests=tests)

    def test_without_the_flag_the_device_is_still_blocked(self):
        api = self.standalone_world({pf_tests.OTHER: [a2a_test(70, '1')]})
        item = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'), repoint=False)
        self.assertEqual(item.status, 'blocked')
        self.assertIn('update them first', item.reasons[0])
        self.assertEqual(item.a2a_targets, [])

    def test_ctx_flag_defaults_to_off(self):
        self.assertFalse(FlowContext(None, None, pf_tests.FakeConsole(), '1', '2').repoint_a2a)
        self.assertTrue(repoint_ctx(None).repoint_a2a)

    def test_with_the_flag_the_device_is_ready_with_a_warning_and_the_recorded_tests(self):
        api = self.standalone_world({
            pf_tests.SRC: [a2a_test(73, '1')],
            pf_tests.OTHER: [a2a_test(70, '1'), a2a_test(71, '2'), a2a_test(72, '1')]})
        item = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(
            sorted((t['aid'], t['test_id'], t['old_target_agent_id']) for t in item.a2a_targets),
            [(pf_tests.SRC, '73', '1'), (pf_tests.OTHER, '70', '1'), (pf_tests.OTHER, '72', '1')])
        self.assertIn('3 agent-to-agent test(s) will be repointed after the move', item.warnings)
        self.assertIn('warning: 3 agent-to-agent test(s) will be repointed after the move', item.reasons)
        # the preflight stays read only: the fake API fails the test on any other call
        self.assertFalse([c for c in api.calls if c[0] not in ('groups', 'agents', 'get_agent', 'tests', 'tags')])

    def test_with_the_flag_a_test_for_another_target_is_not_recorded(self):
        api = self.standalone_world({pf_tests.OTHER: [a2a_test(71, '2')]})
        item = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'ready', item.reasons)
        self.assertEqual(item.a2a_targets, [])
        self.assertEqual(item.warnings, [])

    def test_unreadable_target_is_a_warning_without_the_flag_and_a_blocker_with_it(self):
        api = self.standalone_world({pf_tests.OTHER: [a2a_test(70, '1'), a2a_test(71)]})
        plain = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'), repoint=False)
        self.assertEqual(plain.status, 'blocked')  # test 70 blocks without the flag; look at the warning too
        self.assertTrue(any('no readable targetAgentId' in w for w in plain.warnings))
        only_unreadable = self.standalone_world({pf_tests.OTHER: [a2a_test(71)]})
        warn_item = run_pf(only_unreadable, pf_tests.inv('EA1', '10.0.0.1'), repoint=False)
        self.assertEqual(warn_item.status, 'ready', warn_item.reasons)
        flagged = run_pf(only_unreadable, pf_tests.inv('EA1', '10.0.0.1'))
        self.assertEqual(flagged.status, 'blocked')
        self.assertIn('no readable targetAgentId', flagged.reasons[0])
        self.assertIn('cannot be proven safe', flagged.reasons[0])
        self.assertEqual(flagged.warnings, [])

    def test_target_of_another_type_is_unreadable_and_blocks_with_the_flag(self):
        api = self.standalone_world({pf_tests.OTHER: [a2a_test(70, {'agentId': '1'})]})
        item = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('no readable targetAgentId', item.reasons[0])

    def test_cluster_with_targeting_a2a_tests_is_blocked_with_the_flag(self):
        members = [pf_tests.member('1', 'm1', '10.0.0.1'), pf_tests.member('2', 'm2', '10.0.0.2')]
        raw = pf_tests.cluster_row(5, 'CL1', members)
        api = pf_tests.world(raw, shares=[pf_tests.OTHER], tests={pf_tests.OTHER: [a2a_test(70, '5')]})
        item = run_pf(api, pf_tests.inv('CL1', '', kind='cluster'))
        self.assertEqual(item.status, 'blocked')
        self.assertIn('a2a repoint is not supported for clusters', ' '.join(item.reasons))
        self.assertEqual(item.a2a_targets, [])
        # without the flag the old reason stays
        plain = run_pf(api, pf_tests.inv('CL1', '', kind='cluster'), repoint=False)
        self.assertEqual(plain.status, 'blocked')
        self.assertIn('update them first', ' '.join(plain.reasons))

    def test_cluster_without_targeting_tests_is_unchanged_with_the_flag(self):
        members = [pf_tests.member('1', 'm1', '10.0.0.1'), pf_tests.member('2', 'm2', '10.0.0.2')]
        api = pf_tests.world(pf_tests.cluster_row(5, 'CL1', members), shares=[pf_tests.OTHER],
                             tests={pf_tests.OTHER: [a2a_test(70, '9')]})
        item = run_pf(api, pf_tests.inv('CL1', '', kind='cluster'))
        self.assertEqual(item.status, 'ready', item.reasons)

    def test_unreadable_test_list_of_a_shared_group_blocks_only_with_the_flag(self):
        # An agent-to-agent test in that group could target the device. The repoint
        # cannot be proven complete, and the old agent is deleted at the end.
        api = self.standalone_world({pf_tests.OTHER: ApiError('403 forbidden')})
        plain = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'), repoint=False)
        self.assertEqual(plain.status, 'ready', plain.reasons)
        self.assertTrue(any('could not read tests in group' in w for w in plain.warnings))
        flagged = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'))
        self.assertEqual(flagged.status, 'blocked')
        self.assertIn('repoint cannot be proven complete', ' '.join(flagged.reasons))

    def test_blocked_item_has_no_repoint_warning(self):
        api = self.standalone_world({pf_tests.OTHER: [a2a_test(70, '1'), a2a_test(71)]})
        item = run_pf(api, pf_tests.inv('EA1', '10.0.0.1'))
        self.assertEqual(item.status, 'blocked')
        self.assertFalse([w for w in item.warnings if 'will be repointed' in w])


# ---------------------------------------------------------------------- flow
A2A_ENTRIES = [
    {'aid': sa.SOURCE, 'test_id': '70', 'old_target_agent_id': sa.OLD_ID},
    {'aid': sa.OTHER, 'test_id': '71', 'old_target_agent_id': sa.OLD_ID},
]


class A2aFakeApi(sa.FakeApi):
    """Standalone fake API plus the a2a write. ``targets`` maps (aid, test_id) to the current target id."""

    def __init__(self, world):
        sa.FakeApi.__init__(self, world)
        self.targets = {(sa.SOURCE, '70'): sa.OLD_ID, (sa.OTHER, '71'): sa.OLD_ID}
        self.fail_on = set()

    def set_agent_to_agent_target(self, test_id, new_agent_id, aid, expected_old_agent_id):
        self._log(A2A_CALL, test_id, new_agent_id, aid, expected_old_agent_id)
        if test_id in self.fail_on:
            raise ApiError('API PUT tests/agent-to-agent/%s returned HTTP 500' % test_id)
        current = self.targets[(aid, test_id)]
        if current == new_agent_id:
            return 'already'
        if current != expected_old_agent_id:
            raise A2aTargetChanged('a2a test %s: targetAgentId is %s, expected %s' % (
                test_id, current, expected_old_agent_id))
        self.targets[(aid, test_id)] = new_agent_id
        return 'changed'


class A2aHarness(sa.Harness):
    def __init__(self, apply=True, snapshot_dir=None, token=sa.FAKE_TOKEN):
        sa.Harness.__init__(self, apply=apply, snapshot_dir=snapshot_dir, token=token)
        self.api = A2aFakeApi(self.world)
        self.ctx = FlowContext(
            self.api, self.ctx.ui_factory, self.console, sa.SOURCE, sa.TARGET, target_account_token=token,
            apply=apply, timeout_seconds=2, poll_seconds=1, sleep=self.slept.append, snapshot_dir=snapshot_dir,
            repoint_a2a=True)

    def a2a_calls(self):
        return [c for c in self.api.calls if c[0] == A2A_CALL]

    def writes_of_any_kind(self):
        return [c for c in self.api.calls if c[0] in sa.WRITE_CALLS or c[0] == A2A_CALL]


def a2a_item(entries=None):
    item = sa.make_item()
    item.a2a_targets = [dict(e) for e in (A2A_ENTRIES if entries is None else entries)]
    return item


class FlowRepointTests(unittest.TestCase):
    def test_repoints_after_assign_and_before_delete_with_the_new_id(self):
        h = A2aHarness()
        result = migrate_standalone(h.ctx, a2a_item())
        self.assertTrue(result.ok)
        self.assertEqual(h.a2a_calls(), [
            (A2A_CALL, '70', sa.NEW_ID, sa.SOURCE, sa.OLD_ID),
            (A2A_CALL, '71', sa.NEW_ID, sa.OTHER, sa.OLD_ID)])
        self.assertEqual(h.api.targets, {(sa.SOURCE, '70'): sa.NEW_ID, (sa.OTHER, '71'): sa.NEW_ID})
        events = h.world.events
        first = h.event_index('api', A2A_CALL)
        last = max(i for i, e in enumerate(events) if e[:2] == ('api', A2A_CALL))
        last_assign = max(i for i, e in enumerate(events) if e[:2] == ('api', 'assign_tests'))
        last_share = max(i for i, e in enumerate(events) if e[:2] == ('api', 'set_agent_account_groups'))
        delete_at = h.event_index('api', 'delete_agent')
        self.assertLess(last_share, first)
        self.assertLess(last_assign, first)
        self.assertLess(last, delete_at)
        steps = h.console.steps()
        self.assertEqual(steps.count(STEP), 1)
        self.assertEqual(steps.index(STEP), steps.index('api_assign_tests') + 1)
        self.assertEqual(steps.index('api_delete_old'), steps.index(STEP) + 1)
        self.assertIn(STEP, h.console.steps(StepStatus.CHANGED))
        self.assertIn('2 a2a test(s) repointed', result.detail)

    def test_without_recorded_tests_there_is_no_step_and_no_call(self):
        h = A2aHarness()
        result = migrate_standalone(h.ctx, a2a_item([]))
        self.assertTrue(result.ok)
        self.assertEqual(h.a2a_calls(), [])
        self.assertNotIn(STEP, h.console.steps())
        self.assertNotIn('a2a', result.detail)

    def test_a_test_that_already_points_at_the_new_id_is_skipped_without_a_write(self):
        h = A2aHarness()
        h.api.targets[(sa.SOURCE, '70')] = sa.NEW_ID
        result = migrate_standalone(h.ctx, a2a_item())
        self.assertTrue(result.ok)
        self.assertEqual(h.api.targets[(sa.OTHER, '71')], sa.NEW_ID)
        self.assertTrue(any('already targets agent %s' % sa.NEW_ID in msg for _, msg in h.console.infos))
        self.assertIn('2 a2a test(s) repointed', result.detail)
        self.assertEqual(len(h.a2a_calls()), 2)  # the fake API decides 'already'; it makes no write

    def test_changed_old_target_fails_and_the_old_agent_is_not_deleted(self):
        h = A2aHarness()
        h.api.targets[(sa.OTHER, '71')] = '55'  # someone changed it
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, a2a_item())
        text = str(caught.exception)
        self.assertIn('test 71', text)
        self.assertIn('NOT deleted', text)
        self.assertIn('repointed tests (aid/test): [%s/70]' % sa.SOURCE, text)
        self.assertIn('not repointed: [%s/71]' % sa.OTHER, text)
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])
        self.assertIn(STEP, h.console.steps(StepStatus.FAILED))
        self.assertEqual(caught.exception.new_agent_id, sa.NEW_ID)
        self.assertIn('NOT deleted', caught.exception.state)
        self.assertIn('old agent %s is still present' % sa.OLD_ID, caught.exception.state)
        self.assertNotIn('update_agent_name', [c[0] for c in h.api.calls])

    def test_put_failure_keeps_the_old_agent_and_stops_at_the_first_failure(self):
        h = A2aHarness()
        h.api.fail_on.add('70')
        with self.assertRaises(FlowAborted) as caught:
            migrate_standalone(h.ctx, a2a_item())
        text = str(caught.exception)
        self.assertIn('not repointed: [%s/70, %s/71]' % (sa.SOURCE, sa.OTHER), text)
        self.assertIn('repointed tests (aid/test): []', text)
        self.assertEqual(len(h.a2a_calls()), 1)
        self.assertEqual([c for c in h.api.calls if c[0] == 'delete_agent'], [])
        self.assertIn('new agent %s is online' % sa.NEW_ID, text)

    def test_failure_text_never_holds_the_token(self):
        h = A2aHarness()
        h.api.fail_on.add('70')
        with self.assertRaises(FlowAborted):
            migrate_standalone(h.ctx, a2a_item())
        self.assertNotIn(sa.FAKE_TOKEN, h.console.text())

    def test_dry_run_prints_the_step_and_the_count_and_writes_nothing(self):
        h = A2aHarness(apply=False)
        result = migrate_standalone(h.ctx, a2a_item())
        self.assertTrue(result.ok)
        rows = [r for r in h.console.statuses if r[2] == STEP]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], StepStatus.CHANGED)
        self.assertIn('would:', rows[0][3])
        self.assertIn('2 agent-to-agent test(s)', rows[0][3])
        self.assertEqual(h.writes_of_any_kind(), [])
        self.assertEqual(h.opened, [])

    def test_plan_has_the_step_between_assign_and_delete_only_with_targets(self):
        names = [name for name, _ in plan_standalone(a2a_item(), sa.TARGET)]
        self.assertEqual(names.index(STEP), names.index('api_assign_tests') + 1)
        self.assertEqual(names.index('api_delete_old'), names.index(STEP) + 1)
        self.assertNotIn(STEP, [name for name, _ in plan_standalone(a2a_item([]), sa.TARGET)])


class SnapshotTests(unittest.TestCase):
    def test_snapshot_holds_the_a2a_list_and_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = A2aHarness(snapshot_dir=tmp, token=SECRET_MARKER)
            item = a2a_item()
            # extra keys and extra attributes must not reach the file (whitelist)
            item.a2a_targets[0]['transactionScript'] = SECRET_MARKER
            item.a2a_targets[0]['old_target_agent_id'] = sa.OLD_ID
            result = migrate_standalone(h.ctx, item)
            self.assertTrue(result.ok)
            path = Path(result.snapshot)
            text = path.read_text(encoding='utf-8')
            data = json.loads(text)
        self.assertEqual(data['a2a_targets'], [
            {'aid': sa.SOURCE, 'old_target_agent_id': sa.OLD_ID, 'test_id': '70'},
            {'aid': sa.OTHER, 'old_target_agent_id': sa.OLD_ID, 'test_id': '71'}])
        self.assertNotIn(SECRET_MARKER, text)
        self.assertNotIn('transactionScript', text)

    def test_snapshot_is_written_before_the_first_destructive_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = A2aHarness(snapshot_dir=tmp)
            migrate_standalone(h.ctx, a2a_item())
            infos = [msg for _, msg in h.console.infos if msg.startswith('snapshot file written')]
        self.assertEqual(len(infos), 1)

    def test_snapshot_without_targets_has_an_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = A2aHarness(snapshot_dir=tmp)
            result = migrate_standalone(h.ctx, a2a_item([]))
            data = json.loads(Path(result.snapshot).read_text(encoding='utf-8'))
        self.assertEqual(data['a2a_targets'], [])


# ------------------------------------------------------------------ runner
class RunnerGuardTests(unittest.TestCase):
    def test_cluster_item_with_targets_is_failed_by_the_second_guard(self):
        console = sa.FakeConsole()
        console.play = lambda *a, **k: None
        console.recap = lambda rows: setattr(console, 'rows', rows)
        console.task = lambda *a, **k: None
        inventory = InventoryAgent('AAAA - CL01', '', kind='cluster')
        item = PreflightItem(inventory, record=cli_tests.Rec('13'), status='ready', owner_aid='1',
                             share_aids=['1'])
        item.a2a_targets = [dict(A2A_ENTRIES[0])]
        ctx = FlowContext(mock.Mock(), None, console, '1', '9', apply=False)
        with mock.patch.object(runner, 'run_preflight', return_value=[item]), \
                mock.patch.object(runner, 'migrate_cluster') as cluster_flow:
            results = runner.run_strategy_d(ctx, [inventory], console)
        cluster_flow.assert_not_called()
        self.assertFalse(results[0].ok)
        self.assertIn('a2a repoint is not supported for clusters', results[0].detail)


# --------------------------------------------------------------------- CLI
def make_target_item(inventory, record_id, targets, status='ready'):
    item = PreflightItem(inventory, record=cli_tests.Rec(record_id), status=status, owner_aid='1',
                         share_aids=['1'])
    item.a2a_targets = [{'aid': aid, 'test_id': test_id, 'old_target_agent_id': record_id}
                        for aid, test_id in targets]
    return item


def targeting_preflight(targets):
    """Preflight stand-in: EA-ONE carries the given (aid, test_id) a2a targets."""
    def run(inventory_agents, **unused):
        items = []
        for inv in inventory_agents:
            status = 'excluded' if inv.hostname == 'EA-OFF' else 'ready'
            pairs = targets if inv.hostname == 'EA-ONE' else []
            items.append(make_target_item(inv, cli_tests.IDS[inv.hostname], pairs, status))
        return items
    return run


class GateTests(unittest.TestCase):
    def binding_file(self, items, flag, schema=cli.DRY_RUN_SCHEMA):
        data = {'schema_version': schema}
        data.update(cli._dry_run_binding(items, False, '9', flag))
        return json.loads(json.dumps(data))  # like the file on disk

    def items(self, pairs):
        inventory = InventoryAgent('AAAA - HOST01', '192.0.2.1')
        return [make_target_item(inventory, '11', pairs)]

    def test_schema_is_4(self):
        self.assertEqual(cli.DRY_RUN_SCHEMA, 4)

    def test_binding_holds_the_sorted_pairs_and_the_flag(self):
        binding = cli._dry_run_binding(self.items([('200', '71'), ('100', '70')]), False, '9', True)
        self.assertTrue(binding['repoint_a2a'])
        self.assertEqual(binding['ready'][0]['a2a_targets'], [['100', '70'], ['200', '71']])
        plain = cli._dry_run_binding(self.items([]), False, '9')
        self.assertFalse(plain['repoint_a2a'])
        self.assertEqual(plain['ready'][0]['a2a_targets'], [])

    def test_same_state_passes_whatever_the_order(self):
        data = self.binding_file(self.items([('100', '70'), ('200', '71')]), True)
        cli._compare_dry_run_binding(data, self.items([('200', '71'), ('100', '70')]), False, '9', True)

    def test_changed_set_of_tests_refuses(self):
        data = self.binding_file(self.items([('100', '70')]), True)
        for now in ([('100', '70'), ('100', '72')], [], [('100', '71')]):
            with self.assertRaises(cli.PreconditionRefused) as caught:
                cli._compare_dry_run_binding(data, self.items(now), False, '9', True)
            self.assertIn('agent-to-agent tests to repoint changed', str(caught.exception))
            self.assertIn('Nothing was changed.', str(caught.exception))

    def test_changed_flag_refuses_both_ways(self):
        data = self.binding_file(self.items([('100', '70')]), True)
        with self.assertRaises(cli.PreconditionRefused) as caught:
            cli._compare_dry_run_binding(data, self.items([('100', '70')]), False, '9', False)
        self.assertIn('--repoint-agent-to-agent was set in the dry run and is not set now', str(caught.exception))
        data = self.binding_file(self.items([]), False)
        with self.assertRaises(cli.PreconditionRefused) as caught:
            cli._compare_dry_run_binding(data, self.items([]), False, '9', True)
        self.assertIn('was not set in the dry run and is set now', str(caught.exception))

    def test_schema_3_is_refused(self):
        data = self.binding_file(self.items([]), False, schema=3)
        with self.assertRaises(cli.PreconditionRefused) as caught:
            cli._compare_dry_run_binding(data, self.items([]), False, '9', False)
        self.assertIn('old file format', str(caught.exception))
        self.assertIn('run --dry-run again', str(caught.exception))


class CliTests(cli_tests.CliTestBase):
    ARGS = ['--strategy', 'D', '--repoint-agent-to-agent']
    PAIRS = [('1', '70'), ('9', '71')]

    def gate(self):
        return json.loads(self.dry_run_file().read_text(encoding='utf-8'))

    def apply_argv(self, *extra):
        return ['--strategy', 'D', '--apply', '--confirmed-dry-run-file', str(self.dry_run_file())] + list(extra)

    def capture(self):
        seen = []

        class Ctx(cli.FlowContext):
            def __init__(self, **kwargs):
                seen.append(kwargs.get('repoint_a2a', False))
                super(Ctx, self).__init__(**kwargs)

        return seen, Ctx

    def test_flag_is_off_by_default_and_the_help_explains_it(self):
        parser = cli.build_parser()
        self.assertFalse(parser.parse_args(['--strategy', 'D']).repoint_agent_to_agent)
        self.assertTrue(parser.parse_args(self.ARGS).repoint_agent_to_agent)
        text = ' '.join(parser.format_help().split())
        for word in ('--repoint-agent-to-agent', 'BLOCKER', 'targetAgentId', 'BEFORE the old agent is deleted',
                     'cluster', 'undo by hand'):
            self.assertIn(word, text)

    def test_dry_run_file_binds_the_flag_and_the_tests(self):
        code, out, err = self.run_main(self.ARGS, preflight=targeting_preflight(self.PAIRS))
        self.assertEqual(code, 0, out + err)
        data = self.gate()
        self.assertEqual(data['schema_version'], 4)
        self.assertTrue(data['repoint_a2a'])
        ready = dict((e['name'], e) for e in data['ready'])
        self.assertEqual(ready['EA-ONE']['a2a_targets'], [['1', '70'], ['9', '71']])
        self.assertEqual(ready['CL-ONE']['a2a_targets'], [])

    def test_summary_json_has_the_flag(self):
        for argv, expected in ((self.ARGS, True), (['--strategy', 'D'], False)):
            self.run_main(argv, preflight=targeting_preflight(self.PAIRS if expected else []))
            files = sorted(self.out.glob('**/*_strategy_d.json'))
            self.assertTrue(files)
            data = json.loads(files[-1].read_text(encoding='utf-8'))
            self.assertIs(data['repoint_a2a'], expected)

    def test_context_gets_the_flag_only_when_set(self):
        seen, Ctx = self.capture()
        with mock.patch.object(cli, 'FlowContext', Ctx):
            self.run_main(['--strategy', 'D'], preflight=targeting_preflight([]))
            self.assertEqual(seen, [False])
            del seen[:]
            self.run_main(self.ARGS, preflight=targeting_preflight(self.PAIRS))
        self.assertEqual(seen, [True])

    def test_apply_with_the_same_flag_and_tests_runs(self):
        self.run_main(self.ARGS, preflight=targeting_preflight(self.PAIRS))
        self.flows.reset()
        seen, Ctx = self.capture()
        with mock.patch.object(cli, 'FlowContext', Ctx):
            code, out, err = self.run_main(self.apply_argv('--repoint-agent-to-agent'), env=self.apply_env(),
                                           preflight=targeting_preflight(self.PAIRS))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [True, True])
        self.assertEqual(self.flows.names, ['EA-ONE', 'CL-ONE'])

    def test_apply_is_refused_when_the_flag_differs_from_the_dry_run(self):
        self.run_main(self.ARGS, preflight=targeting_preflight(self.PAIRS))
        self.flows.reset()
        code, out, err = self.run_main(self.apply_argv(), env=self.apply_env(),
                                       preflight=targeting_preflight(self.PAIRS))
        self.assertEqual(code, 2, out + err)
        self.assertIn('--repoint-agent-to-agent was set in the dry run and is not set now', err)
        self.assertEqual(self.flows.names, [])
        self.confirm_mock.assert_not_called()
        self.assertTrue(self.dry_run_file().exists())

    def test_apply_is_refused_when_the_tests_differ_from_the_dry_run(self):
        self.run_main(self.ARGS, preflight=targeting_preflight(self.PAIRS))
        self.flows.reset()
        code, out, err = self.run_main(self.apply_argv('--repoint-agent-to-agent'), env=self.apply_env(),
                                       preflight=targeting_preflight(self.PAIRS + [('1', '72')]))
        self.assertEqual(code, 2, out + err)
        self.assertIn('agent-to-agent tests to repoint changed', err)
        self.assertIn('Nothing was changed.', err)
        self.assertEqual(self.flows.names, [])

    def test_apply_is_refused_for_a_schema_3_file(self):
        self.run_main(self.ARGS, preflight=targeting_preflight(self.PAIRS))
        data = self.gate()
        data['schema_version'] = 3
        self.dry_run_file().write_text(json.dumps(data), encoding='utf-8')
        self.flows.reset()
        code, out, err = self.run_main(self.apply_argv('--repoint-agent-to-agent'), env=self.apply_env(),
                                       preflight=targeting_preflight(self.PAIRS))
        self.assertEqual(code, 2, out + err)
        self.assertIn('old file format', err)
        self.assertEqual(self.flows.names, [])

    def test_flag_is_ignored_with_an_info_line_outside_strategy_d(self):
        fake_runner = mock.Mock()
        with mock.patch.object(cli, 'MigrationRunner', return_value=fake_runner):
            code, out, err = self.run_main(['--strategy', 'A', '--repoint-agent-to-agent'], env=self.apply_env())
        self.assertEqual(code, 0, out + err)
        self.assertIn('--repoint-agent-to-agent applies only to Strategy D; ignored', out)

    def test_old_context_without_the_parameter_warns(self):
        class OldCtx(cli.FlowContext):
            def __init__(self, api, ui_factory, console, source_aid, target_aid, target_account_token='',
                         apply=False, timeout_seconds=600, poll_seconds=10, snapshot_dir=None,
                         auto_reenable_tests=False):
                super(OldCtx, self).__init__(
                    api, ui_factory, console, source_aid, target_aid, target_account_token, apply,
                    timeout_seconds, poll_seconds, snapshot_dir=snapshot_dir)

        with mock.patch.object(cli, 'FlowContext', OldCtx):
            code, out, err = self.run_main(self.ARGS, preflight=targeting_preflight(self.PAIRS))
        self.assertEqual(code, 0, out + err)
        self.assertIn('repoint_a2a is not supported by this build', out)


if __name__ == '__main__':
    unittest.main()
