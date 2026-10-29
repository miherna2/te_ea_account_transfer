# Progress Log

Append only. Add new entries at the end of the log. Do not edit old entries.

Related documents: [Plan](MIGRATION_PLAN.md), [Lab](LAB.md).

## Work rules

- Workers make no network call and no API call.
- Workers do not read secrets or customer data.
- Test data is synthetic.
- Code is Python 3.6 compatible. Only `requests`, `python-dotenv` and `urllib3` are allowed.
- One owner for each file. The orchestrator runs the full test suite after each wave.

## Task map

```
 WAVE 1 (parallel)
  T1 Ansible-style console      output.py
  T2 Inventory v2               inventory.py
  T3 API client v7 additions    api_client.py
  T4 Report core                te_report/
 WAVE 2
  T5 Preflight   T6 Standalone flow   T7 Cluster flow   T8 HTML and Excel
  T9 Review
 WAVE 3
  T10 CLI wiring, block Strategy C, full test run
```

## Log

- 28 Oct 2026: baseline. 18 unit tests pass. Wave 1 starts.
- 28 Oct: T1 done. Console prints `ok`, `changed`, `skipped`, `failed` and a recap. Secrets are masked.
- 28 Oct: T2 done. Inventory v2: column `type`, optional `agent_id`.
- 28 Oct: T3 done. API client: share, cluster, tag and detail calls. Retry on 429 for writes.
- 28 Oct: Discovery ran against the production API (read only). Only dashboard detail calls failed (403, 400).
- 28 Oct: T5 done. Preflight (read only). Offline devices are excluded.
- 28 Oct: T4 done. Report package: loader, graph, impact rules, CSV output.
- 28 Oct: T6 done. Standalone flow with 14 named steps. Dry run sends no call.
- 28 Oct: T8 done. HTML and Excel output.
- 28 Oct: T7 done. Cluster flow (now out of production scope).
- 28 Oct: T9 review. No-go for apply. Go for a dry run after CLI wiring.
  - BLOCKER: the agent list has no share data. Fix: read `GET /agents/{id}` and scan all groups.
  - HIGH: owner never checked. New-agent wait could pick a foreign agent. No snapshot on disk.
  - Report: too many BREAKS. Rules were too wide.
- 28 Oct: fix wave 1. Preflight rewritten (share proof, IP and name must match).
  Strict wait for the new agent. Snapshot file before the first reset.
  Report rules narrowed. Strategy D wired in the CLI. Exit codes 0, 1, 2, 130.
- 28 Oct: `--owner-is-source` added. The API has no owner field.
- 28 Oct: apply prints the preflight before the confirmation question.
- 28 Oct: review T11. Dry run: go. One standalone apply in the lab: conditional go.
  - A re-run moved an agent that the target owned. Fix: block when the target owns it. Bind the dry-run file to device IDs and shares. Make it single use.
  - Cluster issues (UI check before unassign, member wait, re-enable of tests).
- 28 Oct: fix wave 2. Dry-run file schema 2. Cluster checks. Manual re-enable by default.
- 28 Oct: lab plan written. Seed script built (`dev/sandbox`, dry run default, `SBX-` objects only).
- 28 Oct: dashboard view added to the report (`dashboards.html`, `dashboards.csv`, `widgets.csv`).
- 28 Oct: full suite: 632 tests pass (5 skipped without `openpyxl`).
- Later: inventory v3 (`target_aid`, `share_aids`). One run can move devices to several targets.
  One registration token for each target group. Dry-run file schema 3.
  Preflight: "already migrated" skip, stale agent ID warning.
- Later: layout change. Code under `te_agent_migrate/`, `te_report/`, `tools/`. Data under `work/`.
  Strategy runner scripts `run.sh` and `run.ps1` removed.
- 28 Oct: scope is now driven by an Excel file: 95 devices, one source group, 21 groups, 72 online.
  Module `te_agent_migrate.scope` is being written.
- 28 Oct: documents rewritten (README, plan, runbook, reports, lab, this log).
  The old plan text was shortened. The old strategy A, B and C text was removed.

- 28 Oct: scope module built. `python -m te_agent_migrate.scope` writes `prefix_map.csv`,
  `inventory.csv` and `scope_report.csv`. First prefix rule was too strict: one anomalous device
  made a whole prefix REVIEW. Rule changed to count devices for each group. Result: 69 READY,
  23 offline, 2 login problem, 1 needs review (prefix CSC).
- 28 Oct: Excel-scoped reports rebuilt (`work/reports/xlsx_migrate/`). 39 BREAKS from 25
  agent-to-agent tests and 14 agent-ID widget filters.
- 28 Oct: DECISION (user): repoint agent-to-agent tests automatically after the move, behind
  `--repoint-agent-to-agent`. The lab must prove it (cases L16 to L20). On a lab failure, ask the user.
- 28 Oct: DECISION (user): CSC target is not decided. The device stays out of the inventory.
  Ask the customer (see the plan, section "Questions for the customer").
- 28 Oct: agent-to-agent repoint built (`--repoint-agent-to-agent`, dry-run file schema 4).
  Gap closed: with the flag, an unreadable test list of a shared group blocks the device.
  API calls are not proven. Lab cases L16 to L20 decide.
- 28 Oct: lab seed is plan-driven. Plan from the Excel file, 12 devices for 12 VMs, 6 waves.
  Dashboards are not seeded (manual list). Suite: 832 tests pass (5 skipped).

## Next steps

1. Finish `te_agent_migrate.scope`. Add `examples/prefix_map.example.csv`.
2. Test the scope module with a synthetic Excel file.
3. Agent-to-agent repoint: finish the code, then pass lab cases L16 to L20.
   **REMINDER: after the lab run, show the evidence to the user and ask how to proceed
   if any case fails.**
4. Run the lab ([Lab](LAB.md#7-lab-from-the-excel-scope)). Run L1 to L11, then L16 to L20.
5. Run the owner probe. Record the result in the plan.
6. Write the resume procedure for each state ([Plan](MIGRATION_PLAN.md#11-rollback-and-recovery)).
7. Name the owner of each manual step. Set the wave list and dates.
8. Run the unit tests on Python 3.6.
9. Run the pilot (2-3 devices). Review. Then start the waves.
10. Update the code comments that point to old plan sections.
