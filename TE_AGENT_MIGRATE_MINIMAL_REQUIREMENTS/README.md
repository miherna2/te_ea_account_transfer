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

| Input | Where |
|---|---|
| The devices: **name and IP address** (and, if you want, the destination group of each one) | `work/input/inventory.csv`, or type them when the tool asks |
| The ThousandEyes API v7 token | the tool asks (hidden) and checks it at once |
| The source account group | the tool finds it from the devices and asks you to confirm |
| The destination account group | the `target` column, a `prefix_map.csv`, or one question |
| The agent web UI login | the tool asks (hidden) and checks it on every device before the first reset |
| The registration token of each destination group ("Account Group Token" in the ThousandEyes UI, under Add New Enterprise Agent) | the tool asks (hidden), once per destination group, and checks its format |

There are no environment variables and no `.env` file to prepare. Secrets are never written to disk.
A wrong token or password is asked again (3 tries) with the number of characters that was received.

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

The groups that see the old agent keep seeing the new one. You do not list them: the tool reads them
and shares the new agent with the same groups, the source group and the destination group. The
destination group becomes the owner of the new agent.

## Setup (once)

There is no virtual environment. The tool runs with the Python you already have. The first time you start
it, it offers to copy the two small libraries it needs (`requests` and `urllib3`) into a `libs` folder inside
the project (answer `Y`). Nothing is installed into your Python and nothing needs activating. If you prefer
to do it by hand, use the commands below.

You need Python 3.6 or newer, built with OpenSSL 1.1.1 or newer (TLS 1.3).
Use a Python from python.org or Homebrew. Apple's `/usr/bin/python3` is too old.
The computer that runs the tool must reach each agent on TCP 443 and `api.thousandeyes.com`.

In the commands of this guide, macOS and Linux use `python3`. On Windows type `python`
(or `py`) instead, in the **Command Prompt** (press the Windows key, type `cmd`, press Enter, then go to
the project folder with `cd`).

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
connection: use a newer Python (the tool also warns you at the start). To remove the libraries later,
delete the `libs` folder.

## Migrate

```
  [1 inventory]  ->  [2 preview]  ->  clean?  --no-->  read the reason, fix, repeat step 2
   (CSV file)        (no changes)       |
                                        yes
                                        v
                                   [3 apply]  ->  moves the devices, one at a time
```

### 1. Write the inventory

Make the folder and copy the example. Or skip this: when the file does not exist the tool asks you to type
the devices (`name ip`, one per line) and saves them.

```bash
mkdir -p work/input                                    # macOS or Linux
cp examples/inventory.example.csv work/input/inventory.csv
```

```bat
mkdir work\input
copy examples\inventory.example.csv work\input\inventory.csv
```

Edit `work/input/inventory.csv`. One line for each device. Two columns are enough:

```
name,ip_address
MyAgent-01,10.1.1.5
MyAgent-02,10.1.1.6
```

- `name` and `ip_address` must be exactly those of the agent in the source group.
- Optional `target` column: the destination group, as an ID or as the exact group name.
  Each device can have a different one. Without it the tool uses `work/input/prefix_map.csv` (see
  "Examples") and otherwise asks once for the destination of the run.
- Optional `type` column (`agent` or `cluster`). Without it a line with an IP is a standalone agent, and a
  line without an IP is looked up as a cluster.
- The tool accepts other spellings of the column names (`hostname`, `ip`, `destination` ...), `,` `;` or a
  tab as separator, a file saved by Excel, and a file without a header line (`name,ip`). Columns it does not
  know are ignored; a column that looks like a password or token is refused.
- Start with one device.

### 2. Preview (nothing changes)

```bash
python3 -m te_agent_migrate        # Windows: python -m te_agent_migrate
```

The tool asks for what it needs, finds the source group (it looks for the devices in your account groups and
asks `Use it? [Y/n]`), checks every device and prints the plan. It writes the report to `work/dry-run`. Each
preview replaces the previous one. If a device is blocked, the output gives the reason (see
"If the preview says" below). Fix it and run again. The preview also warns about agent UIs that this computer
cannot reach on port 443.

### 3. Apply (moves the devices)

```bash
python3 -m te_agent_migrate --apply
```

The tool finds the last preview by itself. If the inventory or the account groups changed since then,
it says so, shows the new plan and asks again. It also asks you to confirm that the source group is the
owner of the devices (see below). Before the first reset it checks that every agent UI answers and accepts the
login, so a wrong password or a blocked port stops the run while nothing has been changed. Then `y` or `n`, then
it moves the devices one at a time.

**Apply resets the real agent. A reset cannot be undone.** Do not press Ctrl-C during a device.
The tool stops at the first failure and prints the state of that device. It never changes an offline
device. Before each device it saves a snapshot, and the reports of an apply are in `work/apply/<date-time>`.
A device that was moved is remembered (`work/state/migrated.json`), so running the tool again skips it.

### If a run stops half way

A timeout, a closed window or a network drop can stop a run after the new agent is online but before the end.
Run the same command again. The tool finds the device by itself (the old agent is offline and the new agent is
in the destination group), says what is left, and in `--apply` asks `Finish these N migration(s) now?`. It
adds the missing tests and shares, deletes the old agent, restores the name and the tags. It only does what
is missing, so you can run it as many times as you need. It needs the snapshot that the earlier run saved in
`work/apply` (or `work/runs`).

## The options

| Option | What it does |
|---|---|
| `--apply` | Really moves the devices. Without it nothing changes |
| `--inventory FILE` | Another inventory file. Default: `work/input/inventory.csv` |
| `--timeout SECONDS` | How long to wait for each step, only if a step times out on a slow network |
| `-v` | More output |

Older options (`--dry-run`, `--owner-is-source`, `--repoint-agent-to-agent`, `--auto-reenable-tests`)
are still accepted and do nothing: the tool does it by itself.

### Who owns the agent?

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
   what you want.      -> answer y                 -> answer n, take the device out
                                                      of the inventory
```

In `--apply` the tool lists the devices and asks "Is the source group the owner of ALL these devices?".
A device that was registered with the source group's own Account Group Token is owned by the source group.
If you are not sure, ask the team that installed the agent before you move it.

### Tests that point at the agent

Most tests only run **from** an agent, so they keep working after the move. An agent-to-agent test
also uses an agent as its **target**. The new agent has a new ID, so the tool changes the target of those
tests to the new agent, by itself. The preview tells you how many there are.

## Examples

**Several devices to different destinations (one registration token per destination):**

```
name,ip_address,target
MyAgent-01,10.1.1.5,200
MyAgent-02,10.1.1.6,200
MyAgent-03,10.1.1.7,Div Capital
```

```
 100 (source) --+--> MyAgent-01, MyAgent-02 --> 200
                +--> MyAgent-03             --> Div Capital
```

**Destination from the device name, with a prefix map.** For names like `AAAA - HOST01` the tool can place
the devices by the text before ` - `. Put `work/input/prefix_map.csv` next to the inventory (see
`examples/prefix_map.example.csv`); the lines with status `OK` are used, the lines with `REVIEW` are not:

```
prefix,group_name,aid,status
AAAA,Div_A,100,OK
BBBB,Div_B,200,OK
```

## If the preview says

| Message | What to do |
|---|---|
| No agent matches IP and name | Check the name and the IP address in the inventory |
| migration(s) of an earlier run did not finish | A run stopped half way. Run with `--apply` to finish it (see above) |
| The destination ... is not an account group that the token can access | The `target` column has a group name or ID that the token cannot see. Fix it in the inventory |
| These agent UIs do not answer on port 443 | This computer cannot reach the agent. Fix the network or take the device out of the inventory |
| The agent UI refused the user name or password | Type the agent UI login again when asked |
| owner group not proven to be the source group | Normal. In `--apply` you confirm the owner |
| the target group already sees this device | Only a warning: the device was shared with the destination group before. It is moved if the source group owns it |
| Already migrated (skipped) | The device is in the destination group already |
| UI login failed | Check the agent UI user and password |
| no usable earlier preview / the situation changed since the last preview | Only information: the tool shows the new plan and asks you again |

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
```
