# Operations Runbook

This runbook is for the operator.
Run every command from the project root with the virtual environment active.

Related documents: [Plan](MIGRATION_PLAN.md), [Reports](REPORTS.md).

All examples use synthetic names:
Group A (aid 11, source), Group B (aid 22, target), Group C (aid 33),
agent IDs 100 and 200, IP addresses 192.0.2.x.

Environment variables are in [section 9](#9-reference).

```
 1 Discover --> 2 Scope --> 3 Edit map --> 4 Report --> 5 Dry run --> 6 Apply
     read        read         you            read          read        WRITES
```

## 1. Discover

The script reads the API. It writes nothing to ThousandEyes.

```bash
./tools/te_discovery.sh                  # writes work/discovery
```

The script asks for the API token (hidden), or reads `TE_TOKEN`.
It needs `curl`, `jq` and `awk`.

- It skips files that exist. A second run downloads only what is missing.
- Set `FORCE=1` to download everything again.
- It writes `failures.log`. Read it. HTTP 403 on built-in dashboards is normal.

After step 2, run discovery again with the inventory.
Then the script also reads the detail of each device in the inventory:

```bash
INVENTORY_CSV=work/input/inventory.csv ./tools/te_discovery.sh
```

WARNING: Discovery files can hold secrets (transaction scripts).
Do not commit or share them. The folder `work/` is git-ignored.

## 2. Build the scope

Put the Excel file in `work/input/`. Never commit it.

```bash
python -m te_agent_migrate.scope --xlsx work/input/scope.xlsx --out work/input
```

The command writes three files:

| File | Content |
|---|---|
| `prefix_map.csv` | One row for each hostname prefix. You edit it. Status `OK` or `REVIEW` |
| `inventory.csv` | Devices with status READY. Inventory v3 format |
| `scope_report.csv` | Every device and why it is in or out |

Exit code 3 means that at least one prefix needs review.

The scope rules:

- The hostname prefix gives the target group.
- Column H of the sheet "Summary" gives the groups that see the device.
- The target group must be in that list.
- Offline devices and devices with login problems are not READY.

TODO: Document the columns of `prefix_map.csv` and `scope_report.csv`.
TODO: Confirm how the scope module reads the edited `prefix_map.csv`.

## 3. Edit the prefix map

1. Open `work/input/prefix_map.csv`.
2. Check each prefix against the target group.
3. For each row with status `REVIEW`, find the cause.
4. Set the status to `OK` only when the target is correct.
5. Run step 2 again. The exit code must be 0.

WARNING: A wrong prefix sends a device to the wrong group.
Check the first rows of every prefix in `inventory.csv`.

Handle a device whose target is not in column H by hand.
Do not move it until you know why.

## 4. Make the report

The report reads only local files. It calls no API.
The selection file lists one device name, IP or agent ID on each line.

```bash
tail -n +2 work/input/inventory.csv | cut -d, -f2 > work/input/selection.txt
python -m te_report --input work/discovery \
  --selection work/input/selection.txt \
  --output work/reports/wave1 --html
```

This `cut` command works only for names without commas.

Add `--xlsx` for an Excel file. It needs `openpyxl`.
Read the result with [Reports](REPORTS.md).

## 5. Dry run

The dry run reads the API. It sends no write call. It opens no agent UI.
It does not ask for a registration token.

```bash
export TE_OAUTH_TOKEN=...            # or enter it at the hidden prompt
export TE_SOURCE_AID=11              # or choose the group at the prompt
python -m te_agent_migrate --strategy D --owner-is-source \
  --inventory work/input/inventory.csv --output work/runs/wave1
```

`--owner-is-source` states that the source group owns every device.
The API has no owner field, so the preflight blocks all devices without it.

Use `-v` for more output. Use `--timeout SECONDS` to change the wait limit
(default 600 seconds).

A clean dry run writes `work/runs/wave1/dry-run-ok.json`.
A dry run with a blocked device exits with 1 and writes no such file.
Excluded and skipped devices do not make a dry run unclean.

### Read the dry run

The output has these parts:

```
 banner            source and target groups, mode
 PLAY / TASK       preflight: one line for each device
 [info] lines      owner, shares, warnings
 planned steps     changed: [device] => would: ...
 PLAY RECAP        counts for each device
 Run summary       one line for each device
```

Example (synthetic):

```
TASK [Preflight] ***************************************************
ok:       [AAAA - HOST01] => agent, tests=12, shares=3
  [info] AAAA - HOST01: owner=11 shares=[11, 22, 33]
skipped:  [AAAA - HOST02] => excluded (offline): agent is offline [...]
failed:   [BBBB - HOST01] => blocked: name 'BBBB - HOST01' is already used ...
```

The status words:

| Word | Meaning |
|---|---|
| `ok` | The check passed, or a read step is done |
| `changed` | A write step. In dry run: `would: ...`, not sent |
| `skipped` | Not touched (offline, already migrated, manual step) |
| `failed` | A check or step failed. The exit code is 1 |
| `paused` | The tool waits for you |

Check these before apply:

1. The `ready` count equals the number of devices you expect.
2. Every excluded device is offline on purpose.
3. Every `shares=` list is complete.
4. Every `warning:` line is understood.
5. No device shows `failed`.

## 6. Apply

WARNING: Apply resets real agents. A reset cannot be undone.
Make sure the wave plan and the post-wave checklist are ready.

1. Do not edit `inventory.csv` after the dry run.
   The dry-run file stores a hash of the inventory.
2. Set the credentials:

   ```bash
   export TE_AGENT_UI_USERNAME=...    # or enter at the prompt
   export TE_AGENT_UI_PASSWORD=...    # or enter at the hidden prompt
   export TE_TARGET_TOKEN_22=...      # one variable for each target group
   ```

3. Start the tool:

   ```bash
   python -m te_agent_migrate --strategy D --owner-is-source --apply \
     --inventory work/input/inventory.csv --output work/runs/wave1 \
     --confirmed-dry-run-file work/runs/wave1/dry-run-ok.json
   ```

4. Read the preflight that the tool prints. It runs again and reads only.
   The tool compares it with the dry-run file.
   Any difference stops the run (exit code 2).
   A blocked device also stops the run (exit code 1).
5. Enter the missing registration tokens at the hidden prompts.
   The tool collects all tokens before the first reset.
6. Type `y` to start. Any other answer stops the run. Nothing changes.

After you type `y`, the tool does these actions:

- It renames `dry-run-ok.json` to `dry-run-ok.json.used`. The file is single use.
- It moves the devices one at a time.

Do not press Ctrl-C during a device. The device can be in an unknown state.
If you must, read the state line (see [section 8](#8-failures)).

### Several target groups in one run

Each inventory row can name its own `target_aid`.
Then you need one registration token for each target group.
The tool groups the devices by target and prints one PLAY for each target.

Token sources, in this order:

1. `TE_TARGET_TOKEN_<aid>` (example: `TE_TARGET_TOKEN_22`).
2. The pair `TE_TARGET_ACCOUNT_TOKEN` and `TE_TARGET_ACCOUNT_TOKEN_AID`.
3. A hidden prompt.

A token has 32 letters or digits. The tool removes spaces and hidden characters.

## 7. Read the output

### Recap

```
PLAY RECAP *********************************************************
AAAA - HOST01 : ok=9    changed=5    skipped=1    failed=0

Run summary
------------------------------------------------------------------------
AAAA - HOST01 | ok | migrated: old agent 100 -> new agent 200
AAAA - HOST02 | skipped | excluded (offline), not touched: agent is offline
```

- `ok` in the summary means the whole flow is done.
- `skipped` means the device was not touched.
- A device after a failure shows `skipped: not started: batch stopped`.

### Files

All files are in `--output`.

| File | Content |
|---|---|
| `dry-run-ok.json` | Dry-run proof, schema 3 (see below) |
| `dry-run-ok.json.used` | The proof after apply starts |
| `snapshots/<UTC time>_<name>.json` | State before the reset (mode 0600) |
| `te_agent_migration_<mode>_<UTC time>.json` | Audit report (redacted) |
| `te_agent_migration_<mode>_<UTC time>_csv/` | The same data as CSV files |
| `te_agent_migration_<mode>_<UTC time>_strategy_d.json` | Summary for each device |

`<mode>` is `dryrun` or `apply`.

The summary JSON has these fields:
`mode`, `source_aid`, `target_aids`, `exit_code`, `snapshot_dir`,
`results` (name, ok, detail, new_agent_id, warnings, snapshot_path) and `recap`.

Use `new_agent_id` for the post-wave checklist.
Use `target_aids` for runs with several targets.

The `dry-run-ok.json` file holds:
the hash of the inventory, the source group, the ready devices
(ID, target, current shares, final share list, owner flag),
the excluded IDs, the skipped IDs and the flag `--owner-is-source`.

WARNING: Snapshots and reports hold customer IDs. Do not commit them.

## 8. Failures

The tool stops at the first failure. Read the last lines of the output.

### Before any change

The exit code is 1 or 2. Nothing changed. Fix the cause. Run again.

| Message or cause | Action |
|---|---|
| Exit 2: no, old or stale dry-run file | Run the dry run again |
| Exit 2: inventory changed after the dry run | Run the dry run again |
| Exit 2: dry run does not match the current state | Read the differences. Run the dry run again |
| Exit 2: Strategy C in apply mode | Use `--strategy D` |
| Token must be exactly 32 alphanumeric characters | Copy the token again |
| `TE_TARGET_ACCOUNT_TOKEN_AID` missing or not equal to the target | Set the variable. Use `TE_TARGET_TOKEN_<aid>` instead |
| Target group not accessible to the OAuth token | Use a token that sees the group |
| Inventory header or row error | Fix the file. The message gives the line |
| Operator declined | Nothing to do |

### Blocked device (preflight)

The run does not start while one device is blocked.

| Reason | Action |
|---|---|
| No agent matches IP and name | Fix the inventory row. Run scope again |
| Name already used in the target group | Find the duplicate agent. Resolve it |
| Agent already in the target group | Check if the device moved. Unshare it first if needed |
| Owner not proven | Use `--owner-is-source` only if the source owns the device |
| Sharing sources disagree | Run discovery again. Check the device in the UI |
| Shared group cannot be read | Check the token access to that group |
| Agent-to-agent test uses the device as target | See below |
| Duplicate selection | Remove the duplicate row |

An agent-to-agent test that targets the device blocks the move.
The new agent ID is not known before the move.
TODO: Decide the procedure (change the target before the move, then set it again).
Until then, leave these devices out of the wave.

### During a device flow

The tool prints `stopped; state: ...`, the old and new agent IDs,
the final share list and the snapshot file.
Find the state in [the recovery table](MIGRATION_PLAN.md#11-rollback-and-recovery).

| Failed step | Typical cause | Action |
|---|---|---|
| `ui_authenticate` | Wrong UI password, port 443 blocked | Fix it. Nothing changed. Run again |
| `ui_snapshot_before` | UI error | Nothing changed. Run again |
| `ui_reset` | UI error | Check the agent UI. The reset may have run |
| `ui_reachable_after_reset` | The agent does not answer | Wait. Check the network. Enter the token by hand |
| `ui_set_target_token` | Token rejected | Enter the token in the agent UI by hand |
| `api_target_online` | Timeout, two candidates, wrong group | Look in the target and source groups. Do the next steps by hand |
| `api_share_back` | API error, share not visible | Share the new agent by hand. The old agent is still there |
| `api_assign_tests` | Some tests not assigned | Assign the missing tests. The old agent is still there |
| `api_delete_old` | API error | Check if the old agent exists. Delete it by hand |
| `api_rename` | Name already used | Rename the agent by hand |
| `api_restore_tags` | Tag error (warning only) | Assign the tags by hand. Use the snapshot |

A `429` answer on a write call is repeated. The wait is limited to 60 seconds.

For Ctrl-C the tool writes the recap and the summary. The exit code is 130.

WARNING: After any stop in the middle of a device, do not run the tool again
for that device. Fix it by hand first. A second run can see a half-moved device.

## 9. Reference

### Environment variables

| Variable | Use |
|---|---|
| `TE_OAUTH_TOKEN` | API v7 token. Alternate: `THOUSANDEYES_OAUTH_TOKEN`. Hidden prompt if not set |
| `TE_AGENT_UI_USERNAME` | Agent UI user. Prompt if not set (apply only) |
| `TE_AGENT_UI_PASSWORD` | Agent UI password. Hidden prompt if not set (apply only) |
| `TE_SOURCE_AID` | Source group ID. Group list prompt if not set |
| `TE_TARGET_AID` | Target group for rows without `target_aid` |
| `TE_TARGET_TOKEN_<aid>` | Registration token of target group `<aid>` |
| `TE_TARGET_ACCOUNT_TOKEN` | Token for one target. Needs the next variable |
| `TE_TARGET_ACCOUNT_TOKEN_AID` | Group ID of that token |
| `TE_TOKEN` | API token for `te_discovery.sh` only |
| `INVENTORY_CSV` | Discovery: also read these devices |
| `DASHBOARD_AIDS` | Discovery: limit dashboard details to these groups |
| `FORCE` | Discovery: `1` downloads again |
| `NO_COLOR` | Turns off colors |

The tool loads `.env` from the project folder.
The tool never prints a token or password.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean run |
| 1 | Failure, blocker, or operator declined |
| 2 | A safety gate refused. Nothing changed |
| 3 | Scope only: a prefix needs review |
| 130 | Ctrl-C |

### Inventory v3

```
type,name,ip_address,agent_id,target_aid,share_aids
agent,AAAA - HOST01,192.0.2.11,100,22,11;22;33
```

- The first three columns are required.
- `agent_id`, `target_aid` and `share_aids` are optional. Keep this order.
- IDs have digits only. `share_aids` uses `;` between IDs.
- `share_aids` must contain `target_aid`.
  It states that the device is shared with the target already.
- Names and IPs must be unique.
- The old headers `hostname,ip_address` and `type,name,ip_address` still work.

### Command flags

| Flag | Use |
|---|---|
| `--strategy D` | Production strategy. Always set it |
| `--inventory FILE` | Default `work/input/inventory.csv` |
| `--owner-is-source` | The source group owns every device |
| `--apply` | Send the writes |
| `--confirmed-dry-run-file FILE` | Proof of a clean dry run (needed for apply) |
| `--output DIR` | Default `work/runs` |
| `--timeout SECONDS` | Wait limit for each wait step |
| `--auto-reenable-tests` | Clusters only. Out of scope |
| `--parallel N` | Ignored. Strategy D runs one device at a time |
| `-v` | More output |
