# Sandbox Lab

The lab is a ThousandEyes sandbox that copies the production setup at a small size.
It proves the tool before production. The code is in `dev/sandbox/`.

Related documents: [Plan](MIGRATION_PLAN.md), [Runbook](OPERATIONS.md),
[Reports](REPORTS.md).

Status: the lab plan is built from the Excel scope (see [section 7](#7-lab-from-the-excel-scope)). The lab has not run yet.

## 1. Rules

- Use a sandbox token only. Never use a production token.
- Name every object `SBX-...`. Then it cannot be confused with production.
- Use fake targets (`example.com`) and fake credentials.
- Never copy production tests, scripts or names.
- Keep lab files in `work/` (git-ignored).

## 2. Facts

| Item | Answer |
|---|---|
| Account groups | Can be created (4 or 5) |
| Agent type | TEVA appliances (agent UI reset works) |
| VMs | Up to 12 |
| API token | Covers all lab groups |
| Network | The tool machine reaches every agent on TCP 443 |

## 3. Layout

```
 SBX-SOURCE   owns the agents and ALL tests, tags, dashboards
 SBX-TARGET   empty. New owner of the moved agents
 SBX-VIEW-1   sees shared agents. Has its own tests
 SBX-VIEW-2   sees shared agents. Has its own tests
 SBX-OTHER    optional. Owns one agent that it shares into SBX-SOURCE
```

| Agent | Role |
|---|---|
| S1, S2 | Standalone, no sharing. Baseline |
| S3 | Standalone, shared to VIEW-1 and VIEW-2 |
| S4 | Target of an agent-to-agent test |
| S5 | Powered off. Must be excluded |
| S6 | Disabled. Must be excluded |
| X1 | Owned by OTHER, shared into SOURCE. Owner check |
| C1, C2 | Clusters. Out of production scope |

## 4. Seed script

The script fills an empty sandbox. It runs from the project root.
Dry run is the default.

1. Make the groups. Register the agents. Build no clusters by hand.
2. Copy the example map. Write the real IDs. Keep `"sandbox": true`.

   ```bash
   cp examples/sandbox_map.example.json work/input/sandbox_map.json
   export TE_OAUTH_TOKEN=...            # sandbox token
   ```

3. Dry run. It reads only.

   ```bash
   python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json \
     --i-know-this-is-sandbox
   ```

4. Apply. Add `--apply`.
   Add `--steps tests,tags` to run some steps only.
   The steps are `clusters,shares,tests,tags,alerts,report`.

The output goes to `work/sandbox/` (option `--output`):
`sandbox_seed_result.json` (names and IDs) and `sandbox_manual_tasks.md`.

Exit codes: 0 clean, 1 error, 2 a safety gate refused.

The script refuses to run when:

- The map has no `"sandbox": true`, or the flag is missing.
- Source and target are the same group, or a group ID repeats.
- The map has an unknown name.
- The token cannot see a group of the map.

The script is safe to run again. It skips objects that exist.

### What it creates

| Step | Creates |
|---|---|
| `clusters` | C1 and C2 in SOURCE |
| `shares` | S3, C1, C2 shared to VIEW-1 and VIEW-2. X1 shared into SOURCE |
| `tests` | 182 tests in SOURCE, 10 in VIEW-1, 3 in VIEW-2 |
| `tags` | Agent tags and test tags in SOURCE |
| `alerts` | Two alert rules in SOURCE |
| `report` | The file `sandbox_manual_tasks.md` |

The test mix: HTTP tests for S1 and S2, network tests for S3,
two agent-to-agent tests (S1 to S4), 153 tests on C1
(120 of them test the assign chunking, 3 are disabled),
one fake transaction test, and tests on S5.

### Clean up

```bash
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json \
  --i-know-this-is-sandbox --cleanup            # list only
```

Add `--apply` to delete. Cleanup deletes only `SBX-` tests, `SBX-` alert rules
and the four seed tags. It never deletes agents, clusters, shares,
groups or dashboards.

### Manual tasks

The script does not make these:

- An agent notification rule on S1.
- A suppression window on a tag.
- Dashboards D1 to D8.

## 5. Dashboards and expected verdicts

The dashboards give a known answer for the report.

| Dashboard | Content | Expected verdict |
|---|---|---|
| D1 | Filter on tests of S1, S2 | OK |
| D2 | Widget filter on agent ID of S1 | BREAKS |
| D3 | Filter on agent tag `env=sbx` | REVIEW |
| D4 | Widget groups by agent | REVIEW |
| D5 | Agent Status, "All Assigned Agents" | OK |
| D6 | Alert List widget | REVIEW |
| D7 | Global filter on test tag `app=web` | OK |
| D8 | Tests of cluster C1 | REVIEW |

## 6. Lab test cases

| Case | Action | Expected |
|---|---|---|
| L1 | Dry run without `--owner-is-source` | Every device fails: owner not proven. Exit 1 |
| L2 | Dry run with `--owner-is-source` | Exit 0. No write. `dry-run-ok.json` written |
| L3 | Owner probe (below) | Find who owns an agent |
| L4 | Apply S1 | New ID in TARGET. Test IDs stay. Old agent deleted last |
| L5 | Apply S3 | Shares and tests restored in SOURCE, VIEW-1, VIEW-2 |
| L6 | S5 and S6 in the inventory | Excluded, listed, not touched |
| L7 | X1 in the inventory | Blocked: another group owns it |
| L8 | Run L4 again | Skipped or blocked: already migrated |
| L9 | Wrong target token | Wrong-group stop. Nothing deleted |
| L10 | Block port 443 to one agent | The device fails. The batch stops. State line shown |
| L11 | Ctrl-C during a flow | State line, snapshot, exit 130, summary written |
| L12 | Apply C1 (optional) | New cluster ID. 120 tests assigned |
| L13 | Wrong UI password on a cluster member (optional) | Stops before the unassign |
| L14 | Transaction test after C1 (optional) | Credentials unchanged |
| L15 | Compare discovery before and after | Test IDs unchanged. Verdicts match section 5 |

Cases L12 to L14 need clusters. TODO: Decide if the lab keeps clusters.

### Gate: agent-to-agent repoint (L16 to L20)

> **WARNING.** The repoint is a write to tests that the move does not own.
> The API calls for it are not proven against a real API. This gate must pass in the lab first.

In production, 25 agent-to-agent tests use 20 of the 69 devices as target agent. A move gives the
device a new agent ID. The flag `--repoint-agent-to-agent` points each test to the new ID after the
move and before the old agent is deleted.

| Case | Action | Expected |
|---|---|---|
| L16 | Dry run with a targeted device, without the flag | Device blocked. Reason names the test IDs |
| L17 | Dry run with the flag | Warning with the test count. No write. Same step names as apply |
| L18 | Apply with the flag | Test IDs unchanged. `targetAgentId` is the new ID. Old agent deleted last |
| L19 | Make the repoint fail (for example a wrong test ID) | Old agent is kept. State line lists done and not done tests |
| L20 | Run the report before and after | The BREAKS for these tests are gone |

Rules for this gate:

1. The lab must hold at least one agent-to-agent test in a group other than the source group.
2. Record the API request and response shapes that were used. Never record secrets.
3. **If L18, L19 or L20 fails, stop. Show the evidence to the user and ask how to proceed.**
   Do not change the design without that answer.

API assumptions to verify in L18. The code has these from the API documentation, not from a real run:

1. The call reads `GET tests/agent-to-agent/{id}?aid=<aid>` and writes `PUT tests/agent-to-agent/{id}?aid=<aid>`.
   The write must answer HTTP 200.
2. `targetAgentId` is one plain id (text or number).
3. The PUT body is the GET body with only `targetAgentId` changed.
   The tool removes read-only fields and reduces lists (agents, tags, alert rules, monitors,
   shared accounts) to ids. Check that the PUT changes nothing else. Compare the test before and after.
4. The `aid` of the PUT is the group that owns the test. That group must be allowed to change it.
   The new agent is shared with that group before the repoint.
5. The GET after the PUT shows the new `targetAgentId`, the same `enabled` value and the same
   source agents. Any difference stops the flow. The old agent stays.

Edge cases to test: a test whose group cannot be read (the device is blocked when the flag is set),
and a PUT that is applied but answers with another status (the flow stops; check the test by hand).

Status: not run. TODO: Run L16 to L20 and write the result here.

### Owner probe

X1 is owned by SBX-OTHER and shared into SBX-SOURCE.

1. Read X1 with the group ID of OTHER and with the group ID of SOURCE. Compare.
2. Write X1 (same name) with the group ID of SOURCE. Expect an error.
3. Write X1 with the group ID of OTHER. Expect success.
4. Note which field, if any, names the owner.

### Reset between runs

```
 Reset agent (UI) --> token of SBX-SOURCE --> online in SOURCE
 Delete leftovers in TARGET
 Restore the shares and tests (seed script, snapshot files)
```

A full reset must take less than 30 minutes. TODO: Time one full reset.

### First apply: payload fields to check

If the API rejects a field, change only the builder in `dev/sandbox/seed_sandbox.py`.

1. Test fields for agent-to-server, agent-to-agent, DNS and transaction tests.
2. Tag fields. The type for agents is `agent`.
3. The alert expression and `minimumSources`.
4. A full test PUT with `alertRules` attaches the rule.
5. The cluster ID equals the ID of the first member.
6. The test list shows a `type` for each test.

## 7. Lab from the Excel scope

The lab seed now builds its layout from the production Excel file. The plan has no real names.

```
 Excel scope --> lab plan (aliases SBX-G01.., SBX-D001..) --> choose devices --> seed
                      |                                         |
                      +--> work/sandbox/lab_alias_map.json      +--> waves of 12 VMs
                           (real -> alias, mode 0600, local)
```

Run the plan first. It needs no API.

```
python -m dev.sandbox.seed_sandbox --plan-only --i-know-this-is-sandbox \
  --xlsx work/input/<scope file>.xlsx --write-plan work/sandbox/lab_plan.json
```

This prints counts only and writes `lab_plan.json` (no real names) and `lab_alias_map.json`
(real names, mode 0600, keep it local). Use `--help` for all options.

### What the plan covers (counts from the production file)

| Item | Result |
|---|---|
| Account groups | 21 (19 own tests) |
| Lab devices chosen (12 VMs) | 12 |
| Target groups covered | 12 of 13 |
| Share shapes covered | 5 of 6 |
| Test types covered | 7 of 9 |
| Target-not-in-share-list anomaly | covered |
| Waves for all ready devices | 6 (12, 12, 12, 11, 11, 11 devices) |

Rules of the plan:

1. Wave 1 is the archetype set: the anomaly device, every target group (smallest first),
   every missing share shape, every missing test type.
2. The choice has no random part. The same file gives the same plan.
3. Test counts are scaled down (default 25 for each group). Every type keeps at least one test.
4. Each agent-to-agent test has a lab device as target. These must show as BREAKS in the report.
5. Clusters are off. Use `--with-clusters` to turn them on.
6. `--max-calls` stops the run before it floods the sandbox. `--verify` only reads.

### Not seeded

The seed prints `not seeded: <reason>` for these items.

- **Dashboards and suppression windows.** The project has no proven API shape for them.
  The seed writes `lab_manual_tasks.md` with four dashboards for each group:
  agent ID filter, agent tag filter, group by agent and alert list. Build them by hand.
  The expected verdicts are in section 5.
- **Test types voice, sip-server and api.** No payload shape is known.

### What the operator must prepare

1. 21 sandbox groups named `SBX-G01` to `SBX-G21`, and their IDs.
2. 12 TEVA VMs, registered in `SBX-G01` (the source).
3. A sandbox token that sees all groups.
4. `work/input/sandbox_map.json`, copied from `examples/sandbox_map.example.json`.
5. For the next wave: `--cleanup --apply`, register the VMs again, then seed with `--wave N`.
   Use the same `--vms` and `--waves` values each time.

### Checks for the first apply

- `--verify` counts the tests of each device from `testIds` in `GET agents`.
  Check that this list shows the tests of that group.
- The sandbox must accept an interval of 3600 seconds for every test type.
- Wave 1 has no web-transaction, voice, sip-server or api tests.

TODO: Set the VM list for each wave.

## 8. Exit gate for production

- L1 to L11 pass, or each failure has a decision.
- L16 to L20 (agent-to-agent repoint) pass. A failure needs a decision from the user.
- No unexpected change in test IDs.
- Report verdicts match the dashboards in the UI.
- The owner probe result is in the plan.
- A Python 3.6 run of the unit tests passes (if production uses 3.6).
