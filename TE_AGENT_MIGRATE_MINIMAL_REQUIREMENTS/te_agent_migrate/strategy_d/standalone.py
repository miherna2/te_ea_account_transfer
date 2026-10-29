"""Strategy D: standalone Enterprise Agent flow.

Tests stay in the source account group and keep their IDs. The agent is reset,
registered in the target group, shared back to every group that saw it, and
the original tests are assigned to the new agent. The old agent is deleted
only after the new agent is online, shared, and the tests are verified.

Dry run (``ctx.apply`` False) sends no write call and opens no agent UI. It does
run the GET-only checks of apply (clean target group), so both modes give the
same verdict.
"""
from contextlib import contextmanager

from te_agent_migrate.api_client import AmbiguousAgentMatch, ThousandEyesApiClient
from te_agent_migrate.errors import AuthenticationError, UiError, VisibilityTimeout
from te_agent_migrate.models import StepStatus
from te_agent_migrate.strategy_d.context import FlowAborted, FlowResult, attach_stop_info
from te_agent_migrate.ui_client import boolean_from_snapshot

STEP_PREFLIGHT = 'preflight_snapshot'
STEP_UI_AUTH = 'ui_authenticate'
STEP_UI_SNAPSHOT = 'ui_snapshot_before'
STEP_UI_RESET = 'ui_reset'
STEP_UI_REACHABLE = 'ui_reachable_after_reset'
STEP_UI_AUTH_AFTER = 'ui_authenticate_after_reset'
STEP_UI_TOKEN = 'ui_set_target_token'
STEP_TARGET_ONLINE = 'api_target_online'
STEP_SHARE_BACK = 'api_share_back'
STEP_ASSIGN_TESTS = 'api_assign_tests'
STEP_DELETE_OLD = 'api_delete_old'
STEP_RENAME = 'api_rename'
STEP_RESTORE_TAGS = 'api_restore_tags'
STEP_MANUAL_RULES = 'manual_notification_rules'

NEW_AGENT_PLACEHOLDER = '<new-agent-id>'
MANUAL_RULES_TEXT = 'restore agent notification rules by hand in the UI; the API is read-only'

STATE_UNTOUCHED = 'nothing changed'
STATE_RESET = 'agent was reset; no token set; manual action needed'
STATE_TOKEN = 'agent was reset and the target token was submitted; old agent is still present; no test was changed'
STATE_ONLINE = 'new agent is online in the target group; old agent is still present; no test was changed'
STATE_SHARED = 'new agent is online and shared; old agent is still present; tests may be partly assigned'
STATE_VERIFIED = 'new agent is online, shared, tests verified; old agent is still present'
STATE_DELETE_SENT = ('delete sent, not confirmed; the old agent may or may not be deleted; '
                     'new agent is online, shared, tests verified')
STATE_DELETED = 'old agent deleted; new agent is online and serves the tests; name or tags may be incomplete'


# --------------------------------------------------------------------------- helpers
def _aid_key(aid):
    text = str(aid)
    return (0, int(text), text) if text.isdigit() else (1, 0, text)


def _unique(values):
    result = []
    for value in values:
        text = str(value)
        if text not in result:
            result.append(text)
    return result


def _first_ip(item):
    record = getattr(item, 'record', None)
    ips = list(getattr(record, 'ip_addresses', None) or [])
    return str(ips[0]) if ips else ''


def _agent_ip(item):
    """Address of the agent UI: inventory IP set by preflight (``ui_ip``), else the first IP of the record."""
    return str(getattr(item, 'ui_ip', '') or '').strip() or _first_ip(item)


def _device_ips(item):
    record = getattr(item, 'record', None)
    return _unique([_agent_ip(item)] + [str(i) for i in (getattr(record, 'ip_addresses', None) or [])])


def _name_candidates(item):
    """Names the device may register under: inventory name, old agent name, old hostname."""
    record = getattr(item, 'record', None)
    names = [item.name, getattr(record, 'name', None), getattr(record, 'hostname', None)]
    return _unique(n for n in names if n)


def _test_aids(item):
    """aids that hold at least one test for the agent, in stable order."""
    return sorted((str(aid) for aid, tests in item.tests_by_aid.items() if tests), key=_aid_key)


def _desired_groups(item, target_aid):
    """Full share list: target first, then every group that saw the agent or holds tests."""
    wanted = [str(target_aid)] if target_aid not in (None, '') else []
    wanted.extend(sorted((str(a) for a in item.share_aids), key=_aid_key))
    wanted.extend(_test_aids(item))
    return _unique(wanted)


def _format_aids(aids):
    return '[%s]' % ','.join(str(a) for a in aids)


def _tags_text(tags):
    parts = []
    for tag in tags:
        label = str(tag.get('key', tag.get('tagId', '?')))
        if tag.get('value') not in (None, ''):
            label += '=%s' % tag.get('value')
        parts.append('%s (aid %s)' % (label, tag.get('aid', '?')))
    return ', '.join(parts)


def _snapshot_detail(item):
    record = getattr(item, 'record', None)
    old_id = getattr(record, 'agent_id', '?')
    tests_total = sum(len(v) for v in item.tests_by_aid.values())
    return 'old agent %s; owner aid %s; shared with aids %s; %d tests in %d groups; %d tags' % (
        old_id, item.owner_aid or '?', _format_aids(item.share_aids), tests_total,
        len(_test_aids(item)), len(item.tags))


def _assign_text(item):
    parts = []
    for aid in _test_aids(item):
        parts.append('%d tests in aid %s' % (len(item.tests_by_aid[aid]), aid))
    return ', '.join(parts)


def plan_standalone(item, target_aid=None):
    """Return the ordered (step, detail) list of the standalone flow.

    Pure function: no context, no API, no UI. Dry run prints it as planned
    steps. ``target_aid`` is optional and only makes the details concrete.
    """
    ip = _agent_ip(item) or '<no-ip>'
    record = getattr(item, 'record', None)
    old_id = getattr(record, 'agent_id', '?')
    owner = item.owner_aid or '?'
    target_text = str(target_aid) if target_aid not in (None, '') else '<target>'
    wanted = _desired_groups(item, target_aid)
    extra = [a for a in wanted if a != str(target_aid)]
    plan = [(STEP_PREFLIGHT, _snapshot_detail(item))]
    plan.append((STEP_UI_AUTH, 'log in to the agent UI at %s' % ip))
    plan.append((STEP_UI_SNAPSHOT, 'read agent UI settings before the reset (browserbot, crash reports)'))
    plan.append((STEP_UI_RESET, 'reset agent %s (old agent id %s gets a new identity)' % (ip, old_id)))
    plan.append((STEP_UI_REACHABLE, 'wait until the agent UI at %s answers again' % ip))
    plan.append((STEP_UI_AUTH_AFTER, 'log in to the agent UI again after the reset'))
    plan.append((STEP_UI_TOKEN, 'submit the token of target aid %s (value hidden)' % target_text))
    plan.append((STEP_TARGET_ONLINE,
                 'wait for a new online agent %s (%s) in aid %s (id not seen before the reset; IP and name must '
                 'match; stop if two qualify or a new online agent appears in source aid %s)'
                 % (item.name, ip, target_text, owner)))
    if extra:
        plan.append((STEP_SHARE_BACK, 'share agent back to aids %s' % _format_aids(wanted)))
    else:
        plan.append((STEP_SHARE_BACK, 'no other group saw the agent; nothing to share back'))
    assign = _assign_text(item)
    if assign:
        plan.append((STEP_ASSIGN_TESTS, 'assign %s, then verify each test list' % assign))
    else:
        plan.append((STEP_ASSIGN_TESTS, 'no tests to assign'))
    plan.append((STEP_DELETE_OLD,
                 'delete old agent %s in aid %s after share and tests are verified; wait until absent'
                 % (old_id, owner)))
    plan.append((STEP_RENAME, 'set the new agent name to %r in aid %s and wait for it' % (item.name, target_text)))
    if item.tags:
        plan.append((STEP_RESTORE_TAGS, 'restore %d tags: %s' % (len(item.tags), _tags_text(item.tags))))
    else:
        plan.append((STEP_RESTORE_TAGS, 'no tags to restore'))
    plan.append((STEP_MANUAL_RULES, MANUAL_RULES_TEXT))
    return plan


@contextmanager
def _translate(message):
    """Turn UI and wait errors into FlowAborted with an explicit state message."""
    try:
        yield
    except (UiError, AuthenticationError, VisibilityTimeout) as exc:
        raise FlowAborted('%s: %s' % (message, exc))


def _poll_until(ctx, probe):
    """Call probe() -> (done, value) until done or ctx.timeout_seconds passed. No real clock."""
    pause = ctx.poll_seconds if ctx.poll_seconds and ctx.poll_seconds > 0 else 1
    waited = 0
    while True:
        done, value = probe()
        if done:
            return True, value
        if waited >= ctx.timeout_seconds:
            return False, value
        ctx.sleep(pause)
        waited += pause


def _identity_matches(agent, ip, name):
    folded = name.casefold()
    return (ip in agent.ip_addresses
            or (agent.hostname or '').casefold() == folded
            or agent.name.casefold() == folded)


def _source_aids(ctx, item):
    return _unique([item.owner_aid or ctx.source_aid, ctx.source_aid])


def _validate(ctx, item):
    """Return the first IP. Raise FlowAborted when the item cannot run."""
    if getattr(item, 'status', None) != 'ready':
        reasons = '; '.join(getattr(item, 'reasons', []) or [])
        raise FlowAborted('device %s is not ready (status=%s)%s' % (
            item.name, getattr(item, 'status', None), ': ' + reasons if reasons else ''))
    kind = getattr(item.inventory, 'kind', None)
    if kind != 'agent':
        raise FlowAborted('device %s is kind %r; the standalone flow handles kind agent only' % (item.name, kind))
    if getattr(item, 'record', None) is None or not str(item.record.agent_id).strip():
        raise FlowAborted('device %s has no agent record; run preflight again' % item.name)
    ip = _agent_ip(item)
    if not ip:
        raise FlowAborted('device %s has no IP address; the agent UI cannot be reached' % item.name)
    if ctx.apply and not ctx.target_account_token:
        raise FlowAborted('no target account group token set; apply cannot register the agent')
    if str(ctx.target_aid) in ('', 'None'):
        raise FlowAborted('no target account group id set')
    return ip


# --------------------------------------------------------------------------- source / target reads
def _prior_source_ids(ctx, item, ip, old_id):
    ids = set([old_id])
    for aid in _source_aids(ctx, item):
        for agent in ctx.api.list_agents(aid):
            if _identity_matches(agent, ip, item.name):
                ids.add(agent.agent_id)
    return ids


def _require_clean_target(ctx, item, ip, old_id):
    """GET only. Abort when the target already has an online agent for the device. Returns the target listing."""
    listing = list(ctx.api.list_agents(ctx.target_aid))
    existing = [a for a in listing
                if a.online and a.agent_id != old_id and _identity_matches(a, ip, item.name)]
    if existing:
        raise FlowAborted('target aid %s already has an online agent for %s (id %s); the reset was not sent. '
                          'Resolve the duplicate first' % (ctx.target_aid, item.name, ', '.join(a.agent_id for a in existing)))
    return listing


def _require_no_new_source_identity(ctx, item, ip, prior_ids):
    """Abort when the device registered in the source group again. Same rule as the strict wait: a new online
    record needs the device IP AND the device name (exact, or ``name-xxxx``). An IP or a name alone is not enough."""
    names = _name_candidates(item)
    ips = _unique([ip] + _device_ips(item))
    for aid in _source_aids(ctx, item):
        unexpected = ThousandEyesApiClient.match_new_agents(ctx.api.list_agents(aid), names, ips, prior_ids)
        if unexpected:
            raise FlowAborted(
                'a new online agent appeared in source aid %s (agent id %s) after the target token was submitted; '
                'the token went to the wrong group. Nothing was deleted or assigned; manual action needed'
                % (aid, ', '.join(a.agent_id for a in unexpected)))


def _wait_target_online(ctx, item, ip, old_id, prior_ids, target_known):
    """Wait for the new identity. Strict: id not seen in the target before the reset, IP and name match."""
    host = item.name
    known = set(str(i) for i in target_known) | set([old_id])
    names = _name_candidates(item)
    ips = _device_ips(item)

    def on_poll(attempt, remaining):
        ctx.console.info(host, 'target API poll %s; %.0fs remaining' % (attempt, remaining))

    def not_visible():
        _require_no_new_source_identity(ctx, item, ip, prior_ids)

    waited = 0
    pause = ctx.poll_seconds if ctx.poll_seconds and ctx.poll_seconds > 0 else 1
    while True:
        try:
            agent = ctx.api.wait_for_agent(
                ctx.target_aid, hostname=item.name, ip_address=ip,
                timeout_seconds=ctx.timeout_seconds, poll_interval_seconds=ctx.poll_seconds,
                on_poll=on_poll, on_not_visible=not_visible,
                known_ids=known, alt_names=names, alt_ips=ips)
        except AmbiguousAgentMatch as exc:
            raise FlowAborted('more than one new online agent in target aid %s matches %s (ids %s); the new '
                              'agent cannot be told apart. Nothing was shared, assigned or deleted; manual '
                              'action needed' % (ctx.target_aid, item.name, ', '.join(exc.agent_ids) or '?'))
        except VisibilityTimeout as exc:
            seen = [a for a in ctx.api.list_agents(ctx.target_aid)
                    if a.online and a.agent_id in known and _identity_matches(a, ip, item.name)]
            if seen:
                raise FlowAborted('target aid %s returns only known agent id %s for %s; no new identity appeared'
                                  % (ctx.target_aid, ', '.join(a.agent_id for a in seen), item.name))
            raise FlowAborted('new agent did not appear online in target aid %s: %s' % (ctx.target_aid, exc))
        if agent.agent_id not in known:
            not_visible()
            return agent
        # A known identity (the old agent, or one that was already there) is still returned. Keep waiting.
        if waited >= ctx.timeout_seconds:
            raise FlowAborted('target aid %s still returns only the old agent id %s; no new identity appeared'
                              % (ctx.target_aid, old_id))
        not_visible()
        ctx.sleep(pause)
        waited += pause


# --------------------------------------------------------------------------- steps
def _share_back(ctx, item, new_id):
    wanted = _desired_groups(item, ctx.target_aid)
    if not wanted:
        raise FlowAborted('refusing to send an empty account group list')
    if len(wanted) == 1:
        ctx.skipped(item.name, STEP_SHARE_BACK, 'no other group saw the agent; nothing to share back')
        return
    # The lab must prove whether PUT replaces or adds: always send the FULL list, never an empty one.
    with ctx.step(item.name, STEP_SHARE_BACK, 'shared agent %s with aids %s' % (new_id, _format_aids(wanted)),
                  changed=True):
        ctx.api.set_agent_account_groups(ctx.target_aid, new_id, wanted)

        def probe():
            record = ctx.api.get_agent(ctx.target_aid, new_id)
            have = set(str(a) for a in record.shared_aids) | set([str(ctx.target_aid)])
            return (not [a for a in wanted if a not in have]), have

        done, have = _poll_until(ctx, probe)
        if not done:
            missing = [a for a in wanted if a not in have]
            raise FlowAborted('agent %s is not shared with aids %s after the PUT; old agent was NOT deleted'
                              % (new_id, _format_aids(missing)))


def _assign_tests(ctx, item, new_id):
    aids = _test_aids(item)
    if not aids:
        ctx.skipped(item.name, STEP_ASSIGN_TESTS, 'no tests to assign')
        return
    detail = 'assigned %s to agent %s and verified' % (_assign_text(item), new_id)
    with ctx.step(item.name, STEP_ASSIGN_TESTS, detail, changed=True):
        for aid in aids:
            ctx.api.assign_tests(aid, new_id, item.tests_by_aid[aid])
        for aid in aids:
            needed = set(item.tests_by_aid[aid])

            def probe(aid=aid, needed=needed):
                have = set()
                for record in ctx.api.list_agents_with_clusters(aid):
                    if record.agent_id == new_id:
                        have = set(str(t) for t in record.test_ids)
                        break
                return needed <= have, have

            done, have = _poll_until(ctx, probe)
            if not done:
                missing = sorted(needed - have)
                raise FlowAborted('aid %s: %d of %d tests are not assigned to agent %s (%s); old agent was NOT deleted'
                                  % (aid, len(missing), len(needed), new_id, ','.join(missing[:10])))


def _delete_type(ctx, owner, record, old_id):
    """Real type of the old agent for ``delete_agent`` so the cluster guard works. Never a cluster."""
    agent_type = str(getattr(record, 'agent_type', None) or '')
    if not agent_type:
        agent_type = str(ctx.api.get_agent(owner, old_id).agent_type or '')
    if not agent_type:
        raise FlowAborted('the type of old agent %s is unknown; old agent was NOT deleted' % old_id)
    if agent_type.casefold() == 'enterprise-cluster':
        raise FlowAborted('refusing to delete id %s in aid %s: it is a cluster, and deleting a cluster deletes '
                          'its members; old agent was NOT deleted' % (old_id, owner))
    return agent_type


def _delete_old(ctx, item, old_id, new_id, state=None):
    owner = item.owner_aid or ctx.source_aid
    if old_id == new_id:
        ctx.skipped(item.name, STEP_DELETE_OLD, 'old and new agent id are the same (%s); nothing deleted' % old_id)
        return
    with ctx.step(item.name, STEP_DELETE_OLD, 'deleted old agent %s in aid %s' % (old_id, owner), changed=True):
        current = ctx.api.get_agent(ctx.target_aid, new_id)
        if not current.online:
            raise FlowAborted('new agent %s is not online (state %s); old agent was NOT deleted' % (new_id, current.state))
        agent_type = _delete_type(ctx, owner, item.record, old_id)
        if state is not None:
            state[0] = STATE_DELETE_SENT
        ctx.api.delete_agent(owner, old_id, agent_type=agent_type)
        ctx.api.wait_for_agent_absent(owner, agent_id=old_id, timeout_seconds=ctx.timeout_seconds,
                                      poll_interval_seconds=ctx.poll_seconds)


def _rename(ctx, item, new_id):
    with ctx.step(item.name, STEP_RENAME, 'agent %s name is %r' % (new_id, item.name)) as outcome:
        current = ctx.api.get_agent(ctx.target_aid, new_id)
        if current.name != item.name:
            outcome.status = StepStatus.CHANGED  # a write is sent; an equal name is a read-only check (ok)
            ctx.api.update_agent_name(ctx.target_aid, new_id, item.name)
        ctx.api.wait_for_agent_name(ctx.target_aid, agent_id=new_id, expected_name=item.name,
                                    timeout_seconds=ctx.timeout_seconds, poll_interval_seconds=ctx.poll_seconds)


def _restore_tags(ctx, item, new_id, warnings):
    if not item.tags:
        ctx.skipped(item.name, STEP_RESTORE_TAGS, 'no tags to restore')
        return
    done = 0
    for tag in item.tags:
        label = '%s (aid %s)' % (tag.get('key', tag.get('tagId', '?')), tag.get('aid', '?'))
        try:
            ctx.api.assign_tag(tag['aid'], tag['tagId'], [{'id': new_id, 'type': 'v-agent'}])
            done += 1
        except Exception as exc:  # non-fatal by design; recorded as warning, never silent
            text = 'tag restore failed for %s: %s' % (label, exc)
            warnings.append(text)
            ctx.console.info(item.name, 'warning: ' + text)
    ctx.console.status(StepStatus.CHANGED if done else StepStatus.OK, item.name, STEP_RESTORE_TAGS,
                       'restored %d of %d tags on agent %s' % (done, len(item.tags), new_id))


# --------------------------------------------------------------------------- flow
class _State(object):
    """What the flow knows while it runs: the state text (``state[0]``) and the new agent id once it is known."""

    def __init__(self, text=STATE_UNTOUCHED):
        self.text = text
        self.new_id = None

    def __getitem__(self, index):
        if index != 0:
            raise IndexError(index)
        return self.text

    def __setitem__(self, index, value):
        if index != 0:
            raise IndexError(index)
        self.text = value


def _snapshot_text(ctx):
    if ctx.snapshot_path:
        return ctx.snapshot_path
    if ctx.snapshot_dir:
        return 'not written yet (directory %s)' % ctx.snapshot_dir
    return 'none (no snapshot directory set)'


def _stop_line(ctx, state, exc, item=None):
    text = 'stopped; state: %s' % state[0]
    if item is not None:
        old_id = getattr(getattr(item, 'record', None), 'agent_id', '?')
        text += ' | old agent id: %s | new agent id: %s | final share set: %s' % (
            old_id, getattr(state, 'new_id', None) or 'not known yet', _format_aids(_desired_groups(item, ctx.target_aid)))
    text += ' | snapshot file: %s' % _snapshot_text(ctx)
    if not isinstance(exc, Exception):
        text += ' | interrupted by %s' % exc.__class__.__name__
    return text


def migrate_standalone(ctx, item):
    """Run the Strategy D standalone flow for one ready agent. Returns FlowResult.

    A flow that stops re-raises its exception with ``new_agent_id``, ``new_cluster_id`` (always None here),
    ``snapshot`` and ``state`` set as attributes (the runner reads them with ``getattr``).
    """
    host = item.name
    state = _State()
    ctx.snapshot_path = None
    try:
        return _run(ctx, item, host, state)
    except BaseException as exc:  # includes KeyboardInterrupt and SystemExit: print the state, then re-raise
        ctx.console.info(host, _stop_line(ctx, state, exc, item))
        attach_stop_info(exc, new_agent_id=state.new_id, snapshot=ctx.snapshot_path, state=state.text)
        raise


def _dry_run_snapshot_note(ctx, host):
    if ctx.snapshot_dir:
        ctx.console.info(host, 'dry run: a snapshot file would be written to %s before the reset' % ctx.snapshot_dir)
    else:
        ctx.console.info(host, 'dry run: no snapshot directory set; no snapshot file would be written')


def _write_snapshot(ctx, item, host, extra):
    try:
        return ctx.write_snapshot(item, host, extra)
    except OSError as exc:
        raise FlowAborted('snapshot file could not be written (%s); nothing was changed' % exc)


def _run(ctx, item, host, state):
    with ctx.step(host, STEP_PREFLIGHT, _snapshot_detail(item)):
        ip = _validate(ctx, item)
        old_id = str(item.record.agent_id)
        prior_ids = None
        # GET-only guards. Dry run and apply run the same checks.
        target_agents = _require_clean_target(ctx, item, ip, old_id)
        target_known = set(a.agent_id for a in target_agents)
        if ctx.apply:
            prior_ids = _prior_source_ids(ctx, item, ip, old_id)

    if not ctx.apply:
        ctx.console.info(host, 'dry run check: no online agent for %s in target aid %s; %d agents seen there'
                         % (item.name, ctx.target_aid, len(target_agents)))
        _dry_run_snapshot_note(ctx, host)
        for step_name, detail in plan_standalone(item, ctx.target_aid)[1:]:
            if step_name == STEP_MANUAL_RULES:
                ctx.skipped(host, step_name, detail)
            else:
                ctx.planned(host, step_name, detail)
        return FlowResult(host, True, 'dry-run')

    warnings = []
    with ctx.ui_factory(ip) as ui:
        with ctx.step(host, STEP_UI_AUTH, 'logged in to the agent UI at %s' % ip):
            with _translate('agent UI login failed (%s)' % STATE_UNTOUCHED):
                ui.authenticate()
        with ctx.step(host, STEP_UI_SNAPSHOT, 'agent UI settings captured'):
            with _translate('agent UI snapshot failed (%s)' % STATE_UNTOUCHED):
                before = ui.snapshot_state()
            browserbot = boolean_from_snapshot(before, 'browserbot', default=True)
            crash_reports = boolean_from_snapshot(before, 'crash_reports', default=True)
            # The file goes to disk before the first destructive call (the UI reset).
            _write_snapshot(ctx, item, host, {'ui_settings': {'browserbot': browserbot, 'crash_reports': crash_reports}})

        state[0] = 'reset command sent; agent state unknown; no token set; manual action needed'
        with ctx.step(host, STEP_UI_RESET, 'agent %s reset' % ip, changed=True):
            with _translate('agent reset failed'):
                ui.reset_agent()
        state[0] = STATE_RESET

        with ctx.step(host, STEP_UI_REACHABLE, 'agent UI at %s answers' % ip):
            reachable, _ = _poll_until(ctx, lambda: (bool(ui.is_reachable()), None))
            if not reachable:
                raise FlowAborted('agent UI at %s is unreachable after the reset; %s' % (ip, STATE_RESET))

        with ctx.step(host, STEP_UI_AUTH_AFTER, 'logged in again after the reset'):
            def try_login():
                try:
                    ui.authenticate()
                except (UiError, AuthenticationError) as exc:
                    return False, exc
                return True, None
            logged_in, last_error = _poll_until(ctx, try_login)
            if not logged_in:
                raise FlowAborted('login after the reset failed (%s); %s' % (last_error, STATE_RESET))

        with ctx.step(host, STEP_UI_TOKEN, 'target token of aid %s submitted (value hidden)' % ctx.target_aid,
                      changed=True):
            try:
                ui.set_account_group_token(ctx.target_account_token, browserbot=browserbot,
                                           crash_reports=crash_reports)
            except (UiError, AuthenticationError) as exc:
                raise FlowAborted('token submission failed (%s); agent was reset; the token may or may not be set; '
                                  'manual action needed' % exc)
        state[0] = STATE_TOKEN

    with ctx.step(host, STEP_TARGET_ONLINE, 'new agent online in aid %s' % ctx.target_aid):
        new_agent = _wait_target_online(ctx, item, ip, old_id, prior_ids, target_known)
    new_id = new_agent.agent_id
    state.new_id = new_id
    state[0] = STATE_ONLINE

    _share_back(ctx, item, new_id)
    state[0] = STATE_SHARED
    _assign_tests(ctx, item, new_id)
    state[0] = STATE_VERIFIED

    _delete_old(ctx, item, old_id, new_id, state)
    state[0] = STATE_DELETED

    _rename(ctx, item, new_id)
    _restore_tags(ctx, item, new_id, warnings)
    ctx.skipped(host, STEP_MANUAL_RULES, MANUAL_RULES_TEXT)
    state[0] = 'done'
    return FlowResult(host, True, 'migrated: old agent %s -> new agent %s' % (old_id, new_id),
                      new_agent_id=new_id, warnings=warnings, snapshot=ctx.snapshot_path)
