"""Bounded-parallel migration workflow and safety gates."""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.errors import ApiError, HardStop, HumanDecisionRequired, MigrationError, VisibilityTimeout
from te_agent_migrate.models import AccountGroup, AgentRecord, InventoryAgent, LocalSnapshot, Mode, RunOptions, StepEvent, StepStatus, TestRecord, record_to_dict as asdict
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.output import MigrationConsole
from te_agent_migrate.reporting import RunReport
from te_agent_migrate.strategies import API_TEST_TYPES, MONITOR_BASED_TEST_TYPES, TestStrategyExecutor, action_dict
from te_agent_migrate.ui_client import TevaUiClient, boolean_from_snapshot

class AgentSkipped(MigrationError):
    """Operator explicitly chose to leave the failed agent for later review."""

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def recursive_diff(before, after, path=''):
    """Return deterministic leaf-level additions, removals, and changes."""
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            child_path = f'{path}.{key}' if path else str(key)
            if key not in before:
                changes.append({'path': child_path, 'before': None, 'after': after[key]})
            elif key not in after:
                changes.append({'path': child_path, 'before': before[key], 'after': None})
            else:
                changes.extend(recursive_diff(before[key], after[key], child_path))
        return changes
    if before != after:
        return [{'path': path, 'before': before, 'after': after}]
    return []

def test_agent_ids(test):
    """Normalize expanded test agent identifiers for completeness checks."""
    values = set()
    for agent in test.agents:
        value = agent.get('agentId', agent.get('id')) if isinstance(agent, dict) else agent
        if value is not None and str(value):
            values.add(str(value))
    return values

def test_monitor_ids(test):
    """Normalize expanded BGP monitor identifiers for portability checks."""
    values = set()
    raw_monitors = test.raw.get('monitors', [])
    if not isinstance(raw_monitors, list):
        return values
    for monitor in raw_monitors:
        value = monitor.get('monitorId', monitor.get('id')) if isinstance(monitor, dict) else monitor
        if value is not None and str(value):
            values.add(str(value))
    return values

def monitor_catalog_ids(monitors):
    """Normalize monitor IDs returned by GET /v7/monitors."""
    values = set()
    for monitor in monitors:
        value = monitor.get('monitorId', monitor.get('id'))
        if value is not None and str(value):
            values.add(str(value))
    return values

class MigrationRunner:

    def __init__(self, *, api, ui_factory, report, prompter, console, options, source_group, target_group, target_account_token):
        self.api = api
        self.ui_factory = ui_factory
        self.report = report
        self.prompter = prompter
        self.console = console
        self.options = options
        self.source_group = source_group
        self.target_group = target_group
        self.target_account_token = target_account_token
        self.strategy = TestStrategyExecutor(api)
        self._destructive_confirmed = False
        self._source_test_cleanup_candidates = {}
        self._source_test_known_agent_ids = {}
        self._source_test_preserved_agent_ids = {}
        self._selected_source_agent_ids = set()
        self._completed_source_agent_ids = set()
        self._strategy_c_source_tests = {}
        self._strategy_c_unassigned_tests = []
        self._strategy_c_monitor_tests = []
        self._completed_target_agents = {}
        self._recap = []
        self._confirmation_lock = RLock()
        self._prompt_lock = RLock()
        self._state_lock = RLock()
        self._test_lock = RLock()
        self._destructive_correlation_ids = set()

    @contextmanager
    def step(self, correlation_id, agent, name, *, start_detail='in progress'):
        self.console.progress(agent or 'global', name, start_detail)
        started = time.monotonic()
        try:
            yield
        except Exception as exc:
            duration = time.monotonic() - started
            event = StepEvent(utc_now(), correlation_id, agent, name, StepStatus.FAILED, duration, str(exc))
            self.report.record_step(event)
            self.console.status(StepStatus.FAILED, agent or 'global', name, str(exc))
            raise
        duration = time.monotonic() - started
        event = StepEvent(utc_now(), correlation_id, agent, name, StepStatus.OK, duration)
        self.report.record_step(event)
        self.console.status(StepStatus.OK, agent or 'global', name)

    def _event(self, correlation_id, agent, step, status, detail, data=None):
        self.report.record_step(StepEvent(utc_now(), correlation_id, agent, step, status, 0.0, detail, data or {}))
        self.console.status(status, agent or 'global', step, detail)

    def run(self, inventory):
        self.report.metadata.update({'source_aid': self.source_group.aid, 'source_account_group': self.source_group.name, 'target_aid': self.target_group.aid, 'target_account_group': self.target_group.name, 'timeout_seconds': self.options.timeout_seconds, 'parallelism': self.options.parallelism, 'strategy': self.options.strategy.value, 'stale_policy': self.options.stale_policy.value if self.options.strategy.value == 'C' else 'not-applicable', 'source_test_policy': self.options.stale_policy.value if self.options.strategy.value == 'C' else 'not-applicable', 'strategy_c_create_missing_tags': self.options.create_missing_tags if self.options.strategy.value == 'C' else 'not-applicable', 'strategy_c_create_missing_alert_rules': self.options.create_missing_alerts if self.options.strategy.value == 'C' else 'not-applicable'})
        self._prepare_strategy_c_source_test_inventory(inventory)
        batches = [inventory[index:index + self.options.parallelism] for index in range(0, len(inventory), self.options.parallelism)]
        for batch_index, batch in enumerate(batches, start=1):
            self.console.info('run', f'starting batch {batch_index}/{len(batches)}: ' + ', '.join((agent.hostname for agent in batch)))
            outcomes = self._run_batch(batch)
            for inventory_agent, _correlation_id, error in outcomes:
                if error is None:
                    self._recap.append({'agent': inventory_agent.hostname, 'status': 'ok', 'detail': self.options.mode.value})
                    continue
                self.report.record_error('agent', str(error), inventory_agent.hostname)
                if isinstance(error, HardStop):
                    self._recap.append({'agent': inventory_agent.hostname, 'status': 'failed', 'detail': str(error)})
                elif isinstance(error, AgentSkipped):
                    self._recap.append({'agent': inventory_agent.hostname, 'status': 'skipped', 'detail': str(error)})
                else:
                    self._recap.append({'agent': inventory_agent.hostname, 'status': 'failed', 'detail': str(error)})
            self.report.write_incremental()
            hard_stop = next((error for _, _, error in outcomes if isinstance(error, HardStop)), None)
            if hard_stop is not None:
                raise hard_stop
            for inventory_agent, correlation_id, error in outcomes:
                if error is None or isinstance(error, AgentSkipped):
                    continue
                if not self._pause_after_failure(inventory_agent, error, correlation_id):
                    self.report.write_incremental()
                    raise HumanDecisionRequired(f'Operator aborted after failure on {inventory_agent.hostname}') from error
            if batch_index < len(batches) and (not self.prompter.continue_after_batch(batch_index, [agent.hostname for agent in batch])):
                self._event(str(uuid.uuid4()), None, 'batch_pause', StepStatus.PAUSED, f'Operator declined to continue after batch {batch_index}')
                self.report.write_incremental()
                raise HumanDecisionRequired('Operator paused the run between batches')
        self._handle_strategy_c_unassigned_tests(inventory)
        self._handle_strategy_c_monitor_tests(inventory)
        if self.options.mode is Mode.APPLY:
            self._cleanup_source_tests()
        self.report.write_final()
        self.console.recap(self._recap)
        return self.report

    def _run_batch(self, batch):
        jobs = [(agent, str(uuid.uuid4())) for agent in batch]
        if len(jobs) == 1:
            agent, correlation_id = jobs[0]
            return [(agent, correlation_id, self._run_agent_captured(agent, correlation_id))]
        with ThreadPoolExecutor(max_workers=self.options.parallelism, thread_name_prefix='te-agent-migrate') as executor:
            futures = [executor.submit(self._run_agent_captured, agent, correlation_id) for agent, correlation_id in jobs]
        # zip(strict=True) was added in Python 3.10. The two lists are created
        # from the same jobs sequence, so ordinary zip is safe and supports 3.6.
        return [(agent, correlation_id, future.result()) for (agent, correlation_id), future in zip(jobs, futures)]

    def _run_agent_captured(self, inventory_agent, correlation_id):
        try:
            self._run_agent(inventory_agent, correlation_id)
        except Exception as exc:
            return exc
        return None

    def _hard_stop_scope(self):
        if self.options.parallelism == 1:
            return 'remaining agents were not touched'
        return 'agents in later batches were not touched; peers already running in the active batch may have completed'

    def _mark_destructive_started(self, correlation_id):
        with self._state_lock:
            self._destructive_correlation_ids.add(correlation_id)

    def _retry_is_safe(self, correlation_id):
        if self.options.mode is Mode.DRY_RUN:
            return True
        with self._state_lock:
            return correlation_id not in self._destructive_correlation_ids

    @staticmethod
    def _matching_source_identities(agents, inventory_agent):
        """Return every source identity for one appliance without broad name collisions."""
        ip_matches = [agent for agent in agents if inventory_agent.ip_address in agent.ip_addresses]
        if ip_matches:
            return ip_matches
        expected_name = inventory_agent.hostname.casefold()
        return [agent for agent in agents if (agent.hostname or '').casefold() == expected_name or agent.name.casefold() == expected_name]

    def _pause_after_failure(self, inventory_agent, error, correlation_id):
        detail = str(error)
        retry_safe = self._retry_is_safe(correlation_id)
        allow_recheck = not isinstance(error, ApiError) and retry_safe
        while True:
            decision = self.prompter.failure_decision(inventory_agent.hostname, detail, allow_recheck=allow_recheck)
            self._event(correlation_id, inventory_agent.hostname, 'failure_decision', StepStatus.PAUSED, decision)
            if decision == 'abort':
                return False
            if decision == 'skip':
                return True
            ui_reachable = False
            try:
                with self.ui_factory(inventory_agent) as ui:
                    ui_reachable = ui.is_reachable()
                target = self.api.match_agent(self.api.list_agents(self.target_group.aid), hostname=inventory_agent.hostname, ip_address=inventory_agent.ip_address, online_only=True)
                target_detail = 'not-required-in-dry-run' if self.options.mode is Mode.DRY_RUN else str(target is not None)
                retry_safe = self._retry_is_safe(correlation_id)
                detail = f'read-only recheck: ui_reachable={ui_reachable}, target_online={target_detail}, retry_safe={retry_safe}'
            except MigrationError as exc:
                detail = f'read-only recheck failed: {exc}'
            self._event(correlation_id, inventory_agent.hostname, 'read_only_recheck', StepStatus.OK if ui_reachable else StepStatus.PAUSED, detail)
            if ui_reachable and retry_safe:
                mode_name = self.options.mode.value.lower()
                retry_step = 'dry_run_retry' if self.options.mode is Mode.DRY_RUN else 'apply_retry'
                try:
                    with self.step(correlation_id, inventory_agent.hostname, retry_step, start_detail='retrying agent from pre-destructive boundary after successful recheck'):
                        self._run_agent(inventory_agent, correlation_id)
                except Exception as exc:
                    detail = f'{mode_name} retry failed: {exc}'
                    retry_safe = self._retry_is_safe(correlation_id)
                    allow_recheck = not isinstance(exc, ApiError) and retry_safe
                    continue
                for row in reversed(self._recap):
                    if row.get('agent') == inventory_agent.hostname and row.get('status') == 'failed':
                        row['status'] = 'ok'
                        row['detail'] = f'{mode_name} retry succeeded after read-only recheck'
                        break
                return True

    def _run_agent(self, inventory_agent, correlation_id):
        with self.step(correlation_id, inventory_agent.hostname, 'api_prevalidation'):
            source_agents = self.api.list_agents(self.source_group.aid)
            target_agents = self.api.list_agents(self.target_group.aid)
            source_identities = self._matching_source_identities(source_agents, inventory_agent)
            source = self.api.match_agent(source_identities, hostname=inventory_agent.hostname, ip_address=inventory_agent.ip_address)
            target = self.api.match_agent(target_agents, hostname=inventory_agent.hostname, ip_address=inventory_agent.ip_address, online_only=True)
            self._require_target_name_available(target_agents, inventory_agent, current_target=target)
            if source is None and target is None:
                raise HumanDecisionRequired(f'No source or target API identity matched {inventory_agent.ip_address}')
            tests = self._source_tests(source_identities)
            self._record_agent('pre', inventory_agent, source)
            self._record_agent('existing-target', inventory_agent, target)
            for test in tests:
                self.report.add_test(self._test_row('pre', test, source))
        if len(source_identities) > 1:
            self._event(correlation_id, inventory_agent.hostname, 'duplicate_source_identity_recovery', StepStatus.OK, f'Detected {len(source_identities)} matching source identities; test associations will be merged and every source identity will be removed only after the destination identity is online', {'source_agent_ids': [agent.agent_id for agent in source_identities], 'test_bearing_source_agent_ids': [agent.agent_id for agent in source_identities if agent.test_ids]})
        if source is not None and source.online and (target is not None):
            self._event(correlation_id, inventory_agent.hostname, 'dual_online_detected', StepStatus.PAUSED, 'No reset will run; the existing target will be validated before source deletion')
        with self.ui_factory(inventory_agent) as ui:
            with self.step(correlation_id, inventory_agent.hostname, 'ui_authentication'):
                ui.authenticate()
            with self.step(correlation_id, inventory_agent.hostname, 'local_snapshot_pre'):
                before = ui.snapshot_state()
            if self.options.mode is Mode.DRY_RUN:
                self._dry_run_agent(inventory_agent, source, target, tests, before, correlation_id)
                return
            if target is None:
                if source is None or not source.online:
                    raise HumanDecisionRequired('No online source identity is available for the destructive migration')
                self._ensure_destructive_confirmation()
                browserbot = boolean_from_snapshot(before, 'browserbot', default=True)
                crash_reports = boolean_from_snapshot(before, 'crash_reports', default=True)
                self._mark_destructive_started(correlation_id)
                with self.step(correlation_id, inventory_agent.hostname, 'ui_reset'):
                    ui.reset_agent()
                if not ui.is_reachable():
                    raise HardStop(f'{inventory_agent.hostname} became unreachable after reset; {self._hard_stop_scope()}')
                self._event(correlation_id, inventory_agent.hostname, 'post_reset_reachability', StepStatus.OK, 'HTTPS UI reachable')
                with self.step(correlation_id, inventory_agent.hostname, 'ui_post_reset_authentication'):
                    ui.authenticate()
                with self.step(correlation_id, inventory_agent.hostname, 'ui_target_token'):
                    ui.set_account_group_token(self.target_account_token, browserbot=browserbot, crash_reports=crash_reports)
                target = self._wait_for_target(inventory_agent, correlation_id, prior_source_agent_ids={agent.agent_id for agent in source_identities})
            else:
                self._event(correlation_id, inventory_agent.hostname, 'identity_migration', StepStatus.SKIPPED, 'Healthy target identity already exists; destructive UI actions skipped')
            with self.step(correlation_id, inventory_agent.hostname, 'local_snapshot_post'):
                after = ui.snapshot_state()
            self._record_diff(inventory_agent, before, after)
            self._mark_destructive_started(correlation_id)
            self._handle_tests(tests, target, correlation_id, inventory_agent, source_agent_id=source.agent_id if source is not None else None)
            if source_identities:
                target = self._finalize_agent_migration(source_identities, target, inventory_agent, correlation_id)
                with self._test_lock:
                    self._completed_source_agent_ids.update((agent.agent_id for agent in source_identities))
                assert source is not None
                self._verify_identity_metadata(source, target, correlation_id, inventory_agent)
            else:
                target = self._enforce_exact_target_name(target, inventory_agent, correlation_id)
            with self._test_lock:
                self._completed_target_agents[inventory_agent.hostname] = (inventory_agent, target)
            self._record_agent('post', inventory_agent, target)

    def _wait_for_target(self, inventory_agent, correlation_id, *, prior_source_agent_ids):
        try:
            with self.step(correlation_id, inventory_agent.hostname, 'api_target_visibility', start_detail=f'polling destination API for up to {self.options.timeout_seconds} seconds'):
                return self.api.wait_for_agent(self.target_group.aid, hostname=inventory_agent.hostname, ip_address=inventory_agent.ip_address, timeout_seconds=self.options.timeout_seconds, on_poll=lambda attempt, remaining: self.console.info(inventory_agent.hostname, f'destination API poll {attempt}; {remaining:.0f}s remaining'), on_not_visible=lambda: self._require_no_new_online_source_identity(inventory_agent, prior_source_agent_ids))
        except VisibilityTimeout as exc:
            while True:
                with self._prompt_lock:
                    decision = self.prompter.failure_decision(inventory_agent.hostname, str(exc))
                self._event(correlation_id, inventory_agent.hostname, 'visibility_timeout_decision', StepStatus.PAUSED, decision)
                if decision == 'recheck':
                    with self.step(correlation_id, inventory_agent.hostname, 'api_target_visibility_recheck'):
                        target = self.api.match_agent(self.api.list_agents(self.target_group.aid), hostname=inventory_agent.hostname, ip_address=inventory_agent.ip_address, online_only=True)
                    if target is not None:
                        return target
                    self._event(correlation_id, inventory_agent.hostname, 'api_target_visibility_recheck', StepStatus.PAUSED, 'One-shot read-only recheck still found no online target identity')
                    continue
                if decision == 'skip':
                    raise AgentSkipped('Destination visibility remained indeterminate; agent skipped without remediation') from exc
                raise HumanDecisionRequired('Destination visibility remained indeterminate; no rollback or reset was attempted') from exc

    def _require_no_new_online_source_identity(self, inventory_agent, prior_source_agent_ids):
        source_identities = self._matching_source_identities(self.api.list_agents(self.source_group.aid), inventory_agent)
        unexpected = [agent for agent in source_identities if agent.online and agent.agent_id not in prior_source_agent_ids]
        if not unexpected:
            return
        unexpected_ids = ', '.join((agent.agent_id for agent in unexpected))
        raise HardStop(f'A new online source identity appeared after the destination token was submitted (source aid={self.source_group.aid}, agent ids={unexpected_ids}). The configured target account-group token registered the appliance in the wrong account group; the destination wait was stopped without deleting source agents or tests')

    def _require_target_name_available(self, target_agents, inventory_agent, *, current_target):
        conflicts = [agent for agent in target_agents if agent.name.casefold() == inventory_agent.hostname.casefold() and (current_target is None or agent.agent_id != current_target.agent_id)]
        if not conflicts:
            return
        summary = ', '.join((f'id={agent.agent_id} state={agent.state}' for agent in conflicts))
        raise HardStop(f'Destination display name {inventory_agent.hostname!r} is already owned by another target identity ({summary}); no reset was attempted')

    def _finalize_agent_migration(self, source_identities, target, inventory_agent, correlation_id):
        self._ensure_destructive_confirmation()
        ordered_identities = sorted({agent.agent_id: agent for agent in source_identities}.values(), key=lambda agent: (int(agent.agent_id) if agent.agent_id.isdigit() else -1, agent.agent_id))
        for source in ordered_identities:
            try:
                with self.step(correlation_id, inventory_agent.hostname, 'api_source_delete', start_detail=f'deleting source identity {source.agent_id}'):
                    self.api.delete_agent(self.source_group.aid, source.agent_id)
                with self.step(correlation_id, inventory_agent.hostname, 'api_source_absent', start_detail=f'verifying source identity {source.agent_id} is absent for up to {self.options.timeout_seconds} seconds'):
                    self.api.wait_for_agent_absent(self.source_group.aid, agent_id=source.agent_id, timeout_seconds=self.options.timeout_seconds, on_poll=lambda attempt, remaining: self.console.info(inventory_agent.hostname, f'source-absence poll {attempt}; {remaining:.0f}s remaining'))
            except MigrationError as exc:
                raise HardStop(f'Source identity {source.agent_id} could not be deleted and confirmed absent after destination identity {target.agent_id} became online; {self._hard_stop_scope()}') from exc
            self.report.add_agent({'phase': 'source-removed', 'inventory_hostname': inventory_agent.hostname, 'ip_address': inventory_agent.ip_address, 'agent_id': source.agent_id, 'name': source.name, 'hostname': source.hostname, 'aid': source.aid, 'account_group': source.account_group_name, 'state': 'removed', 'source_agent_removed': True, 'source_absent_confirmed': True})
        target = self._enforce_exact_target_name(target, inventory_agent, correlation_id)
        self._event(correlation_id, inventory_agent.hostname, 'migration_invariants', StepStatus.OK, f'source_identity_count={len(ordered_identities)}, source_agent_removed=True, source_absent_confirmed=True, target_online=True, target_name_exact=True')
        return target

    def _enforce_exact_target_name(self, target, inventory_agent, correlation_id):
        try:
            if target.name != inventory_agent.hostname:
                with self.step(correlation_id, inventory_agent.hostname, 'api_target_exact_name', start_detail=f'renaming destination identity {target.agent_id} from {target.name!r} to {inventory_agent.hostname!r}'):
                    self.api.update_agent_name(self.target_group.aid, target.agent_id, inventory_agent.hostname)
        except MigrationError as exc:
            raise HardStop(f'Destination identity {target.agent_id} could not be renamed exactly to {inventory_agent.hostname!r}; {self._hard_stop_scope()}') from exc
        try:
            with self.step(correlation_id, inventory_agent.hostname, 'api_target_identity', start_detail='verifying target online status and exact target name'):
                target = self.api.wait_for_agent_name(self.target_group.aid, agent_id=target.agent_id, expected_name=inventory_agent.hostname, timeout_seconds=self.options.timeout_seconds, on_poll=lambda attempt, remaining: self.console.info(inventory_agent.hostname, f'exact-name poll {attempt}; {remaining:.0f}s remaining'))
        except MigrationError as exc:
            raise HardStop(f'Destination identity {target.agent_id} did not become online with exact name {inventory_agent.hostname!r}; {self._hard_stop_scope()}') from exc
        return target

    def _dry_run_agent(self, inventory_agent, source, target, tests, before, correlation_id):
        projected = target or AgentRecord(agent_id='<new-agent-id>', name=inventory_agent.hostname, hostname=inventory_agent.hostname, ip_addresses=[inventory_agent.ip_address], state='projected-online', aid=self.target_group.aid)
        self.report.add_diff({'agent': inventory_agent.hostname, 'phase': 'dry-run', 'before': asdict(before), 'projected': {'reset': 'not executed', 'target_token': 'not submitted', 'source_agent_deletion': 'projected after target validation' if source is not None else 'not needed', 'automatic_registration_name': f'{inventory_agent.hostname}-<new-local-id>', 'name_normalization': 'API v7 exact-name update projected', 'new_identity': asdict(projected)}})
        target_tests = self.api.list_tests(self.target_group.aid)
        self._record_dry_run_test_readiness(tests, target_tests, correlation_id, inventory_agent)
        self._handle_tests(tests, projected, correlation_id, inventory_agent, target_tests=target_tests, dry_run=True, source_agent_id=source.agent_id if source is not None else None)
        with self._test_lock:
            self._completed_target_agents[inventory_agent.hostname] = (inventory_agent, projected)
        self._event(correlation_id, inventory_agent.hostname, 'dry_run', StepStatus.OK, 'All UI/API operations were read-only')

    def _record_dry_run_test_readiness(self, source_tests, target_tests, correlation_id, inventory_agent):
        """Audit target inventory and Option C adapter readiness."""
        data = {'target_test_count': len(target_tests), 'source_assigned_test_count': len(source_tests)}
        detail = 'Read-only target test inventory completed'
        if self.options.strategy.value == 'C':
            type_readiness = [{'test_type': test_type, 'adapter_supported': test_type in API_TEST_TYPES} for test_type in sorted({test.test_type for test in source_tests})]
            data['option_c_type_readiness'] = type_readiness
            detail = 'Option C adapter and target-inventory readiness recorded'
        self._event(correlation_id, inventory_agent.hostname, 'dry_run_test_readiness', StepStatus.OK, detail, data)

    def _prepare_strategy_c_source_test_inventory(self, inventory):
        if self.options.strategy.value != 'C':
            return
        correlation_id = str(uuid.uuid4())
        with self.step(correlation_id, None, 'strategy_c_source_test_inventory', start_detail='capturing source tests and selected-agent associations before migration'):
            source_agents = self.api.list_agents(self.source_group.aid, agent_types='CLOUD,ENTERPRISE')
            source_enterprise_agents = [agent for agent in source_agents if agent.agent_type in {None, 'enterprise'}]
            source_identity_groups = [self._matching_source_identities(source_enterprise_agents, item) for item in inventory]
            selected_source_agents = [agent for source_identities in source_identity_groups for agent in source_identities]
            summaries = self.api.list_tests(self.source_group.aid)
        selected_source_agent_ids = {agent.agent_id for agent in selected_source_agents}
        self._selected_source_agent_ids = set(selected_source_agent_ids)
        selected_test_ids = {
            str(test_id)
            for agent in selected_source_agents
            for test_id in agent.test_ids
        }
        scoped_summaries = [test for test in summaries if test.test_id in selected_test_ids]
        scope_name = 'selected-inventory-agents'
        details = [
            self.api.get_test(self.source_group.aid, test.test_type, test.test_id)
            for test in scoped_summaries
        ]
        outside_inventory = {}
        for test in details:
            source_agent_ids = test_agent_ids(test)
            portable_agent_ids = set()
            unknown_agent_ids = sorted(source_agent_ids - selected_source_agent_ids)
            if unknown_agent_ids:
                outside_inventory[test.test_id] = unknown_agent_ids
            key = (test.test_type, test.test_id)
            self._source_test_known_agent_ids[key] = set(source_agent_ids)
            self._source_test_preserved_agent_ids[key] = portable_agent_ids
        self._strategy_c_source_tests = {test.test_id: test for test in details}
        self._strategy_c_monitor_tests = []
        self._strategy_c_unassigned_tests = []
        if self._strategy_c_monitor_tests:
            target_monitor_ids = monitor_catalog_ids(self.api.list_monitors(self.target_group.aid))
            missing_monitors = {test.test_id: sorted(test_monitor_ids(test) - target_monitor_ids) for test in self._strategy_c_monitor_tests if test_monitor_ids(test) - target_monitor_ids}
            if missing_monitors:
                summary = '; '.join((f"test {test_id}: {','.join(monitor_ids)}" for test_id, monitor_ids in sorted(missing_monitors.items())))
                raise HardStop(f'Strategy C cannot safely recreate monitor-based tests because some source monitor IDs are unavailable in the target account group ({summary})')
        all_inventory_agents_matched = all(source_identity_groups)
        if self._strategy_c_unassigned_tests and (not all_inventory_agents_matched):
            raise HardStop('Strategy C found unassigned source tests but not every inventory agent exists in the source account group; assign-all would be ambiguous')
        recreatable_tests = [test for test in details if test.test_type in API_TEST_TYPES]
        if self.options.mode is Mode.APPLY and (self.options.create_missing_tags or self.options.create_missing_alerts):
            self._ensure_destructive_confirmation()
        with self.step(correlation_id, None, 'strategy_c_resource_reconciliation', start_detail='reconciling requested tags and alert rules before any agent move' if self.options.create_missing_tags or self.options.create_missing_alerts else 'tag and alert-rule creation disabled; tests will omit both'):
            resource_summary = self.strategy.prepare_recreate_resources(source_tests=recreatable_tests, source_aid=self.source_group.aid, target_aid=self.target_group.aid, create_missing_tags=self.options.create_missing_tags, create_missing_alerts=self.options.create_missing_alerts, dry_run=self.options.mode is Mode.DRY_RUN)
        self.report.metadata.update({'strategy_c_scope': scope_name, 'strategy_c_total_source_test_count': len(summaries), 'strategy_c_source_test_count': len(details), 'strategy_c_skipped_out_of_scope_test_count': len(summaries) - len(details), 'strategy_c_tests_with_non_migrated_associations': len(outside_inventory), 'strategy_c_unassigned_source_test_count': 0, 'strategy_c_unassigned_test_policy': 'out-of-scope-not-associated-with-inventory', 'strategy_c_monitor_based_source_test_count': 0, 'strategy_c_monitor_based_test_policy': 'out-of-scope-not-associated-with-inventory', 'strategy_c_test_name_policy': 'preserve-exact', 'strategy_c_preserved_cloud_agent_association_count': 0, 'strategy_c_tags_created': resource_summary.tags_created, 'strategy_c_tags_reused': resource_summary.tags_reused, 'strategy_c_tags_projected': resource_summary.tags_projected, 'strategy_c_alert_rules_created': resource_summary.alert_rules_created, 'strategy_c_alert_rules_reused': resource_summary.alert_rules_reused, 'strategy_c_alert_rules_projected': resource_summary.alert_rules_projected})
        self.console.info('strategy_c_resource_reconciliation', f'tags created={resource_summary.tags_created}, reused={resource_summary.tags_reused}, projected={resource_summary.tags_projected}; alert rules created={resource_summary.alert_rules_created}, reused={resource_summary.alert_rules_reused}, projected={resource_summary.alert_rules_projected}')
        association_note = ''
        if outside_inventory:
            association_note = f'; {len(outside_inventory)} selected test(s) also reference non-migrated agents and will remain in source when removal would be unsafe'
        self._event(correlation_id, None, 'strategy_c_source_test_inventory', StepStatus.OK, f'scope={scope_name}; selected {len(details)} of {len(summaries)} source test(s); {len(self._strategy_c_unassigned_tests)} unassigned test(s); {len(self._strategy_c_monitor_tests)} monitor-based test(s){association_note}')

    def _handle_strategy_c_unassigned_tests(self, inventory):
        if self.options.strategy.value != 'C' or not self._strategy_c_unassigned_tests:
            return
        correlation_id = str(uuid.uuid4())
        completed = [self._completed_target_agents[item.hostname] for item in inventory if item.hostname in self._completed_target_agents]
        if len(completed) != len(inventory):
            self._event(correlation_id, None, 'strategy_c_unassigned_tests', StepStatus.SKIPPED, 'Unassigned source tests retained because not every inventory agent completed')
            return
        if self.options.mode is Mode.DRY_RUN:
            target_agent_ids = [target.agent_id for _, target in completed]
            target_agent_names = [item.hostname for item, _ in completed]
            for test in self._strategy_c_unassigned_tests:
                self.report.add_test({'original_test_id': test.test_id, 'original_name': test.name, 'test_type': test.test_type, 'action': 'recreate-and-assign-all', 'status': 'projected', 'target_test_id': None, 'target_test_name': test.name, 'detail': 'Unassigned source test will be enabled and assigned to all migrated agents', 'source_aid': self.source_group.aid, 'target_aid': self.target_group.aid, 'target_agent_ids': target_agent_ids, 'target_agent_names': target_agent_names, 'enabled': True, 'source_tags_preserved': self.options.create_missing_tags, 'source_alert_rules_preserved': self.options.create_missing_alerts})
                self.report.add_stale_entry({'entry_type': 'source-test', 'agent': 'all-migrated-agents', 'source_test_id': test.test_id, 'source_test_name': test.name, 'test_type': test.test_type, 'source_aid': self.source_group.aid, 'policy': self.options.stale_policy.value, 'recreated_in_target': True, 'action': f'projected-{self.options.stale_policy.value}'})
            self._event(correlation_id, None, 'strategy_c_unassigned_tests', StepStatus.CHANGED, f'projected {len(self._strategy_c_unassigned_tests)} unassigned test(s) across {len(completed)} migrated agent(s)')
            return
        for test in self._strategy_c_unassigned_tests:
            for inventory_agent, target in completed:
                self._handle_tests([test], target, correlation_id, inventory_agent, source_agent_id=None)
        self._event(correlation_id, None, 'strategy_c_unassigned_tests', StepStatus.CHANGED, f'migrated {len(self._strategy_c_unassigned_tests)} unassigned test(s) and assigned each to {len(completed)} target agent(s)')

    def _handle_strategy_c_monitor_tests(self, inventory):
        if self.options.strategy.value != 'C' or not self._strategy_c_monitor_tests:
            return
        correlation_id = str(uuid.uuid4())
        completed = [self._completed_target_agents[item.hostname] for item in inventory if item.hostname in self._completed_target_agents]
        if len(completed) != len(inventory):
            self._event(correlation_id, None, 'strategy_c_monitor_tests', StepStatus.SKIPPED, 'Monitor-based source tests retained because not every inventory agent completed')
            return
        context_agent, target_agent = completed[0]
        dry_run = self.options.mode is Mode.DRY_RUN
        target_tests = self.api.list_tests(self.target_group.aid)
        for test in self._strategy_c_monitor_tests:
            with self._test_lock:
                action = self.strategy.apply(strategy=self.options.strategy, source_test=test, source_aid=self.source_group.aid, target_aid=self.target_group.aid, target_agent=target_agent, target_tests=target_tests, dry_run=dry_run, create_missing_tags=self.options.create_missing_tags, create_missing_alerts=self.options.create_missing_alerts)
            row = action_dict(action)
            row.update({'agent': 'monitor-based', 'source_aid': self.source_group.aid, 'target_aid': self.target_group.aid, 'target_agent_id': None, 'enabled': True, 'source_tags_preserved': self.options.create_missing_tags, 'source_alert_rules_preserved': self.options.create_missing_alerts, 'monitor_ids': sorted(test_monitor_ids(test)), 'monitor_count': len(test_monitor_ids(test))})
            if action.status == 'unsupported':
                self.report.add_unsupported_test(row)
            else:
                self.report.add_test(row)
            if not dry_run and action.status in {'changed', 'existing'} and (action.target_test_id is not None):
                with self.step(correlation_id, None, 'api_target_monitor_test_ready', start_detail=f'verifying monitor-based target test {action.target_test_id} is enabled with its exact name, prefix, and monitors'):
                    self.api.wait_for_monitor_test_ready(self.target_group.aid, test_type=action.test_type, test_id=action.target_test_id, expected_name=test.name, expected_prefix=str(test.raw.get('prefix', '')), expected_use_public_bgp=bool(test.raw.get('usePublicBgp', False)), expected_monitor_ids=test_monitor_ids(test), timeout_seconds=self.options.timeout_seconds, on_poll=lambda attempt, remaining: self.console.info('global', f'target-monitor-test poll {attempt}; {remaining:.0f}s remaining'))
            self._record_source_test_policy(test, action.status, context_agent, dry_run, agent_label='monitor-based')
            self._event(correlation_id, None, f'test_{action.action}', StepStatus.SKIPPED if action.status in {'skipped', 'unsupported'} else StepStatus.CHANGED, action.detail)
        self._event(correlation_id, None, 'strategy_c_monitor_tests', StepStatus.CHANGED, f"{('projected' if dry_run else 'migrated')} {len(self._strategy_c_monitor_tests)} monitor-based test(s) exactly once without agent associations")

    def _source_tests(self, source_identities):
        test_ids = list(dict.fromkeys((test_id for source in source_identities for test_id in source.test_ids)))
        if not test_ids:
            return []
        if self.options.strategy.value == 'C' and self._strategy_c_source_tests:
            snapshot_details = []
            for test_id in test_ids:
                test = self._strategy_c_source_tests.get(test_id)
                if test is None:
                    self.report.add_unsupported_test({'test_id': test_id, 'reason': 'Assigned test not visible in Strategy C source snapshot'})
                    continue
                if test.test_type in MONITOR_BASED_TEST_TYPES:
                    continue
                snapshot_details.append(test)
            return snapshot_details
        summaries = {test.test_id: test for test in self.api.list_tests(self.source_group.aid)}
        details = []
        for test_id in test_ids:
            summary = summaries.get(test_id)
            if summary is None:
                self.report.add_unsupported_test({'test_id': test_id, 'reason': 'Assigned test not visible in source inventory'})
                continue
            if self.options.strategy.value == 'C' and summary.test_type in MONITOR_BASED_TEST_TYPES:
                continue
            details.append(self.api.get_test(self.source_group.aid, summary.test_type, summary.test_id))
        return details

    def _handle_tests(self, tests, target, correlation_id, inventory_agent, *, target_tests=None, dry_run=False, source_agent_id=None):
        with self._test_lock:
            self._handle_tests_locked(tests, target, correlation_id, inventory_agent, target_tests=target_tests, dry_run=dry_run, source_agent_id=source_agent_id)

    def _handle_tests_locked(self, tests, target, correlation_id, inventory_agent, *, target_tests=None, dry_run=False, source_agent_id=None):
        if target_tests is None:
            target_tests = self.api.list_tests(self.target_group.aid)
        for test in tests:
            action = self.strategy.apply(strategy=self.options.strategy, source_test=test, source_aid=self.source_group.aid, target_aid=self.target_group.aid, target_agent=target, target_tests=target_tests, dry_run=dry_run, create_missing_tags=self.options.create_missing_tags, create_missing_alerts=self.options.create_missing_alerts, preserved_agent_ids=sorted(self._source_test_preserved_agent_ids.get((test.test_type, test.test_id), set())))
            row = action_dict(action)
            row.update({'agent': inventory_agent.hostname, 'source_aid': self.source_group.aid, 'target_aid': self.target_group.aid, 'target_agent_id': target.agent_id, 'enabled': True if self.options.strategy.value == 'C' and action.status in {'changed', 'existing', 'projected'} else test.enabled, 'source_tags_preserved': self.options.strategy.value != 'C' or self.options.create_missing_tags, 'source_alert_rules_preserved': self.options.strategy.value != 'C' or self.options.create_missing_alerts})
            if action.status == 'unsupported':
                self.report.add_unsupported_test(row)
            else:
                self.report.add_test(row)
            if self.options.strategy.value == 'C':
                if not dry_run and action.status in {'changed', 'existing'} and (action.target_test_id is not None):
                    with self.step(correlation_id, inventory_agent.hostname, 'api_target_test_ready', start_detail=f'verifying target test {action.target_test_id} is enabled and assigned to agent {target.agent_id}'):
                        self.api.wait_for_test_ready(self.target_group.aid, test_type=action.test_type, test_id=action.target_test_id, agent_id=target.agent_id, timeout_seconds=self.options.timeout_seconds, on_poll=lambda attempt, remaining: self.console.info(inventory_agent.hostname, f'target-test poll {attempt}; {remaining:.0f}s remaining'))
                self._record_source_test_policy(test, action.status, inventory_agent, dry_run, source_agent_id=source_agent_id)
            self._event(correlation_id, inventory_agent.hostname, f'test_{action.action}', StepStatus.SKIPPED if action.status in {'skipped', 'unsupported'} else StepStatus.CHANGED, action.detail)

    def _record_source_test_policy(self, test, action_status, inventory_agent, dry_run, *, source_agent_id=None, agent_label=None):
        successfully_recreated = action_status in {'changed', 'existing', 'projected'}
        key = (test.test_type, test.test_id)
        if source_agent_id is not None:
            self._source_test_known_agent_ids.setdefault(key, set()).add(source_agent_id)
        if successfully_recreated and (not dry_run) and (self.options.stale_policy.value == 'remove'):
            self._source_test_cleanup_candidates[key] = test
        policy_detail = {}
        if action_status == 'unsupported':
            action = 'kept-unsupported'
        elif dry_run and self.options.stale_policy.value == 'remove':
            known_agent_ids = self._source_test_known_agent_ids.get(key, set())
            preserved_agent_ids = self._source_test_preserved_agent_ids.get(key, set())
            expected_source_agent_ids = (test_agent_ids(test) | known_agent_ids) - preserved_agent_ids
            non_migrated_agent_ids = sorted(
                expected_source_agent_ids - self._selected_source_agent_ids
            )
            if non_migrated_agent_ids:
                action = 'projected-keep-incomplete-associations'
                policy_detail['missing_source_agent_ids'] = non_migrated_agent_ids
            else:
                action = 'projected-remove'
        elif dry_run:
            action = f'projected-{self.options.stale_policy.value}'
        elif self.options.stale_policy.value == 'remove':
            action = 'deferred-remove'
        else:
            action = 'kept'
        row = {'entry_type': 'source-test', 'agent': agent_label or inventory_agent.hostname, 'source_test_id': test.test_id, 'source_test_name': test.name, 'test_type': test.test_type, 'source_aid': self.source_group.aid, 'policy': self.options.stale_policy.value, 'recreated_in_target': successfully_recreated, 'action': action}
        row.update(policy_detail)
        self.report.add_stale_entry(row)

    def _cleanup_source_tests(self):
        correlation_id = str(uuid.uuid4())
        if self.options.strategy.value != 'C':
            self._event(correlation_id, None, 'source_test_cleanup', StepStatus.SKIPPED, f'Not applicable to Strategy {self.options.strategy.value}')
            return
        if self.options.stale_policy.value == 'keep':
            self._event(correlation_id, None, 'source_test_cleanup', StepStatus.SKIPPED, 'Strategy C keep policy retained source tests')
            return
        if not self._source_test_cleanup_candidates:
            self._event(correlation_id, None, 'source_test_cleanup', StepStatus.SKIPPED, 'No successfully recreated source tests were eligible for deletion')
            return
        for test in self._source_test_cleanup_candidates.values():
            key = (test.test_type, test.test_id)
            known_agent_ids = self._source_test_known_agent_ids.get(key, set())
            preserved_agent_ids = self._source_test_preserved_agent_ids.get(key, set())
            expected_source_agent_ids = (test_agent_ids(test) | known_agent_ids) - preserved_agent_ids
            missing_source_agent_ids = sorted(expected_source_agent_ids - self._completed_source_agent_ids)
            if missing_source_agent_ids:
                self.report.add_stale_entry({'entry_type': 'source-test', 'source_test_id': test.test_id, 'source_test_name': test.name, 'test_type': test.test_type, 'source_aid': self.source_group.aid, 'policy': 'remove', 'action': 'kept-incomplete-associations', 'recreated_in_target': True, 'missing_source_agent_ids': missing_source_agent_ids})
                self._event(correlation_id, None, 'source_test_cleanup', StepStatus.SKIPPED, f"Source test {test.test_id} retained because associated source agents did not complete: {', '.join(missing_source_agent_ids)}")
                continue
            with self.step(correlation_id, None, 'delete_source_test', start_detail=f'deleting recreated source test {test.test_id} ({test.test_type})'):
                self.api.delete_test(self.source_group.aid, test.test_type, test.test_id)
            with self.step(correlation_id, None, 'source_test_absent', start_detail=f'verifying source test {test.test_id} is absent for up to {self.options.timeout_seconds} seconds'):
                self.api.wait_for_test_absent(self.source_group.aid, test_id=test.test_id, timeout_seconds=self.options.timeout_seconds, on_poll=lambda attempt, remaining: self.console.info('global', f'source-test absence poll {attempt}; {remaining:.0f}s remaining'))
            self.report.add_stale_entry({'entry_type': 'source-test', 'source_test_id': test.test_id, 'source_test_name': test.name, 'test_type': test.test_type, 'source_aid': self.source_group.aid, 'policy': 'remove', 'action': 'removed', 'recreated_in_target': True})

    def _ensure_destructive_confirmation(self):
        with self._confirmation_lock:
            if self._destructive_confirmed:
                return
            with self._prompt_lock:
                self.console.destination_banner(self.source_group, self.target_group, self.options.mode, destructive=True)
                if not self.prompter.confirm_destructive():
                    raise HumanDecisionRequired('Operator declined the destructive action gate')
            self._destructive_confirmed = True

    def _record_agent(self, phase, inventory_agent, agent):
        if agent is None:
            return
        self.report.add_agent({'phase': phase, 'inventory_hostname': inventory_agent.hostname, 'ip_address': inventory_agent.ip_address, 'agent_id': agent.agent_id, 'name': agent.name, 'hostname': agent.hostname, 'aid': agent.aid, 'account_group': agent.account_group_name, 'labels': agent.labels, 'state': agent.state, 'test_ids': agent.test_ids})

    def _record_diff(self, inventory_agent, before, after):
        self.report.add_diff({'agent': inventory_agent.hostname, 'before_captured_at': before.captured_at, 'after_captured_at': after.captured_at, 'changes': recursive_diff(before.state, after.state)})

    def _verify_identity_metadata(self, source, target, correlation_id, inventory_agent):
        preserved_name = inventory_agent.hostname == target.name
        preserved_labels = source.labels == target.labels
        detail = f'name_preserved={preserved_name}, labels_preserved={preserved_labels}'
        self._event(correlation_id, inventory_agent.hostname, 'identity_metadata', StepStatus.OK if preserved_name and preserved_labels else StepStatus.CHANGED, detail)

    @staticmethod
    def _test_row(phase, test, source):
        return {'phase': phase, 'test_id': test.test_id, 'name': test.name, 'type': test.test_type, 'owning_aid': test.aid, 'shared_with': test.shared_with, 'bound_agents': test.agents, 'enabled': test.enabled, 'alert_rules': test.alert_rules, 'source_agent_id': None if source is None else source.agent_id}
