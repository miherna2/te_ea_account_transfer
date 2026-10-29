# Sandbox seed script

This script fills an EMPTY ThousandEyes SANDBOX with `SBX-` resources.
Use it to rehearse the migration with the same shape as production.
The default layout is PLAN-DRIVEN: the lab copies the shape of the scope workbook.
The old generic layout (S1 to S6, clusters C1 and C2, X1) is still available with `--layout generic`.

No real name, host name, IP address or id is written in any tracked file.
The lab uses anonymous aliases: groups `SBX-G01` to `SBX-G21`, devices `SBX-D001` and up.

## How the lab is built

1. `lab_plan.py` reads the workbook (through `te_agent_migrate/scope.py`) and builds a LAB PLAN:
   - each account group gets an alias in the order of first appearance (the source group is `SBX-G01`);
   - each device gets an alias in workbook order, with its status, target alias, share aliases,
     share SHAPE (sorted aliases, the target written as `@T`), anomaly flag and test-type counter;
   - each group gets its test-type counter, the number of tests and the scaled mix.
   Devices that are not READY stay in the plan with their status. They are never started in the lab.
2. The lab has a limited number of TEVA VMs (`--vms`, default 12). The script chooses devices in this order:
   the anomaly (target not in the share list), every target group (smallest first), every share shape,
   every test type, then the devices that add most groups. No random choice: the same input gives the same lab.
   The output lists what is NOT covered.
3. `--waves K` splits all READY devices into K waves of at most N VMs. Wave 1 is the archetype set.
   The operator registers the SAME VMs again for each wave (see "Waves").
4. The seed creates, for each group that a lab device can see:
   - tests of the real mix, scaled to `--max-tests-per-group` (default 25), named `<group alias>-<type>-<n>`
     (for example `SBX-G03-http-server-02`). Each test runs on all lab devices that are shared into the group;
   - one alert rule `<group alias>-ALERT`, attached to the `http-server` tests of the group;
   - agent-to-agent tests whose TARGET is a lab device (te_report must show BREAKS). If the source group has none,
     one extra test `<alias>-agent-to-agent-LAB-01` is made;
   - one agent tag `sbx-lab=agents` on every lab device (for the agent-tag widget).
   Tests run once an hour (`interval` 3600) because the lab needs the objects, not the data.
   All targets are `example.com`. The web-transaction test uses a FAKE user and password made at run time.

Not seeded (no API shape for them is used by this project). The run prints `not seeded: <reason>`:

- dashboards and suppression windows. Build them by hand. `lab_manual_tasks.md` (in `--output`) lists, for each group,
  four dashboards: widget filter on agent id (BREAKS), widget filter on agent tag, group by agent, alert list;
- test types without a known payload shape (voice, sip-server, api);
- groups that no lab device sees, and agent-to-agent tests in groups with only one lab device.

## What the operator must prepare

1. Create the 21 account groups in the sandbox org. Name them `SBX-G01` to `SBX-G21` (any `SBX-` name is accepted,
   but the map key must be the alias). Write down the group ids.
2. Register the TEVA VMs (default 12) in the source group `SBX-G01`. Use private IP addresses.
3. Use a SANDBOX token that sees all groups. NEVER use the production token.
4. Copy `examples/sandbox_map.example.json` to `work/input/sandbox_map.json` (git-ignored).
   Write the xlsx path, the VMs (name and IP) and the group ids (alias to id). Keep `"sandbox": true`.
5. Export the token:

```
export TE_OAUTH_TOKEN=...      # sandbox token
```

## How to run

Run from the project root.

```
# 1. Plan only. No API, no token, no map needed. Counts only.
python -m dev.sandbox.seed_sandbox --xlsx work/input/ThousandEyes-Agents-ToMigrate.xlsx \
    --prefix-map work/input/prefix_map.csv --plan-only --write-plan work/sandbox/lab_plan.json

# 2. Dry run (default). Reads only. Zero writes. Shows the planned objects and an estimate of the API calls.
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox

# 3. Apply.
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --apply

# 4. Verify (read only): per lab device, the shares and the number of assigned tests against the plan.
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --verify
```

`--verify` prints one `OK` or `MISMATCH` line for each lab device. The exit code is 1 when one line is a MISMATCH.

Options:

| Option | Meaning |
|---|---|
| `--map FILE` | The map file. Needed for every run except `--plan-only`. |
| `--xlsx FILE`, `--prefix-map FILE` | Scope workbook and prefix map. Default: the values in the map. Without a prefix map the map is derived from the workbook. |
| `--plan FILE` | Read a lab plan JSON file (made with `--write-plan`) instead of the workbook. |
| `--plan-only` | Print the plan summary and stop. |
| `--write-plan FILE` | Write the anonymized plan JSON (mode 0600). The real-to-alias map goes to `<output>/lab_alias_map.json` (mode 0600, git-ignored directory). It is never printed. |
| `--vms N` | Number of lab VMs. Default 12 (or the number of VMs in the map if smaller). |
| `--waves K`, `--wave W` | Split in K waves. Seed wave W (default 1). |
| `--max-tests-per-group N` | Scale the tests of each group to at most N (default 25). |
| `--max-calls N` | Stop when the run would need, or makes, more than N API calls (default 2000; 0 = off). |
| `--steps LIST` | `clusters,shares,tests,tags,alerts,report`. Default: all but `clusters`. |
| `--with-clusters` | Also run the clusters step. OFF by default: the real scope has no clusters. It needs `clusters` in the map. |
| `--apply` | Send the writes. Without it, the run is a dry run. |
| `--verify` | Read only check (see above). Never writes, even with `--apply`. |
| `--cleanup` | List the `SBX-` objects (dry run) or delete them (with `--apply`). |
| `--output DIR` | Output directory. Default `work/sandbox` (mode 0700). |
| `--layout generic` | The old generic layout. It needs its own map format (groups SOURCE, TARGET, VIEW1, VIEW2; agents S1 to S5; cluster C1). |
| `--i-know-this-is-sandbox` | Required (not for `--plan-only`). |

Exit codes: `0` clean, `1` hard error or a MISMATCH, `2` a safety gate refused.

## Waves

A lab with 12 VMs cannot hold all 69 READY devices at once. Use waves:

```
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --waves 6 --wave 1 --apply
# ... rehearse the migration for the devices of wave 1 ...
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --cleanup --apply
# register the same VMs again (new agent ids), then:
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --waves 6 --wave 2 --apply
```

The n-th device of the wave uses the n-th VM of the map (devices in alias order). The script finds the agent id of
each VM by IP or name in the source group at run time, so a VM that registered again is found.
Use the same `--vms` and `--waves` values for every wave.

## Safety gates

The script refuses to run (exit 2) when:

- the map has no `"sandbox": true`, or the flag `--i-know-this-is-sandbox` is missing;
- a group key of the map is not an `SBX-G..` alias, or a group id repeats;
- the map has no group id for a group that the wave uses, or too few VMs for the wave;
- the token cannot see a group of the map;
- the estimated number of API calls is more than `--max-calls`.

The script touches only the groups in the map. It warns when a group name does not start with `SBX-`.
It creates only objects with names that start with `SBX-`. The token is never printed or written.
In a dry run the client is wrapped in `ReadOnlyApi`: any write call raises an error.

## Run again and clean up

The script is safe to run again. It looks for the same name in the group and skips it.

```
# List only
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --cleanup
# Delete
python -m dev.sandbox.seed_sandbox --map work/input/sandbox_map.json --i-know-this-is-sandbox --cleanup --apply
```

Cleanup deletes only tests and alert rules whose name starts with `SBX-` (all groups of the map)
and the tag `sbx-lab=agents`. It never deletes agents, groups or dashboards.

## Files in `--output`

`sandbox_seed_result.json` (aliases and sandbox ids, no secrets, API call count, `not_seeded` list),
`lab_manual_tasks.md` (dashboards and suppression windows to build by hand),
`lab_alias_map.json` (only with `--write-plan`: real names, keep it private).

## Lab must verify (first run)

The payload builders are in `seed_sandbox.py` (`build_*`). If the API rejects a field, change only the builder.
Check these on the first `--apply`:

1. Test fields: `agent-to-server` (`protocol`, `port`), `agent-to-agent` (`targetAgentId`, `protocol`, `port`),
   `dns-server` (`domain` with record type, `dnsServers` as `serverName`), `web-transactions` (`transactionScript`).
2. Tag fields: `objectType` is `agent` for agent tags. `color` is accepted.
3. Alert rule: the expression `((error != "None"))` is valid for `http-server`.
4. A PUT of the full test payload with `alertRules` (list of ids) attaches the rule.
5. The test list shows a `type` for each test (cleanup needs it).
6. `--verify` reads the assigned tests from the `testIds` of `GET agents` in each group. Check that this list
   shows the tests of that group. If it does not, `--verify` shows MISMATCH for the test counts only.
7. The sandbox accepts an interval of 3600 for all test types.

## Old generic layout

`--layout generic` seeds the old layout: 182 tests in SOURCE, 10 in VIEW1, 3 in VIEW2, clusters C1 and C2,
tags, alert rules and the dashboard checklist D1 to D8 (`sandbox_manual_tasks.md`).
It uses the old map format (groups SOURCE, TARGET, VIEW1, VIEW2, OTHER; agents S1 to S6, X1; clusters).
