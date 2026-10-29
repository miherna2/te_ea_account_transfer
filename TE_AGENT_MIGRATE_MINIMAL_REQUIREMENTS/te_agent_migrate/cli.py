"""Argparse command-line entry point for Python 3.6 through 3.9."""

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from te_agent_migrate.api_client import ThousandEyesApiClient
from te_agent_migrate.errors import ConfigurationError, MigrationError
from te_agent_migrate.inventory import describe_inventory, load_inventory
from te_agent_migrate.models import Mode, RunOptions, StalePolicy, StepStatus, TestStrategy
from te_agent_migrate.operator import OperatorPrompter
from te_agent_migrate.output import MigrationConsole, mask_secrets
from te_agent_migrate.redaction import redact_value
from te_agent_migrate.reporting import RunReport
from te_agent_migrate.strategy_d.context import FlowAborted, FlowContext, a2a_targets_of, item_target_aid
from te_agent_migrate.strategy_d.runner import run_strategy_d
from te_agent_migrate.strategy_d.standalone import desired_share_aids
from te_agent_migrate.target_tokens import (
    TOKEN_PATTERN,
    TargetTokens,
    clean_token,
    normalize_token as _normalize_account_group_token,
)
from te_agent_migrate.strategy_d.preflight import (
    has_blockers as preflight_has_blockers,
    print_preflight,
    run_preflight,
    summarize as preflight_summary,
)
from te_agent_migrate.ui_client import TevaUiClient
from te_agent_migrate.workflow import MigrationRunner


DRY_RUN_SCHEMA = 4  # 4: binds the a2a repoint targets and the --repoint-agent-to-agent flag
UI_USERNAME_ENV = "TE_AGENT_UI_USERNAME"
UI_PASSWORD_ENV = "TE_AGENT_UI_PASSWORD"
TARGET_ACCOUNT_TOKEN_ENV = "TE_TARGET_ACCOUNT_TOKEN"
TARGET_ACCOUNT_TOKEN_AID_ENV = "TE_TARGET_ACCOUNT_TOKEN_AID"
DRY_RUN_OK_FILENAME = "dry-run-ok.json"
DEFAULT_LEGACY_TIMEOUT = 180
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


def _parallelism(value):
    result = _positive_integer(value)
    if result > 5:
        raise argparse.ArgumentTypeError("must be between 1 and 5")
    return result


def build_parser():
    parser = argparse.ArgumentParser(
        prog="te-agent-migrate",
        description="Migrate ThousandEyes Enterprise Agents between account groups.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default).")
    mode.add_argument("--apply", action="store_true", help="Execute the migration.")
    parser.add_argument(
        "--inventory",
        default="work/input/inventory.csv",
        help=(
            "Inventory CSV path. Columns: type,name,ip_address[,agent_id][,target_aid][,share_aids] "
            "(inventory v3; the last three are optional, in this order). "
            "Strategy D: a row target_aid overrides the run target for that device."
        ),
    )
    parser.add_argument(
        "--strategy",
        help=(
            "A=agents only, B=share tests, C=recreate tests (apply is blocked; "
            "tests cannot stay in the source group), D=move the agent and keep the "
            "tests in the source group (share back and assign). "
            "D moves only the devices listed in the inventory and runs one device at a time. "
            "D reads the v3 columns: target_aid is the target group of the row and overrides "
            "the run target (TE_TARGET_AID); share_aids lists the groups that must see the "
            "device after the move. When every row has a target_aid, the run target is not "
            "needed. Each target group needs its own registration token "
            "(TE_TARGET_TOKEN_<aid>, or a hidden prompt). A, B and C ignore these columns."
        ),
    )
    parser.add_argument(
        "--owner-is-source",
        dest="owner_is_source",
        action="store_true",
        help=(
            "Strategy D: state that the source account group OWNS every selected device. "
            "The API gives no owner field, so preflight blocks every device without this flag. "
            "A device whose data names another owner is still blocked."
        ),
    )
    parser.add_argument(
        "--auto-reenable-tests",
        dest="auto_reenable_tests",
        action="store_true",
        help=(
            "Strategy D only (default off): after a cluster is rebuilt, re-enable the tests that "
            "the unassign disabled. Without this flag the tool prints a manual list. "
            "Uses a full-payload PUT; lab check 7 must pass first."
        ),
    )
    parser.add_argument(
        "--repoint-agent-to-agent",
        dest="repoint_agent_to_agent",
        action="store_true",
        help=(
            "Strategy D only (default off; ignored with an info line for A, B and C): after a move the "
            "device has a NEW agent id, and agent-to-agent tests name their target by id. Without this "
            "flag preflight BLOCKS every device that such a test targets. With this flag preflight does "
            "not block; it warns '<N> agent-to-agent test(s) will be repointed after the move' and "
            "records each test (account group, test id, old target id). After the new agent is online, "
            "shared and has its tests, and BEFORE the old agent is deleted, the tool changes only "
            "targetAgentId of each recorded test to the new id (full-payload PUT, then a read-back). If "
            "one test fails, the old agent is kept. A test whose targetAgentId cannot be read becomes a "
            "BLOCKER (the repoint cannot be proven safe). A cluster that such a test targets is a BLOCKER "
            "(not supported). The snapshot file lists the old target ids, so you can undo by hand. The "
            "dry-run file binds the flag and the tests. Lab check: the PUT must change nothing else."
        ),
    )
    parser.add_argument(
        "--confirmed-dry-run-file",
        dest="confirmed_dry_run_file",
        metavar="PATH",
        help=(
            "Strategy D with --apply: path to the dry-run-ok.json that a clean dry run "
            "of the same inventory wrote into the report directory. The file is bound to the "
            "ready devices and their shares, and it is single use (renamed to .used at start)."
        ),
    )
    parser.add_argument("--stale", help="Strategy C source-test policy: keep or remove.")
    parser.add_argument(
        "--create-missing-tags",
        action="store_true",
        help="Strategy C: create/reuse missing target test tags.",
    )
    parser.add_argument(
        "--create-missing-alerts",
        action="store_true",
        help="Strategy C: create/reuse missing target alert rules.",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_integer,
        default=None,
        help="Wait limit in seconds (A/B/C default 180; D uses the flow default).",
    )
    parser.add_argument(
        "--parallel",
        type=_parallelism,
        default=1,
        dest="parallelism",
        help="Parallel connections (1-5). Strategy D always uses 1.",
    )
    parser.add_argument("--output", default="work/runs", help="Artifact directory.")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


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


def _resolve_target_account_token(target, report, prompter, console):
    target_token = _environment_value(TARGET_ACCOUNT_TOKEN_ENV)
    if target_token is None:
        target_token = prompter.secret(
            "Target account-group token for %s [%s]" % (target.name, target.aid)
        )
        report.record_decision(
            "Target account-group token binding",
            "hidden prompt bound to %s [%s]" % (target.name, target.aid),
        )
    else:
        token_aid = _environment_value(TARGET_ACCOUNT_TOKEN_AID_ENV)
        if token_aid is None:
            raise ConfigurationError(
                "%s must also be set when %s comes from the environment; expected %s. "
                "No agents were reset."
                % (TARGET_ACCOUNT_TOKEN_AID_ENV, TARGET_ACCOUNT_TOKEN_ENV, target.aid)
            )
        if token_aid != target.aid:
            raise ConfigurationError(
                "%s=%s does not match selected target %s [%s]. No agents were reset."
                % (TARGET_ACCOUNT_TOKEN_AID_ENV, token_aid, target.name, target.aid)
            )
        report.record_decision(
            "Target account-group token", "<environment:%s>" % TARGET_ACCOUNT_TOKEN_ENV
        )
        report.record_decision(
            "Target account-group token binding",
            "%s [%s] via %s" % (target.name, target.aid, TARGET_ACCOUNT_TOKEN_AID_ENV),
        )
        console.info(
            "input",
            "Target token loaded from %s and bound to %s [%s] via %s"
            % (TARGET_ACCOUNT_TOKEN_ENV, target.name, target.aid, TARGET_ACCOUNT_TOKEN_AID_ENV),
        )
    return clean_token(target_token, console)


def _resolve_source_test_policy(strategy, supplied_policy, report, prompter, console):
    if strategy is TestStrategy.RECREATE:
        value = supplied_policy or prompter.required_choice(
            "Strategy C source-test policy", ("keep", "remove")
        )
        value = value.lower()
        if value not in ("keep", "remove"):
            raise ConfigurationError("--stale must be keep or remove")
        policy = StalePolicy(value)
        report.record_decision("Strategy C source-test policy", policy.value)
        console.info("configuration", "Strategy C source-test policy=%s" % policy.value)
        return policy
    supplied = "not supplied" if supplied_policy is None else "--stale=%s" % supplied_policy.lower()
    detail = "not applicable to Strategy %s; %s and ignored" % (strategy.value, supplied)
    report.record_decision("Strategy C source-test policy", detail)
    console.info("configuration", "stale policy %s" % detail)
    return StalePolicy.KEEP


def _group_from_env(groups, env_name, label, prompter):
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
    return prompter.choose_account_group("Select %s account group" % label, groups)


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
            "Strategy D --apply needs --confirmed-dry-run-file PATH (the %s written by a clean "
            "dry run of the same inventory). Run --dry-run first. Nothing was changed."
            % DRY_RUN_OK_FILENAME
        )
    path = Path(path_text)
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
    if not isinstance(data, dict) or data.get("strategy") != TestStrategy.DIRECT_SHARE.value:
        raise PreconditionRefused(
            "Dry-run confirmation file %s is not from a Strategy D dry run. Nothing was changed."
            % path
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


def _strategy_gate(strategy, mode, console):
    """Print strategy warnings. Block Strategy C in apply mode (exit code 2)."""
    apply_mode = mode is Mode.APPLY
    if strategy is TestStrategy.RECREATE:
        if apply_mode:
            raise PreconditionRefused(
                "Strategy C cannot keep tests in the source account group; use D. "
                "Nothing was changed."
            )
        console.info(
            "strategy",
            "WARNING: Strategy C cannot keep tests in the source account group; "
            "apply is blocked. Use D.",
        )
    elif strategy in (TestStrategy.AGENTS_ONLY, TestStrategy.SHARE):
        console.info(
            "strategy",
            "WARNING: Strategy %s shares or recreates tests differently from D. "
            "D keeps the original tests and their IDs in the source group." % strategy.value,
        )


def _lazy(resolver):
    cache = []

    def get():
        if not cache:
            cache.append(resolver())
        return cache[0]

    return get


def _make_ip_ui_factory(get_credentials):
    """Factory for Strategy D: takes the device IP, returns a TevaUiClient (context manager)."""

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
            return FlowContext(**params)
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


def _write_strategy_d_summary(
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
        "strategy": TestStrategy.DIRECT_SHARE.value,
        "source_aid": source.aid,
        "target_aid": target.aid,
        "target_aids": list(target_aids or [target.aid]),
        "snapshot_dir": str(snapshot_dir),
        "repoint_a2a": bool(repoint_a2a),
        "exit_code": exit_code,
        "results": items,
        "recap": list(getattr(results, "rows", [])),
    }
    path = report.output_dir / (report.basename + "_strategy_d.json")
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


def _compare_dry_run_binding(data, items, owner_is_source, default_target_aid=None, repoint_a2a=False):
    """Refuse (PreconditionRefused) when the fresh preflight differs from the dry run."""
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < DRY_RUN_SCHEMA:
        raise PreconditionRefused(
            "Dry-run confirmation has an old file format (schema %s, schema %s is needed): "
            "run --dry-run again. Nothing was changed." % (schema, DRY_RUN_SCHEMA)
        )
    expected = data.get("ready")
    if not isinstance(expected, list) or "excluded_ids" not in data or "owner_is_source" not in data:
        raise PreconditionRefused(
            "Dry-run confirmation has no device binding (old file format). "
            "Run --dry-run again. Nothing was changed."
        )
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
    if problems:
        shown = "; ".join(problems[:8])
        if len(problems) > 8:
            shown += "; ... (+%d more)" % (len(problems) - 8)
        raise PreconditionRefused(
            "Dry-run confirmation does not match the current state: %s. "
            "Run --dry-run again. Nothing was changed." % shown
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
    target_aids=None, repoint_a2a=False,
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
        "strategy": TestStrategy.DIRECT_SHARE.value,
        "inventory_file": str(inventory_path),
        "inventory_sha256": _sha256_file(inventory_path),
        "source_aid": source.aid,
        "target_aid": default_target_aid,
        "target_aids": sorted(set(str(aid) for aid in (target_aids or []))),
        "entries": len(agents),
        "created_utc": _utc_now_text(),
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


def _run_strategy_d(
    args, api, groups, source, target, selected_mode, agents, console, report, prompter,
    output, inventory_path, get_ui_credentials, dry_run_data=None,
):
    apply_mode = selected_mode is Mode.APPLY
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
    repoint_a2a = bool(getattr(args, "repoint_agent_to_agent", False))
    if repoint_a2a:  # passed only when set: an older FlowContext without the parameter still works
        kwargs["repoint_a2a"] = True
    report.record_decision(
        "Strategy D repoint agent-to-agent tests",
        "enabled" if repoint_a2a else "disabled (devices that a2a tests target are blocked)",
    )
    report.record_decision(
        "Strategy D auto re-enable tests", "enabled" if auto_reenable else "disabled (manual list)"
    )
    owner_is_source = bool(getattr(args, "owner_is_source", False))
    all_aids = [group.aid for group in groups]
    if apply_mode:
        # Show a fresh, read-only preflight BEFORE the operator is asked to confirm.
        # The context is a dry-run context: its API wrapper refuses every write call.
        pre_kwargs = dict(kwargs, apply=False, target_account_token="")
        pre_ctx = _build_flow_context(pre_kwargs, str(snapshot_dir))
        pre_ctx.target_names = dict(target_names)
        console.play("Strategy D: preflight before confirmation", Mode.DRY_RUN, "D")
        pre_items = run_preflight(
            ctx=pre_ctx,
            inventory_agents=agents,
            all_aids=all_aids,
            assume_owner_source=owner_is_source,
        )
        print_preflight(pre_ctx, pre_items)
        summary = preflight_summary(pre_items)
        console.info(
            "preflight",
            "ready=%s blocked=%s excluded=%s skipped=%s"
            % (summary["ready"], summary["blocked"], summary["excluded"], summary.get("skipped", 0)),
        )
        # The dry run is bound to the ready devices and their shares. Any difference refuses (exit 2).
        if dry_run_data is None:
            raise PreconditionRefused(
                "Strategy D --apply has no dry-run confirmation data. Run --dry-run first. "
                "Nothing was changed."
            )
        _compare_dry_run_binding(dry_run_data, pre_items, owner_is_source, default_target_aid, repoint_a2a)
        report.record_decision("Strategy D apply", "dry-run binding matches the fresh preflight")
        if preflight_has_blockers(pre_items):
            report.record_decision("Strategy D apply", "preflight blocked")
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
        if not prompter.confirm_destructive():
            report.record_decision("Strategy D apply", "operator declined")
            console.info("run", "operator declined; nothing was changed")
            report.write_final()
            return EXIT_FAILED
        report.record_decision("Strategy D apply", "operator confirmed")
        # Single use: no flow starts unless the dry-run file is marked as used.
        _consume_dry_run_file(args.confirmed_dry_run_file)
        console.info("gate", "dry-run confirmation marked as used (%s.used)" % args.confirmed_dry_run_file)
    ctx = _build_flow_context(kwargs, str(snapshot_dir))
    ctx.target_tokens = dict(tokens)
    ctx.target_names = dict(target_names)
    all_target_aids = _all_target_aids(agents, target)
    console.info(
        "run",
        "Strategy D: one device at a time; %s device(s) selected; %s target group(s)"
        % (len(agents), len(all_target_aids)),
    )
    try:
        results = run_strategy_d(
            ctx=ctx,
            inventory_agents=agents,
            console=console,
            all_aids=all_aids,
            assume_owner_source=owner_is_source,
        )
    except BaseException as exc:
        partial = getattr(exc, "strategy_d_results", None)
        if partial is not None and not isinstance(exc, Exception):
            # Ctrl-C or SystemExit in a flow: keep the evidence, then let the exception go on.
            code = EXIT_INTERRUPTED if isinstance(exc, KeyboardInterrupt) else EXIT_FAILED
            try:
                summary_path = _write_strategy_d_summary(
                    report, partial, selected_mode, source, target, snapshot_dir, code, all_target_aids,
                    repoint_a2a,
                )
                report.write_final()
                console.info("run", "Strategy D summary (interrupted run): %s" % summary_path)
            except Exception as write_error:
                console.info(
                    "run", "could not write the summary of the interrupted run: %s"
                    % _mask_exception(write_error),
                )
        raise
    code = _exit_code(console, results)
    clean = code == EXIT_OK
    confirmation = None
    if not apply_mode:
        confirmation = _update_dry_run_confirmation(
            output, clean and not getattr(results, "blocked", False),
            inventory_path, source, default_target_aid, agents,
            items=getattr(results, "preflight_items", None), owner_is_source=owner_is_source,
            target_aids=all_target_aids, repoint_a2a=repoint_a2a,
        )
    summary_path = _write_strategy_d_summary(
        report, results, selected_mode, source, target, snapshot_dir, code, all_target_aids, repoint_a2a
    )
    report.write_final()
    console.info("run", "Strategy D summary: %s" % summary_path)
    if confirmation is not None:
        console.info(
            "run",
            "clean dry run; apply needs --confirmed-dry-run-file %s" % confirmation,
        )
    elif not apply_mode:
        console.info("run", "dry run is not clean; %s was not written" % DRY_RUN_OK_FILENAME)
    return code


def main(argv=None):
    """Entry point. Runs with umask 077 so report folders and files are private (0700/0600)."""
    old_umask = os.umask(0o077)
    try:
        return _main(argv)
    finally:
        os.umask(old_umask)


def _main(argv=None):
    load_dotenv()
    args = build_parser().parse_args(argv)
    selected_mode = Mode.APPLY if args.apply else Mode.DRY_RUN
    inventory = Path(args.inventory)
    output = Path(args.output)
    console = MigrationConsole(verbose=args.verbose)
    console.banner()
    console.info("startup", "mode=%s; report output=%s" % (selected_mode.value, output))
    report = RunReport(output, selected_mode.value, {"inventory": str(inventory)})
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
        console.progress("global", "inventory", "reading %s" % inventory)
        agents = load_inventory(inventory)
        console.status(StepStatus.OK, "global", "inventory", "loaded %s agent(s)" % len(agents))
        strategy_value = args.strategy or prompter.required_choice(
            "Test-handling strategy", ("A", "B", "C", "D")
        )
        strategy_value = strategy_value.upper()
        if strategy_value not in ("A", "B", "C", "D"):
            raise ConfigurationError("--strategy must be A, B, C, or D")
        selected_strategy = TestStrategy(strategy_value)
        report.record_decision("Test-handling strategy", selected_strategy.value)
        is_strategy_d = selected_strategy is TestStrategy.DIRECT_SHARE
        apply_mode = selected_mode is Mode.APPLY
        _strategy_gate(selected_strategy, selected_mode, console)
        if getattr(args, "auto_reenable_tests", False) and not is_strategy_d:
            console.info("configuration", "--auto-reenable-tests applies only to Strategy D; ignored")
        if getattr(args, "repoint_agent_to_agent", False) and not is_strategy_d:
            console.info("configuration", "--repoint-agent-to-agent applies only to Strategy D; ignored")
        dry_run_data = None
        if is_strategy_d and apply_mode:
            dry_run_data = _load_dry_run_confirmation(args.confirmed_dry_run_file, inventory)
            console.info(
                "gate",
                "dry-run confirmation accepted (created %s)" % dry_run_data.get("created_utc", "?"),
            )
        elif is_strategy_d and args.confirmed_dry_run_file:
            console.info("gate", "--confirmed-dry-run-file is only used with --apply; ignored")
        parallelism = args.parallelism
        if is_strategy_d and parallelism > 1:
            console.info(
                "configuration",
                "Strategy D runs one device at a time; --parallel %s forced to 1" % parallelism,
            )
            parallelism = 1
        selected_stale_policy = _resolve_source_test_policy(
            selected_strategy, args.stale, report, prompter, console
        )
        strategy_c = selected_strategy is TestStrategy.RECREATE
        create_tags = strategy_c and args.create_missing_tags
        create_alerts = strategy_c and args.create_missing_alerts
        report.record_decision(
            "Strategy C create missing tags",
            "enabled" if create_tags else "disabled" if strategy_c else "not-applicable",
        )
        report.record_decision(
            "Strategy C create missing alert rules",
            "enabled" if create_alerts else "disabled" if strategy_c else "not-applicable",
        )
        if is_strategy_d and not apply_mode:
            # The agent UI credentials are asked only if a flow really opens the UI.
            get_ui_credentials = _lazy(lambda: _resolve_agent_ui_inputs(report, prompter, console))
        else:
            credentials = _resolve_agent_ui_inputs(report, prompter, console)
            get_ui_credentials = lambda: credentials
        api_token = os.getenv("TE_OAUTH_TOKEN") or os.getenv("THOUSANDEYES_OAUTH_TOKEN")
        if api_token:
            report.record_decision("API v7 OAuth bearer token", "<environment>")
            console.info("input", "API OAuth token loaded from environment")
        else:
            api_token = prompter.secret("API v7 OAuth bearer token")
        options = RunOptions(
            mode=selected_mode,
            strategy=selected_strategy,
            stale_policy=selected_stale_policy,
            timeout_seconds=args.timeout if args.timeout is not None else DEFAULT_LEGACY_TIMEOUT,
            parallelism=parallelism,
            create_missing_tags=create_tags,
            create_missing_alerts=create_alerts,
            verbose=args.verbose,
        )
        with ThousandEyesApiClient(api_token) as api:
            console.progress("global", "api_account_groups", "connecting to ThousandEyes API v7")
            groups = api.list_account_groups()
            console.status(
                StepStatus.OK,
                "global",
                "api_account_groups",
                "OAuth token can access %s account group(s)" % len(groups),
            )
            # Strategy D: an unknown row target stops the run before any prompt or flow.
            if is_strategy_d:
                _check_inventory_targets(agents, groups)
            source = _group_from_env(groups, "TE_SOURCE_AID", "source", prompter)
            own_targets = any(agent.target_aid for agent in agents)
            if own_targets and not is_strategy_d:
                console.info(
                    "configuration", "target_aid and share_aids columns apply only to Strategy D; ignored"
                )
            # Strategy D: when every row names its target, the run needs no target of its own.
            needs_target = (not is_strategy_d) or _needs_run_target(agents)
            if needs_target:
                target = _group_from_env(groups, "TE_TARGET_AID", "target", prompter)
            else:
                # Every row has its own target. The run target is the first one (for the context only).
                target = next(group for group in groups if group.aid == agents[0].target_aid)
            all_target_aids = _all_target_aids(agents, target) if is_strategy_d else [target.aid]
            if source.aid in all_target_aids:
                raise ConfigurationError(
                    "Source and target account groups must be different (source %s is also a target)"
                    % source.aid
                )
            if dry_run_data is not None:
                _check_dry_run_groups(dry_run_data, source, all_target_aids)
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
            if is_strategy_d:
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
                    console.destination_banner(
                        source, target, selected_mode, destructive=apply_mode
                    )
                code = _run_strategy_d(
                    args, api, groups, source, target, selected_mode, agents, console,
                    report, prompter, output, inventory, get_ui_credentials,
                    dry_run_data=dry_run_data,
                )
                print("JSON report: %s" % report.json_path)
                print("CSV reports: %s" % report.csv_dir)
                return code
            console.destination_banner(source, target, selected_mode)
            target_token = _resolve_target_account_token(target, report, prompter, console)
            runner = MigrationRunner(
                api=api,
                ui_factory=lambda item: TevaUiClient(
                    item.ip_address, get_ui_credentials()[0], get_ui_credentials()[1]
                ),
                report=report,
                prompter=prompter,
                console=console,
                options=options,
                source_group=source,
                target_group=target,
                target_account_token=target_token,
            )
            console.info(
                "run",
                "starting batched processing for %s agent(s); parallel connections=%s"
                % (len(agents), parallelism),
            )
            runner.run(agents)
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
    console.info("run", "completed; final audit artifacts were written")
    print("JSON report: %s" % report.json_path)
    print("CSV reports: %s" % report.csv_dir)
    return EXIT_FAILED if console.has_failures() else EXIT_OK
