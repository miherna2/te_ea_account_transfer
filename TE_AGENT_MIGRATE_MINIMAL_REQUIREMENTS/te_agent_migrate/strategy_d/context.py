"""Shared context for Strategy D flows."""
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from te_agent_migrate.models import StepStatus

WRITE_HTTP_METHODS = ('POST', 'PUT', 'DELETE', 'PATCH')

# Methods of the API client that send a write. Any other name that starts with one of the
# prefixes below is treated as a write too (default deny for new write helpers).
WRITE_API_METHODS = frozenset((
    'delete_agent', 'update_agent_name', 'assign_tests', 'set_agent_account_groups', 'cluster_assign',
    'cluster_unassign', 'assign_tag', 'set_test_enabled', 'set_test_enabled_minimal', 'update_test', 'create_test', 'create_tag',
    'create_alert_rule', 'delete_test',
))
WRITE_API_PREFIXES = ('create_', 'delete_', 'update_', 'assign_', 'unassign_', 'set_', 'cluster_', 'put_',
                      'post_', 'patch_', 'remove_', 'add_')

SNAPSHOT_SCHEMA = 1
SNAPSHOT_NAME_MAX = 80
_UNSAFE_NAME = re.compile(r'[^A-Za-z0-9._-]')
_MEMBER_KEYS = ('memberId', 'agentId', 'name', 'agentName', 'ipAddresses', 'ipAddress', 'state', 'agentState')
_TAG_KEYS = ('aid', 'tagId', 'key', 'value')


class StepOutcome(object):
    """What ``FlowContext.step`` prints when the body ends without an error."""

    def __init__(self, detail='', changed=False):
        self.detail = detail
        self.status = StepStatus.CHANGED if changed else StepStatus.OK


class ReadOnlyApi(object):
    """Wrap an API client so that a dry run can read but never write.

    Reads are delegated. Every write method raises ``RuntimeError('write call in dry run: <method>')``
    when it is called. ``request`` raises for POST, PUT, DELETE and PATCH.
    """

    def __init__(self, api):
        self._api = api

    @property
    def wrapped(self):
        return self._api

    def request(self, method, *args, **kwargs):
        if str(method).upper() in WRITE_HTTP_METHODS:
            raise RuntimeError('write call in dry run: request %s' % str(method).upper())
        return self._api.request(method, *args, **kwargs)

    def __getattr__(self, name):
        if name.startswith('__') or name == '_api':
            raise AttributeError(name)
        if name in WRITE_API_METHODS or name.startswith(WRITE_API_PREFIXES):
            def blocked(*args, **kwargs):
                raise RuntimeError('write call in dry run: %s' % name)
            blocked.__name__ = name
            return blocked
        return getattr(self._api, name)


def safe_file_part(text):
    """Letters, digits, dot, dash and underscore only. Never empty, never starts with a dot."""
    cleaned = _UNSAFE_NAME.sub('_', str(text or '')).lstrip('.')[:SNAPSHOT_NAME_MAX]
    return cleaned or 'item'


def _jsonable_enabled(value):
    result = {}
    for aid, tests in (value or {}).items():
        result[str(aid)] = dict((str(t), (None if flag is None else bool(flag)))
                                for t, flag in (tests or {}).items())
    return result


def _pick(mapping, keys):
    return dict((k, mapping[k]) for k in keys if isinstance(mapping, dict) and k in mapping)


class FlowContext(object):
    """Everything a Strategy D flow needs. Build one per run.

    api                  ThousandEyesApiClient (wrapped by ReadOnlyApi when ``apply`` is False)
    ui_factory           callable(host_or_ip) -> context manager yielding TevaUiClient
    console              MigrationConsole
    source_aid           source account group id (str)
    target_aid           target account group id (str)
    target_account_token target account group token (secret, never print, never write to a file)
    apply                False = dry run (GET calls only, no UI)
    timeout_seconds      wait limit for each polling step
    poll_seconds         pause between polls
    sleep                injectable for tests
    snapshot_dir         directory for the JSON snapshot written before the first destructive call
                         (None = no file is written; an info line says so)
    snapshot_path        path of the last snapshot file written (None until written)
    auto_reenable_tests  cluster flow only. False (default) = the flow never calls ``set_test_enabled``; it prints a
                         manual task with the test ids that were enabled before and are disabled now.
                         True = re-enable with the full-payload PUT (``set_test_enabled``).
                         'minimal' = re-enable with ``set_test_enabled_minimal`` (PUT of ``{"enabled": true}`` only;
                         experimental, lab check 7). Any other value is read as True or False by truth value.
    """

    def __init__(self, api, ui_factory, console, source_aid, target_aid,
                 target_account_token='', apply=False, timeout_seconds=600,
                 poll_seconds=10, sleep=time.sleep, snapshot_dir=None, auto_reenable_tests=False):
        self.apply = bool(apply)
        self.api = api if self.apply or isinstance(api, ReadOnlyApi) else ReadOnlyApi(api)
        self.ui_factory = ui_factory
        self.console = console
        self.source_aid = str(source_aid)
        self.target_aid = str(target_aid)
        self.target_account_token = target_account_token
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self.snapshot_dir = snapshot_dir
        self.snapshot_path = None
        if isinstance(auto_reenable_tests, str):
            self.auto_reenable_tests = 'minimal' if auto_reenable_tests.strip().lower() == 'minimal' else (
                auto_reenable_tests.strip().lower() in ('1', 'true', 'yes', 'on'))
        else:
            self.auto_reenable_tests = bool(auto_reenable_tests)

    @contextmanager
    def step(self, host, name, detail='', changed=False):
        """Run one real step. Prints ok (or changed when ``changed`` is True) on success, failed on any error
        (also Ctrl-C), then re-raises.

        Use ``changed=True`` for a step that sends a write (apply mode) and leave it False for a read-only check.
        Yields a ``StepOutcome``; the body may change ``outcome.detail`` or ``outcome.status`` (for example to
        ``StepStatus.SKIPPED``) before the result line is printed.
        """
        outcome = StepOutcome(detail, changed)
        try:
            yield outcome
        except GeneratorExit:
            raise
        except BaseException as exc:
            self.console.status(StepStatus.FAILED, host, name, str(exc) or exc.__class__.__name__)
            raise
        self.console.status(outcome.status, host, name, outcome.detail)

    def changed(self, host, name, detail=''):
        self.console.status(StepStatus.CHANGED, host, name, detail)

    def planned(self, host, name, detail=''):
        """Dry run: print what apply mode would do, as ``changed: [host] => would: ...``."""
        self.console.status(StepStatus.CHANGED, host, name, 'would: ' + detail if detail else 'would run')

    def skipped(self, host, name, detail=''):
        self.console.status(StepStatus.SKIPPED, host, name, detail)

    # ------------------------------------------------------------------ snapshot on disk
    def snapshot_data(self, item, extra=None):
        """JSON-able dict of what the flow needs for a manual recovery. Whitelist only: no token, no raw API data."""
        inventory = getattr(item, 'inventory', None)
        record = getattr(item, 'record', None)
        data = {
            'schema': SNAPSHOT_SCHEMA,
            'created_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source_aid': self.source_aid,
            'target_aid': self.target_aid,
            'name': str(getattr(item, 'name', '')),
            'inventory': {
                'hostname': getattr(inventory, 'hostname', None),
                'ip_address': getattr(inventory, 'ip_address', None),
                'kind': getattr(inventory, 'kind', None),
                'agent_id': getattr(inventory, 'agent_id', None),
            },
            'record': None if record is None else {
                'agent_id': getattr(record, 'agent_id', None),
                'name': getattr(record, 'name', None),
                'hostname': getattr(record, 'hostname', None),
                'ip_addresses': list(getattr(record, 'ip_addresses', None) or []),
                'state': getattr(record, 'state', None),
                'agent_type': getattr(record, 'agent_type', None),
                'aid': getattr(record, 'aid', None),
            },
            'ui_ip': getattr(item, 'ui_ip', '') or '',
            'owner_aid': getattr(item, 'owner_aid', ''),
            'share_aids': list(getattr(item, 'share_aids', None) or []),
            'tests_by_aid': dict((str(k), list(v)) for k, v in (getattr(item, 'tests_by_aid', None) or {}).items()),
            'test_enabled': _jsonable_enabled(getattr(item, 'test_enabled', None)),
            'tags': [_pick(t, _TAG_KEYS) for t in (getattr(item, 'tags', None) or [])],
            'members': [_pick(m, _MEMBER_KEYS) for m in (getattr(item, 'members', None) or [])],
            'warnings': [str(w) for w in (getattr(item, 'warnings', None) or [])],
        }
        if extra:
            data['extra'] = extra
        return data

    def write_snapshot(self, item, host, extra=None):
        """Write the snapshot file before the first destructive call. Returns the path, or None without a directory.

        The directory gets mode 0o700 when it is created, the file 0o600. The target token is never
        written. Raises OSError when the file cannot be written (the caller must stop before any change).
        """
        self.snapshot_path = None
        if not self.snapshot_dir:
            self.console.info(host, 'no snapshot directory set; no snapshot file is written')
            return None
        directory = str(self.snapshot_dir)
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
            os.chmod(directory, 0o700)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        base = '%s_%s' % (stamp, safe_file_part(getattr(item, 'name', '')))
        text = json.dumps(self.snapshot_data(item, extra), indent=2, sort_keys=True, default=str)
        token = self.target_account_token
        if token:
            text = text.replace(str(token), '<redacted>')
        counter = 0
        while True:
            suffix = '' if counter == 0 else '_%d' % counter
            path = os.path.join(directory, base + suffix + '.json')
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                counter += 1
                continue
            break
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text + '\n')
        os.chmod(path, 0o600)
        self.snapshot_path = path
        self.console.info(host, 'snapshot file written: %s' % path)
        return path


class FlowResult(object):
    """Outcome of one agent or cluster flow. ``snapshot`` is the snapshot file path (None when none was written)."""

    def __init__(self, name, ok, detail='', new_agent_id=None, new_cluster_id=None, warnings=None, snapshot=None):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.new_agent_id = new_agent_id
        self.new_cluster_id = new_cluster_id
        self.warnings = list(warnings or [])
        self.snapshot = snapshot


class FlowAborted(Exception):
    """A flow stopped on purpose before or after a risky step. State is described in the message.

    The flows attach ``new_agent_id``, ``new_cluster_id``, ``snapshot`` and ``state`` (text of the state line)
    to every exception that leaves them, so the runner can store them (read with ``getattr``). The class
    attributes below are the defaults.
    """
    new_agent_id = None
    new_cluster_id = None
    snapshot = None
    state = None


def attach_stop_info(exc, new_agent_id=None, new_cluster_id=None, snapshot=None, state=None):
    """Set ``new_agent_id``, ``new_cluster_id``, ``snapshot`` and ``state`` on an exception the flow re-raises.

    The runner reads them with ``getattr``. Never raises: an exception object that refuses attributes is left alone.
    """
    for name, value in (('new_agent_id', new_agent_id), ('new_cluster_id', new_cluster_id),
                        ('snapshot', snapshot), ('state', state)):
        try:
            setattr(exc, name, value)
        except Exception:  # some exception objects do not take attributes; the state line is printed anyway
            pass
    return exc


class PreflightItem(object):
    """One selected device after preflight. Built by preflight.py, read by standalone.py and cluster.py.

    inventory      InventoryAgent (kind 'agent' or 'cluster')
    record         AgentRecord from the owner group (None when not found)
    status         'ready', 'blocked' or 'excluded' (excluded = offline)
    reasons        list of str, why blocked or excluded
    owner_aid      account group that owns the agent now (str)
    share_aids     every aid that sees the agent now, owner included, sorted
    tests_by_aid   dict aid -> sorted list of test id (str) assigned to the agent in that group
    tags           list of dict {'aid','tagId','key','value'} the agent has in each group (may be empty)
    members        for clusters: list of dict {'memberId','name','ipAddresses','state'}

    Optional attributes set by preflight (read with getattr and a default):
    ui_ip          inventory IP for the agent UI (standalone)
    test_enabled   {aid: {test_id: bool or None}}
    warnings       list of str
    """

    def __init__(self, inventory, record=None, status='ready', reasons=None, owner_aid='',
                 share_aids=None, tests_by_aid=None, tags=None, members=None):
        self.inventory = inventory
        self.record = record
        self.status = status
        self.reasons = list(reasons or [])
        self.owner_aid = str(owner_aid)
        self.share_aids = sorted(str(a) for a in (share_aids or []))
        self.tests_by_aid = dict((str(k), sorted(str(t) for t in v)) for k, v in (tests_by_aid or {}).items())
        self.tags = list(tags or [])
        self.members = list(members or [])

    @property
    def name(self):
        return self.inventory.hostname

    @property
    def ready(self):
        return self.status == 'ready'
