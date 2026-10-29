"""Strategy D batch runner: preflight, then one device at a time.

Rules (see MIGRATION_PLAN.md sections 13 and 14):

* Dry run and apply print the same play, tasks and step names.
* Any blocker stops the run before the first change (apply and dry run).
  A dry run still prints the plan of the ready devices.
* Devices run one at a time, in inventory order. No parallelism.
* Offline (excluded) devices are never touched. They are ``skipped`` rows.
* The first failure stops the batch (``stop_on_failure``), because the
  device may be in an unknown state.
* Ctrl-C and ``SystemExit`` (any ``BaseException``) during a flow: the device gets a failed
  row ``interrupted``, the devices not started get ``skipped`` rows, the recap is printed, then the
  exception is re-raised with the ``ResultList`` attached as ``strategy_d_results`` so the caller can
  still write its summary and report.
* A flow that raises can attach ``new_agent_id``, ``new_cluster_id``, ``snapshot`` and ``state`` to the
  exception (``FlowAborted`` does). They are copied to the failed ``FlowResult``.
"""

from te_agent_migrate.models import StepStatus
from te_agent_migrate.output import mask_secrets
from te_agent_migrate.strategy_d.cluster import migrate_cluster
from te_agent_migrate.strategy_d.context import FlowAborted, FlowResult
from te_agent_migrate.strategy_d.preflight import has_blockers, print_preflight, run_preflight, summarize
from te_agent_migrate.strategy_d.standalone import migrate_standalone

PLAY_TITLE = 'Strategy D: agent migration'
TASK_FLOW = 'strategy D : flow result'
TASK_BATCH = 'strategy D : batch'

ROW_OK = 'ok'
ROW_FAILED = 'failed'
ROW_SKIPPED = 'skipped'


class ResultList(list):
    """List of FlowResult that also carries the recap rows and the blocker flag."""

    def __init__(self, *args):
        list.__init__(self, *args)
        self.rows = []
        self.blocked = False
        self.preflight_items = []


def _row(agent, status, detail):
    return {'agent': agent, 'status': status, 'detail': mask_secrets(detail)}


def _kind(item):
    return 'cluster' if item.inventory.is_cluster else 'agent'


def _reasons(item):
    return '; '.join(str(r) for r in item.reasons)


def _call_preflight(ctx, inventory_agents, all_aids, assume_owner_source=False):
    kwargs = {}
    if all_aids is not None:
        kwargs['all_aids'] = all_aids
    if assume_owner_source:
        kwargs['assume_owner_source'] = True
    return run_preflight(ctx=ctx, inventory_agents=inventory_agents, **kwargs)


def _fail(console, name, message):
    """Print one failed line for a device and return the masked message."""
    text = mask_secrets(message)
    console.status(StepStatus.FAILED, name, TASK_FLOW, text)
    return text


def _with_state(message, exc):
    """Add the ``state`` text of the exception (when it has one and the message lacks it)."""
    state = getattr(exc, 'state', None)
    if state and str(state) not in message:
        message = '%s [state: %s]' % (message, state)
    return message


def _failed_result(ctx, name, text, exc):
    """FlowResult for a flow that raised. Takes ids and snapshot from the exception if present."""
    snapshot = getattr(exc, 'snapshot', None) or getattr(ctx, 'snapshot_path', None)
    return FlowResult(
        name, False, text,
        new_agent_id=getattr(exc, 'new_agent_id', None),
        new_cluster_id=getattr(exc, 'new_cluster_id', None),
        snapshot=snapshot,
    )


def run_strategy_d(ctx, inventory_agents, console, all_aids=None, stop_on_failure=True,
                   assume_owner_source=False):
    """Run Strategy D for the selected devices. Returns a list of FlowResult."""
    results = ResultList()
    rows = results.rows
    mode = 'apply' if ctx.apply else 'dry-run'
    console.play(PLAY_TITLE, mode, 'D')

    try:
        items = _call_preflight(ctx, inventory_agents, all_aids, assume_owner_source)
    except Exception as exc:
        text = _fail(console, 'preflight', 'preflight failed, nothing was changed: %s' % exc)
        results.append(FlowResult('preflight', False, text))
        rows.append(_row('preflight', ROW_FAILED, text))
        console.recap(rows)
        return results

    results.preflight_items = list(items)
    print_preflight(ctx, items)
    counts = summarize(items)
    console.info(
        'preflight',
        'ready=%d blocked=%d excluded=%d'
        % (counts.get('ready', 0), counts.get('blocked', 0), counts.get('excluded', 0)),
    )

    blockers = bool(has_blockers(items))
    results.blocked = blockers
    if blockers:
        if ctx.apply:
            console.info('run', 'blocking issues found; no device is changed')
        else:
            console.info('run', 'blocking issues found; dry run prints the plan of the ready devices only')

    stopped_by = None
    interrupted = None
    for index, item in enumerate(items):
        name = item.name
        if item.status == 'excluded':
            rows.append(_row(name, ROW_SKIPPED, 'excluded (offline), not touched: %s' % _reasons(item)))
            continue
        if item.status == 'blocked':
            text = mask_secrets('blocked: %s' % _reasons(item))
            results.append(FlowResult(name, False, text))
            rows.append(_row(name, ROW_FAILED, text))
            continue
        if not item.ready:
            rows.append(_row(name, ROW_SKIPPED, 'status %s, not touched' % item.status))
            continue
        if blockers and ctx.apply:
            rows.append(_row(name, ROW_SKIPPED, 'not started: blocking issues in the selection'))
            continue
        if stopped_by is not None:
            rows.append(_row(name, ROW_SKIPPED, 'not started: batch stopped after the failure of %s' % stopped_by))
            continue

        # a snapshot path left by an earlier device must not be reported for this one
        try:
            ctx.snapshot_path = None
        except AttributeError:
            pass
        try:
            if item.inventory.is_cluster:
                result = migrate_cluster(ctx=ctx, item=item)
            else:
                result = migrate_standalone(ctx=ctx, item=item)
        except Exception as exc:
            if isinstance(exc, FlowAborted):
                text = _fail(console, name, _with_state('aborted: %s' % exc, exc))
            else:
                text = _fail(console, name, _with_state('%s: %s' % (type(exc).__name__, exc), exc))
            failed = _failed_result(ctx, name, text, exc)
            results.append(failed)
            rows.append(_row(name, ROW_FAILED, failed.detail))
            if stop_on_failure:
                stopped_by = name
            continue
        except BaseException as exc:
            # Ctrl-C, SystemExit: the device may be half way through. Record it, then re-raise.
            text = _fail(console, name, _with_state(
                'interrupted (%s); the state of this device may be unknown' % type(exc).__name__, exc))
            failed = _failed_result(ctx, name, text, exc)
            results.append(failed)
            rows.append(_row(name, ROW_FAILED, failed.detail))
            interrupted = exc
            for later in items[index + 1:]:
                rows.append(_row(later.name, ROW_SKIPPED, 'not started: run interrupted'))
            break

        if getattr(result, 'snapshot', None) is None and getattr(ctx, 'snapshot_path', None):
            result.snapshot = ctx.snapshot_path
        results.append(result)
        if result.ok:
            detail = result.detail or _kind(item)
            if result.warnings:
                detail = '%s (%d warning(s))' % (detail, len(result.warnings))
            rows.append(_row(name, ROW_OK, detail))
        else:
            text = _fail(console, name, result.detail or 'flow reported failure')
            rows.append(_row(name, ROW_FAILED, text))
            if stop_on_failure:
                stopped_by = name

    if interrupted is not None:
        console.info('run', 'run interrupted; the device above may be in an unknown state')
        console.recap(rows)
        try:
            interrupted.strategy_d_results = results
        except (AttributeError, TypeError):
            pass
        raise interrupted
    if stopped_by is not None:
        console.info('run', 'batch stopped after the failure of %s; the device may be in an unknown state' % stopped_by)
    console.recap(rows)
    return results
