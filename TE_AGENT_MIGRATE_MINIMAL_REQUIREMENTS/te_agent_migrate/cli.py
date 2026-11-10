"""Argparse command-line entry point for Python 3.6 through 3.9."""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import ssl
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.autodetect import detect_source, resolve_targets
from te_agent_migrate.errors import ApiError, ConfigurationError, MigrationError
from te_agent_migrate.inventory import capture_inventory, describe_inventory, load_inventory
from te_agent_migrate.models import Mode, StepStatus
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.precheck import check_devices
from te_agent_migrate.resume import find_interrupted, load_snapshots, resume_device
from te_agent_migrate.output import MigrationConsole, mask_secrets
from te_agent_migrate.redaction import redact_value
from te_agent_migrate.reporting import RunReport
from te_agent_migrate.flow.context import FlowAborted, FlowContext, FlowResult, a2a_targets_of, item_target_aid
from te_agent_migrate.flow.runner import run_migration
from te_agent_migrate.flow.standalone import desired_share_aids
from te_agent_migrate.target_tokens import TargetTokens
from te_agent_migrate.flow.preflight import (
    has_blockers as preflight_has_blockers,
    print_preflight,
    run_preflight,
    summarize as preflight_summary,
)
from te_agent_migrate.ui_client import TevaUiClient


DRY_RUN_SCHEMA = 8
UI_USERNAME_ENV = "TE_AGENT_UI_USERNAME"
UI_PASSWORD_ENV = "TE_AGENT_UI_PASSWORD"
DRY_RUN_OK_FILENAME = "dry-run-ok.json"
# Fixed places under the project folder (never relative to the folder the operator stands in).
PROJECT_DIR = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_DIR / "work"
DEFAULT_INVENTORY = WORK_DIR / "input" / "inventory.csv"
DRY_RUN_DIR = WORK_DIR / "dry-run"      # the latest preview only: it is emptied at the start of each preview
APPLY_ROOT = WORK_DIR / "apply"         # one timestamped folder per apply: snapshots and logs are evidence
MIGRATED_FILE = WORK_DIR / "state" / "migrated.json"
PREFIX_MAP_FILE = WORK_DIR / "input" / "prefix_map.csv"   # optional: device name prefix -> destination group
TOKEN_TRIES = 3
UI_LOGIN_TRIES = 3
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130


class PreconditionRefused(ConfigurationError):
    """A safety gate refused to start. Nothing was changed. Exit code 2."""


def _environment_value(name):
    value = os.getenv(name)
    if value is None:
        return None
    if not value:
        raise ConfigurationError("%s is set but empty" % name)
    return value


def _positive_integer(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def build_parser():
    parser = argparse.ArgumentParser(
        prog="te-agent-migrate",
        description=(
            "Move ThousandEyes Enterprise Agents to other account groups. Only the agent moves: "
            "its tests stay in their account group, and the agent is shared back and gets the "
            "same tests. Without --apply you only get a preview: nothing changes."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true",
        help="Move the devices. The tool shows the plan and asks you to confirm before it changes anything.",
    )
    parser.add_argument(
        "--inventory", default=None,
        help=("CSV with the devices: name,ip_address and, optionally, target (destination group id or "
              "name) and type. Default: work/input/inventory.csv. If the file does not exist the tool "
              "asks you to type the devices."),
    )
    parser.add_argument(
        "--timeout", type=_positive_integer, default=None,
        help="Wait limit in seconds for each wait step (optional).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="More output.")
    # Old options. They are still accepted so that old instructions do not fail, but they do nothing now.
    mode.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--owner-is-source", dest="owner_is_source", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repoint-agent-to-agent", dest="repoint_agent_to_agent", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--auto-reenable-tests", dest="auto_reenable_tests", action="store_true",
                        help=argparse.SUPPRESS)
    # Lab and tests only: another folder for the reports, another place for the preview file.
    parser.add_argument("--output", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--confirmed-dry-run-file", dest="confirmed_dry_run_file", default=None,
                        help=argparse.SUPPRESS)
    return parser


def _old_option_notes(args):
    """One line for each old option that was given: it is not needed any more."""
    notes = []
    if getattr(args, "dry_run", False):
        notes.append("--dry-run is not needed: without --apply you always get a preview")
    if getattr(args, "owner_is_source", False):
        notes.append("--owner-is-source is not needed: --apply asks you to confirm the owner")
    if getattr(args, "repoint_agent_to_agent", False):
        notes.append("--repoint-agent-to-agent is not needed: agent-to-agent tests are repointed automatically")
    return notes


def _empty_dir(path):
    """Remove the content of the preview folder. Only the fixed DRY_RUN_DIR is ever emptied."""
    if Path(path) != DRY_RUN_DIR:
        raise ConfigurationError("refusing to empty %s" % path)
    if DRY_RUN_DIR.exists():
        shutil.rmtree(str(DRY_RUN_DIR), ignore_errors=True)


def _apply_stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class _UiCredentials(object):
    """The agent UI login. Calling it returns (user, password); ``replace`` stores a corrected pair."""

    def __init__(self, pair):
        self._pair = pair

    def __call__(self):
        if callable(self._pair):
            self._pair = self._pair()
        return self._pair

    def replace(self, username, password):
        self._pair = (username, password)


def _finish_interrupted(console, prompter, report, api, source, agents, kwargs, snapshot_dir, target_names,
                        apply_mode, migrated_ids):
    """Find migrations of an earlier run that stopped half way and, in apply mode, finish them.

    Return False when the run must stop (a device could not be finished), else True.
    Remember each successful replacement before continuing with the normal migration.
    """
    known_completed = set(migrated_ids)
    try:
        interrupted = find_interrupted(api, source.aid, agents,
                                       load_snapshots(WORK_DIR, snapshot_dir=snapshot_dir), console,
                                       target_aid=kwargs.get('target_aid'), completed_ids=migrated_ids)
    except Exception as exc:
        # Uncertain recovery must not fall through to another destructive reset.
        console.info("resume", "cannot safely check earlier runs (%s); stopping before a new migration"
                     % _mask_exception(exc))
        report.record_decision("Finish earlier runs", "inspection failed")
        return False
    if apply_mode:
        discovered = set(migrated_ids) - known_completed
        _remember_migrated(console, [
            FlowResult("completed recovery (agent %s)" % new_id, True, new_agent_id=new_id)
            for new_id in sorted(discovered)
        ])
    if not interrupted:
        return True
    console.info("resume", "%d migration(s) of an earlier run did not finish:" % len(interrupted))
    for entry in interrupted:
        console.info("resume", "  - %s" % entry.describe())
    if not apply_mode:
        console.info("resume", "Run the same command with --apply to finish them first.")
        return True
    if prompter.required_choice("Finish these %d migration(s) now?" % len(interrupted), ("y", "n")) != "y":
        report.record_decision("Finish earlier runs", "no")
        return True
    report.record_decision("Finish earlier runs", "yes")
    ctx = _build_flow_context(dict(kwargs, apply=True, target_account_token=""), str(snapshot_dir))
    ctx.target_names = dict(target_names)
    console.play("Finish earlier runs", Mode.APPLY)
    for entry in interrupted:
        try:
            result = resume_device(ctx, entry)
        except FlowAborted as exc:
            console.status(StepStatus.FAILED, entry.name, "resume", _mask_exception(exc))
            report.record_decision("Finish earlier runs", "failed for %s" % entry.name)
            return False
        # Keep same-run protection even when the completion state cannot be saved.
        migrated_ids.add(str(result.new_agent_id))
        _remember_migrated(console, [result])
        report.add_agent({"agent": entry.name, "status": "recovered",
                          "new_agent_id": result.new_agent_id, "detail": result.detail})
        console.status(StepStatus.OK, entry.name, "resume", result.detail)
    return True


def _precheck_devices(console, prompter, report, items, get_ui_credentials):
    """Before the first reset: every agent UI answers and accepts the login. Return True when all do.

    A refused login is asked again (3 tries). A UI that does not answer stops the run: retyping does not
    help, and a reset cannot be undone.
    """
    for attempt in range(1, UI_LOGIN_TRIES + 1):
        result = check_devices(items, _make_ip_ui_factory(get_ui_credentials))
        if result.ok:
            console.info("precheck", "%d agent UI(s) answer and accept the login" % result.checked)
            report.record_decision("Pre-reset checks", "ok")
            return True
        if result.unreachable:
            console.info("precheck", "These agent UIs do not answer on port 443 from this computer: %s. "
                                     "Fix the network or take them out of the inventory."
                         % "; ".join(result.unreachable))
        for text in result.other:
            console.info("precheck", "The agent UI answered badly: %s" % text)
        if result.unreachable or result.other:
            break
        console.info("precheck", "The agent UI refused the user name or password on %d device(s): %s"
                     % (len(result.bad_login), "; ".join(result.bad_login[:5])))
        if attempt == UI_LOGIN_TRIES or not hasattr(get_ui_credentials, "replace"):
            break
        console.info("input", "Type the agent UI login again (try %d of %d)" % (attempt + 1, UI_LOGIN_TRIES))
        get_ui_credentials.replace(prompter.text("Agent UI username"), prompter.secret("Agent UI password"))
    report.record_decision("Pre-reset checks", "failed")
    console.info("run", "pre-reset checks failed; nothing was changed")
    return False


def _resolve_agent_ui_inputs(report, prompter, console):
    ui_username = _environment_value(UI_USERNAME_ENV)
    if ui_username is None:
        ui_username = prompter.text("Agent UI username")
    else:
        report.record_decision("Agent UI username", "%s via %s" % (ui_username, UI_USERNAME_ENV))
        console.info("input", "Agent UI username loaded from %s" % UI_USERNAME_ENV)
    ui_password = _environment_value(UI_PASSWORD_ENV)
    if ui_password is None:
        ui_password = prompter.secret("Agent UI password")
    else:
        report.record_decision("Agent UI password", "<environment:%s>" % UI_PASSWORD_ENV)
        console.info("input", "Agent UI password loaded from %s" % UI_PASSWORD_ENV)
    return ui_username, ui_password


def _group_from_env(groups, env_name, label, prompter, exclude_aid=None):
    value = os.getenv(env_name)
    if value:
        group = next((item for item in groups if item.aid == value), None)
        if group is None:
            raise ConfigurationError("%s=%s is not accessible to the OAuth token" % (env_name, value))
        prompter.record(
            "Select %s account group" % label,
            "%s [%s] via environment" % (group.name, group.aid),
        )
        return group
    choices = [group for group in groups if group.aid != exclude_aid]
    return prompter.choose_account_group("Select %s account group" % label, choices)


def _preview_group(groups, preview, key, console):
    if preview:
        group = next((g for g in groups if g.aid == str(preview.get(key))), None)
        if group is not None:
            console.info("gate", "reusing preview %s: %s [%s]" % (key, group.name, group.aid))
            return group
    return None


def _source_group(api, groups, agents, prompter, console, preview=None):
    """Use an explicit source or a matching preview before discovering the devices."""
    if os.getenv("TE_SOURCE_AID"):
        return _group_from_env(groups, "TE_SOURCE_AID", "source", prompter)
    return (_preview_group(groups, preview, "source_aid", console)
            or detect_source(api, groups, agents, prompter, console))


def _auth_failed(exc):
    text = str(exc)
    return "HTTP 401" in text or "HTTP 403" in text


def _open_api(stack, prompter, report, console):
    """Open the API client and read the account groups. Return (api, groups).

    A token typed on the console is checked at once: a rejected token is asked again (3 tries) with the
    number of characters that was received, because a short or cut paste is the usual cause. A token from the
    environment (labs) is tried once.
    """
    env_token = os.getenv("TE_OAUTH_TOKEN") or os.getenv("THOUSANDEYES_OAUTH_TOKEN")
    tries = 1 if env_token else TOKEN_TRIES
    if env_token:
        report.record_decision("API v7 OAuth bearer token", "<environment>")
        console.info("input", "API OAuth token loaded from environment")
    for attempt in range(1, tries + 1):
        token = env_token or prompter.secret("API v7 OAuth bearer token")
        attempt_stack = contextlib.ExitStack()
        api = attempt_stack.enter_context(ThousandEyesApiClient(token))
        api.on_retry = lambda method, number, delay, reason: console.info(
            "api_retry", "%s retry after attempt %s: %s; wait %.1fs" % (method, number, reason, delay))
        api.on_page = lambda resource, aid, page: console.info(
            "api_read", "%s: group %s, page %d" % (resource, aid if aid is not None else "accessible", page))
        console.progress("global", "api_account_groups", "connecting to ThousandEyes API v7")
        try:
            groups = api.list_account_groups()
        except ApiError as exc:
            attempt_stack.close()
            if env_token or attempt == tries or not _auth_failed(exc):
                raise
            console.info(
                "input",
                "The API did not accept that token (%d characters received). Copy the whole token again "
                "(try %d of %d)." % (len(token), attempt + 1, tries),
            )
            continue
        stack.callback(attempt_stack.close)
        return api, groups


def _utc_now_text():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path):
    """Fingerprint of the inventory file contents (change detection, not a security use)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _print_error(text):
    print("Error: %s" % mask_secrets(text), file=sys.stderr)


def _mask_exception(exc):
    return mask_secrets("%s" % exc)


def _load_dry_run_confirmation(path_text, inventory_path):
    """Return the dry-run confirmation data or raise PreconditionRefused."""
    if not path_text:
        raise PreconditionRefused(
            "--apply needs --confirmed-dry-run-file PATH (the %s written by a clean "
            "dry run of the same inventory). Run --dry-run first. Nothing was changed."
            % DRY_RUN_OK_FILENAME
        )
    path = Path(path_text)
    if path.name.endswith(".used"):
        raise PreconditionRefused(
            "Dry-run confirmation has already been used: %s. Run the preview again. Nothing was changed." % path
        )
    if not path.is_file():
        raise PreconditionRefused(
            "Dry-run confirmation file does not exist: %s. Run --dry-run first. "
            "Nothing was changed." % path
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PreconditionRefused(
            "Dry-run confirmation file is not readable JSON: %s. Run --dry-run again. "
            "Nothing was changed." % path
        )
    if not isinstance(data, dict):
        raise PreconditionRefused(
            "Dry-run confirmation file %s is not a dry-run file. Nothing was changed." % path
        )
    if data.get("schema_version") != DRY_RUN_SCHEMA:
        raise PreconditionRefused(
            "Dry-run confirmation %s has an old file format (schema %s, schema %s is needed): "
            "run --dry-run again. Nothing was changed."
            % (path, data.get("schema_version"), DRY_RUN_SCHEMA)
        )
    if data.get("inventory_sha256") != _sha256_file(inventory_path):
        raise PreconditionRefused(
            "Dry-run confirmation is stale: the inventory file changed after the dry run. "
            "Run --dry-run again for the current inventory. Nothing was changed."
        )
    return data


def _check_dry_run_groups(data, source, target_aids):
    """The source group and the SET of target groups must be the ones of the dry run.

    ``target_aids`` holds every target aid of this run (row targets and run target). The order
    does not matter. The key ``target_aid`` of the file (run target) is for reading only.
    """
    recorded_source = data.get("source_aid")
    if (None if recorded_source is None else str(recorded_source)) != str(source.aid):
        raise PreconditionRefused(
            "Dry-run confirmation was made for source_aid %s, but this run uses source_aid %s. "
            "Run --dry-run again. Nothing was changed." % (recorded_source, source.aid)
        )
    recorded = data.get("target_aids")
    if not isinstance(recorded, list):
        raise PreconditionRefused(
            "Dry-run confirmation has no list of target groups (old file format): "
            "run --dry-run again. Nothing was changed."
        )
    before = sorted(str(aid) for aid in recorded)
    now = sorted(set(str(aid) for aid in target_aids))
    if before != now:
        raise PreconditionRefused(
            "Dry-run confirmation was made for target group(s) [%s], but this run uses [%s]. "
            "Run --dry-run again. Nothing was changed." % (", ".join(before), ", ".join(now))
        )


def _lazy(resolver):
    cache = []

    def get():
        if not cache:
            cache.append(resolver())
        return cache[0]

    return get


def _make_ip_ui_factory(get_credentials):
    """Factory for the flows: takes the device IP, returns a TevaUiClient (context manager)."""

    def factory(ip_address):
        username, password = get_credentials()
        return TevaUiClient(ip_address, username, password)

    return factory


OPTIONAL_CONTEXT_PARAMS = ("snapshot_dir", "auto_reenable_tests", "repoint_a2a")


def _build_flow_context(kwargs, snapshot_dir):
    """Build FlowContext. Drops optional parameters this build does not have yet.

    ``snapshot_dir``, ``auto_reenable_tests`` and ``repoint_a2a`` are optional. When FlowContext rejects one of them
    (TypeError that names it), the call is repeated without it. A dropped ``auto_reenable_tests=True``
    is announced, because the operator asked for it.
    """
    params = dict(kwargs)
    params["snapshot_dir"] = snapshot_dir
    while True:
        try:
            ctx = FlowContext(**params)
            ctx.standalone_only = True
            return ctx
        except TypeError as exc:
            names = [name for name in OPTIONAL_CONTEXT_PARAMS if name in params and name in str(exc)]
            if not names:
                raise
            name = names[0]
            value = params.pop(name)
            if name == "snapshot_dir":
                kwargs["console"].info(
                    "configuration", "snapshot_dir is not supported by this build; no snapshot files"
                )
            elif name == "repoint_a2a" and value:
                kwargs["console"].info(
                    "configuration",
                    "WARNING: repoint_a2a is not supported by this build; it is ignored and preflight "
                    "blocks every device that agent-to-agent tests target",
                )
            elif value:
                kwargs["console"].info(
                    "configuration",
                    "WARNING: %s is not supported by this build; it is ignored and the tests "
                    "stay disabled (manual list)" % name,
                )


def _results_clean(console, results):
    return not console.has_failures() and all(result.ok for result in results)


def _exit_code(console, results):
    return EXIT_OK if _results_clean(console, results) else EXIT_FAILED


def _write_summary(
    report, results, mode, source, target, snapshot_dir, exit_code, target_aids=None, repoint_a2a=False
):
    # Target group of each device, from the preflight items (None when the result has no device).
    target_of = {}
    for pre_item in getattr(results, "preflight_items", None) or []:
        aid = _item_target(pre_item, target.aid)
        if aid:
            target_of[pre_item.name] = aid
    items = []
    for result in results:
        snapshot_path = getattr(result, "snapshot", None) or getattr(result, "snapshot_path", None)
        items.append(
            {
                "name": result.name,
                "target": target_of.get(result.name),
                "ok": bool(result.ok),
                "detail": result.detail,
                "new_agent_id": result.new_agent_id,
                "new_cluster_id": result.new_cluster_id,
                "warnings": list(result.warnings),
                "snapshot_path": None if snapshot_path is None else str(snapshot_path),
            }
        )
    payload = {
        "schema_version": 1,
        "generated_at": _utc_now_text(),
        "mode": mode.value,
        "source_aid": source.aid,
        "target_aid": target.aid,
        "target_aids": list(target_aids or [target.aid]),
        "snapshot_dir": str(snapshot_dir),
        "repoint_a2a": bool(repoint_a2a),
        "exit_code": exit_code,
        "results": items,
        "recap": list(getattr(results, "rows", [])),
    }
    path = report.output_dir / (report.basename + "_summary.json")
    _write_private_text(
        path, json.dumps(redact_value(payload), indent=2, sort_keys=True) + "\n"
    )
    for item in items:
        report.add_agent(
            {
                "agent": item["name"],
                "status": "ok" if item["ok"] else "failed",
                "detail": item["detail"],
                "new_agent_id": item["new_agent_id"],
                "new_cluster_id": item["new_cluster_id"],
            }
        )
    return path


def _write_private_text(path, text):
    """Write a file readable by the owner only (0600), whatever the umask."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(str(path), 0o600)


def _item_agent_id(item):
    record = getattr(item, "record", None)
    value = getattr(record, "agent_id", None) if record is not None else None
    if not value:
        value = getattr(item.inventory, "agent_id", None)
    return str(value) if value else ""


def _item_key(item):
    return _item_agent_id(item) or "name:%s" % item.name


def _item_target(item, default_target_aid):
    """Target group of a device: set by preflight, else the inventory row, else the run-level target."""
    return str(
        getattr(item, "target_aid", "")
        or getattr(item.inventory, "target_aid", None)
        or default_target_aid
        or ""
    )


def _a2a_pairs(item):
    """Sorted [aid, test_id] pairs (str) of the agent-to-agent tests that the flow would repoint."""
    return sorted([entry["aid"], entry["test_id"]] for entry in a2a_targets_of(item))


def _pairs_text(pairs):
    return ", ".join("%s/%s" % (pair[0], pair[1]) for pair in pairs)


def _dry_run_binding(items, owner_is_source, default_target_aid=None, repoint_a2a=False):
    """What a dry run saw.

    Ready devices: id, kind, target group, the groups that see the device now (share_aids), the final
    share list the flow sends (final_share_aids, resolved: inventory + live + test groups + target),
    owner flag, and ``a2a_targets``: the sorted (aid, test_id) pairs of the agent-to-agent tests the flow
    would repoint (empty without ``--repoint-agent-to-agent``). Top level ``repoint_a2a`` is the flag.
    Also the excluded (offline) keys and the skipped (already migrated) keys.
    """
    ready = []
    excluded = []
    skipped = []
    for item in items:
        if item.status == "ready":
            target = _item_target(item, default_target_aid)
            ready.append(
                {
                    "name": item.name,
                    "kind": "cluster" if item.inventory.is_cluster else "agent",
                    "agent_id": _item_agent_id(item),
                    "key": _item_key(item),
                    "target_aid": target,
                    "share_aids": sorted(str(aid) for aid in item.share_aids),
                    "final_share_aids": sorted(desired_share_aids(item, target)),
                    "owner": "assumed" if getattr(item, "owner_assumed", False) else "proven",
                    "a2a_targets": _a2a_pairs(item),
                    "tests_by_aid": {str(aid): sorted(str(t) for t in ids)
                                     for aid, ids in item.tests_by_aid.items()},
                    "test_enabled": {str(aid): dict(states) for aid, states in
                                     (getattr(item, "test_enabled", {}) or {}).items()},
                }
            )
        elif item.status == "excluded":
            excluded.append(_item_key(item))
        elif item.status == "skipped":
            skipped.append(_item_key(item))
    ready.sort(key=lambda entry: entry["key"])
    return {
        "owner_is_source": bool(owner_is_source),
        "repoint_a2a": bool(repoint_a2a),
        "ready": ready,
        "excluded_ids": sorted(set(excluded)),
        "skipped_ids": sorted(set(skipped)),
    }


def _dry_run_problems(data, items, owner_is_source, default_target_aid=None, repoint_a2a=False):
    """List what differs between the saved preview and the fresh preflight (empty list = same)."""
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < DRY_RUN_SCHEMA:
        return ["the preview file has an old file format (schema %s, schema %s is needed)" % (schema, DRY_RUN_SCHEMA)]
    expected = data.get("ready")
    if not isinstance(expected, list) or "excluded_ids" not in data or "owner_is_source" not in data:
        return ["the preview file has no device binding (old format)"]
    if (not isinstance(data.get("excluded_ids"), list)
            or not isinstance(data.get("skipped_ids", []), list)
            or not isinstance(data.get("owner_is_source"), bool)
            or not isinstance(data.get("repoint_a2a"), bool)
            or any(not isinstance(entry, dict) for entry in expected)):
        return ["the preview file has a malformed device binding"]
    for entry in expected:
        if any(not isinstance(entry.get(field), list)
               for field in ("share_aids", "final_share_aids", "a2a_targets")):
            return ["the preview file has a malformed device binding"]
        if any(not isinstance(pair, list) or len(pair) != 2 for pair in entry["a2a_targets"]):
            return ["the preview file has malformed agent-to-agent target bindings"]
    now = _dry_run_binding(items, owner_is_source, default_target_aid, repoint_a2a)
    problems = []
    if bool(data.get("repoint_a2a")) != now["repoint_a2a"]:
        problems.append(
            "--repoint-agent-to-agent was %s in the dry run and is %s now"
            % ("set" if data.get("repoint_a2a") else "not set", "set" if now["repoint_a2a"] else "not set")
        )
    if bool(data.get("owner_is_source")) != now["owner_is_source"]:
        problems.append(
            "--owner-is-source was %s in the dry run and is %s now"
            % ("set" if data.get("owner_is_source") else "not set",
               "set" if now["owner_is_source"] else "not set")
        )
    before = dict((str(entry.get("key")), entry) for entry in expected if isinstance(entry, dict))
    after = dict((entry["key"], entry) for entry in now["ready"])
    status_now = dict((_item_key(item), item.status) for item in items)
    for key in sorted(set(after) - set(before)):
        problems.append("%s is ready now but was not ready in the dry run" % after[key]["name"])
    for key in sorted(set(before) - set(after)):
        problems.append(
            "%s was ready in the dry run but is now %s"
            % (before[key].get("name", key), status_now.get(key, "not selected"))
        )
    for key in sorted(set(before) & set(after)):
        old, new = before[key], after[key]
        for field in ("tests_by_aid", "test_enabled"):
            if old.get(field) != new[field]:
                problems.append("%s: %s changed since the approved preview" % (new["name"], field))
        if sorted(str(a) for a in old.get("share_aids") or []) != new["share_aids"]:
            problems.append(
                "%s: share set changed from [%s] to [%s]"
                % (new["name"], ", ".join(sorted(str(a) for a in old.get("share_aids") or [])),
                   ", ".join(new["share_aids"]))
            )
        if str(old.get("target_aid")) != new["target_aid"]:
            problems.append(
                "%s: target group changed from %s to %s" % (new["name"], old.get("target_aid"), new["target_aid"])
            )
        if sorted(str(a) for a in old.get("final_share_aids") or []) != new["final_share_aids"]:
            problems.append(
                "%s: final share list changed from [%s] to [%s]"
                % (new["name"], ", ".join(sorted(str(a) for a in old.get("final_share_aids") or [])),
                   ", ".join(new["final_share_aids"]))
            )
        old_pairs = sorted(
            [str(pair[0]), str(pair[1])]
            for pair in old.get("a2a_targets") or []
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        )
        if old_pairs != new["a2a_targets"]:
            problems.append(
                "%s: agent-to-agent tests to repoint changed from [%s] to [%s] (aid/test)"
                % (new["name"], _pairs_text(old_pairs), _pairs_text(new["a2a_targets"]))
            )
        if old.get("kind") != new["kind"]:
            problems.append("%s: kind changed from %s to %s" % (new["name"], old.get("kind"), new["kind"]))
        if old.get("owner") != new["owner"]:
            problems.append(
                "%s: owner was %s in the dry run and is %s now"
                % (new["name"], old.get("owner"), new["owner"])
            )
    if sorted(str(i) for i in data.get("excluded_ids") or []) != now["excluded_ids"]:
        problems.append("the set of excluded (offline) devices changed")
    if sorted(str(i) for i in data.get("skipped_ids") or []) != now["skipped_ids"]:
        problems.append("the set of skipped (already migrated) devices changed")
    return problems


def _compare_dry_run_binding(data, items, owner_is_source, default_target_aid=None, repoint_a2a=False):
    """Refuse (PreconditionRefused) when the fresh preflight differs from the dry run."""
    problems = _dry_run_problems(data, items, owner_is_source, default_target_aid, repoint_a2a)
    if problems:
        shown = "; ".join(problems[:8])
        if len(problems) > 8:
            shown += "; ... (+%d more)" % (len(problems) - 8)
        raise PreconditionRefused(
            "Dry-run confirmation does not match the current state: %s. "
            "Run the preview again. Nothing was changed." % shown
        )


def _consume_dry_run_file(path_text):
    """Single use: rename the dry-run file to <name>.used before the first flow runs."""
    source = str(path_text)
    try:
        os.replace(source, source + ".used")
    except OSError as exc:
        raise PreconditionRefused(
            "Could not mark the dry-run confirmation as used (%s); refusing to start. "
            "Nothing was changed." % exc.__class__.__name__
        )


def _update_dry_run_confirmation(
    output, clean, inventory_path, source, default_target_aid, agents, items=None, owner_is_source=False,
    target_aids=None, repoint_a2a=False, discovery_aids=None,
):
    """Write dry-run-ok.json after a clean dry run. Remove an older one when not clean.

    ``target_aids`` is every target group of the run. The file stores it sorted. ``target_aid`` is the
    run target (None when every row names its own target); it is there for reading only.
    """
    path = Path(output) / DRY_RUN_OK_FILENAME
    if not clean or items is None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        return None
    payload = {
        "schema_version": DRY_RUN_SCHEMA,
        "inventory_file": str(inventory_path),
        "inventory_sha256": _sha256_file(inventory_path),
        "source_aid": source.aid,
        "target_aid": default_target_aid,
        "target_aids": sorted(set(str(aid) for aid in (target_aids or []))),
        "entries": len(agents),
        "created_utc": _utc_now_text(),
        "discovery_aids": (sorted(set(str(aid) for aid in (discovery_aids or [])))
                           if all(getattr(item, "discovery_complete", False)
                                  for item in items if item.status == "ready") else []),
    }
    payload.update(_dry_run_binding(items, owner_is_source, default_target_aid, repoint_a2a))
    _write_private_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _needs_run_target(agents):
    """True when at least one inventory row has no target_aid (it uses the target of the run)."""
    return any(not agent.target_aid for agent in agents)


def _all_target_aids(agents, run_target):
    """Distinct target aids of the inventory, in order of first use."""
    aids = []
    for agent in agents:
        aid = agent.target_aid or run_target.aid
        if aid not in aids:
            aids.append(aid)
    return aids


def _check_inventory_targets(agents, groups):
    """Every target_aid of the inventory must be a group the OAuth token can see."""
    known = set(group.aid for group in groups)
    missing = sorted(set(a.target_aid for a in agents if a.target_aid and a.target_aid not in known))
    if missing:
        raise ConfigurationError(
            "The inventory names target group id(s) that the OAuth token cannot access: %s. "
            "No agents were reset." % ", ".join(missing)
        )


def _confirm_owner(console, prompter, report, source, items):
    """Ask the operator to confirm that the source group owns the devices whose owner the API did not name.

    The API does not tell which group owns an agent. The move deletes the old agent, so a device that the
    source group only SEES (another group owns it) must not be moved: that group would lose its agent.
    """
    assumed = [item for item in items if item.status == "ready" and getattr(item, "owner_assumed", False)]
    if not assumed:
        return True
    label = "%s [%s]" % (source.name, source.aid)
    console.info("owner", "The source group %s gives away these %d device(s):" % (label, len(assumed)))
    for item in assumed:
        note = "   (the target group already sees it)" if getattr(item, "visible_in_target", False) else ""
        console.info("owner", "  - %s%s" % (item.name, note))
    console.info(
        "owner",
        "That is right ONLY if %s is the Primary Account Group (the owner) of every device above. "
        "If another group owns one, remove it from the inventory: that group would lose its agent." % label,
    )
    console.info(
        "owner",
        "Check in ThousandEyes: Cloud & Enterprise Agents > the agent > Basic Configuration > Primary Account Group.",
    )
    answer = prompter.required_choice("Is %s the owner of ALL these devices?" % label, ("y", "n")) == "y"
    report.record_decision("Source group owns the devices", "yes" if answer else "no")
    return answer


def _load_migrated_ids(console):
    """Ids of the new agents that this tool created in earlier runs (so a re-run skips them)."""
    try:
        data = json.loads(MIGRATED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    entries = data.get("agents") if isinstance(data, dict) else None
    ids = set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("new_id"):
            ids.add(str(entry["new_id"]))
    return ids


def _remember_migrated(console, results):
    """Record the new agent ids of the devices that moved. A failure here is only a note."""
    new = []
    for result in results:
        new_id = getattr(result, "new_agent_id", None) or getattr(result, "new_cluster_id", None)
        if getattr(result, "ok", False) and new_id:
            new.append({"name": result.name, "new_id": str(new_id), "time": _utc_now_text()})
    if not new:
        return
    try:
        MIGRATED_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(MIGRATED_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        agents = data.get("agents") if isinstance(data, dict) and isinstance(data.get("agents"), list) else []
        known = set(str(e.get("new_id")) for e in agents if isinstance(e, dict))
        agents.extend(e for e in new if e["new_id"] not in known)
        _write_private_text(MIGRATED_FILE, json.dumps({"agents": agents}, indent=2) + "\n")
    except OSError as exc:
        console.info("run", "could not save %s (%s); a re-run may ask about these devices again"
                     % (MIGRATED_FILE, exc.__class__.__name__))


def _find_preview(path, inventory_path, console):
    """The saved preview that matches this inventory, or None (then --apply makes and shows its own)."""
    try:
        return _load_dry_run_confirmation(str(path), inventory_path)
    except PreconditionRefused as exc:
        console.info("gate", "no usable earlier preview (%s)" % str(exc).replace(" Nothing was changed.", ""))
        return None


def _run_migration(
    args, api, groups, source, target, selected_mode, agents, console, report, prompter,
    output, inventory_path, get_ui_credentials, dry_run_data=None, gate_path=None,
):
    apply_mode = selected_mode is Mode.APPLY
    if any(agent.is_cluster for agent in agents):
        raise FlowAborted("Cluster migration is not qualified for production; select standalone agents only.")
    snapshot_dir = Path(output) / "snapshots"
    target_names = dict((group.aid, group.name) for group in groups)
    target_labels = dict((aid, "%s [%s]" % (name, aid)) for aid, name in target_names.items())
    default_target_aid = target.aid if _needs_run_target(agents) else None
    if apply_mode:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
    else:
        console.info("input", "Dry run: the target account-group tokens are not requested or used")
    target_token = ""
    tokens = {}
    for line in describe_inventory(agents).splitlines():
        console.info("inventory", line.strip())
    kwargs = dict(
        api=api,
        ui_factory=_make_ip_ui_factory(get_ui_credentials),
        console=console,
        source_aid=source.aid,
        target_aid=target.aid,
        target_account_token=target_token,
        apply=apply_mode,
    )
    if args.timeout is not None:
        kwargs["timeout_seconds"] = args.timeout
    auto_reenable = bool(getattr(args, "auto_reenable_tests", False))
    kwargs["auto_reenable_tests"] = auto_reenable
    # Agent-to-agent tests that use a device as their target are always repointed to the new agent. The
    # plan lists them before the operator confirms. A test that cannot be proven safe still blocks the device.
    repoint_a2a = True
    kwargs["repoint_a2a"] = True
    report.record_decision("Repoint agent-to-agent tests", "automatic")
    report.record_decision("Old registration cleanup",
                           "disable, tag migration-status=MIGRATED-TO-BE-DELETED and rename "
                           "MIGRATED-TO-BE-DELETED-<old-id>; never delete")
    report.record_decision(
        "Auto re-enable tests", "enabled" if auto_reenable else "disabled (manual list)"
    )
    # The API does not name the owner of an agent. The preview assumes the source group owns the devices
    # (a field that names another group still blocks) and --apply asks the operator to confirm it.
    owner_is_source = True
    migrated_ids = _load_migrated_ids(console)
    all_aids = [group.aid for group in groups]
    strict_confirmation = apply_mode and bool(getattr(args, "confirmed_dry_run_file", None))
    pre_ctx, pre_items = None, None
    checked_migrated_ids = set(migrated_ids)

    def read_current_preflight():
        # The dry-run wrapper refuses writes, including when checking an explicitly named confirmation.
        pre_kwargs = dict(kwargs, apply=False, target_account_token="")
        current_ctx = _build_flow_context(pre_kwargs, str(snapshot_dir))
        current_ctx.target_names = dict(target_names)
        current_ctx.migrated_ids = set(migrated_ids)
        if (dry_run_data and dry_run_data.get("discovery_aids") == sorted(set(all_aids))
                and isinstance(dry_run_data.get("ready"), list)
                and all(isinstance(entry, dict) and isinstance(entry.get("share_aids"), list)
                        for entry in dry_run_data["ready"])):
            current_ctx.preview_bindings = {
                str(entry.get("agent_id")): entry for entry in dry_run_data["ready"]}
        elif dry_run_data:
            console.info("gate", "preview coverage is missing or group access changed; "
                         "full sharing discovery is required")
        current_items = run_preflight(
            ctx=current_ctx, inventory_agents=agents, all_aids=all_aids,
            assume_owner_source=owner_is_source,
        )
        return current_ctx, current_items

    if strict_confirmation:
        if dry_run_data is None:
            dry_run_data = _load_dry_run_confirmation(str(gate_path), inventory_path)
        _check_dry_run_groups(dry_run_data, source, _all_target_aids(agents, target))
        pre_ctx, pre_items = read_current_preflight()
        _compare_dry_run_binding(dry_run_data, pre_items, owner_is_source, default_target_aid, repoint_a2a)
        if preflight_has_blockers(pre_items):
            raise PreconditionRefused("Current preflight has blockers. Nothing was changed.")
    if not _finish_interrupted(console, prompter, report, api, source, agents, kwargs, snapshot_dir,
                               target_names, apply_mode, migrated_ids):
        console.info("run", "an earlier run could not be finished; no other device was started")
        report.write_final()
        return EXIT_FAILED
    if apply_mode:
        # Reuse the strict read-only check unless recovery changed which devices are already complete.
        if pre_items is None or checked_migrated_ids != set(migrated_ids):
            pre_ctx, pre_items = read_current_preflight()
        console.play("Preflight before confirmation", Mode.DRY_RUN)
        print_preflight(pre_ctx, pre_items)
        summary = preflight_summary(pre_items)
        console.info(
            "preflight",
            "ready=%s blocked=%s excluded=%s skipped=%s"
            % (summary["ready"], summary["blocked"], summary["excluded"], summary.get("skipped", 0)),
        )
        # The fresh preview above is the plan. When an earlier preview exists, say what changed since then.
        if dry_run_data is None:
            console.info("gate", "no earlier preview matches; the fresh preview above is the plan. Read it "
                                 "before you confirm")
            report.record_decision("Apply", "no earlier preview; fresh preflight shown")
        else:
            differences = _dry_run_problems(dry_run_data, pre_items, owner_is_source, default_target_aid,
                                            repoint_a2a)
            if differences:
                if strict_confirmation:
                    _compare_dry_run_binding(dry_run_data, pre_items, owner_is_source, default_target_aid,
                                             repoint_a2a)
                console.info("gate", "WARNING: the situation changed since the last preview: %s"
                             % "; ".join(differences[:8]))
                report.record_decision("Apply", "changed since the preview: %s" % "; ".join(differences[:8]))
                dry_run_data = None  # the preview is not the plan; keep the file untouched
            else:
                report.record_decision("Apply", "same as the last preview")
        if preflight_has_blockers(pre_items):
            report.record_decision("Apply", "preflight blocked")
            console.info("run", "preflight has blockers; nothing was changed")
            report.write_final()
            return EXIT_FAILED
        # Ask for every needed token now, before the first reset: a missing token must not stop the run later.
        needed = []
        for item in pre_items:
            aid = item_target_aid(pre_ctx, item)
            if item.status == "ready" and aid not in needed:
                needed.append(aid)
        tokens = TargetTokens(report, prompter, console, labels=target_labels).collect(needed)
        target_token = tokens.get(target.aid, "")
        kwargs["target_account_token"] = target_token
        if needed:
            if not _confirm_owner(console, prompter, report, source, pre_items):
                console.info("run", "owner not confirmed; nothing was changed")
                report.write_final()
                return EXIT_FAILED
            if not _precheck_devices(console, prompter, report, pre_items, get_ui_credentials):
                report.write_final()
                return EXIT_FAILED
            if not prompter.confirm_destructive():
                report.record_decision("Apply", "operator declined")
                console.info("run", "operator declined; nothing was changed")
                report.write_final()
                return EXIT_FAILED
            report.record_decision("Apply", "operator confirmed")
            # Single use: consume the matching preview only before a new device migration.
            if dry_run_data is not None and gate_path:
                _consume_dry_run_file(gate_path)
                console.info("gate", "preview marked as used (%s.used)" % gate_path)
        else:
            report.record_decision("Apply", "no new migrations")
            console.info("run", "No new migrations are ready; nothing will be reset.")
    ctx = _build_flow_context(kwargs, str(snapshot_dir))
    if apply_mode:
        approved = _dry_run_binding(pre_items, owner_is_source, default_target_aid, repoint_a2a)
        approved["schema_version"] = DRY_RUN_SCHEMA
        ctx.preflight_items = pre_items
        covered = set(_item_agent_id(item) for item in pre_items
                      if getattr(item, "discovery_complete", False))
        pre_ctx.preview_bindings = {str(entry["agent_id"]): entry for entry in approved["ready"]
                                    if str(entry["agent_id"]) in covered}
        ctx.validate_preflight = lambda items: _compare_dry_run_binding(
            approved, items, owner_is_source, default_target_aid, repoint_a2a)

        def validate_device(item):
            console.info("preflight", "%s: fresh validation before reset" % item.name)
            fresh = run_preflight(ctx=pre_ctx, inventory_agents=[item.inventory], all_aids=all_aids,
                                  assume_owner_source=owner_is_source)
            binding = _dry_run_binding([item], owner_is_source, default_target_aid, repoint_a2a)
            binding["schema_version"] = DRY_RUN_SCHEMA
            _compare_dry_run_binding(binding, fresh, owner_is_source, default_target_aid, repoint_a2a)

        ctx.validate_device = validate_device
    ctx.migrated_ids = set(migrated_ids)
    ctx.target_tokens = dict(tokens)
    ctx.target_names = dict(target_names)
    all_target_aids = _all_target_aids(agents, target)
    console.info(
        "run",
        "One device at a time; %s device(s) selected; %s target group(s)"
        % (len(agents), len(all_target_aids)),
    )
    try:
        results = run_migration(
            ctx=ctx,
            inventory_agents=agents,
            console=console,
            all_aids=all_aids,
            assume_owner_source=owner_is_source,
        )
    except BaseException as exc:
        partial = getattr(exc, "flow_results", None)
        if partial is not None and not isinstance(exc, Exception):
            # Ctrl-C or SystemExit in a flow: keep the evidence, then let the exception go on.
            code = EXIT_INTERRUPTED if isinstance(exc, KeyboardInterrupt) else EXIT_FAILED
            try:
                summary_path = _write_summary(
                    report, partial, selected_mode, source, target, snapshot_dir, code, all_target_aids,
                    repoint_a2a,
                )
                report.write_final()
                console.info("run", "Summary (interrupted run): %s" % summary_path)
            except Exception as write_error:
                console.info(
                    "run", "could not write the summary of the interrupted run: %s"
                    % _mask_exception(write_error),
                )
        raise
    code = _exit_code(console, results)
    clean = code == EXIT_OK
    if apply_mode:
        _remember_migrated(console, results)
    else:
        # The preview has no UI login, but it can already say which agent UIs this computer cannot reach.
        reach = check_devices(getattr(results, "preflight_items", None) or [])
        if reach.unreachable:
            console.info("precheck", "WARNING: these agent UIs do not answer on port 443 from this computer "
                                     "(--apply stops on them): %s" % "; ".join(reach.unreachable))
    confirmation = None
    nothing_ready = False
    if not apply_mode:
        preflight_items = getattr(results, "preflight_items", None) or []
        nothing_ready = clean and not any(
            getattr(item, "status", None) == "ready" for item in preflight_items
        )
        confirmation = _update_dry_run_confirmation(
            output, clean and not nothing_ready and not getattr(results, "blocked", False),
            inventory_path, source, default_target_aid, agents,
            items=getattr(results, "preflight_items", None), owner_is_source=owner_is_source,
            target_aids=all_target_aids, repoint_a2a=repoint_a2a, discovery_aids=all_aids,
        )
    summary_path = _write_summary(
        report, results, selected_mode, source, target, snapshot_dir, code, all_target_aids, repoint_a2a
    )
    report.write_final()
    console.info("run", "Summary: %s" % summary_path)
    if confirmation is not None:
        console.info(
            "run",
            "clean preview (%s). To move the devices run the same command with --apply" % confirmation,
        )
    elif nothing_ready:
        console.info(
            "run",
            "nothing to migrate: no device is ready (see the excluded devices above); "
            "%s was not written" % DRY_RUN_OK_FILENAME,
        )
    elif not apply_mode:
        console.info("run", "dry run is not clean; %s was not written" % DRY_RUN_OK_FILENAME)
    return code


def main(argv=None):
    """Entry point. Runs with umask 077 so report folders and files are private (0700/0600)."""
    old_umask = os.umask(0o077)
    try:
        return _main(argv)
    except OSError as exc:
        _print_error("Local file operation failed: %s. No further migration steps will run; "
                     "preserve work/ and inspect the last saved snapshot and report." % _mask_exception(exc))
        return EXIT_FAILED
    finally:
        os.umask(old_umask)


def _main(argv=None):
    args = build_parser().parse_args(argv)
    selected_mode = Mode.APPLY if args.apply else Mode.DRY_RUN
    inventory = Path(args.inventory) if args.inventory else DEFAULT_INVENTORY
    if args.output:  # lab and tests: one folder chosen by the caller, kept as it is
        output = Path(args.output)
    elif selected_mode is Mode.APPLY:
        output = APPLY_ROOT / _apply_stamp()
    else:
        output = DRY_RUN_DIR
        _empty_dir(output)  # only the latest preview is kept
    # The preview that --apply looks for: the one in its own folder, or the file the caller named.
    gate_path = Path(args.confirmed_dry_run_file) if args.confirmed_dry_run_file else (
        (Path(args.output) if args.output else DRY_RUN_DIR) / DRY_RUN_OK_FILENAME
    )
    console = MigrationConsole(verbose=args.verbose)
    console.banner()
    console.info("startup", "mode=%s; report output=%s" % (selected_mode.value, output))
    for note in _old_option_notes(args):
        console.info("startup", note)
    report = RunReport(output, selected_mode.value, {"inventory": str(inventory)})
    console.event_sink = report.record_console
    prompter = OperatorPrompter(report.append_decision)
    try:
        if (
            os.getenv("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
            and os.getenv("TE_ALLOW_CODEX_SANDBOX_NETWORK") != "1"
        ):
            raise ConfigurationError(
                "Codex network sandbox blocks direct agent HTTPS/443 connections. "
                "Run this command from a local terminal outside Codex."
            )
        if not ssl.HAS_TLSv1_3:
            console.info("startup", "WARNING: this Python has no TLS 1.3 (%s). Agents that require it will refuse "
                                    "the connection; use a newer Python." % ssl.OPENSSL_VERSION)
        if not inventory.is_file() and sys.stdin.isatty():
            capture_inventory(inventory, input)
        console.progress("global", "inventory", "reading %s" % inventory)
        agents = load_inventory(inventory)
        console.status(StepStatus.OK, "global", "inventory", "loaded %s agent(s)" % len(agents))
        apply_mode = selected_mode is Mode.APPLY
        dry_run_data = None
        if apply_mode:
            if args.confirmed_dry_run_file:
                dry_run_data = _load_dry_run_confirmation(str(gate_path), inventory)
            else:
                dry_run_data = _find_preview(gate_path, inventory, console)
            if dry_run_data is not None:
                console.info("gate", "earlier preview found (created %s)" % dry_run_data.get("created_utc", "?"))
        if not apply_mode:
            # The agent UI credentials are asked only if a flow really opens the UI.
            get_ui_credentials = _lazy(lambda: _resolve_agent_ui_inputs(report, prompter, console))
        else:
            if args.confirmed_dry_run_file:
                get_ui_credentials = _UiCredentials(lambda: _resolve_agent_ui_inputs(report, prompter, console))
            else:
                get_ui_credentials = _UiCredentials(_resolve_agent_ui_inputs(report, prompter, console))
        with contextlib.ExitStack() as stack:
            api, groups = _open_api(stack, prompter, report, console)
            console.status(
                StepStatus.OK,
                "global",
                "api_account_groups",
                "OAuth token can access %s account group(s)" % len(groups),
            )
            # Group names and the prefix map become group ids; an unknown target stops the run before any prompt.
            resolve_targets(agents, groups, PREFIX_MAP_FILE, console)
            _check_inventory_targets(agents, groups)
            source = _source_group(api, groups, agents, prompter, console, preview=dry_run_data)
            own_targets = any(agent.target_aid for agent in agents)
            # When every row names its target, the run needs no target of its own.
            needs_target = _needs_run_target(agents)
            if needs_target:
                target = (None if os.getenv("TE_TARGET_AID") else
                          _preview_group(groups, dry_run_data, "target_aid", console))
                if target is None:
                    target = _group_from_env(groups, "TE_TARGET_AID", "target", prompter, exclude_aid=source.aid)
            else:
                # Every row has its own target. The run target is the first one (for the context only).
                target = next(group for group in groups if group.aid == agents[0].target_aid)
            all_target_aids = _all_target_aids(agents, target)
            if source.aid in all_target_aids:
                raise ConfigurationError(
                    "Source and target account groups must be different (source %s is also a target)"
                    % source.aid
                )
            if dry_run_data is not None:
                try:
                    _check_dry_run_groups(dry_run_data, source, all_target_aids)
                except PreconditionRefused as exc:
                    if args.confirmed_dry_run_file:
                        raise
                    console.info("gate", "the earlier preview is for other groups; it is not used (%s)"
                                 % str(exc).split(". ")[0])
                    dry_run_data = None
            if needs_target:
                console.info(
                    "configuration",
                    "source=%s [%s]; target=%s [%s]"
                    % (source.name, source.aid, target.name, target.aid),
                )
            else:
                console.info(
                    "configuration",
                    "source=%s [%s]; every inventory row names its own target group"
                    % (source.name, source.aid),
                )
            if own_targets:
                counts = {}
                for agent in agents:
                    aid = agent.target_aid or target.aid
                    counts[aid] = counts.get(aid, 0) + 1
                names = dict((group.aid, group.name) for group in groups)
                console.targets_banner(
                    source,
                    [("%s [%s]" % (names.get(aid, aid), aid), counts[aid]) for aid in all_target_aids],
                    selected_mode,
                    destructive=apply_mode,
                )
            else:
                console.destination_banner(source, target, selected_mode, destructive=apply_mode)
            code = _run_migration(
                args, api, groups, source, target, selected_mode, agents, console,
                report, prompter, output, inventory, get_ui_credentials,
                dry_run_data=dry_run_data, gate_path=gate_path,
            )
            print("JSON report: %s" % report.json_path)
            print("CSV reports: %s" % report.csv_dir)
            return code
    except PreconditionRefused as exc:
        detail = _mask_exception(exc)
        report.record_error("run", detail)
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", detail)
        _print_error(detail)
        return EXIT_REFUSED
    except FlowAborted as exc:
        detail = _mask_exception(exc)
        report.record_error("run", detail)
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", detail)
        _print_error(detail)
        return EXIT_FAILED
    except MigrationError as exc:
        detail = _mask_exception(exc)
        report.record_error("run", detail)
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", detail)
        _print_error(detail)
        return EXIT_FAILED
    except KeyboardInterrupt:
        detail = "Interrupted by the operator; the state of the current device may be unknown"
        report.record_error("run", detail)
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", detail)
        _print_error(detail)
        return EXIT_INTERRUPTED
    except Exception as exc:
        detail = "Unexpected failure (%s)" % type(exc).__name__
        report.record_error("run", detail)
        report.write_final()
        console.status(StepStatus.FAILED, "global", "run", detail)
        if args.verbose:
            print(mask_secrets(traceback.format_exc()), file=sys.stderr)
        _print_error(detail)
        return EXIT_FAILED
