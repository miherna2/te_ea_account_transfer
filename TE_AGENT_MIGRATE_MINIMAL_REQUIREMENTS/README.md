# ThousandEyes Enterprise Agent Migration

Moves ThousandEyes Enterprise Agents (TEVA appliances) from one account group to another.

- **Only the agent moves.** The tests stay in their account group and keep their IDs.
- The tool finds the groups that see the agent and shares the new agent with the same groups.
- It assigns the same tests to the new agent, then deletes the old agent (always last).
- A dry run is the default. Nothing changes until you apply.

## What happens to one device

```
BEFORE                                       AFTER
------                                       -----
 Group 100 (source)    Group 300             Group 100 (source)    Group 300
 +-----------------+  +-------------+        +-----------------+  +-------------+
 | tests T1, T2    |  | test T3     |        | tests T1, T2    |  | test T3     |
 | agent  A (old)  |  |  (uses A)   |        |  (use agent B)  |  | (uses B)    |
 +--------+--------+  +------^------+        +-----------------+  +------^------+
          |  shared with     |                                      shared with
          +------------------+                Group 200 (destination)    |
                                              +-----------------+        |
                                              | agent  B (new)  +--------+
                                              | same name, same |  also shared back
                                              | IP, new agent ID|  to group 100
                                              +-----------------+
```

The steps, one device at a time:

```
 1 read     2 reset     3 register in    4 share B to     5 give B the    6 delete
 the old    the VM      group 200 with   groups 100, 300  same tests      old agent A
 agent A    (agent UI)  its token  (B)   (as before)      as A had        (last)
    |          |            |                 |                |              |
    +----------+------------+-----------------+----------------+--------------+
       if anything fails the tool stops and prints the state of that device;
       the old agent A is deleted only after every step before it worked
```

## What you give it

| Input | Example |
|---|---|
| The device: **name and IP address** | `MyAgent-01`, `10.1.1.5` |
| The source account group ID | `100` |
| The destination account group ID | `200` |

The tool checks that the name and the IP address belong to the same agent in the source group.
If they do not match, the device is blocked and nothing is touched. This protects you from moving
the wrong device.

```
 inventory line:  MyAgent-01 , 10.1.1.5
                      |            |
                      v            v
 source group 100:  [ agent named MyAgent-01 ]  AND  [ it has IP 10.1.1.5 ]
                      both true, same agent  ->  READY
                      anything else          ->  BLOCKED, nothing touched
```

You also need these secrets. Never put them in a file you share.

| Secret | Variable |
|---|---|
| ThousandEyes API v7 token | `TE_OAUTH_TOKEN` |
| Agent web UI login | `TE_AGENT_UI_USERNAME`, `TE_AGENT_UI_PASSWORD` |
| Registration token of the destination group (the "Account Group Token" in the ThousandEyes UI, under Add New Enterprise Agent) | `TE_TARGET_TOKEN_200` (use the destination group ID) |

The source group ID goes in `TE_SOURCE_AID`.
If a variable is missing, the tool asks for it with a hidden prompt.

## Setup (once)

There is no virtual environment. The tool runs with the Python you already have. One command
copies the three small libraries it needs (`requests`, `urllib3`, `python-dotenv`) into a `libs`
folder inside the project. Nothing is installed into your Python and nothing needs activating.

You need Python 3.6 or newer, built with OpenSSL 1.1.1 or newer (TLS 1.3).
Use a Python from python.org or Homebrew. Apple's `/usr/bin/python3` is too old.
The computer that runs the tool must reach each agent on TCP 443 and `api.thousandeyes.com`.

In the commands of this guide, macOS and Linux use `python3`. On Windows type `python`
(or `py`) instead. On Windows use the **Command Prompt**: press the Windows key, type `cmd`, press
Enter, then go to the project folder with `cd`. In `set NAME=value` do not put quotes around the value. If a password has the characters `& ^ % < >`,
leave its variable out: the tool then asks for it with a hidden prompt.

**macOS or Linux** (Terminal, in the project folder):

```bash
python3 -m pip install --target libs -r requirements.txt
python3 -c "import ssl; print(ssl.OPENSSL_VERSION, ssl.HAS_TLSv1_3)"
```

**Windows** (Command Prompt, in the project folder):

```bat
python -m pip install --target libs -r requirements.txt
python -c "import ssl; print(ssl.OPENSSL_VERSION, ssl.HAS_TLSv1_3)"
```

The last line must end with `True`. If it says `False`, agents that need TLS 1.3 will refuse the
connection: use a newer Python. To remove the libraries later, delete the `libs` folder.

## Migrate

```
  [1 inventory]  ->  [2 dry run]  ->  clean?  --no-->  read the reason, fix, repeat step 2
   (CSV file)        (no changes)       |
                                       yes: dry-run-ok.json is written
                                        |
                                        v
                                   [3 apply]  ->  moves the devices, one at a time
```

### 1. Write the inventory

Make the folder and copy the example.

```bash
mkdir -p work/input                                    # macOS or Linux
cp examples/inventory.example.csv work/input/inventory.csv
```

```bat
mkdir work\input
copy examples\inventory.example.csv work\input\inventory.csv
```

Edit `work/input/inventory.csv`. One line for each device:

```
type,name,ip_address,agent_id,target_aid
agent,MyAgent-01,10.1.1.5,,200
agent,MyAgent-02,10.1.1.6,,200
```

- `name` and `ip_address` must be exactly those of the agent in the source group.
- `target_aid` is the destination group ID. Each device can have a different one (see the examples).
- Leave `agent_id` empty (or put the current agent ID).
- Start with one device.

**Column `share_aids`.** Usually you leave it out. Add it in one case: **the device is already
shared with the destination group** (for example, tests of the destination group already run on it).
Without `share_aids` the dry run blocks that device with "agent is already in the target group",
to protect you from moving a device that has already been moved. Put the destination group in the
row to confirm that the sharing is intended:

```
type,name,ip_address,agent_id,target_aid,share_aids
agent,MyAgent-01,10.1.1.5,1234567,200,200;100
```

- `share_aids` is a list of group IDs separated by `;`. It must contain `target_aid`.
- Put the current `agent_id` in the row when you use `share_aids`.
- The new agent is always shared with: the destination group, every group that sees the agent now
  (including the source group) and every group that holds tests of the agent. `share_aids` only adds
  to that list, it never removes a group.

### 2. Dry run (nothing changes)

**macOS or Linux:**

```bash
export TE_SOURCE_AID=100
export TE_OAUTH_TOKEN=...
export TE_AGENT_UI_USERNAME=admin
export TE_AGENT_UI_PASSWORD=...
export TE_TARGET_TOKEN_200=...

python3 -m te_agent_migrate --owner-is-source \
  --inventory work/input/inventory.csv --output work/runs/w1
```

**Windows (Command Prompt):**

```bat
set TE_SOURCE_AID=100
set TE_OAUTH_TOKEN=...
set TE_AGENT_UI_USERNAME=admin
set TE_AGENT_UI_PASSWORD=...
set TE_TARGET_TOKEN_200=...

python -m te_agent_migrate --owner-is-source ^
  --inventory work\input\inventory.csv --output work\runs\w1
```

A clean dry run writes `work/runs/w1/dry-run-ok.json`.
If a device is blocked, the output gives the reason (see "If the dry run says" below). Fix it and run again.

### 3. Apply (moves the devices)

The tool asks `y` or `n`, then moves the devices one at a time.

**macOS or Linux:**

```bash
python3 -m te_agent_migrate --owner-is-source --apply \
  --inventory work/input/inventory.csv --output work/runs/w1 \
  --confirmed-dry-run-file work/runs/w1/dry-run-ok.json
```

**Windows (Command Prompt):**

```bat
python -m te_agent_migrate --owner-is-source --apply ^
  --inventory work\input\inventory.csv --output work\runs\w1 ^
  --confirmed-dry-run-file work\runs\w1\dry-run-ok.json
```

Do not edit the inventory between the dry run and the apply.
`dry-run-ok.json` works once and only for the same inventory, groups and flags.
**Apply resets the real agent. A reset cannot be undone.** Do not press Ctrl-C during a device.

The tool stops at the first failure and prints the state of that device.
It never changes an offline device. Before each device it saves a snapshot in the output folder.

## The flags

Use the same flags in the dry run and in the apply, except `--apply` and `--confirmed-dry-run-file`.

| Flag | What it does | When to use it |
|---|---|---|
| `--inventory FILE` | The CSV with the devices | Always. Default: `work/input/inventory.csv` |
| `--output FOLDER` | Folder for the reports, the snapshots and `dry-run-ok.json` | Always. Default: `work/runs`. Use a new folder for each wave |
| `--owner-is-source` | You confirm that the **source** group owns the devices | When the source group owns every device in the inventory (see below) |
| `--repoint-agent-to-agent` | Points the tests that use a device as their target agent to the new agent | Only when the dry run reports agent-to-agent tests on a device (see below) |
| `--apply` | Really moves the devices | Only in step 3 |
| `--confirmed-dry-run-file FILE` | The `dry-run-ok.json` of a clean dry run of the same inventory | Only with `--apply` |
| `--timeout SECONDS` | How long to wait for each step | Only if a step times out on a slow network |
| `-v` | More output | When you look for a problem |

### Which flags do I need?

```
 Do you own the devices in the source group (you registered them there)?
   |
   +-- yes --> add  --owner-is-source           (dry run and apply)
   |
   +-- no / not sure --> do not move them: see "--owner-is-source" below

 Does the dry run say "agent-to-agent test(s) use this device as target agent"?
   |
   +-- yes --> add  --repoint-agent-to-agent    (dry run and apply)
   |
   +-- no  --> nothing to add

 Typical first run:   python3 -m te_agent_migrate --owner-is-source ...
 Typical apply:       same flags  +  --apply  +  --confirmed-dry-run-file ...
```

### `--owner-is-source`: who owns the agent?

An agent that you see in the source group is either **owned** by that group or only **shared** with it.
The ThousandEyes API does not tell which. This matters because the move deletes the old agent:

```
 CASE 1: the source group OWNS the agent         CASE 2: the source group only SEES it

   Group 100 (source)                              Group 500 (owner)
   +--------------+                                +--------------+
   | agent A      |  <- registered here            | agent A      |  <- registered here
   +------+-------+                                +------+-------+
          | shared with 300                               | shared with 100 (source)
          v                                               v
   Group 300 sees A                                Group 100 sees A, but does not own it

   Move A to group 200:                            Move A to group 200:
   100 gives the agent away. This is               group 500 LOSES its agent.
   what you want.      -> use the flag             -> do NOT use the flag, take the
                                                      device out of the inventory
```

Without `--owner-is-source` the tool blocks every device with "owner not proven", so a mistake
cannot silently take an agent from another group. The flag is your statement that the source group
is the owner of **all** devices in the inventory. If the inventory mixes owned and shared devices,
put only the owned ones in it.

How to know: an agent that was registered with the source group's own Account Group Token is owned
by the source group. If you are not sure, ask the team that installed the agent before you move it.

### `--repoint-agent-to-agent`: tests that point at the agent

Most tests only run **from** an agent, so they keep working after the move. An agent-to-agent test
also uses an agent as its **target**. The new agent has a new ID, so those tests would point at the
deleted agent. The flag makes the tool change their target to the new agent.

```
 agent-to-agent test T9 (group 100):   from agent X   --->   target agent A (the device)

 Without the flag the dry run is BLOCKED:
   "1 agent-to-agent test(s) use this device as target agent"

 With the flag, apply does this:
   T9:  from agent X ---> target A (old)        becomes        from agent X ---> target B (new)
```

Use it only when the dry run reports this message. Put it in the dry run **and** in the apply.

## Examples

**One device, source 100 to destination 200 (the usual case):**

```
type,name,ip_address,agent_id,target_aid
agent,MyAgent-01,10.1.1.5,,200
```
```
python3 -m te_agent_migrate --owner-is-source --inventory work/input/inventory.csv --output work/runs/w1
```

**Several devices to different destinations (one token per destination):**

```
type,name,ip_address,agent_id,target_aid
agent,MyAgent-01,10.1.1.5,,200
agent,MyAgent-02,10.1.1.6,,200
agent,MyAgent-03,10.1.1.7,,250
```
```
 100 (source) --+--> MyAgent-01, MyAgent-02 --> 200     needs TE_TARGET_TOKEN_200
                +--> MyAgent-03             --> 250     needs TE_TARGET_TOKEN_250
```

**The dry run reports agent-to-agent tests:** run both steps again with the extra flag.

```
python3 -m te_agent_migrate --owner-is-source --repoint-agent-to-agent \
  --inventory work/input/inventory.csv --output work/runs/w1
python3 -m te_agent_migrate --owner-is-source --repoint-agent-to-agent --apply \
  --inventory work/input/inventory.csv --output work/runs/w1 \
  --confirmed-dry-run-file work/runs/w1/dry-run-ok.json
```

## If the dry run says

| Message | What to do |
|---|---|
| owner group not proven to be the source group | Add `--owner-is-source`, only if the source group owns the devices |
| No agent matches IP and name | Check the name and the IP address in the inventory |
| N agent-to-agent test(s) use this device as target agent | Add `--repoint-agent-to-agent` to the dry run and to the apply |
| agent is already in the target group | The device is shared with the destination group already. Add `share_aids` to its inventory row (see step 1). If it was moved before, remove it from the inventory |
| Already migrated (skipped) | The device is in the destination group already |
| UI login failed | Check `TE_AGENT_UI_USERNAME` and `TE_AGENT_UI_PASSWORD` |
| Dry-run confirmation is stale | The inventory changed after the dry run. Run the dry run again |

## After the move

- Recreate the agent notification rules by hand. The API cannot write them.
- Dashboards that filter on a specific agent ID must point to the new agent.

## Optional: impact report

To see before the move what depends on an agent, run `./tools/te_discovery.sh` (read-only, saves your
account setup to `work/discovery/`), then
`python3 -m te_report --input work/discovery --selection selection.txt --output work/reports/w1 --html`.
`selection.txt` lists one device name per line.

## Files

```
te_agent_migrate/   the migration tool
te_report/          the optional impact report
tools/              te_discovery.sh (optional)
examples/           example input files
.env.example        the variables (you can use a .env file instead)
```
