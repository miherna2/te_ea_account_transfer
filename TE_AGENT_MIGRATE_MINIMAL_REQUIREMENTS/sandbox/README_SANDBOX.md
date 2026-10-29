# Sandbox seed script

This script fills an EMPTY ThousandEyes SANDBOX with `SBX-` resources.
Use it to test the Strategy D migration tool.
Plan: `MIGRATION_PLAN.md`, section 20.

## Before you start

1. Make the account groups in the sandbox org: SOURCE, TARGET, VIEW1, VIEW2 (OTHER is optional).
   Name them `SBX-...`.
2. Register the agents. Build no clusters by hand: the script builds them.
3. Copy `sandbox/sandbox_map.example.json` to `sandbox_map.json`.
   Write the real group ids and agent ids. Keep `"sandbox": true`.
4. Use a SANDBOX token. NEVER use the production token.

```
export TE_OAUTH_TOKEN=...      # sandbox token
```

## How to run

Run from the project root.

```
# 1. Dry run (default). Reads only. Zero writes.
python -m sandbox.seed_sandbox --map sandbox_map.json --i-know-this-is-sandbox

# 2. Apply.
python -m sandbox.seed_sandbox --map sandbox_map.json --i-know-this-is-sandbox --apply

# One or more steps only (the order is always the same).
python -m sandbox.seed_sandbox --map sandbox_map.json --i-know-this-is-sandbox --apply --steps tests,tags
```

Options:

| Option | Meaning |
|---|---|
| `--map FILE` | The map file (required). |
| `--steps LIST` | `clusters,shares,tests,tags,alerts,report`. Default: all. |
| `--apply` | Send the writes. Without it, the run is a dry run. |
| `--cleanup` | List the `SBX-` objects (dry run) or delete them (with `--apply`). |
| `--output DIR` | Output directory. Default `sandbox_output/` (mode 0700). |
| `--i-know-this-is-sandbox` | Required. |

Exit codes: `0` clean, `1` hard error (the state line shows what was made), `2` a safety gate refused.

## Safety gates

The script refuses to run (exit 2) when:

- the map has no `"sandbox": true`, or the flag `--i-know-this-is-sandbox` is missing;
- SOURCE and TARGET are the same, or a group id repeats;
- the map has a group, agent or cluster name that the script does not know;
- the token cannot see a group of the map.

The script touches only the groups in the map. It warns when a group name does not start with `SBX-`.

## What it creates

| Step | Creates |
|---|---|
| `clusters` | C1 and C2 in SOURCE (base = first member). |
| `shares` | S3, C1, C2 shared to VIEW1 and VIEW2. X1 (owner OTHER) shared into SOURCE. Always the full group list. |
| `tests` | 182 tests in SOURCE, 10 in VIEW1, 3 in VIEW2 (below). |
| `tags` | In SOURCE: agent tags `env=sbx`, `site=a` on S1, S3, C1. Test tags `app=web` (HTTP tests of S1, S2), `app=dns` (DNS tests). |
| `alerts` | Rule `SBX-ALERT-HTTP-DEFAULT` on the HTTP tests of S1, S2. Rule `SBX-ALERT-C1-MIN-SOURCES-2` (`minimumSources` 2) on the 30 C1 tests. |
| `report` | `sandbox_manual_tasks.md`: manual tasks and the dashboard checklist D1 to D8. |

Tests in SOURCE:

| Set | Count | Agent |
|---|---|---|
| HTTP, 5 min | 5 + 5 | S1, S2 |
| Network (ICMP and TCP) | 10 | S3 |
| Agent-to-agent | 2 | source S1, target S4 |
| HTTP | 30 | C1 |
| HTTP bulk (`SBX-C1-BULK-`) | 120 | C1 |
| DNS, page load | 3 + 1 | S2 |
| Web transaction | 1 | S1 |
| HTTP | 2 | S5 (offline) |
| HTTP, disabled | 3 | C1 |

All targets are `example.com`. The transaction test uses a FAKE user and password.
The script makes them at run time. It never prints or stores them.
The test description is `SBX fake credentials`.

Files in `--output`: `sandbox_seed_result.json` (names and ids, no secrets) and `sandbox_manual_tasks.md`.

## Manual tasks

The script does NOT make these. Do them in the UI (the file `sandbox_manual_tasks.md` lists them):

- agent notification rule on S1;
- suppression window on a tag;
- dashboards D1 to D8.

## Run again

The script is safe to run again. It looks for the same name in the group and skips it
(`ok: [name] => already exists`).

## Clean up

```
# List only
python -m sandbox.seed_sandbox --map sandbox_map.json --i-know-this-is-sandbox --cleanup
# Delete
python -m sandbox.seed_sandbox --map sandbox_map.json --i-know-this-is-sandbox --cleanup --apply
```

Cleanup deletes only:

- tests whose name starts with `SBX-` (all groups of the map);
- alert rules whose name starts with `SBX-`;
- the tags `env=sbx`, `site=a`, `app=web`, `app=dns` in SOURCE.

Cleanup never deletes agents, clusters, shares, groups or dashboards.

## Lab must verify (first run)

The payload builders are in `sandbox/seed_sandbox.py` (`build_*`). If the API rejects a field, change only the builder.
Check these on the first `--apply`:

1. Test fields: `agent-to-server` (`protocol`, `port`), `agent-to-agent` (`targetAgentId`, `protocol`, `port`),
   `dns-server` (`domain` with record type, `dnsServers` as `serverName`), `web-transactions` (`transactionScript`).
2. Tag fields: `objectType` is `agent` for agent tags. `color` is accepted.
3. Alert rule: the expression `((error != "None"))` is valid for `http-server`. `minimumSources` is accepted.
4. A PUT of the full test payload with `alertRules` (list of ids) attaches the rule.
5. The cluster id is the id of the first member.
6. The test list shows a `type` for each test (cleanup needs it).
