# Migration Plan

Status: working draft. About 70% complete.
Lines that start with `TODO:` show missing information.

Related documents:
[Runbook](OPERATIONS.md), [Reports](REPORTS.md),
[Lab](LAB.md), [Progress](PROGRESS.md).

## 1. Goal

Move Enterprise Agents (TEVA) from one source account group
to their target account groups.

The tests stay in the source account group. They keep their IDs.
Dashboards and alert rules must keep working.

## 2. Scope

The Excel file defines the scope. It is never committed.

```
 95 devices
  |-- 1 source group
  |-- 21 account groups
  |-- 0 clusters
  |-- 72 online  --> move
  `-- 23 not online --> excluded, listed in a report
```

- The hostname prefix gives the target group.
- Column H of the sheet "Summary" lists every group that sees the device.
- The target group is always in that list. The device is shared with it.
- A device with a login problem is excluded, like an offline device.
- Clusters are out of scope. The code exists. Do not use it in production.

Example (synthetic):

```
 Hostname         Prefix  Target    Groups that see it (column H)
 AAAA - HOST01    AAAA    Group B   Group A; Group B; Group C
```

The tool `te_agent_migrate.scope` reads the Excel file.
It writes three files. See [the runbook](OPERATIONS.md#2-build-the-scope).

## 3. Rules

- Dry run is the default. `--apply` needs a clean dry run.
- The operator chooses the devices. Nothing moves by default.
- Tests do not move. Do not create, copy or delete tests.
- Offline devices do not move. A report lists them.
- The tool moves one device at a time.
- The tool stops at the first failure.
- The tool deletes the old agent last, after verification.
- The tool never prints or stores tokens or passwords.
- The assistant never calls the production API.
  The operator runs the discovery script and the tool.

## 4. Flow

One run moves devices from one source group.
Each device can have its own target group.

```
 Excel file
    |
    v
 scope  ------>  prefix_map.csv  (operator edits, status OK / REVIEW)
    |
    v
 inventory.csv  (READY devices only)
    |
    +--> te_report  --> impact report (what can break)
    |
    v
 dry run  ------>  dry-run-ok.json  (only when clean)
    |
    v
 apply (--confirmed-dry-run-file)
    |
    v
 one wave done --> post-wave checklist --> next wave
```

## 5. Why Strategy D

A reset gives the agent a new agent ID.
A test with no agent is disabled.

Strategy D avoids this. The tool shares the new agent back
and assigns the same tests to it.

```
 BEFORE                          AFTER
 Group A (source)                Group A (source)
 +----------------+              +----------------+
 | Test 101       |              | Test 101       |
 |  Agent 100     |              |  Agent 200 ----+--> owned by Group B
 +----------------+              +----------------+
                                 Group B (target)
 Group B: no agent               +----------------+
                                 | Agent 200      |
                                 +----------------+
```

The test ID does not change. The agent ID changes (100 to 200).

## 6. Share model

A reset removes all sharing. The new agent starts in the target group only.
The tool shares it again with the full list of groups.

```
 Old agent 100 (owner: Group A)
   seen by: Group A, Group B, Group C
        |
        |  reset + new token
        v
 New agent 200 (owner: Group B)
   share list = [Group B, Group A, Group C]   never empty
```

- The target group is always in the list.
- The list also holds every group with tests on the old agent.
- It is not proven if the API call replaces or adds to the list.
  The tool always sends the full list, so both cases work.

## 7. Strategy D steps

The tool runs 14 steps for a standalone device.
Dry run prints the same steps. It sends no write call.

```
  1 preflight_snapshot      read, check, plan
  2 ui_authenticate         log in to the agent UI
  3 ui_snapshot_before      read UI settings
        |
        |  snapshot file is written here (mode 0600)
        v
  4 ui_reset                 DESTRUCTIVE
  5 ui_reachable_after_reset
  6 ui_authenticate_after_reset
  7 ui_set_target_token
  8 api_target_online       wait for the new agent
  9 api_share_back          full share list
 10 api_assign_tests        assign, then verify
 11 api_delete_old          only after step 10 is verified
 12 api_rename              restore the exact name
 13 api_restore_tags        tags through the API
 14 manual_notification_rules   operator does this by hand
```

WARNING: Step 4 cannot be undone.

Step 8 accepts a new agent only when all these are true:

- The agent ID was not in the target group before the reset.
- The IP address matches.
- The name matches (exactly, or with the suffix `-xxxx`).
- Only one agent qualifies. Two candidates stop the run.

Step 8 also stops the run if a new matching agent appears in the source group.
This means the token went to the wrong group.

Step 10 sends one assign call for each group. It then reads each
test list again. The old agent stays until all tests are verified.

## 8. Preconditions

Check these before the first apply.

1. The discovery data is current. TODO: Set the maximum age.
2. `prefix_map.csv` has no `REVIEW` row.
3. The impact report is made and read.
4. The post-wave checklist is ready ([Reports](REPORTS.md#6-post-wave-checklist)).
5. A clean dry run wrote `dry-run-ok.json`.
6. One registration token for each target group is ready.
7. The agent UI login works for all devices in the wave.
8. The machine that runs the tool reaches every agent on TCP 443.
9. The owner of manual steps is known. TODO: Name the owner.
10. A change window is agreed. TODO: Set the wave dates.

## 9. Per-device checks

The preflight reads only. Dry run and apply use the same checks.

A device gets one status:

| Status | Meaning |
|---|---|
| ready | The tool can move it |
| blocked | A check failed. The run does not start |
| excluded | The device is not online. It is never touched |
| skipped | The device is already migrated |

A device is blocked when one of these is true:

- No agent matches both IP and name in the source group.
  If the row has an `agent_id`, the ID must match.
- The name is already used in the target group.
- The agent is already in the target group (a re-run).
  An inventory row that lists the target in `share_aids` is an exception.
  It states that the device is shared with the target already.
- The API names another group as owner.
  The API has no owner field. The operator states the owner with
  `--owner-is-source`.
- Two sources of sharing disagree.
  The tool compares `GET /agents/{id}` with a scan of all groups.
- A shared group cannot be read. Tests would be lost.
- An agent-to-agent test uses the device as target agent.
  This blocks the device unless you set `--repoint-agent-to-agent` (see section 12).
- The device appears twice in the inventory.

The preflight gives these warnings. They do not block:

- The inventory `agent_id` is old. The tool uses the live ID (matched by name and IP).
- A test has the device as its only agent.
  The old agent stays until verification, so the test stays enabled.
- A tag cannot be read.

A device is "already migrated" when it is not in the source group
and the target group has an online agent with the same name and IP.
The tool skips it. This protects against a double move.

## 10. Waves

A wave is one run of the tool. Plan waves by target group.

```
 Pilot    2-3 devices, one target group
   |
   |  check, report, fix
   v
 Wave 1   smallest target group
 Wave 2   next target group
  ...
 Wave N   largest target group
   |
   v
 Last     offline devices (or never)
```

- Keep each wave to one target group. Then one token is enough.
- A run with several targets is possible. The tool needs one token for each target.
- Run the report before each wave.
- Run the post-wave checklist after each wave.
- Stop after any failure. Do not start the next wave.

TODO: Set the wave list (group, device count, date).
TODO: Set the pilot devices and the pilot date.

## 11. Rollback and recovery

There is no automatic rollback. A reset cannot be undone.
The tool has no resume mode.

If the tool stops, it prints a state line:
`stopped; state: ...`. It also names the snapshot file.

| State | Action |
|---|---|
| nothing changed | Fix the cause. Run again |
| agent was reset, no token set | Enter the target token in the agent UI by hand |
| token submitted, old agent present | Wait. Check the target group for the new agent |
| new agent online | Share it back and assign the tests by hand |
| shared, tests may be partly assigned | Assign the missing tests. Verify them |
| tests verified | Delete the old agent by hand |
| delete sent, not confirmed | Check if the old agent still exists |
| old agent deleted | Fix the name and tags by hand |

The snapshot file holds: old and new IDs, share list, test IDs for each group,
enabled flags, tags and the target group. It holds no token.

WARNING: The snapshot file holds customer IDs. Do not commit it.

A device in the wrong group needs a second move.
Use the old group token and the snapshot.

TODO: Write a resume procedure for each state.
TODO: Test every recovery action in the lab.

## 12. Manual steps

The API cannot do these steps.

- Restore the agent notification rules. The API v7 is read-only for them.
  The report lists the affected agents.
- Check alert suppression windows that use a moved agent.
- Agent-to-agent tests that use a moved agent as target.
  With `--repoint-agent-to-agent` the tool points them to the new agent ID.
  The old agent is kept if this step fails. Lab cases L16 to L20 must pass first
  ([Lab](LAB.md#gate-agent-to-agent-repoint-l16-to-l20)).
  Without the flag, the device is blocked.
- Update widget filters that use a moved agent ID.
- Check "Owned Agents" widgets and "Alert List" widgets.

See [Reports](REPORTS.md#6-post-wave-checklist) for the checklist.

TODO: Name who does each manual step.
TODO: Set the time limit after a wave.

## 13. Risks

| Risk | Control |
|---|---|
| Reset without a working new registration | Check UI login and token before apply |
| Token for the wrong group | One token for each target. The tool checks the source group |
| Sharing lost | Full share list. Never empty |
| Test loses its agent | Assign tests before delete |
| Agent-to-agent test breaks | Preflight blocks the device, or the opt-in repoint step fixes it before the old agent is deleted |
| Widget filter on agent ID breaks | Report lists it. Fix after the wave |
| Double move | "Already migrated" skip. Inventory hash |
| Stale discovery data | Preflight reads live data |
| Secrets in files | Hidden prompts. No token in files. Mode 0600 |
| Agent UI uses unverified TLS | Only for the agent UI. The API keeps full checks |

## 14. Recommendations

1. Move by target group. Start with the smallest group.
2. Start with a pilot of 2-3 devices.
3. Check `prefix_map.csv` before the first wave.
4. Handle devices whose target is not in column H by hand.
   Do not move them until the cause is known.
5. Move offline devices last, or never.
6. Fix agent-ID widget filters after each wave. Use `findings.csv` as the checklist.
   Agent-to-agent targets: use `--repoint-agent-to-agent` after the lab gate passes.
7. Make a new report after each wave.
8. Check that no device moved twice. Look for `skipped: already migrated`.
9. Run the lab in waves on 12 VMs ([Lab](LAB.md)).
10. Rotate the API token after the project.

## 15. Open points

- The API has no owner field. Lab check: find any field that names the owner.
- Does a tag of the source group apply to an agent that the target group owns?
  Or must the tag exist in the target group?
- The agent-to-agent repoint is not proven against a real API.
  The lab gate (L16 to L20) decides. If a case fails, ask the user how to proceed.
  See [Operations](OPERATIONS.md#8-failures).

## 16. Questions for the customer

Send these questions before the first wave.

1. **Device in prefix CSC.** One online device has no division group.
   Its column H lists only enterprise groups. Which account group must own it?
   The tool suggests the first enterprise group in its column H. Nobody has confirmed it.
   Until the customer answers, the device stays out of the inventory.
2. **Devices whose target is not in column H.** Two devices have a prefix group
   that is missing from their column H. The tool adds the target group to the share list
   and prints a warning. Is this correct?
3. **Prefix GCN.** The only device has a login problem. Which group must own it
   when the login is fixed?
4. **Agent-to-agent tests.** 25 tests use 20 devices as target. May the tool change the
   target of these tests to the new agent ID? Who owns these tests?
5. **Devices already migrated.** Five devices of one division have new agent IDs and
   are online in the target group. Was this done on purpose? Do they need other checks?
   Status: open. Nobody on our side knows who moved them.
6. **Change window and wave dates.** When may the tool reset agents?
7. **Manual steps.** Who restores the agent notification rules after each wave?
