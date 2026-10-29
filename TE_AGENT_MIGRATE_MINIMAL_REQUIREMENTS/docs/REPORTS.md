# Reports

The report package shows what a move can break.
It reads only local discovery files. It calls no API.

Related documents: [Plan](MIGRATION_PLAN.md), [Runbook](OPERATIONS.md).

## 1. Purpose

A reset gives an agent a new agent ID.
Tests keep their IDs. Items that point to the old agent ID can break.

```
 work/discovery  --+
                   |--> te_report --> findings, dashboards, widgets, tree
 selection.txt   --+
```

Run the report before every wave. Run it again after every wave.

## 2. Run the report

The command is in [the runbook](OPERATIONS.md#4-make-the-report).

```bash
python -m te_report --input work/discovery --selection FILE \
  --output work/reports/<name> [--html] [--xlsx]
```

- The selection file has one agent ID, name or IP on each line.
- Without `--selection`, the report shows the inventory only. It has no verdicts.
- `--xlsx` needs `openpyxl`.
- The command prints counts. Read the line `Selection:`.
  It shows how many identifiers are unmatched or ambiguous.

## 3. Output files

All files are in the `--output` folder.

| File | Content |
|---|---|
| `findings.csv` | One row for each finding, worst first |
| `dashboards.csv` | One row for each dashboard, with a verdict |
| `dashboards.html` | The same, as one page |
| `widgets.csv` | One row for each widget, with a verdict |
| `selected.csv` | The devices that the selection matched |
| `offline.csv` | Devices that are not online. They do not move |
| `shares.csv` | Which group sees which device |
| `tree.csv` | The full tree: group, device, test, dashboard |
| `graph.json` | The data model |
| `report.html` | One page with tree and findings (with `--html`) |
| `report.xlsx` | The same as Excel (with `--xlsx`) |

The `findings.csv` columns are:
`level`, `element_kind`, `element_id`, `element_name`, `reason`, `fix`,
`fix_owner` (`tool` or `operator`), `rule` and `group`.

## 4. Verdicts

### Findings

| Level | Meaning |
|---|---|
| `BREAKS` | It stops working after the move. A fix is needed |
| `REVIEW` | Check it by hand |
| `OK` | No change expected |

The main rules:

| Rule | Level | Meaning |
|---|---|---|
| `test_target_agent` | BREAKS | An agent-to-agent test uses a moved agent as target |
| `filter_agent_id` | BREAKS | A widget or global filter uses a moved agent ID |
| `filter_agent_tag` | REVIEW | A filter uses an agent tag. The tool restores the tags |
| `widget_group_by_agent` | REVIEW | History splits between old and new agent |
| `widget_alert_list` | REVIEW | Active alerts clear. New ones start |
| `widget_owned_agents` | REVIEW | A widget shows "Owned Agents". Ownership changes |
| `agent_notification_rule` | REVIEW | Rules need a manual restore |
| `suppression_agent` | REVIEW | A suppression window names a moved agent |
| `sharing_lost` | REVIEW | The tool shares the agent again |
| `test_all_agents_selected` | OK | The tool assigns the tests before it deletes the old agent |
| `test_keeps_id` | OK | The test keeps its ID and group |
| `filter_test_only` | OK | The filter uses only tests |
| `single_group` | OK | One group sees the agent. Nothing to share |

Rules that start with `data_gap_` mean that the discovery data cannot
link an item. Check those items by hand.

Cluster rules (`cluster_id_changes`, `alert_min_sources`) do not apply:
clusters are out of scope.

### Dashboards and widgets

| Verdict | Meaning |
|---|---|
| `BREAKS` | A filter uses a moved agent ID. The widget shows no data |
| `CHANGES` | The widget works. Its content changes. Check it |
| `OK` | It uses moved agents or their tests. Nothing changes |
| `UNAFFECTED` | No link to any moved agent |
| `NOT CHECKED` | Built-in dashboard, or no detail was downloaded |

A dashboard gets the worst verdict of its widgets and its global filter.

## 5. Read the HTML

`dashboards.html` is the first page to read.

1. Read the summary table. It counts dashboards and widgets for each verdict.
2. Use the verdict buttons to show one verdict.
3. Use the search box for a dashboard, widget or group.
4. Open each `BREAKS` dashboard. Note the widget and the agent ID.

`report.html` shows the tree and the findings.

1. Use "Expand all" and "Collapse all" for the tree.
2. Use the level buttons and the text box to filter the findings.
3. Read each `BREAKS` finding first. The `fix` text gives the action.

## 6. Post-wave checklist

Do this after every wave. Use the summary JSON for the new agent IDs.
See [the runbook](OPERATIONS.md#7-read-the-output).

1. Make a new report with the same selection.
2. For each `test_target_agent` finding:
   update the test target to the new agent ID.
3. For each `filter_agent_id` finding:
   update the widget or global filter to the new agent ID.
4. For each `agent_notification_rule` finding:
   restore the agent notification rules in the UI.
5. Check each `suppression_agent` finding.
6. Open each dashboard that was `BREAKS` or `CHANGES`. Check the data.
7. Check that each moved agent is online, in the target group and has its tags.
8. Check that each test has its agent and is enabled.
9. Check that no device moved twice. Look for `skipped: already migrated`.
10. Write the result in [Progress](PROGRESS.md).

TODO: Name who does steps 2 to 5.
TODO: Set the time limit for the checklist after each wave.

## 7. Limits

- The agent list has no owner field. The report cannot show the owner group.
- Alert rules and notification rules have no links to tests or agents
  in the discovery data. The report cannot flag them. It gives `data_gap_` findings.
- The HTML tree has no name search.
- Dashboard details come from custom dashboards only.
  Built-in dashboards show `NOT CHECKED`.
- The verdict rules are not verified against a known answer.
  The lab dashboards give one ([Lab](LAB.md)).
