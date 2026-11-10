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
an IP is treated as a cluster lookup. Explicit and automatically detected clusters are refused.

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
When an agent list omits assignment fields, the tool requests expanded test IDs from that agent's
detail endpoint in the same group. Only a matching ID and an explicit valid list establish its assignments.
Unresolved or malformed assignment data remains a blocker, not proof of zero tests.

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
The API token also needs source-group agent and tag-management permissions for retirement.

**Apply resets real appliances; a reset cannot be undone.** Allow for reset and registration downtime.
Do not close the terminal or interrupt a device deliberately. Devices run one at a time; a failure stops
the batch. Reports go to `work/apply/<date-time>`. Keep the reported old/new IDs and snapshot paths.
The tool checks the approved plan again after confirmation and before each device starts.
Changed assignments, enabled states, shares or target dependencies stop the affected migration.

Keep sharing, tests and target settings unchanged for the full move. Immediately before disabling the old
registration, the tool rechecks replacement identity, online status and explicit API `enabled=true`,
all saved/current shares and assignments,
target dependencies, and saved test enabled states. Missing or unreadable data stops retirement.
An old registration still reporting online is not itself a blocker. The tool disables it through the API,
verifies `enabled=false`, and ensures the tag `migration-status=MIGRATED-TO-BE-DELETED` is assigned.
It then renames the disabled, marked old record to `MIGRATED-TO-BE-DELETED-<old-id>` and verifies that name.
This frees the original full name for the active replacement; the retained old record still exists.
Other existing tags and shares are preserved. No old registration is deleted or force-deleted.
If disablement, tag or archived-name verification fails, execution stops with the checkpoint intact. Resolve the failure
and resume; do not reset the appliance again.
The final reads and writes are not atomic; concurrent configuration changes remain a risk.

### Strict preview compatibility

`--output DIR` chooses a report folder without replacing the standard preview directory. An explicitly
supplied `--confirmed-dry-run-file FILE` requires a usable clean preview for the same inventory, groups,
and current plan before writes. A missing, invalid, used, or mismatched explicit proof refuses apply.
A matching proof is single-use and is marked `.used` when apply starts.
Create a new preview after an upgrade. Schema 8 binds retirement and old-name archiving, per-group test IDs
and enabled states. Schema 7 approvals, schema 6 deletion-era approvals and older proofs are rejected.

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

- The identified replacement is online with explicit API `enabled=true` in the intended owner group,
  with the expected name and IP.
- Source and all dependent sharing groups see it; no existing replacement share was lost during recovery.
- Every expected test ID is assigned in its original group. Saved enabled/disabled states are preserved.
- Supported agent-to-agent targets use the replacement; blocked Voice dependencies are handled manually.
- The old registration remains with `enabled=false` and `migration-status=MIGRATED-TO-BE-DELETED`;
  its name is `MIGRATED-TO-BE-DELETED-<old-id>`. Its other tags and shares remain unchanged.
  The replacement's original full name and tags are restored afterward.
- There is one active replacement and one disabled marked old record, not one total registration.
- Agent notification rules and dashboards filtering old agent IDs have their manual follow-up completed.

A rename failure after retirement returns failure. Replacement tag-restoration failures remain nonfatal
warnings; inspect them and finish those tags manually. Old-record disablement and its migration-status tag
must be verified, not treated as optional metadata. Retain snapshots until all checks are complete.

## 6. If a run stops

A timeout can occur after the API applied a request. Do not infer that its assignments were rejected.
The current assignment flow sends batches of at most 10, rereads assignments, and retries missing tests
for up to three rounds. It verifies every group before old-agent disablement. Standalone enabled-state
regressions require manual correction; the tool does not send a full test update to re-enable them.

For old-agent disablement or either rename, a transport error or HTTP 5xx can leave the result
uncertain. The tool uses bounded GET verification without repeating the PUT. Verified `enabled=false`
or the exact requested name allows it to continue; an unverified result or a known HTTP 4xx stops the run.
Keep the checkpoint and resolve the failure without another appliance reset.

Run the same preview again with the same current inventory and source/destination choices. Integrated
recovery reads snapshots under `work/apply` and older `work/runs`. For a custom apply output, rerun with
the same `--output DIR` so recovery also reads `DIR/snapshots`. A preview reports findings without writes.
Apply offers a separate confirmation to finish a safely identified interrupted standalone move.
Use ordinary `--apply` recovery, without the old `--confirmed-dry-run-file` or `.used` approval.
It requires a matching snapshot, current selection, and one online replacement with explicit API
`enabled=true` and sufficient device
identity. An IP match alone, ambiguous replacements, conflicting ownership, unreadable identity/dependencies,
or missing critical test state does not authorize retirement. The old record reporting online is not itself
a blocker. Recovery preserves live shares and adds missing tests before disabling and marking the old record.
It verifies the archived old name before restoring the replacement's original full name. Repeating recovery
reuses the same records and completes only missing work, without another reset.
After verified registration, the snapshot is updated atomically with the replacement ID, destination and
observed identity. This checkpoint permits recovery of a temporary name without weakening identity checks.
Without that checkpoint, the replacement must match the saved name or hostname; a suffix alone is not enough.
Trusted checkpoint evidence also permits recovery of the archived old record when its hostname is absent;
an archived name or matching IP alone does not authorize recovery.
Legacy snapshots with no saved enabled-state baseline produce a manual-verification warning; compare
those states with the operator's before-state because current data cannot establish the original values.
An explicit source-interface binding on the old agent also refuses automatic recovery when readable.
If an old snapshot omitted that binding and the old record is gone, inspect the operator's before-state;
the tool cannot reconstruct the omitted setting from agent-level test membership.

If automatic recovery refuses or lacks trusted saved evidence, stop and retain the printed state.
Do not guess identities or bypass the refusal. With a trusted snapshot/checkpoint and an approved manual
procedure, prove replacement identity and ownership; restore/verify every share, test, enabled state, and target
dependency; then disable the confirmed old registration, verify `enabled=false`, ensure
`migration-status=MIGRATED-TO-BE-DELETED`, rename old to `MIGRATED-TO-BE-DELETED-<old-id>` and verify it,
then finish the replacement's original full name/tags.
Preserve other tags and shares; never delete or force-delete the old record. Do not reset the
appliance again to clear a partial state. Interrupted legacy cluster moves require a manual procedure;
production cluster migration is refused and is not qualified.

For a failure after disablement, marking or old-name archiving, recovery uses the trusted snapshot/replacement checkpoint to
finish retirement and metadata without another reset. If a legacy run already deleted the old record,
rerunning cannot restore it. Never delete snapshots or completion state merely to bypass a refusal.

The JSON execution report is saved after each step start/result and retry message, including recovery.
It is redacted and atomically replaced. An interrupted incremental report is not a completion record.
If the report or checkpoint cannot be saved, execution stops; restore local write access and inspect the
saved state before resuming. Keep the console transcript if local storage is unavailable.

## 7. Files, options, and compatibility

| Item | Location or behavior |
|---|---|
| Latest preview | `work/dry-run/` |
| Apply reports and pre-reset snapshots | `work/apply/<date-time>/`, including `snapshots/*.json` |
| Older snapshots inspected for recovery | `work/runs/**/snapshots/` |
| Custom apply snapshots | Supply the same `--output DIR` again; recovery also reads `DIR/snapshots/` |
| Completed replacements | `work/state/migrated.json`; prevents a new reset of a completed replacement |
| Audit output | Incremental JSON steps/retries/recovery; final CSV tables and per-device summary |
| `--inventory FILE` | Select another inventory |
| `--timeout SECONDS` | Wait limit; not a guarantee a timed-out request did not apply |
| `-v` | More output |
| `--auto-reenable-tests` | Legacy option; no standalone state repair and no permission to migrate clusters |
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
