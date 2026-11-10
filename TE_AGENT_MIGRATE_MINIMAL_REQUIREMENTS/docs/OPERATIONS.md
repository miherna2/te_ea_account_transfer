# Operations Runbook

Run migration commands from `TE_AGENT_MIGRATE_MINIMAL_REQUIREMENTS`. A virtual environment is optional;
the minimal distribution supports dependencies installed in its `libs` folder. See the
[setup guide](../README.md#setup-once). On Windows use `python` or `py -3` in place of `python3`.

## 1. Prepare the inventory and scope

Start with one approved standalone device. The default file is `work/input/inventory.csv`:

```csv
name,ip_address,target
AAAA - HOST01,192.0.2.11,22
```

`target` is optional and accepts a group ID or exact group name. If omitted, an approved `OK` entry in
`work/input/prefix_map.csv` supplies the destination; otherwise the tool asks once for the run destination.
A standalone needs both name and IP. Explicit `agent_id` and `type` columns remain supported. A row without
an IP is treated as a cluster lookup; clusters are outside the current production scope.

If the inventory does not exist, an interactive run asks for `name ip` lines and saves them. Do not put
passwords or tokens in an inventory: secret-looking columns are refused. Confirm source ownership and
all destinations with the account owners; a source group that sees an agent may only be a sharing group.

For a workbook-based scope, this optional local command writes `prefix_map.csv`, `inventory.csv`, and
`scope_report.csv`:

```bash
python3 -m te_agent_migrate.scope --xlsx work/input/scope.xlsx --out work/input
```

Review excluded/login-problem devices, `REVIEW` prefixes, and targets missing from the workbook's group
list. Resolve them explicitly before adding a device to a wave. The generated inventory and map are
outputs: keep any approved manual corrections when regenerating them.

## 2. Inspect impacts before the preview

Optional discovery reads ThousandEyes configuration and writes local files. It requires `curl`, `jq`, and
`awk`; on Windows run it using Git Bash or WSL:

```bash
INVENTORY_CSV=work/input/inventory.csv ./tools/te_discovery.sh work/discovery
```

The script prompts for its token or uses `TE_TOKEN`. Inspect `work/discovery/failures.log`; missing group
data is not evidence that no dependencies exist. Reused discovery files can be stale. Discovery may contain
transaction scripts and customer data; do not commit or share the folder.

For a local impact report, create a selection file with one selected device name per line, then run:

```bash
python3 -m te_report --input work/discovery --selection selection.txt --output work/reports/wave1 --html
```

This report calls no API. It identifies agent-ID dashboards, notification rules, and other manual work;
it does not replace live migration preflight or prove manual follow-up is complete.
Missing or unreadable required agent/test datasets produce INCOMPLETE warnings in usable partial outputs
and a nonzero report exit. With no usable required dataset, no report is written. Do not interpret partial
outputs or optional-data warnings as complete coverage.

## 3. Preview

```bash
python3 -m te_agent_migrate
```

For a different inventory, add `--inventory FILE` to both preview and apply. The CLI prompts for the API
token and group choices unless exported settings provide them. It checks the inventory against live
agent identities, discovers sharing and tests, and prints READY/BLOCKED/EXCLUDED/SKIPPED results. It does
not request destination registration tokens or mutate appliance/ThousandEyes configuration. It can probe
TCP 443 and writes local reports under `work/dry-run`; a new preview replaces that folder's prior output.

Read the complete sharing list, per-group test counts, target dependencies, exclusions, warnings, and
source/destination IDs. A Voice test targeting the device blocks automatic migration for manual handling.
A test pinned to an explicit source interface on the selected old agent/member also blocks automatic
migration: normal assignment does not preserve its per-agent `sourceIpAddress`. Handle it through a
qualified manual procedure. A source group's visibility alone does not establish ownership.
Compare discovered groups with the known sharing list: groups hidden from both the token and returned API
metadata cannot be proven dependency-free by an empty scan.

## 4. Apply

```bash
python3 -m te_agent_migrate --apply
```

The tool inspects matching interrupted standalone migrations first. Completing them has its own explicit
confirmation and may change those devices before normal new-migration preflight. It then shows a fresh
preflight for new migrations, compares any usable earlier preview, collects destination registration
tokens, asks you to confirm source ownership, checks the agent UIs, and asks
`Begin destructive migration actions? (y/n)`.

The default workflow can proceed from that fresh confirmed plan if the earlier preview is missing or
changed; it reports the difference. For a mandatory matching saved preview, use the strict option below.
All required group tokens must be available before the first new-migration reset. A token format check
cannot prove that it belongs to the intended group; wrong-group registration detection stops the flow.

**Apply resets real appliances; a reset cannot be undone.** Allow for reset and registration downtime.
Do not close the terminal or interrupt a device deliberately. Devices run one at a time; a failure stops
the batch. Reports go to `work/apply/<date-time>`. Keep the reported old/new IDs and snapshot paths.

### Strict preview compatibility

`--output DIR` chooses a report folder without replacing the standard preview directory. An explicitly
supplied `--confirmed-dry-run-file FILE` requires a usable clean preview for the same inventory, groups,
and current plan before writes. A missing, invalid, used, or mismatched explicit proof refuses apply.
A matching proof is single-use and is marked `.used` when apply starts.

```bash
python3 -m te_agent_migrate --inventory work/input/inventory.csv --output work/preview-one
python3 -m te_agent_migrate --apply --inventory work/input/inventory.csv --confirmed-dry-run-file work/preview-one/dry-run-ok.json
```

Use the same inventory and source/destination choices for both commands. Strict proof does not remove
ownership, recovery, or destructive-action confirmations and does not prove a registration token's group.
For drift, generate a fresh preview and use its proof. For an interrupted move, use the ordinary `--apply`
recovery workflow instead of trying to reuse the obsolete strict proof from before the partial move.

## 5. Verify each completed device

Compare the snapshot and live UI for every required account group:

- The identified replacement is online in the intended owner group, with the expected name and IP.
- Source and all dependent sharing groups see it; no existing replacement share was lost during recovery.
- Every expected test ID is assigned in its original group. Saved enabled/disabled states are preserved.
- Supported agent-to-agent targets use the replacement; blocked Voice dependencies are handled manually.
- The old agent is gone only after the replacement checks pass. Name and tags are restored afterward.
- Agent notification rules and dashboards filtering old agent IDs have their manual follow-up completed.

A rename failure after deletion returns failure. Tag-restoration failures remain nonfatal warnings and can
accompany a successful exit; inspect the warnings and finish those tags manually. Successful replacement
alone is not complete metadata restoration. Retain snapshots until all checks are complete.

## 6. If a run stops

A timeout can occur after the API applied a request. Do not infer that its assignments were rejected.
The current assignment flow sends batches of at most 10, rereads assignments, and retries missing tests
for up to three rounds. It verifies every group before old-agent deletion. Standalone enabled-state
regressions require manual correction; the tool does not send a full test update to re-enable them.

Run the same preview again with the same current inventory and source/destination choices. Integrated
recovery reads snapshots under `work/apply` and older `work/runs`; a preview reports its findings without
writes. Apply offers a separate confirmation to finish a safely identified interrupted standalone move.
It requires a matching snapshot, current selection, and one online replacement with sufficient device
identity. An IP match alone, an online old agent, ambiguous replacements, conflicting ownership, or missing
critical test state does not authorize deletion. Recovery preserves live shares and adds missing tests.
Legacy snapshots with no saved enabled-state baseline produce a manual-verification warning; compare
those states with the operator's before-state because current data cannot establish the original values.
An explicit source-interface binding on the old agent also refuses automatic recovery when readable.
If an old snapshot omitted that binding and the old record is gone, inspect the operator's before-state;
the tool cannot reconstruct the omitted setting from agent-level test membership.

If automatic recovery refuses or lacks the snapshot, use the printed state and a manual procedure:
prove replacement identity and ownership; restore/verify every share, test, enabled state, and target
dependency; then delete only the confirmed offline old record and finish name/tags. Do not reset the
appliance again to clear a partial state. Interrupted cluster moves require a manual procedure.

For a failure after deletion, the old record cannot be restored by rerunning. Recovery must finish the
replacement's metadata. Never delete snapshots or completion state merely to bypass a refusal.

## 7. Files, options, and compatibility

| Item | Location or behavior |
|---|---|
| Latest preview | `work/dry-run/` |
| Apply reports and pre-reset snapshots | `work/apply/<date-time>/`, including `snapshots/*.json` |
| Older snapshots inspected for recovery | `work/runs/**/snapshots/` |
| Completed replacements | `work/state/migrated.json`; prevents a new reset of a completed replacement |
| Audit output | JSON report, CSV tables, and per-device summary printed at the end |
| `--inventory FILE` | Select another inventory |
| `--timeout SECONDS` | Wait limit; not a guarantee a timed-out request did not apply |
| `-v` | More output |
| `--auto-reenable-tests` | Explicit cluster-only full test update; masked secrets may make it unsafe |
| Legacy flags | `--dry-run`, `--owner-is-source`, `--repoint-agent-to-agent` are accepted but unnecessary |

Migration exit 0 can still include nonfatal tag-restoration warnings. Exit 1 means failure, blocker, or an
operator decline; exit 2 means a refused precondition; exit 130 means interruption. The impact report returns
nonzero for missing/invalid required discovery data or output failures. Scope can return 3 for
prefixes needing review. Inspect the reported state to learn whether changes already occurred.

Prompts require no environment preparation and `.env` is not loaded automatically. Existing exported
variables remain supported:

| Variable | Use |
|---|---|
| `TE_OAUTH_TOKEN` or `THOUSANDEYES_OAUTH_TOKEN` | API token; exported invalid input fails rather than opening the prompted retry loop |
| `TE_SOURCE_AID` | Source group ID |
| `TE_TARGET_AID` | Default target for rows without a resolved destination |
| `TE_AGENT_UI_USERNAME`, `TE_AGENT_UI_PASSWORD` | Appliance UI credentials |
| `TE_TARGET_TOKEN_<aid>` | Registration token for the named destination |
| `TE_TARGET_ACCOUNT_TOKEN` with `TE_TARGET_ACCOUNT_TOKEN_AID` | Compatibility pair for one destination |
| `TE_TOKEN` | Discovery token only |

Check for leftover exported settings when group choices or credentials differ from the expected prompts.
The API uses certificate verification; appliance UI requests currently disable certificate verification
(`verify=False`). Reports and snapshots contain customer identifiers and are private local runtime data;
keep credentials out of files and do not commit runtime folders.
