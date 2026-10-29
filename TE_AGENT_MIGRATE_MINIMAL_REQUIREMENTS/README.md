# ThousandEyes Enterprise Agent Migration

This tool moves ThousandEyes Enterprise Agents (TEVA appliances)
from one account group to other account groups.
It uses the ThousandEyes API v7 and the HTTPS UI of each agent.

The tests stay in their account group. They keep their IDs.
Dashboards and alert rules keep working.

## How it works

For each device, the tool does these actions:

```
 1 Reset the agent               (new agent ID: 100 -> 200)
 2 Register it in the target group (Group B)
 3 Share it with every group that saw the old agent
 4 Assign the same tests to the new agent
 5 Delete the old agent          (always last)
```

This is "Strategy D". Read [the plan](docs/MIGRATION_PLAN.md) for details.

Safety rules:

- Dry run is the default. Apply needs a clean dry run of the same inventory.
- The tool stops at the first failure.
- The tool never changes an offline device.
- The tool never prints or stores tokens or passwords.

## Requirements

- Python 3.6 or newer.
- Python must use OpenSSL 1.1.1 or newer (TLS 1.3).
- Access to the ThousandEyes API and to each agent on TCP 443.
- For discovery: `curl`, `jq`, `awk`.
- An API v7 token, the agent UI login, one registration token per target group.

Apple's `/usr/bin/python3` uses LibreSSL 2.8.3.
It cannot connect to agents that need TLS 1.3.
Use a Python.org or Homebrew Python.

TODO: Confirm if the scope module needs `openpyxl` to read `.xlsx` files.
It is not in `requirements.txt`.

## Setup

From the project root:

```bash
PYTHON_BIN=/path/to/python3 ./tools/setup_venv.sh       # macOS or Linux
. .venv/bin/activate
cp .env.example .env                                     # then edit .env
```

```powershell
.\tools\setup_venv.ps1 -PythonBin py                     # Windows
.\.venv\Scripts\Activate.ps1
```

The scripts do not upgrade `pip`. Python 3.6 needs an old `pip`.
Check the TLS support. The last value must be `True`:

```bash
python -c "import ssl; print(ssl.OPENSSL_VERSION, ssl.HAS_TLSv1_3)"
```

`.env` is not committed. The variables are in [the runbook](docs/OPERATIONS.md#9-reference).

## Quick start

The input is an Excel file in `work/input/`. Never commit it.

1. Discover. This step only reads. It writes `work/discovery/`.

   ```bash
   ./tools/te_discovery.sh
   ```

2. Build the scope from the Excel file.

   ```bash
   python -m te_agent_migrate.scope --xlsx work/input/scope.xlsx --out work/input
   ```

   It writes `prefix_map.csv`, `inventory.csv` and `scope_report.csv`.
   Exit code 3 means that a prefix needs review.

3. Edit `work/input/prefix_map.csv`. Set each prefix status to `OK`.
   Run step 2 again.

   TODO: Confirm that the scope module reads the edited `prefix_map.csv`.

4. Make the impact report. `selection.txt` lists the device names
   of `inventory.csv` ([runbook](docs/OPERATIONS.md#4-make-the-report)).

   ```bash
   python -m te_report --input work/discovery \
     --selection work/input/selection.txt --output work/reports/wave1 --html
   ```

5. Do a dry run. Nothing changes.

   ```bash
   python -m te_agent_migrate --strategy D --owner-is-source \
     --inventory work/input/inventory.csv --output work/runs/wave1
   ```

6. Apply. Use the file that the clean dry run wrote.

   ```bash
   python -m te_agent_migrate --strategy D --owner-is-source --apply \
     --inventory work/input/inventory.csv --output work/runs/wave1 \
     --confirmed-dry-run-file work/runs/wave1/dry-run-ok.json
   ```

WARNING: Apply resets real agents. A reset cannot be undone.
Read [the plan](docs/MIGRATION_PLAN.md) before step 6.

## Directory tree

```
.
+-- README.md, requirements.txt, .env.example
+-- te_agent_migrate/     production tool (strategy_d/ = Strategy D flows)
+-- te_report/            production offline report
+-- tools/                te_discovery.sh, setup_venv.sh, setup_venv.ps1
+-- docs/                 documents
+-- examples/             example input files
+-- dev/                  development only: tests/, sandbox/ (lab seed)
+-- work/                 not committed
    +-- input/            Excel file, prefix map, inventory
    +-- discovery/        output of te_discovery.sh
    +-- reports/          output of te_report
    +-- runs/             dry-run files, snapshots, run reports
```

## Documents

- [docs/MIGRATION_PLAN.md](docs/MIGRATION_PLAN.md): goal, rules, flow, waves, risks
- [docs/OPERATIONS.md](docs/OPERATIONS.md): runbook with commands and failures
- [docs/REPORTS.md](docs/REPORTS.md): how to read the impact report
- [docs/LAB.md](docs/LAB.md): sandbox lab
- [docs/PROGRESS.md](docs/PROGRESS.md): progress log and next steps

## Tests

The tests call no API:

```bash
python -m unittest discover -s dev/tests -t .
```

Strategies A, B and C are old. Do not use them. Strategy C is blocked in apply mode.
The scripts `run.sh` and `run.ps1` do not exist. Run `python -m te_agent_migrate`.
