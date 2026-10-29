import io
import unittest
from unittest import mock

from te_agent_migrate.models import InventoryAgent
from te_agent_migrate.output import MigrationConsole
from te_agent_migrate.strategy_d import runner
from te_agent_migrate.strategy_d.context import FlowAborted, FlowContext, FlowResult, PreflightItem

FAKE_TOKEN = 'a1b2c3d4' * 4  # fake 32-character value for masking tests, not a credential


def make_console():
    stream = io.StringIO()
    return MigrationConsole(stream=stream, use_color=False), stream


def make_ctx(console, apply=False):
    return FlowContext(
        api=object(), ui_factory=None, console=console, source_aid='1', target_aid='9', apply=apply
    )


def agent_item(name, status='ready', reasons=None):
    inv = InventoryAgent(name, '10.0.0.%d' % (len(name) + 1), 'agent')
    return PreflightItem(inv, status=status, reasons=reasons, owner_aid='1', share_aids=['1'])


def cluster_item(name, status='ready'):
    inv = InventoryAgent(name, '', 'cluster')
    return PreflightItem(inv, status=status, owner_aid='1', share_aids=['1'])


class Calls(object):
    def __init__(self):
        self.standalone = []
        self.cluster = []
        self.fail_on = {}

    def migrate_standalone(self, ctx, item):
        self.standalone.append(item.name)
        action = self.fail_on.get(item.name)
        if isinstance(action, BaseException):
            raise action
        if action == 'notok':
            return FlowResult(item.name, False, 'reported failure')
        return FlowResult(item.name, True, 'migrated', new_agent_id='99')

    def migrate_cluster(self, ctx, item):
        self.cluster.append(item.name)
        action = self.fail_on.get(item.name)
        if isinstance(action, BaseException):
            raise action
        return FlowResult(item.name, True, 'cluster migrated', new_cluster_id='55')


class RunnerTests(unittest.TestCase):
    def run_batch(self, items, apply=False, calls=None, **kwargs):
        calls = calls or Calls()
        console, stream = make_console()
        ctx = make_ctx(console, apply=apply)
        with mock.patch.object(runner, 'run_preflight', return_value=items) as preflight, \
                mock.patch.object(runner, 'migrate_standalone', side_effect=calls.migrate_standalone), \
                mock.patch.object(runner, 'migrate_cluster', side_effect=calls.migrate_cluster):
            results = runner.run_strategy_d(ctx, [i.inventory for i in items], console, **kwargs)
        self.preflight = preflight
        return results, console, stream.getvalue(), calls

    def test_dry_run_prints_play_and_runs_ready_items_in_order(self):
        items = [agent_item('A1'), cluster_item('C1'), agent_item('A2')]
        results, console, text, calls = self.run_batch(items)
        self.assertIn('PLAY [Strategy D: agent migration]', text)
        self.assertIn('MODE: DRY-RUN   STRATEGY: D', text)
        self.assertIn('PLAY RECAP', text)
        self.assertEqual(calls.standalone, ['A1', 'A2'])
        self.assertEqual(calls.cluster, ['C1'])
        self.assertEqual([r.name for r in results], ['A1', 'C1', 'A2'])
        self.assertTrue(all(r.ok for r in results))
        self.assertFalse(console.has_failures())

    def test_apply_mode_is_shown_and_results_are_ok(self):
        results, console, text, calls = self.run_batch([agent_item('A1')], apply=True)
        self.assertIn('MODE: APPLY   STRATEGY: D', text)
        self.assertEqual(calls.standalone, ['A1'])
        self.assertEqual([r.ok for r in results], [True])
        self.assertEqual([row['status'] for row in results.rows], ['ok'])

    def test_offline_items_are_skipped_and_never_touched(self):
        items = [
            agent_item('A1'),
            agent_item('OFF1', status='excluded', reasons=['offline']),
            cluster_item('COFF', status='excluded'),
        ]
        results, console, text, calls = self.run_batch(items, apply=True)
        self.assertEqual(calls.standalone, ['A1'])
        self.assertEqual(calls.cluster, [])
        statuses = dict((row['agent'], row['status']) for row in results.rows)
        self.assertEqual(statuses, {'A1': 'ok', 'OFF1': 'skipped', 'COFF': 'skipped'})
        self.assertEqual([r.name for r in results], ['A1'])

    def test_blockers_stop_everything_in_apply(self):
        items = [agent_item('A1'), agent_item('BAD', status='blocked', reasons=['name collision']),
                 agent_item('A3')]
        results, console, text, calls = self.run_batch(items, apply=True)
        self.assertEqual(calls.standalone, [])
        self.assertEqual(calls.cluster, [])
        statuses = dict((row['agent'], row['status']) for row in results.rows)
        self.assertEqual(statuses, {'A1': 'skipped', 'BAD': 'failed', 'A3': 'skipped'})
        self.assertTrue(console.has_failures())
        self.assertTrue(results.blocked)
        self.assertIn('name collision', text)
        self.assertTrue(any(not r.ok for r in results))

    def test_blockers_in_dry_run_still_print_plan_of_ready_items(self):
        items = [agent_item('A1'), agent_item('BAD', status='blocked', reasons=['x'])]
        results, console, text, calls = self.run_batch(items, apply=False)
        self.assertEqual(calls.standalone, ['A1'])
        self.assertTrue(results.blocked)
        self.assertTrue(console.has_failures())
        self.assertFalse(all(r.ok for r in results))

    def test_failure_stops_the_batch(self):
        calls = Calls()
        calls.fail_on['A2'] = RuntimeError('boom')
        items = [agent_item('A1'), agent_item('A2'), agent_item('A3'), cluster_item('C1')]
        results, console, text, calls = self.run_batch(items, apply=True, calls=calls)
        self.assertEqual(calls.standalone, ['A1', 'A2'])
        self.assertEqual(calls.cluster, [])
        statuses = dict((row['agent'], row['status']) for row in results.rows)
        self.assertEqual(statuses, {'A1': 'ok', 'A2': 'failed', 'A3': 'skipped', 'C1': 'skipped'})
        self.assertTrue(console.has_failures())
        self.assertIn('boom', text)
        self.assertIn('unknown state', text)

    def test_result_not_ok_also_stops_the_batch(self):
        calls = Calls()
        calls.fail_on['A1'] = 'notok'
        results, console, text, calls = self.run_batch(
            [agent_item('A1'), agent_item('A2')], apply=True, calls=calls
        )
        self.assertEqual(calls.standalone, ['A1'])
        self.assertTrue(console.has_failures())
        self.assertEqual([r.ok for r in results], [False])

    def test_stop_on_failure_false_continues(self):
        calls = Calls()
        calls.fail_on['A1'] = RuntimeError('boom')
        results, console, text, calls = self.run_batch(
            [agent_item('A1'), agent_item('A2')], apply=True, calls=calls, stop_on_failure=False
        )
        self.assertEqual(calls.standalone, ['A1', 'A2'])
        self.assertEqual([r.ok for r in results], [False, True])

    def test_flow_aborted_message_is_recorded_and_masked(self):
        calls = Calls()
        calls.fail_on['A1'] = FlowAborted('agent reset, token %s left behind; manual action' % FAKE_TOKEN)
        results, console, text, calls = self.run_batch([agent_item('A1')], apply=True, calls=calls)
        self.assertIn('manual action', text)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_TOKEN, ' '.join(r.detail for r in results))
        self.assertNotIn(FAKE_TOKEN, ' '.join(row['detail'] for row in results.rows))
        self.assertEqual(results.rows[0]['status'], 'failed')
        self.assertTrue(console.has_failures())

    def test_preflight_exception_is_a_failed_row_and_nothing_runs(self):
        console, stream = make_console()
        ctx = make_ctx(console, apply=True)
        with mock.patch.object(runner, 'run_preflight', side_effect=RuntimeError('api down %s' % FAKE_TOKEN)), \
                mock.patch.object(runner, 'migrate_standalone') as standalone:
            results = runner.run_strategy_d(ctx, [], console)
        standalone.assert_not_called()
        self.assertFalse(results[0].ok)
        self.assertNotIn(FAKE_TOKEN, stream.getvalue())
        self.assertTrue(console.has_failures())

    def test_all_aids_is_passed_to_preflight(self):
        self.run_batch([agent_item('A1')], all_aids=['1', '9'])
        self.assertEqual(self.preflight.call_args[1]['all_aids'], ['1', '9'])

    def test_owner_is_source_is_passed_to_preflight(self):
        self.run_batch([agent_item('A1')], assume_owner_source=True)
        self.assertTrue(self.preflight.call_args[1]['assume_owner_source'])

    def test_owner_flag_is_not_passed_by_default(self):
        self.run_batch([agent_item('A1')])
        self.assertNotIn('assume_owner_source', self.preflight.call_args[1])

    def test_unrelated_type_error_in_preflight_is_not_retried(self):
        console, stream = make_console()
        ctx = make_ctx(console)
        with mock.patch.object(runner, 'run_preflight', side_effect=TypeError('bad value')) as preflight:
            results = runner.run_strategy_d(ctx, [], console, all_aids=['1'])
        self.assertEqual(preflight.call_count, 1)
        self.assertFalse(results[0].ok)

    # ---- M1: ids, state and snapshot of a failed flow
    def test_failed_flow_keeps_ids_state_and_snapshot_from_the_exception(self):
        calls = Calls()
        exc = FlowAborted('delete not confirmed')
        exc.new_agent_id = '4711'
        exc.new_cluster_id = '4712'
        exc.snapshot = '/tmp/snap-a1.json'
        exc.state = 'reset sent; new agent online; old agent still present'
        calls.fail_on['A1'] = exc
        results, console, text, calls = self.run_batch([agent_item('A1')], apply=True, calls=calls)
        failed = results[0]
        self.assertFalse(failed.ok)
        self.assertEqual(failed.new_agent_id, '4711')
        self.assertEqual(failed.new_cluster_id, '4712')
        self.assertEqual(failed.snapshot, '/tmp/snap-a1.json')
        self.assertIn('old agent still present', failed.detail)
        self.assertIn('old agent still present', text)
        self.assertIn('old agent still present', results.rows[0]['detail'])

    def test_failed_flow_without_attributes_uses_the_context_snapshot_path(self):
        calls = Calls()
        calls.fail_on['A1'] = RuntimeError('boom')
        console, stream = make_console()
        ctx = make_ctx(console, apply=True)
        original = calls.migrate_standalone

        def flow(ctx, item):
            ctx.snapshot_path = '/tmp/snap-from-ctx.json'
            return original(ctx, item)

        item = agent_item('A1')
        with mock.patch.object(runner, 'run_preflight', return_value=[item]), \
                mock.patch.object(runner, 'migrate_standalone', side_effect=flow):
            results = runner.run_strategy_d(ctx, [item.inventory], console)
        self.assertEqual(results[0].snapshot, '/tmp/snap-from-ctx.json')
        self.assertIsNone(results[0].new_agent_id)

    def test_snapshot_path_of_an_earlier_device_is_not_reused(self):
        console, stream = make_console()
        ctx = make_ctx(console, apply=True)
        seen = []

        def flow(ctx, item):
            seen.append(ctx.snapshot_path)
            if item.name == 'A1':
                ctx.snapshot_path = '/tmp/snap-a1.json'
                return FlowResult(item.name, True, 'ok', snapshot='/tmp/snap-a1.json')
            raise RuntimeError('early failure')

        items = [agent_item('A1'), agent_item('A2')]
        with mock.patch.object(runner, 'run_preflight', return_value=items), \
                mock.patch.object(runner, 'migrate_standalone', side_effect=flow):
            results = runner.run_strategy_d(ctx, [i.inventory for i in items], console)
        self.assertEqual(seen, [None, None])
        self.assertIsNone(results[1].snapshot)

    def test_preflight_items_are_kept_on_the_result_list(self):
        items = [agent_item('A1')]
        results, console, text, calls = self.run_batch(items)
        self.assertEqual(results.preflight_items, items)

    # ---- M3: Ctrl-C and SystemExit
    def test_keyboard_interrupt_records_row_prints_recap_and_reraises_with_results(self):
        calls = Calls()
        calls.fail_on['A2'] = KeyboardInterrupt()
        items = [agent_item('A1'), agent_item('A2'), agent_item('A3'), agent_item('OFF1', status='excluded')]
        console, stream = make_console()
        ctx = make_ctx(console, apply=True)
        with mock.patch.object(runner, 'run_preflight', return_value=items), \
                mock.patch.object(runner, 'migrate_standalone', side_effect=calls.migrate_standalone):
            with self.assertRaises(KeyboardInterrupt) as caught:
                runner.run_strategy_d(ctx, [i.inventory for i in items], console)
        results = caught.exception.strategy_d_results
        statuses = dict((row['agent'], row['status']) for row in results.rows)
        self.assertEqual(statuses['A1'], 'ok')
        self.assertEqual(statuses['A2'], 'failed')
        self.assertEqual(statuses['A3'], 'skipped')
        self.assertIn('interrupted', [r for r in results.rows if r['agent'] == 'A2'][0]['detail'])
        self.assertEqual(calls.standalone, ['A1', 'A2'])
        self.assertEqual([r.ok for r in results], [True, False])
        self.assertIn('interrupted', results[1].detail)
        self.assertIn('PLAY RECAP', stream.getvalue())
        self.assertTrue(console.has_failures())

    def test_system_exit_is_recorded_and_reraised(self):
        calls = Calls()
        calls.fail_on['A1'] = SystemExit(3)
        console, stream = make_console()
        ctx = make_ctx(console, apply=True)
        item = agent_item('A1')
        with mock.patch.object(runner, 'run_preflight', return_value=[item]), \
                mock.patch.object(runner, 'migrate_standalone', side_effect=calls.migrate_standalone):
            with self.assertRaises(SystemExit) as caught:
                runner.run_strategy_d(ctx, [item.inventory], console)
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(caught.exception.strategy_d_results.rows[0]['status'], 'failed')
        self.assertIn('PLAY RECAP', stream.getvalue())

    def test_interrupt_keeps_state_attribute_of_the_exception(self):
        calls = Calls()
        interrupt = KeyboardInterrupt()
        interrupt.state = 'reset sent'
        calls.fail_on['A1'] = interrupt
        console, stream = make_console()
        item = agent_item('A1')
        with mock.patch.object(runner, 'run_preflight', return_value=[item]), \
                mock.patch.object(runner, 'migrate_standalone', side_effect=calls.migrate_standalone):
            with self.assertRaises(KeyboardInterrupt) as caught:
                runner.run_strategy_d(make_ctx(console, apply=True), [item.inventory], console)
        self.assertIn('reset sent', caught.exception.strategy_d_results[0].detail)


if __name__ == '__main__':
    unittest.main()
