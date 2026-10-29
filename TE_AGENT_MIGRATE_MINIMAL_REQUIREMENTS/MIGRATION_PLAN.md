# Enterprise Agent Migration Plan

Status: **Draft for review.** No change was made to code, agents or tests.

Written in simplified technical English (ASD-STE100 style, partial).

---

## 1. Goal

Move Enterprise Agents (standalone and cluster) from one account group
to another account group. Use the agent UI (TEVA) and ThousandEyes API v7.

The tests stay in the source account group. The tests keep their IDs.
Dashboards and alerts must keep working.

---

## 2. Rules

- Tests do not move. Do not recreate tests. Do not delete tests.
- The operator chooses the agents or clusters to move. Nothing moves
  by default.
- Offline agents do not move. They go into a report.
- Dry run is the default. Apply needs an explicit confirmation.
- Strategy D must support dry run (see section 13).
- The tool prints all output in Ansible style (see section 13).
- The operator runs all production API calls that are not part of the
  tool. Read-only first.

---

## 3. Current state (discovery, 28 Oct 2026)

```
34 account groups (6 have no agents)
680 Enterprise Agents
   668 standalone     (149 online, 515 offline, 4 disabled)
    12 clusters       (11 with 2 members, 1 with 3 members)
106 agents or clusters are shared with other groups
3,622 tests   (2,994 agent-to-server, 126 agent-to-agent)
855 dashboards (651 custom, 128 with a default global filter)
57 global filters
```

Sharing is wide:

```
6 clusters   -> visible in 17 or 18 groups each
6 clusters   -> visible in 2 or 3 groups each
```

Tests on a cluster can be many (14 to 572 per cluster).
Most tests of a shared agent are in one group only.

Known data gaps:

- The agent list does not show the owner group.
- The dashboard list has no widgets. Details are missing.
- Tag assignments are missing.
- `inventory.csv` holds lab data, not production data.

---

## 4. Why the tests need a special step

A reset gives the agent a new agent ID. The old agent is deleted.
A test that loses its last agent is disabled.

```
BEFORE                         AFTER (no extra step)

 Source group                   Source group
 +-----------+                  +-----------+
 | Test 101  |--- Agent 11      | Test 101  |--- (no agent) DISABLED
 +-----------+                  +-----------+
                                Target group
                                 Agent 99 (new ID, no tests)
```

The fix is "Strategy D": share the new agent back and assign the
original tests to it.

```
AFTER (Strategy D)

 Source group                   Target group
 +-----------+                  +------------------+
 | Test 101  |--- shared ------>| Agent 99 (owner) |
 +-----------+    agent         +------------------+
 Test ID is unchanged.
```

API v7 calls used:

- `PUT /agents/{id}` with `accountGroups` (share the agent).
- `POST /agents/{id}/tests/assign?aid=` (assign existing tests).

Both calls must be proven in the lab before production.

---

## 5. Standalone agent flow

```
 [1] Preflight       read-only checks, snapshot
        |
 [2] UI reset        agent gets a new identity
        |
 [3] Target token    agent registers in the target group
        |
 [4] Wait online     new agent ID appears in the API
        |
 [5] Share back      PUT accountGroups (all original groups)
        |
 [6] Assign tests    one call for each group
        |
 [7] Verify          each test is enabled and has the agent
        |
 [8] Delete old      remove the old agent entry
        |
 [9] Rename          restore the exact agent name
        |
 [10] Restore        tags (API), notification rules (manual)
```

Rule: assign tests (step 6) before deleting the old agent (step 8).

---

## 6. Cluster flow (maintenance window)

A cluster has one `agentId`. Members have only a `memberId`.
Tests are assigned to the cluster, not to the members.
All members must use the same account token.

So a cluster cannot move as one unit. The cluster must be taken apart.

```
 Source group                         Target group
 +------------------------+           +-------------------------+
 | Cluster C (2 members)  |           |                         |
 |  member A   member B   |           |                         |
 +------------------------+           +-------------------------+
        | 1. unassign all members
        v
   A, B standalone (cluster deleted, tests lose the agent)
        | 2. reset + target token, for A and B
        v                              A', B' online
                                        | 3. cluster/assign
                                        v
                                       Cluster C' (new cluster ID)
                                        | 4. share, assign tests,
                                        |    re-enable tests
                                        v
                                       Cluster C' serves the tests
```

All tests on the cluster have no agent during the window.

Cluster risks:

1. **Cluster ID changes.** The new cluster gets the ID of its first
   member. Dashboards that filter on the old ID break.
2. **Tests lose the agent.** Removing members clears the test
   assignments. Tests are disabled when the last agent is removed.
   The tool must save the test IDs and the enabled state first, then
   restore both.
3. **Deleting a cluster deletes its members.** The tool must never
   call `DELETE` on a cluster ID.
4. **Wrong match.** A cluster has the name of its first member. The
   current code matches by name and IP. It can select the cluster as
   a source agent. The new code must match clusters by ID.
5. **Members need UI access.** Each member needs HTTPS/443 access on
   its own IP. Cluster IPs are inside `clusterMembers`.
6. **Same family rule.** All members must use the same IP family and
   the same account group.
7. **Big clusters.** The largest clusters hold 330 to 572 tests. One
   assign call per group must handle them. Add rate-limit handling.
8. **Offline member.** The preflight blocks a cluster with an
   offline member.
9. **Cluster-only test targets.** Agent-to-agent tests that use the
   cluster as target need an update.

Sources: ThousandEyes documentation "Working with Enterprise Agent
Clusters" and API v7 cluster endpoints (`cluster/assign`,
`cluster/unassign`).

---

## 7. Shared agents

A reset removes the sharing. Every group that used the agent loses it.

```
 Cluster C  ---- shared with ----> group 1 ... group 17
```

The tool does this for each agent:

1. Save the list of groups (`accountGroups`).
2. Save the tests of the agent in each group.
3. After the move, share the agent with the same groups.
4. Assign the saved tests in each group.

The tool must scan tests in every group, not only the source and
target groups.

---

## 8. What can change

Dashboards use test IDs, test tags and test labels. Tests keep their
IDs, so test-based widgets stay the same.

Agent-based items can change:

```
Item                                   Effect
-------------------------------------  ------------------------------
Filter on TEST, TEST_LABEL, TEST_TAG   No change
Filter on AGENT                        Breaks (agent ID changes)
Filter on TARGET_AGENT                 Breaks
Filter on AGENT_LABEL, AGENT_TAG       Works after tags are restored
Group by AGENT                         History splits (old/new ID)
Agent Status, "Owned Agents"           Agent leaves the source list
Agent Status, "All Assigned Agents"    Agent stays (while shared)
Alert List widget                      Active alerts clear
Alert rules                            No change
Alert thresholds (minimum sources)     Can fire during the gap
Agent notification rules               Reset to default (manual)
Alert suppression windows (by agent)   Lost
Agent-to-agent test with agent target  Update target agent ID
```

---

## 9. Tags and labels

Agent tags can be restored through the API. Old labels became tags
on 24 June 2024.

- Tag object type for agents: `v-agent`.
- Read: `GET /tags?expand=assignments`.
- Write: `POST /tags/{id}/assign` with
  `{"assignments":[{"id":"<agentId>","type":"v-agent"}]}`.
- The discovery data has 395 agent tags.

Tags belong to one account group. Open points for the lab:

- Does the tag of the source group apply to an agent shared into it?
- Or must the tag exist in the target group?

Agent notification rules are read-only in API v7. Set them in the UI.
The tool gives a checklist for each agent.

---

## 10. Offline agents

The preflight splits the selection:

```
 selected agents --+--> online   --> migrate
                   |
                   +--> offline --> not migrated, listed in the report
```

515 of 664 agents are offline. The report lists them with last seen
date, group and test count.

---

## 11. Operator selection

The operator writes the list of agents or clusters to move.
Nothing else moves.

Proposed inventory format:

```
type,name,ip_address
agent,SITE-A-EA01,10.1.1.10
cluster,SEDC - SEDCLPAPPCTE01,
```

- `agent`: match by IP and name.
- `cluster`: match by cluster name or cluster ID. The tool reads
  the members from the API.

The tool shows the selection and asks for confirmation before apply.

---

## 12. Relationship report

A separate offline tool reads the discovery files. It makes no API
call. It does not change the migration tool.

Goal: show what belongs to what, and flag what the migration can break.

The report has two views of the same data: the current inventory
and the impact of the selected move.

### View 1: current inventory (what belongs to what)

```
Account group
 +- Agent / Cluster          (state, owner, shared with N groups)
 |   +- Member               (clusters only)
 |   +- Tag                  (agent tags)
 |   +- Test                 (type, enabled, owner group)
 |       +- Alert rule       (rule name, severity)
 |       +- Tag              (test tags)
 |       +- Dashboard widget (filter type)
 +- Dashboard
 |   +- Global filter        (default filter, filter values)
 |   +- Widget               (type, filters, agents used)
 +- Alert rule               (tests that use it)
 +- Suppression window       (tests or agents in scope)
```

Reverse lookups are also available. Start from any element and see
its parents and children (for example: agent -> tests -> dashboards).

Link rules:

- Agent -> test: `testIds` in the agent list, for each group.
- Test -> alert rule: alert rules in the test details. The test list
  does not have them. A new discovery step fetches the details of
  tests that use a selected agent (not all 3,622 tests).
- Test or agent -> tag: tag assignments (`expand=assignments`).
- Widget -> test or agent: widget filters (`TEST`, `TEST_TAG`,
  `TEST_LABEL`, `AGENT`, `AGENT_TAG`, `AGENT_LABEL`,
  `TARGET_AGENT`) and global filters.
- Agent -> group: account groups where the agent is visible.

### View 2: impact of the selected move

The report reads the operator selection. It marks every element that
the move touches. Each flag uses one level:

```
 OK       No change expected.
 REVIEW   Check by hand (for example, a tag filter).
 BREAKS   Stops working unless a fix runs (for example, an
          agent ID filter on a moved agent).
```

The report gives the reason, the fix, and who does the fix (tool or
operator). Examples:

- Widget filters on `AGENT` with a moved agent: BREAKS.
- Agent-to-agent test with a moved agent as target: BREAKS.
- Test whose only agent is moved: BREAKS until tests are assigned.
- Agent Status widget with "Owned Agents": REVIEW.
- Suppression window for a moved agent: REVIEW.
- Alert rule on a test of a moved agent: OK (tests keep their IDs).

### Output

Excel sheets:

1. Summary: counts for each flag.
2. Tree: one row for each element, indented by level.
3. Selected: agents and clusters chosen for migration.
4. Offline: agents that will not move.
5. Shares: agent by group matrix.
6. Clusters: members, tests, utilization.
7. Tests and alerts: test, agents, alert rules, tags, flag.
8. Dashboards: dashboard, widget, filter, agents, flag.
9. Manual tasks: notification rules, suppression windows, test targets.

An HTML file shows the same tree. It is one page with no external
files. The operator can expand and collapse each level, search by name,
and filter by flag. The HTML view is the main way to see the
relationships.

Excel needs `openpyxl`. The migration tool allows only three libraries.
The report tool is separate, so it can use `openpyxl`. If this is not
allowed, the report uses CSV files that open in Excel.

The tool runs again after the migration. It compares the two results
and lists any difference.

---

## 13. Output style and dry run

### Ansible-style output

The tool prints all output in Ansible style. This applies to dry run
and to apply. One play for each run. One task for each step.

```
PLAY [Migrate Enterprise Agents: SRC (1000001) -> TGT (2000001)] ***
MODE: DRY-RUN   STRATEGY: D

TASK [preflight : check agent state] *****************************
ok:      [SITE-A-EA01]
skipped: [SITE-B-EA02] => offline, not migrated

TASK [ui : reset agent] ******************************************
changed: [SITE-A-EA01] => would reset (dry run)

TASK [api : share agent back to source group] ********************
failed:  [SITE-C-EA03] => HTTP 403 (request-id=abc123)

PLAY RECAP *******************************************************
SITE-A-EA01   : ok=8  changed=5  skipped=1  failed=0
SITE-B-EA02   : ok=1  changed=0  skipped=1  failed=0
SITE-C-EA03   : ok=5  changed=2  skipped=0  failed=1
```

Rules:

- Use these status words: `ok`, `changed`, `skipped`, `failed`.
  Use `paused` when the tool waits for the operator.
- Print each task header once. Print one result line for each
  agent or cluster.
- Print a `PLAY RECAP` at the end with counts for each host.
- A host is an agent or a cluster.
- Use color only on a terminal. Respect `NO_COLOR`.
- Never print tokens, passwords or cookies.
- The exit code is not zero when any host has `failed`.
- The JSON and CSV reports keep the same task names and statuses.

### Dry run for Strategy D

Dry run is the default. It must show every action that apply does.

```
 Step                     Dry run                  Apply
 -----------------------  -----------------------  ---------------
 Read API and agent UI    runs                     runs
 UI reset                 not sent                 sent
 Target token             not sent                 sent
 Share agent back         not sent                 sent
 Assign tests             not sent                 sent
 Re-enable tests          not sent                 sent
 Restore tags             not sent                 sent
 Cluster unassign/assign  not sent                 sent
 Delete old agent         not sent                 sent
 Rename agent             not sent                 sent
```

Dry run rules:

- Dry run sends only read calls (`GET`) and UI login.
- Dry run prints each write action as `changed` with the text
  `would ...` (for example `would share with 17 groups`).
- Dry run shows the planned values: groups to share, test IDs to
  assign per group, tags to restore, and cluster members to rebuild.
- Dry run runs the same preflight as apply. A blocking issue gives
  `failed` in dry run too.
- Dry run lists the manual tasks (notification rules, suppression
  windows, test targets).
- Dry run needs no target account token. Apply needs it.
- Dry run writes the same reports. The reports show the mode.
- A clean dry run is required before apply for the same selection.
  The tool shows the dry-run result again before it asks to confirm.

---

## 14. Tool changes (Phase 2)

- Add Strategy D with full dry-run support. Block Strategy C in apply
  mode. Warn on A and B.
- Replace the console output with Ansible-style output (section 13).
- Add the `type` column to the inventory.
- Add the preflight: offline agents, clusters with offline members,
  shared agents, agent-to-agent targets, last-agent tests.
- Add the cluster module (unassign, reset, rebuild, share, assign,
  re-enable).
- Match clusters by ID. Never delete a cluster ID.
- Restore sharing, tests and tags.
- Add backoff for `429` on write calls.
- Save a snapshot before every change.
- Add unit tests with a mocked API.
- Keep Python 3.6 compatibility and the three-library limit.

There is no automatic rollback. A reset cannot be undone. Manual
recovery means a second move with the old token and the snapshot.

---

## 15. Phases and gates

```
 P0  Discovery                  DONE
 P0b Fix script, run again      needs your OK
 P1  Relationship report        needs your OK
 P2  Tool changes               needs your OK
 P3a Sandbox build (section 20) agents, groups, tests, dashboards
 P3  Lab tests                  standalone, then cluster
 P4  Pilot in production        1 agent, then 1 small cluster
 P5  Waves                      operator-selected batches
```

Each phase ends with your review. No phase starts without your OK.

P0b tasks:

- Fix the dashboard detail loop (list is a plain array).
- Limit dashboard details to custom dashboards in the groups you name.
- Add `expand=assignments` to the tag call.
- Add `GET /agents/{id}` for the selected agents (owner, sharing).
- Add test details (`GET /tests/{type}/{id}`) for tests that use a
  selected agent. This gives alert rules, tags and agent lists.
- Agent-to-agent targets are already in the test list
  (`targetAgentId`, 221 tests).
- Add `discovery_output/` to `.gitignore`.
- Review the 8 transaction scripts that contain the words
  "password" or "credential".

---

## 16. Lab checks before production

0. Dry run output matches the apply result for the same selection
   (same tasks, same planned values).
1. `PUT /agents/{id}` with `accountGroups` shares the agent. Does it
   replace the list or add to it?
2. `tests/assign?aid=<source>` works for an agent owned by the target.
3. A test that was disabled by an agent removal can be enabled again.
4. Tag assignment on a shared agent works (section 9).
5. Cluster unassign, reset and rebuild keep the cluster name.
6. Member and cluster IDs after the rebuild.
7. Dashboards in the source group still show data.
8. Name rule: agent names must be unique.

The lab has no cluster now. A lab cluster is required.

---

## 17. Open items

- Production inventory and the source and target group IDs.
- Names of the groups for the dashboard details.
- Excel with `openpyxl` (yes or no).
- Maintenance window length for clusters.
- Who does the manual steps (notification rules, suppression windows).
- A lab cluster (the sandbox in section 20 gives it).
- Sandbox facts: see section 20.1 (questions).

---

## 18. Security notes

- `.env` holds a live token and an agent UI password in plain text,
  in a synced folder. Move them to a secret store. Rotate the token
  after the migration.
- The discovery files can hold secrets in transaction scripts. Do not
  commit or share them.
- The agent UI uses HTTPS without certificate checks (self-signed
  agent certificates). The API v7 connection keeps full checks.

---

## 19. Orchestration and progress log

Started: 28 Oct 2026. Work that does not need new discovery data.

### Rules for all workers

- No network call. No API call. No agent UI call.
- Do not read secrets. Do not print `transactionScript` fields.
- Use synthetic test data. Do not copy discovery data into the repo.
- Python 3.6 syntax. Standard library only (migration tool: also
  `requests`, `python-dotenv`, `urllib3`).
- Edit files with small replacements. Do not rewrite shared files.
- Each worker owns the files listed for its task.
- The orchestrator runs the full test suite after each wave.
- The orchestrator is the only writer of this log.

### Model use

- Orchestrator: this session.
- Workers: `claude-sonnet-5-5-high` (no Haiku model is available).
- Review: `claude-opus-5-5-medium`.

### Task map

```
 WAVE 1 (parallel, separate files)
  T1 Ansible-style console      output.py, tests/test_output.py
  T2 Inventory v2 (type col)    inventory.py, InventoryAgent in models.py
  T3 API client v7 additions    api_client.py, AgentRecord in models.py
  T4 Report core (graph, flags) te_report/ (new package)

 WAVE 2 (needs wave 1)
  T5 Strategy D preflight       te_agent_migrate/strategy_d/preflight.py
  T6 Strategy D standalone flow te_agent_migrate/strategy_d/standalone.py
  T7 Strategy D cluster flow    te_agent_migrate/strategy_d/cluster.py
  T8 Report HTML + CSV/Excel    te_report/render_*.py
  T9 Opus review                all new code

 WAVE 3 (orchestrator)
  T10 CLI wiring, block C, full test run, update this log
```

### Contracts between tasks

API client (T3), used by T5 to T7:

```
get_agent(aid, agent_id)                      -> AgentRecord
list_agents_with_clusters(aid)                -> [AgentRecord]
set_agent_account_groups(aid, agent_id, ids)  -> None
assign_tests(aid, agent_id, test_ids)         -> None   (exists)
cluster_assign(aid, base_agent_id, agent_ids) -> dict
cluster_unassign(aid, cluster_id, member_ids) -> dict
list_tags(aid, expand_assignments=False)      -> [dict]
assign_tag(aid, tag_id, assignments)          -> None
```

`AgentRecord` gets: `is_cluster`, `cluster_members`
(list of dict: memberId, name, ipAddresses, state), `shared_aids`
(list of str).

Inventory (T2): `InventoryAgent.kind` is `agent` or `cluster`.
Legacy header `hostname,ip_address` stays valid (kind = `agent`).
New header: `type,name,ip_address`.

Console (T1): same method names as today, plus `play()`, `task()`,
`recap()` in Ansible style. `has_failures()` tells the exit code.

Report (T4): `python -m te_report --input DIR --selection FILE
--output DIR` writes `graph.json` and CSV files.

### Progress

| Task | Model | State |
|---|---|---|
| T1 | sonnet | done (21 tests pass) |
| T2 | sonnet | done (26 tests pass) |
| T3 | sonnet | done (33 tests pass) |
| T4 | sonnet | done (48 tests pass) |
| T5 | sonnet | done (27 tests pass) |
| T6 | sonnet | done (38 tests pass) |
| T7 | sonnet | done (66 tests pass) |
| T8 | sonnet | done (22 tests pass) |
| T9 | opus | done: NO-GO for apply; GO for read-only dry run after T10 |
| F1 to F4 | sonnet | done (fix wave) |
| T11 | opus | done: dry run GO; standalone apply conditional GO; cluster apply NO-GO |
| G1, G2 | sonnet | done (fix wave 2) |
| T10 | orchestrator | waiting |

### Log

- 28 Oct: baseline = 18 unit tests pass.
- 28 Oct: added `discovery_output/` and `report_out/` to `.gitignore`.
- 28 Oct: wave 1 started. T1, T2, T3 and T4 run in parallel (Sonnet).
- 28 Oct: T2 done. Inventory v2 (3 headers, `kind`, `agent_id`, `describe_inventory`). Own tests pass. Full suite has 3 errors in `tests/test_output.py`; T1 still in progress.
- 28 Oct: T1 done. Ansible-style `MigrationConsole` (`play`, `task`, `recap`, `has_failures`, secret masking). Full suite: 62 tests pass. T10 must call `play()` and set the exit code from `has_failures()`.
- 28 Oct: T3 done. API v7 client: `get_agent`, `list_agents_with_clusters`, `set_agent_account_groups`, `cluster_assign`, `cluster_unassign`, `assign_tag`, cluster delete guard, 429-only write retry. Lab checks: bare-array response from cluster assign/unassign; 200/201/204 accepted.
- 28 Oct: wave 2 prep. Shared contract written: `te_agent_migrate/strategy_d/context.py` (`FlowContext`, `FlowResult`, `FlowAborted`, `PreflightItem`). T5 to T7 not launched yet.
- 28 Oct: discovery re-run finished. 34 groups. 1012 index rows, 680 unique agent ids (910 enterprise, 102 cluster rows). 106 agents detailed (71 online, 19 offline, 4 disabled, 12 clusters). Only failures: 36 HTTP 403 and 3 HTTP 400, all on dashboard detail calls (built-in or shared dashboards). No agent, test, tag or alert call failed.
- 28 Oct: T5 done. `strategy_d/preflight.py`: `run_preflight`, `print_preflight`, `summarize`, `has_blockers`. Read-only. Offline device = excluded; unreadable shared group, same source/target, name collision = blocked. Full suite: 170 tests pass. Lab checks: cluster member field names; `accountGroups` lists all shares; tag assignment type name for agents; `testIds` in each shared group.
- 28 Oct: T4 done. `te_report/` (loader, graph, impact, outputs, CLI). On real discovery output: 34 groups, 668 agents, 12 clusters, 4132 widgets, 1s. Test selection gave 2195 BREAKS, 415 REVIEW, 1423 OK. Not verified against expected results. Gaps: agent details have no owner field (all `owner_unknown`); suppression and alert-rule files have no agent or test links.
- 28 Oct: T6 done. `strategy_d/standalone.py`: `migrate_standalone`, `plan_standalone`. 14 named steps. Dry run makes zero API and UI calls. Share list is always full and never empty. Wrong-group check before delete. Full suite: 208 tests pass. Lab checks: `accountGroups` replace vs add; `GET /agents/{id}` returns `accountGroups`; test assign with source aid; new agent name right after registration; UI delay after reset.
- 28 Oct: T7 was not running (no `cluster.py`). T7 (cluster flow) and T8 (HTML and Excel renderers) launched now, in parallel. T9 (Opus review) starts after both finish. Review must also check the BREAKS rules in `te_report/impact.py` against section 8 (2195 BREAKS looks too high).
- 28 Oct: T8 done. `te_report/render_html.py` and `render_xlsx.py`; CLI flags `--html`, `--xlsx`. Real data smoke test: HTML 6.1 MB in 0.16 s; xlsx 1.67 MB. No secret-like words in output. Full suite: 230 tests (5 skipped without openpyxl). Not done: browser check of the HTML; no name search in the tree; Tree sheet size not checked on large data.
- 28 Oct: T7 done. `strategy_d/cluster.py`: `migrate_cluster`, `plan_cluster`; `api_client.set_test_enabled` added. Failure line has RECOVERY DATA (old ids, members, shares, test ids by enabled state). Full suite: 296 tests (5 skipped). Lab checks: new cluster id = base agent id; tests disabled (not deleted) after unassign; full-payload PUT re-enables a test without side effects; assign 100 ids per call; `accountGroups` PUT replace vs add; cluster tags use `v-agent`; reset keeps member IP.
- 28 Oct: T9 (Opus review) launched. Read-only. Scope: strategy_d/*, api_client additions, output.py, inventory.py, te_report/*.
- 28 Oct: T9 done. Verdict: NO-GO for a lab apply run. GO for a read-only dry run after CLI wiring.
  - BLOCKER: preflight reads shares from the agent list, which has no `accountGroups` (0 of 1012 rows). `share_aids` was always the source only. Fix: use `GET /agents/{id}` plus a scan of every group listing.
  - HIGH: owner group never checked; match by IP alone; wait-for-new-agent accepts IP or name and can pick a foreign agent; a write that returns a JSON array raises after the write; no state line on Ctrl-C and no snapshot on disk.
  - Impact report: `test_all_agents_selected` gave 2078 of 2195 BREAKS. Standalone becomes OK. Cluster becomes REVIEW. Expected BREAKS about 117. Missing rules: group by agent (326 widgets), alert list widgets (180), agent notification rules, minimum-sources alerts. Stop matching on public IPs.
  - Wiring: Strategy D missing in CLI; dry run asks for token; exit code always 0; `play()` and `recap()` unused; parallel must be 1; `FlowAborted` not caught.
- 28 Oct: fix wave launched. F1 preflight. F2 flows, context and write handling. F3 report rules. F4 CLI wiring.
- 28 Oct: F1 done. `preflight.py` rewritten (68 tests). Shares from `GET /agents/{id}` plus scan of every group; mismatch blocks. IP and name must both match. Sets `ui_ip`, `warnings`, `test_enabled`, `owner_proven`, `share_sources` on each item. OPEN ISSUE: owner proof needs `ownerAid` or `ownerAccountGroupId` in the API data. The discovery has no owner field, so every device would be blocked. Needs a decision (see below).
- 28 Oct: F3 done. Report rules fixed. Real data (83 selected, 23 offline excluded): BREAKS 117 (58 `filter_agent_id`, 59 `test_target_agent`), REVIEW 2339, OK 1854. New rules: group by agent (181), alert list (93), notification rule, min sources, three data-gap summaries. 89 report tests pass. Dead on real data: `alert_min_sources`, `agent_notification_rule` (alert rules have no test links; notification rules have no agent links). Full suite failures (33) are in Strategy D tests while F2 and F4 are still editing.
- 28 Oct: F4 done. `strategy_d/runner.py` (`run_strategy_d`), CLI strategy D, `--confirmed-dry-run-file` (apply needs a clean dry run of the same inventory, hash checked), dry run needs no token, C blocked in apply (exit 2), exit 1 on failure, parallel forced to 1, stop on first failure, summary JSON, snapshot dir. Exit codes: 0 clean, 1 failure, 2 gate refused, 130 Ctrl-C. Open: preflight runs after the confirmation prompt, not before.
- 28 Oct: orchestrator added `--owner-is-source` (CLI) and `assume_owner_source` (runner, preflight). Without it, every device is blocked because the API has no owner field. A device whose data names another owner is still blocked. Lab must confirm the real owner behavior. Waiting for F2; suite still shows failures in `test_strategy_d_cluster` and `standalone` (F2 editing).
- 28 Oct: F2 done. Strict wait for the new agent (id not in the pre-reset target snapshot, IP and name must match, two candidates abort). Writes accept any 2xx body. `BaseException` caught, state line printed. Snapshot file (0600) written before the first destructive call (`snapshot_dir`). Delete state text split into sent and confirmed. Old member ids mapped from the owner listing after unassign. `ReadOnlyApi` wraps the API in dry run. Dry run prints `changed: ... would:`. Retry-After capped at 60 s.
- 28 Oct: orchestrator: CLI apply now runs a read-only preflight and prints it BEFORE the confirmation prompt; blockers stop the run before the prompt. Replaced an obsolete runner test. Full suite: 489 tests pass (5 skipped).
- 28 Oct: T11 (second Opus review) launched, read-only.

- 28 Oct: T11 done. Verdicts: read-only lab dry run GO. Lab apply on one standalone agent: conditional GO. Lab apply on one cluster: NO-GO until H2, H3, H4, M1, M2 are fixed.
  - H1: with `--owner-is-source`, a re-run moved an agent already owned by the target and sent DELETE with the source aid. Fix: block when the target is in the shares or lists the id; bind `dry-run-ok.json` to ids and shares; make it single-use.
  - H2: member UI login happens after the unassign. Fix: check every member UI before.
  - H3: member wait accepts only the member name. Fix: add old hostname and IPs.
  - H4: re-enable PUTs the full test payload (may overwrite masked secrets). Fix: manual list by default; opt-in `--auto-reenable-tests`; minimal `{"enabled":true}` mode for the lab.
  - M1 to M5: summary JSON fields, stop line ids, Ctrl-C summary, ready-set compare dry run vs apply, recovery data on re-enable failure.
  - Not done: M6 (UI login check in dry run, optional), L7 (Python 3.6 run).
- 28 Oct: fix wave 2 launched. G1 preflight, runner, CLI. G2 flows, context, API client, UI client.
- 28 Oct: plan section 20 added (sandbox lab: groups, agents, tests, dashboards, test cases). Seed script not built; waits for your OK. Log lines stay in section 19.
- 28 Oct: G1 done. Preflight blocks a device when the target is in its shares or lists its id (re-run). `dry-run-ok.json` schema 2 binds ready ids, share sets, owner flag and excluded ids; apply compares after a fresh preflight and refuses on any difference (exit 2); file renamed to `.used` before the first flow. Runner stores new ids, state and snapshot of a failed flow; Ctrl-C writes recap and summary. `os.umask(0o077)`; `--auto-reenable-tests` flag. 157 tests in G1 files pass. Open: the runner's own preflight after the prompt is not compared again with the dry-run file.
- 28 Oct: G2 done. Cluster flow checks every member UI before the unassign (`cluster_member_ui_check`). Member wait uses old name, hostname and IPs (IP AND name). Re-enable is manual by default (MANUAL TASK lines); `auto_reenable_tests` = True (full PUT) or `'minimal'` (`{"enabled": true}`, experimental). Stop line has old and new ids, shares, snapshot path; exceptions carry `new_agent_id`, `new_cluster_id`, `snapshot`, `state`. Exact name match or `name-` prefix. IPv6 URL brackets. Writing steps print `changed`. Full suite: 586 tests pass (5 skipped).
- 28 Oct: sandbox answers received (see 20.1). Seed script approved. Worker S1 builds `sandbox/seed_sandbox.py` (dry run default, only touches groups named in a map file, only `SBX-` objects).
- 28 Oct: S1 done. `sandbox/seed_sandbox.py` (38 tests): steps clusters, shares, tests, tags, alerts, report; dry run default; `--apply`; `--cleanup` (only `SBX-` objects); gates (sandbox flag in map and CLI, group visibility, no repeated ids). Creates 182 tests in SOURCE, 10 in VIEW1, 3 in VIEW2; 4 tags; 2 alert rules. Manual: dashboards D1 to D8, agent notification rule, suppression window (file `sandbox_manual_tasks.md`). Lab must verify v7 payload fields on first apply (listed in the README). Full suite: 624 tests (5 skipped).
- 28 Oct: dashboard-first report added (`te_report/dashboard_view.py`, `render_dashboards.py`). With `--selection` the report now also writes `dashboards.html`, `dashboards.csv`, `widgets.csv`: one verdict per dashboard and per widget (BREAKS, CHANGES, OK, UNAFFECTED, NOT CHECKED). Real data, 83 selected agents: dashboards BREAKS 31, CHANGES 173, OK 136, UNAFFECTED 272, NOT CHECKED 243; widgets BREAKS 58, CHANGES 554, OK 885, UNAFFECTED 2635. Full suite: 632 tests (5 skipped).


---

## 20. Sandbox lab (P3a)

The sandbox is a copy of the customer setup, at a small size.
It proves the tool and the lab checks (section 16) before production.

### 20.1 Facts to confirm first

| # | Question | Why |
|---|---|---|
| 1 | Can you create account groups in the sandbox org? | Need 4 to 5 groups. |
| 2 | Which agent type can you run? TEVA (OVA) or Docker/Linux package? | The tool resets agents in the **TEVA web UI**. Docker and Linux package agents have no such UI. |
| 3 | How many agent VMs can you run? | Minimum 8. Better 11. |
| 4 | Does the API token reach all sandbox groups? | The tool and discovery need it. |
| 5 | Can the VMs reach the Internet and the TE cloud (443)? | Agents must register. |
| 6 | Can the machine that runs the tool reach each agent on HTTPS/443? | UI reset and token step. |

Answers (28 Oct): groups can be created (4 to 5); all agents are TEVA
appliances; up to 12 VMs (use the 12-VM layout with C2); the token covers
all groups; the tool machine reaches every agent on 443 and agents reach
the TE cloud. Result: the full layout in 20.4 is possible, including the
UI reset path.

If only Docker or Linux agents are possible, you can test only the API
side (share, assign, cluster, delete, report). You cannot test the UI
reset. Keep at least 3 TEVA agents for that.

### 20.2 Rules

- Sandbox only. Use a **sandbox** token. Never use the production token.
- Use fake targets (for example `example.com`, a lab web server).
- Use fake credentials in transaction tests. Never copy production scripts.
- Name everything `SBX-...` so it cannot be confused with production.
- Keep the discovery of the sandbox in its own folder (`sandbox_output/`).

### 20.3 Account groups

```
 SBX-SOURCE   owns the agents today; owns ALL tests, tags, dashboards
 SBX-TARGET   empty; the new owner of the moved agents
 SBX-VIEW-1   sees shared agents; has its own tests
 SBX-VIEW-2   sees shared agents; has its own tests
 SBX-OTHER    (optional) owns one agent that it shares into SBX-SOURCE
              (owner check, lab check 4)
```

Production has 34 groups. Four or five are enough. The cluster
sharing in production is wide (17 or 18 groups). VIEW-1 and VIEW-2 show
the pattern.

### 20.4 Agents

```
 SBX-SOURCE
  S1  standalone, no sharing, 5 tests         baseline
  S2  standalone, no sharing, 5 tests         baseline, 2nd device
  S3  standalone, shared to VIEW-1, VIEW-2    tests in SOURCE + VIEW-1
  S4  standalone, target of an agent-to-agent test        flag: BREAKS
  S5  standalone, powered OFF, 3 tests        offline: excluded
  S6  standalone, disabled in the UI/API      disabled: excluded
  C1  cluster, 2 members (C1a, C1b), shared to VIEW-1, VIEW-2
  C2  cluster, 3 members (optional, as in production)
 SBX-OTHER
  X1  standalone, owned by OTHER, shared into SOURCE      owner check
```

VM count: S1 to S6 = 6, C1 = 2, X1 = 1 -> 9. With C2 -> 12.
Check the TE sizing guide for CPU and memory per agent.

Why each agent exists:

| Agent | Proves |
|---|---|
| S1, S2 | Standard flow. Batch of two (one at a time). |
| S3 | Share back to many groups; tests in two groups. |
| S4 | Agent-to-agent target. The report must flag BREAKS. |
| S5, S6 | Offline and disabled agents are excluded and reported. |
| C1 | Cluster flow, window, tests lose agent, re-enable. |
| C2 | Three-member cluster (optional). |
| X1 | Owner is not the source. The tool must block it. |

### 20.5 Tests (in SBX-SOURCE unless noted)

Production mix (3,622 tests): 83% agent-to-server, 4% agent-to-agent.
The sandbox copy:

| Set | Count | Assigned to | Purpose |
|---|---|---|---|
| HTTP server tests, 5 min | 5 each | S1, S2 | Baseline. |
| Network tests (ICMP/TCP) | 10 | S3 | Shared agent tests in SOURCE. |
| Tests in VIEW-1 | 5 | S3, C1 | Tests in a second group. |
| Agent-to-agent test | 2 | S4 is the target; S1 is the source | `test_target_agent` BREAKS. |
| Tests on cluster | 30 | C1 | Cluster flow. |
| Large cluster set | **120** | C1 | More than 100 ids: proves assign chunking (lab check 8). |
| DNS, page load | 3 | S2 | Other test types. |
| Transaction test | 1 | S1 | Fake script with a fake user and password. Proves that re-enable does not damage credentials (T11 finding H4). |
| Test with one agent only | 2 | S5 | Test loses its last agent. |
| Disabled test | 3 | C1 | Must stay disabled after the move. |

Production has 14 to 572 tests per cluster. 120 on C1 covers the chunk
rule. Add a second cluster with 300 tests only if time allows.

### 20.6 Tags, alerts, dashboards

Tags:

- Agent tags: `env=sbx`, `site=a` on S1, S3, C1.
- Test tags: `app=web`, `app=dns` on a few tests.

Alerts (in SBX-SOURCE):

- 1 default alert rule on the HTTP tests.
- 1 rule with minimum sources 2 on the cluster tests
  (rule `alert_min_sources`).
- 1 agent notification rule on S1 (manual step).
- 1 suppression window on a tag.

Dashboards (in SBX-SOURCE). Use the production widget mix: Time Series,
Color Grid, Number, Test Table, Alert List, Agent Status, Map.

| Dashboard | Content | Expected flag |
|---|---|---|
| D1 | Test filter only (tests of S1, S2) | OK |
| D2 | Widget filter on **agent id** (S1) | BREAKS (`filter_agent_id`) |
| D3 | Filter on **agent tag** `env=sbx` | REVIEW |
| D4 | Widget **group by agent** | REVIEW |
| D5 | Agent Status, "All Assigned Agents" | OK |
| D6 | Alert List widget | REVIEW |
| D7 | Global filter on test tag `app=web` | OK |
| D8 | Cluster tests, Color Grid | REVIEW (cluster) |

This gives the same flags that the report shows on production data.
The result is a known answer to compare with.

### 20.7 Build steps

```
 1. Create the 4 or 5 account groups.            (UI)
 2. Create a sandbox token with access to all.   (UI)
 3. Deploy and register the agents in SBX-SOURCE (X1 in SBX-OTHER).
 4. Build C1 (and C2) from two or three registered agents.
 5. Share S3, C1 (C2) to VIEW-1 and VIEW-2.      (UI or PUT accountGroups)
 6. Create tags, tests, alert rules, suppression.   (script, API v7)
 7. Create dashboards D1 to D8.                  (UI, then export for a copy)
 8. Run te_discovery.sh on the sandbox.          -> sandbox_output/BEFORE
 9. Run the report (te_report). Compare flags with the table in 20.6.
```

Steps 6 and 7 can run from a script. Plan: `sandbox/seed_sandbox.py`
(tests, tags, alert rules) with a dry-run mode. Dashboards are easier by
hand in the UI. The script is built: `sandbox/seed_sandbox.py` (see `sandbox/README_SANDBOX.md`).

### 20.8 Lab test cases

| # | Case | Expected |
|---|---|---|
| L1 | Dry run, no `--owner-is-source` | Every device failed, "owner not proven", exit 1. |
| L2 | Dry run with `--owner-is-source` | Exit 0, zero writes, `dry-run-ok.json` written. |
| L3 | Owner probe (section 20.9) | Proves who owns an agent. |
| L4 | Apply S1 | New id in TARGET; tests in SOURCE keep ids; old agent deleted last. |
| L5 | Apply S3 | Shares and tests restored in SOURCE, VIEW-1, VIEW-2. |
| L6 | S5, S6 in the inventory | Excluded, listed, untouched. |
| L7 | X1 in the inventory | Blocked (owner is another group). |
| L8 | Run L4 again (re-run) | Blocked (already in target). |
| L9 | Wrong target token | Wrong-group abort; nothing deleted. |
| L10 | Block port 443 to one agent | Preflight or UI step fails; batch stops; state line shown. |
| L11 | Press Ctrl-C during a flow | State line, snapshot file, exit 130, summary written. |
| L12 | Apply C1 | New cluster id = first new member; 120 tests assigned; disabled tests stay disabled. |
| L13 | Cluster: wrong UI password on member B | Aborts **before** the unassign (needs fix H2). |
| L14 | Transaction test after C1 | Credentials unchanged (H4); try minimal re-enable. |
| L15 | Compare BEFORE and AFTER discovery | Test ids unchanged; dashboards still show data; flags match 20.6. |

Lab checks 1 to 8 of section 16 are covered by L3 to L15.

### 20.9 Owner probe (lab check)

Agent X1 is owned by SBX-OTHER and shared into SBX-SOURCE.

1. `GET /agents/{X1}` with `aid=OTHER` and with `aid=SOURCE`. Compare.
2. `PUT /agents/{X1}` with the same `agentName`, `aid=SOURCE`.
   Expect an error (not the owner).
3. The same `PUT` with `aid=OTHER`. Expect 200.
4. Write down which field, if any, tells the owner. Put it in the plan.

### 20.10 Reset between runs

```
 Reset agent (UI) -> token of SBX-SOURCE -> online in SOURCE
 Delete leftovers in TARGET
 Rebuild C1 and the shares
 Restore tests from the BEFORE discovery or from the snapshot file
```

Keep the VMs. Keep a clean copy of the test and dashboard set (script
plus dashboard export). A full reset must take less than 30 minutes.

### 20.11 Exit gate for P4

- L1 to L15 pass, or each failure has a decision.
- No unexpected change in test ids.
- Dashboard flags in the report match what you see in the UI.
- The owner probe result is in the plan.
- Python 3.6 run of the unit tests (if production runs 3.6).
