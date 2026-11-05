# ThousandEyes Enterprise Agent Migration

Moves ThousandEyes Enterprise Agents (TEVA appliances) from one account group to another.

- **Only the agent moves.** The tests stay in their account group and keep their IDs.
- The tool finds the groups that share the agent and shares the new agent with the same groups.
- It assigns the same tests to the new agent, then deletes the old agent (always last).
- A dry run is the default. Nothing changes until you apply.

## What you give it

| Input | Example |
|---|---|
| The device: **name and IP address** | `MyAgent-01`, `10.1.1.5` |
| The source account group ID | `100` |
| The destination account group ID | `200` |

The tool checks that the name and the IP address belong to the same agent in the source group.
If they do not match, the device is blocked and nothing is touched. This protects you from moving
the wrong device.

You also need these secrets. Never put them in a file you share.

| Secret | Variable |
|---|---|
| ThousandEyes API v7 token | `TE_OAUTH_TOKEN` |
| Agent web UI login | `TE_AGENT_UI_USERNAME`, `TE_AGENT_UI_PASSWORD` |
| Registration token of the destination group (the "Account Group Token" in the ThousandEyes UI, under Add New Enterprise Agent) | `TE_TARGET_TOKEN_200` (use the destination group ID) |

The source group ID goes in `TE_SOURCE_AID`.
If a variable is missing, the tool asks for it with a hidden prompt.

## Setup (once)

You need Python 3.6 or newer, built with OpenSSL 1.1.1 or newer (TLS 1.3).
Use a Python from python.org or Homebrew. Apple's `/usr/bin/python3` is too old.
The computer that runs the tool must reach each agent on TCP 443 and `api.thousandeyes.com`.

**macOS or Linux** (Terminal):

```bash
PYTHON_BIN=/path/to/python3 ./tools/setup_venv.sh
. .venv/bin/activate
```

**Windows** (PowerShell, in the project folder):

```powershell
.\tools\setup_venv.ps1 -PythonBin py
.\.venv\Scripts\Activate.ps1
```

If Windows blocks the scripts, run `Set-ExecutionPolicy -Scope Process Bypass` first.
Activate the environment again in every new terminal window.

## Migrate

### 1. Write the inventory

Make the folder and copy the example.

```bash
mkdir -p work/input                                    # macOS or Linux
cp examples/inventory.example.csv work/input/inventory.csv
```

```powershell
mkdir work\input                                       # Windows
copy examples\inventory.example.csv work\input\inventory.csv
```

Edit `work/input/inventory.csv`. One line for each device:

```
type,name,ip_address,agent_id,target_aid
agent,MyAgent-01,10.1.1.5,,200
agent,MyAgent-02,10.1.1.6,,200
```

- `name` and `ip_address` must be exactly those of the agent in the source group.
- `target_aid` is the destination group ID. Each device can have a different one.
- Leave `agent_id` empty (or put the current agent ID).
- The example file also has an optional `share_aids` column. You can leave it out: the tool finds the groups itself.
- Start with one device.

### 2. Dry run (nothing changes)

**macOS or Linux:**

```bash
export TE_SOURCE_AID=100
export TE_OAUTH_TOKEN=...
export TE_AGENT_UI_USERNAME=admin
export TE_AGENT_UI_PASSWORD=...
export TE_TARGET_TOKEN_200=...

python -m te_agent_migrate --owner-is-source \
  --inventory work/input/inventory.csv --output work/runs/w1
```

**Windows (PowerShell):**

```powershell
$env:TE_SOURCE_AID = "100"
$env:TE_OAUTH_TOKEN = "..."
$env:TE_AGENT_UI_USERNAME = "admin"
$env:TE_AGENT_UI_PASSWORD = "..."
$env:TE_TARGET_TOKEN_200 = "..."

python -m te_agent_migrate --owner-is-source `
  --inventory work\input\inventory.csv --output work\runs\w1
```

A clean dry run writes `work/runs/w1/dry-run-ok.json`.
If a device is blocked, the output gives the reason (see the table below). Fix it and run again.

### 3. Apply (moves the devices)

The tool asks `y` or `n`, then moves the devices one at a time.

**macOS or Linux:**

```bash
python -m te_agent_migrate --owner-is-source --apply \
  --inventory work/input/inventory.csv --output work/runs/w1 \
  --confirmed-dry-run-file work/runs/w1/dry-run-ok.json
```

**Windows (PowerShell):**

```powershell
python -m te_agent_migrate --owner-is-source --apply `
  --inventory work\input\inventory.csv --output work\runs\w1 `
  --confirmed-dry-run-file work\runs\w1\dry-run-ok.json
```

Do not edit the inventory between the dry run and the apply.
**Apply resets the real agent. A reset cannot be undone.** Do not press Ctrl-C during a device.

The tool stops at the first failure and prints the state of that device.
It never changes an offline device. Before each device it saves a snapshot in the output folder.

## The flags

Use the same flags in the dry run and in the apply, except `--apply` and `--confirmed-dry-run-file`.

| Flag | What it does | When to use it |
|---|---|---|
| `--inventory FILE` | The CSV with the devices | Always. Default: `work/input/inventory.csv` |
| `--output FOLDER` | Folder for the reports, the snapshots and `dry-run-ok.json` | Always. Default: `work/runs`. Use a new folder for each wave |
| `--owner-is-source` | You confirm that the **source** group owns the devices (see below) | Always, when the source group owns the devices |
| `--repoint-agent-to-agent` | Points the tests that use a device as their target agent to the new agent | Only if the dry run says "agent-to-agent test(s) use this device as target agent". Add it to the dry run and the apply |
| `--apply` | Really moves the devices | Only in step 3 |
| `--confirmed-dry-run-file FILE` | The `dry-run-ok.json` of a clean dry run of the same inventory. It works once | Only with `--apply` |
| `--timeout SECONDS` | How long to wait for each step | Only if a step times out on a slow network |
| `-v` | More output | When you look for a problem |


### What is `--owner-is-source`?

An agent that you see in the source group can be owned by that group, or owned by another group and
shared with it. The ThousandEyes API does not say which. If you move an agent that another group
owns, that group loses it. So the tool blocks every device until you confirm the owner.

Add `--owner-is-source` when you know that the source group owns the devices, for example because
you registered them there. If another group owns a device, remove it from the inventory.

## If the dry run says

| Message | What to do |
|---|---|
| owner not proven | Add `--owner-is-source` (only if the source group owns the devices) |
| No agent matches IP and name | Check the name and the IP address in the inventory |
| N agent-to-agent test(s) use this device as target agent | Add `--repoint-agent-to-agent` to the dry run and to the apply |
| Already migrated (skipped) | The device is in the destination group already |
| UI login failed | Check `TE_AGENT_UI_USERNAME` and `TE_AGENT_UI_PASSWORD` |

## After the move

- Recreate the agent notification rules by hand. The API cannot write them.
- Dashboards that filter on a specific agent ID must point to the new agent.

## Optional: impact report

To see before the move what depends on an agent, run `./tools/te_discovery.sh` (read-only, saves your
account setup to `work/discovery/`), then
`python -m te_report --input work/discovery --selection selection.txt --output work/reports/w1 --html`.
`selection.txt` lists one device name per line.

## Files

```
te_agent_migrate/   the migration tool
te_report/          the optional impact report
tools/              setup_venv.sh, setup_venv.ps1, te_discovery.sh
examples/           example input files
.env.example        the variables (you can use a .env file instead)
```
