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

This is "Strategy D".

Safety rules:

- Dry run is the default. Apply needs a clean dry run of the same inventory.
- The tool stops at the first failure.
- The tool never changes an offline device.
- The tool never prints or stores tokens or passwords.

## Requirements

- Python 3.6 or newer.
- Python must use OpenSSL 1.1.1 or newer (TLS 1.3).
- Access to the ThousandEyes API and to each agent on TCP 443.
- An API v7 token, the agent UI login, one registration token per target group.

Apple's `/usr/bin/python3` uses LibreSSL 2.8.3.
It cannot connect to agents that need TLS 1.3.
Use a Python.org or Homebrew Python.

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

`.env` is not committed. The variables are listed in `.env.example`.

## Inventory file

The inventory is a CSV file that lists the devices to move.
Copy `examples/inventory.example.csv` to `work/input/inventory.csv` and edit it.

```
type,name,ip_address,agent_id,target_aid,share_aids
agent,AAAA - HOST01,192.0.2.11,5001,100,100;900;400
agent,AAAA - HOST02,192.0.2.12,5002,100,100;900;300
agent,BBBB - HOST03,192.0.2.13,,200,200;900
```

| Column | Required | Meaning |
|---|---|---|
| `type` | yes | `agent` for a single agent (`cluster` for a cluster) |
| `name` | yes | Agent name, exactly as the agent list shows it |
| `ip_address` | yes for `agent` | The IP address of the agent (its UI must answer on TCP 443) |
| `agent_id` | no | Current agent ID, digits only. With it the tool also skips a device that already moved |
| `target_aid` | no | ID of the account group that receives this device. Empty: `TE_TARGET_AID` |
| `share_aids` | no | Account group IDs, separated by `;`. Must contain `target_aid`. Empty: the tool shares the new agent with the groups that see the old agent today, plus the target |

Rules:

- Keep the header and the column order. After `ip_address`, each of the last three columns is optional.
- Names and IP addresses must be unique. IDs have digits only. Do not add credential columns.
- One row for each device. Leave an optional value empty, but keep the comma.
- Each row may name its own target group. You need one registration token for each target group.

Find the IDs with the API (use the API v7 token in `TE_OAUTH_TOKEN`):

```bash
curl -s -H "Authorization: Bearer $TE_OAUTH_TOKEN" \
  https://api.thousandeyes.com/v7/account-groups
curl -s -H "Authorization: Bearer $TE_OAUTH_TOKEN" \
  "https://api.thousandeyes.com/v7/agents?agentTypes=ENTERPRISE&aid=<source group id>"
```

The first call lists account group names and IDs (`aid`). The second lists the agents of
the source group with their `agentId`, `agentName` and IP addresses.

Do not edit the inventory between the dry run and the apply. The dry-run file stores a hash of it.

## Quick start

1. Set the access in `.env` (see `.env.example`) or in the shell:

   ```bash
   export TE_OAUTH_TOKEN=...                 # API v7 token
   export TE_SOURCE_AID=...                  # ID of the source account group
   export TE_AGENT_UI_USERNAME=...           # agent UI login
   export TE_AGENT_UI_PASSWORD=...
   export TE_TARGET_TOKEN_<aid>=...          # registration token of each target group
   ```

   Without a variable the tool asks for the value in a hidden prompt.
   The tool never prints or stores tokens or passwords.

2. Make `work/input/inventory.csv` (section above). Start with one device.

3. Do a dry run. Nothing changes.

   ```bash
   python -m te_agent_migrate --strategy D --owner-is-source \
     --inventory work/input/inventory.csv --output work/runs/wave1
   ```

   Use `--owner-is-source` only when the source group owns every device.
   A clean dry run writes `work/runs/wave1/dry-run-ok.json`.
   If a device is blocked, the output gives the reason. Fix the inventory or the cause and run again.

4. Apply. Use the file that the clean dry run wrote. The tool asks `y` or `n` before it starts.

   ```bash
   python -m te_agent_migrate --strategy D --owner-is-source --apply \
     --inventory work/input/inventory.csv --output work/runs/wave1 \
     --confirmed-dry-run-file work/runs/wave1/dry-run-ok.json
   ```

WARNING: Apply resets real agents. A reset cannot be undone.
Do a clean dry run first, and move one wave at a time.

If a test uses a device as the target of an agent-to-agent test, add `--repoint-agent-to-agent`
to the dry run and to the apply. The tool then points those tests to the new agent.

## Directory tree

```
.
+-- README.md, requirements.txt, .env.example
+-- te_agent_migrate/     production tool (strategy_d/ = Strategy D flows)
+-- te_report/            production offline report
+-- tools/                setup_venv.sh, setup_venv.ps1
+-- examples/             example input files
+-- work/                 not committed
    +-- input/            inventory.csv
    +-- runs/             dry-run files, snapshots, run reports
```

Strategies A, B and C are old. Do not use them. Strategy C is blocked in apply mode.
The scripts `run.sh` and `run.ps1` do not exist. Run `python -m te_agent_migrate`.
