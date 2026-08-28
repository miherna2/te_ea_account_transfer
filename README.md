# ThousandEyes Enterprise Agent Account-Group Migration

Safety-focused Python 3.11+ CLI for migrating OVA-based ThousandEyes Enterprise Agents
from one source account group to one target account group.

The tool is dry-run by default. Runtime appliance actions use the agent HTTPS UI only;
there is no SSH, sudo, shell execution, service restart, or OS-level action.

## Safety model

- Bounded batch processing with `--parallel 1` by default and a maximum of five concurrent agent
  connections.
- Explicit confirmation before the first destructive action in apply mode.
- Operator confirmation before moving to the next batch. The prompt appears only after every agent
  in the current batch has finished.
- A hard stop prevents every later batch from starting. With parallelism greater than one, peers
  already running in the active batch are allowed to finish and may have completed changes.
- Other failures pause for an operator decision. Reachability-related failures offer read-only
  recheck, skip, or abort; deterministic API request errors offer only skip or abort.
- After the initial visibility window, `recheck` performs one bounded API inventory lookup rather
  than silently starting another 180-second poll.
- No automatic rollback, source-token re-injection, repeated reset, or modifying-request retry.
- Destination API visibility is polled for **180 seconds** by default.
- A visibility timeout is indeterminate and pauses for human review.
- A migration is successful only when the exact source identity has been deleted and confirmed
  absent, the target identity is online, and its display name exactly matches the inventory
  hostname.
- Simultaneous online source and target identities are a recovery path: reset/token actions are
  skipped, target-dependent test handling completes, and the source identity is then deleted.
- ThousandEyes may initially create `hostname-<local-agent-id>`; apply mode normalizes the target
  name through API v7 only after the source identity is deleted and confirmed absent.
- UI sessions and CSRF tokens are isolated per appliance and never shared between agents.
- API v7 uses one thread-safe pooled HTTPX client. Test sharing/recreation is serialized inside the
  process so agents assigned to the same source test cannot create duplicate target tests.
- Secrets are recursively redacted from JSON and Excel artifacts.
- Agent UI TLS verification is disabled by design and announced once in the CLI banner.

Phase 0 evidence and endpoint contracts are in [`reports/lab-findings.md`](reports/lab-findings.md).

## Installation

The Python CLI supports macOS and Windows with Python 3.11 or newer. Install
[`uv`](https://docs.astral.sh/uv/getting-started/installation/), then run the same project commands
from Terminal on macOS or PowerShell/Command Prompt on Windows:

```bash
uv sync --all-groups
```

Run without installing a global command:

```bash
uv run te-agent-migrate --help
```

## Inventory

Copy and edit `inventory.csv`. It has exactly two columns and must never contain credentials:

```csv
hostname,ip_address
agent-01,192.0.2.10
```

Duplicate hostnames/IPs, invalid IP addresses, extra columns, and empty inventories are rejected.

## Authentication and inputs

The CLI reads the shared agent UI username/password and target account-group token from these
environment variables when present:

macOS Terminal (`zsh`/`bash`):

```bash
export TE_AGENT_UI_USERNAME='admin'
export TE_AGENT_UI_PASSWORD='<agent-ui-password>'
export TE_TARGET_ACCOUNT_TOKEN='<32-character-target-token>'
```

Windows PowerShell:

```powershell
$env:TE_AGENT_UI_USERNAME = 'admin'
$env:TE_AGENT_UI_PASSWORD = '<agent-ui-password>'
$env:TE_TARGET_ACCOUNT_TOKEN = '<32-character-target-token>'
```

Windows Command Prompt (`cmd.exe`):

```bat
set "TE_AGENT_UI_USERNAME=admin"
set "TE_AGENT_UI_PASSWORD=YOUR_AGENT_UI_PASSWORD"
set "TE_TARGET_ACCOUNT_TOKEN=YOUR_32_CHARACTER_TARGET_TOKEN"
```

Any missing value is requested interactively; passwords and tokens use hidden prompts. Variables
that are set but empty are rejected. Password and token values are never written to console logs or
audit artifacts. To remove them from the current terminal session after the run:

```bash
unset TE_AGENT_UI_USERNAME TE_AGENT_UI_PASSWORD TE_TARGET_ACCOUNT_TOKEN
```

PowerShell equivalent:

```powershell
Remove-Item Env:TE_AGENT_UI_USERNAME
Remove-Item Env:TE_AGENT_UI_PASSWORD
Remove-Item Env:TE_TARGET_ACCOUNT_TOKEN
```

Command Prompt equivalent:

```bat
set "TE_AGENT_UI_USERNAME="
set "TE_AGENT_UI_PASSWORD="
set "TE_TARGET_ACCOUNT_TOKEN="
```

The API bearer token is resolved in this order:

1. `TE_OAUTH_TOKEN`
2. `THOUSANDEYES_OAUTH_TOKEN`
3. Hidden prompt

The source and target account groups are selected from the account groups accessible to the
bearer token. Non-interactive account-group selection can use:

```bash
export TE_SOURCE_AID=144165
export TE_TARGET_AID=2136224
```

In PowerShell, use `$env:TE_SOURCE_AID = '144165'` and
`$env:TE_TARGET_AID = '2136224'`. In Command Prompt, use `set TE_SOURCE_AID=144165` and
`set TE_TARGET_AID=2136224`.

Never store UI passwords, account-group tokens, or OAuth tokens in `inventory.csv`, command-line
arguments, source files, shell history, or committed configuration files. Prefer session-scoped
exports or your terminal's secure secret-management integration.

## Usage

Dry-run is the default and performs the UI credential/CSRF handshake, reachability checks, API v7
account-group/agent/test discovery, target test inventory, recreation-adapter compatibility check,
and projected reporting without reset, token submission, agent deletion, or test changes.

The CLI prints timestamped `INFO`, `RUNNING`, and final status lines before and after every
potentially slow operation. Destination visibility polling also prints the poll number and seconds
remaining. Add `-v` to include non-secret diagnostic context such as the inventory hostnames.

### Running from OneDrive on macOS

Do not keep the Python virtual environment inside the synchronized project folder. OneDrive can
offload `.venv` executables as `dataless` placeholders, which makes `uv run` appear to hang before
the CLI starts. This project uses a local environment outside OneDrive:

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/Library/Caches/te-ea-account-transfer/.venv"
uv sync
```

After exporting that variable, the normal command works. Alternatively, use the included launcher,
which sets the local environment and prints feedback before invoking `uv`:

```bash
./run.sh --inventory inventory.csv --strategy A
```

### Running from OneDrive on Windows

Keep the virtual environment outside the synchronized project directory. In PowerShell:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\te-ea-account-transfer\.venv"
uv sync
uv run te-agent-migrate --inventory inventory.csv --strategy A
```

Alternatively, use the included Windows launcher, which selects that external environment and
prints startup feedback:

```powershell
.\run.ps1 --inventory inventory.csv --strategy A
```

If PowerShell policy prevents running local scripts, use the standard `uv run te-agent-migrate`
command directly; the PowerShell launcher is optional.

Run the migration from regular macOS Terminal, Windows PowerShell, or Windows Command Prompt—not
the Codex embedded/sandboxed terminal. The ThousandEyes public API may remain reachable through its
proxy while direct LAN connections to agent HTTPS port 443 are blocked by the Codex sandbox. The CLI
detects this condition and exits before requesting credentials. Agent UI traffic always uses
HTTPS/443; there is no HTTP fallback.

If an external terminal still reports `[Errno 65] No route to host` while the browser can open the
agent UI, allow that terminal application (Terminal, iTerm, or another launcher) under **System
Settings > Privacy & Security > Local Network**, then completely quit and reopen it. Confirm the
same terminal process can reach the required endpoint before retrying:

```bash
nc -vz 192.168.1.52 443
curl --noproxy '*' -kI https://192.168.1.52/login
```

The UI client sets `trust_env=False`, so it connects directly and does not inherit HTTP(S) proxy
variables for private agent addresses.

On Windows, the equivalent HTTPS reachability check is:

```powershell
Test-NetConnection 192.168.1.52 -Port 443
curl.exe --noproxy "*" -kI https://192.168.1.52/login
```

```bash
uv run te-agent-migrate \
  --inventory inventory.csv \
  --strategy A
```

PowerShell uses a backtick for multiline commands, or the command can be entered on one line:

```powershell
uv run te-agent-migrate `
  --inventory inventory.csv `
  --strategy A `
  --parallel 2
```

Apply mode:

```bash
uv run te-agent-migrate \
  --apply \
  --inventory inventory.csv \
  --strategy C \
  --stale keep \
  --create-missing-tags \
  --create-missing-alerts \
  --parallel 3 \
  --timeout 180
```

`--parallel` controls the batch size and number of simultaneous agent connections. Accepted values
are `1` through `5`; the default `1` preserves serial behavior. For example, five inventory agents
with `--parallel 2` run as batches of two, two, and one. After the first two batches the CLI asks
whether to continue; it does not ask after the final batch.

Options:

```text
--dry-run             Preview only, no changes (default)
--apply               Execute the migration
--inventory PATH      Inventory CSV (default: inventory.csv)
--strategy [A|B|C]    Test handling strategy; prompted when omitted
--stale [keep|remove] Strategy C source-test policy; ignored for A/B
--create-missing-tags Strategy C: create/reuse target tags and attach them to tests
--create-missing-alerts
                       Strategy C: create/reuse target alert rules and attach them to tests
--timeout SECONDS     Destination validation window (default: 180)
--parallel INTEGER    Parallel agent connections per batch, 1-5 (default: 1)
--output DIR          Artifact directory (default: ./reports)
-v                    Verbose output
--help                Show concise help
```

## Test-handling strategies

### A — Agents only

No test action. Source tests remain in the source account group. The source agent is still deleted.

### B — Own and share

The source test remains owned by the source group. The tool adds the target account group to the
test's `sharedWithAccounts` value only when needed and additively assigns the shared test to the
migrated target agent. Other source test settings are preserved.
The source agent is still deleted.

### C — Recreate in target

The tool creates an enabled target-owned equivalent with the exact original name, associates it with
each migrated target agent that was assigned to the source test, preserves type-specific settings,
and, by default, removes source labels/tags and alert rules. An existing target test with the same
migrated name and type is updated, enabled, and reused on a rerun instead of being duplicated. Names
never gain an `(MT)` or other migration suffix, including across round trips. Unknown or unsupported
API v7 test types are reported without failing the run.

Tag and alert-rule migration is independently opt-in:

- No resource flags: create the test without tags or alert rules and set `alertsEnabled=false`.
- `--create-missing-tags`: reuse semantically matching target tags or create missing static test
  tags, then attach the target tag UUIDs to the migrated test.
- `--create-missing-alerts`: reuse semantically matching target alert rules or create missing
  rules, then attach the target rule IDs to the migrated test. `alertsEnabled` preserves the source
  value when at least one rule is attached; otherwise it remains false so target defaults are not
  applied accidentally.
- Both flags: reconcile and attach both resource classes.

The flags apply only to Strategy C. They are accepted but recorded as `not-applicable` for
Strategies A and B. Source IDs are never copied because tag and alert-rule identifiers are scoped to
their account group.

Strategy C snapshots every source test before the first agent changes account groups. A test with
no source-agent association is recreated using the selected resource flags, enabled, and assigned
to all successfully migrated inventory agents only when its API v7 type is agent-based. If a test
references a source agent outside the selected inventory, the run stops before destructive work
rather than producing an incomplete copy.

BGP tests are handled separately because API v7 defines them as prefix-and-monitor resources, not
Enterprise Agent resources. Strategy C recreates each BGP test exactly once with its exact original
name, prefix, `usePublicBgp` setting, and monitor IDs; it never adds an `agents` field or calls the
agent-assignment endpoint for BGP. Before the first agent move, every selected source monitor ID must
be visible in the destination account group or the run stops. After recreation, the tool verifies
that the target BGP test is enabled, has no agent associations, and has the exact expected name,
prefix, public-BGP setting, and monitor set. A `--stale remove` BGP source test becomes eligible for
deletion only after that readiness check succeeds. Dry-run reports this as
`recreate-monitor-based`, never `recreate-and-assign-all`.

Associations with Cloud Agents are preserved when the same Cloud Agent ID is visible in the
destination account group. A source-only Cloud Agent, or any other agent outside the selected
inventory that is not portable to the destination, still stops the run during preflight.

Resource reconciliation is also a preflight gate before the first agent reset or token change:

- In `--apply` mode, enabling either reconciliation flag moves the destructive-action confirmation
  before the first tag or alert-rule creation, so no target resource is written before approval.
- Resource creation is additive and idempotent. If one resource is created successfully and a later
  preflight operation fails, the created resource may remain in the target; a retry detects and
  reuses it instead of creating a duplicate.
- Tags are matched by `objectType`, `type`, `key`, and `value`. Strategy C supports only
  `objectType=test`, `type=static` tags. Duplicate identities are treated as ambiguous. Missing
  `system` or `partner` tags cannot be user-created and stop the run before agent migration.
- Alert rules are matched by a canonical fingerprint of their API v7 create fields. A target rule
  with the same `ruleName` and `alertType` but a different definition stops the run rather than
  silently overwriting or duplicating it. A conflicting default rule for the same alert type also
  stops preflight.
- Alert notifications are copied as part of the rule definition. If they reference a notification
  integration unavailable in the target account group, API v7 rejects the rule during preflight;
  agents have not yet moved, and the operator can create/reconcile that dependency before retrying.

For newly created and reused agent-based tests, the tool sends the agent assignment explicitly and
then polls API v7 until the target test is visibly enabled and associated with that exact target
agent. Monitor-based BGP tests instead use the exact-configuration readiness check described above.
The source agent is not deleted until the applicable verification succeeds.

Expanded GET responses are converted back to the API v7 create-request shape: read-only response
fields are removed, agent and monitor identifiers are serialized as strings, and account-local tag
and alert-rule IDs are replaced by their reconciled target IDs. When alert reconciliation is not
enabled, alerts are explicitly disabled so default alert rules are not attached.

In dry-run mode, Option C records adapter support and the complete target test inventory. There is
no target account-group licensing constraint; a test is reported as unsupported only when its API
v7 test type has no implemented recreation adapter. Resource flags perform read-only source/target
reconciliation and report projected creates without creating tags, alert rules, or tests.

## Strategy C source-test policy

- `keep`: retain each original source test after its target copy is created or reused.
- `remove`: delete each successfully recreated source test once all source agents associated with
  that specific test have completed migration. An unrelated failed agent does not block cleanup;
  a failed or unselected agent that is actually associated with the test causes that test to be
  retained. Unsupported or failed recreations are never deleted. After deletion, API absence is
  verified before the test is reported as removed.

This option applies only to Strategy C. It is ignored for Strategies A and B and never controls
agent cleanup. In every strategy, the source agent is deleted immediately after target validation
and test handling, and its absence is verified before the target is renamed. ThousandEyes documents
that deleting the final agent assigned to a retained source test can disable that test; the test
record itself remains in the source account group. The selected policy and every action are included
in the audit artifacts.

## Reports

Every run writes a timestamped JSON log. Finalized runs also write an Excel workbook with:

- `Summary`
- `Agents`
- `Tests`
- `Unsupported Tests`
- `Stale Entries`
- `Diffs`
- `Decisions`
- `Errors`

The JSON audit includes correlation IDs, step timing/status, errors, local diffs, and operator
prompt responses. Secret prompts are recorded only as redacted placeholders.
Domain failures and sanitized unexpected-failure types both finalize the JSON and workbook before
the CLI exits.

## Partial-run and idempotency behavior

- A healthy existing target identity causes reset/token actions to be skipped.
- Matching online identities in both groups enter recovery without another reset; the source agent
  is deleted after target test handling and its absence is verified.
- Existing suffixed target identities are repaired to the exact inventory hostname only after the
  corresponding source identity is deleted and confirmed absent.
- A conflicting destination identity that already owns the exact name hard-stops before reset.
- Existing `(MT)` tests are reused rather than duplicated.
- Test sharing and assignments are additive; reused Strategy C tests retain prior migrated-agent
  associations, add the current migrated agent, and are forced enabled.
- Strategy C tag/alert reconciliation is idempotent: reruns reuse exact semantic matches. Ambiguous
  duplicates or same-name alert-rule conflicts stop before agent changes instead of selecting an
  arbitrary resource.
- Source-test removal for Strategy C is evaluated per test. A source test is removed only when all
  source agents listed on that test completed, so unrelated agent failures do not block cleanup.
- Strategy A intentionally migrates agents without tests. A later Strategy C run cannot infer
  associations that were already removed by a prior Strategy A migration; restore/reset the source
  associations before using Strategy C in that direction.
- Batch results and continuation prompts follow inventory order even though agent log lines inside a
  batch can interleave.
- If one agent hard-stops a parallel batch, no later batch starts, but already-running agents in the
  active batch are not cancelled mid-migration.

## Development and verification

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

## API and UI contracts

- API v7 base: `https://api.thousandeyes.com/v7/`
- API authentication: OAuth bearer token
- Account-group scoping: `aid` query parameter
- Agent UI bootstrap: `GET /login` → `GET /api/session` → `POST /api/login`
- UI login success requires HTTP 200 **and** JSON `success: true`
- Reset: `POST /api/resetstate`
- Target token: `POST /api/agent`
- CSRF failures discard the cookie/token pair and require a fresh login

Official references used for implementation:

- <https://developer.cisco.com/docs/thousandeyes/getting-started/>
- <https://developer.cisco.com/docs/thousandeyes/account-group-context/>
- <https://developer.cisco.com/docs/thousandeyes/list-configured-tests/>
- <https://developer.cisco.com/docs/thousandeyes/list-cloud-and-enterprise-agents/>
- <https://developer.cisco.com/docs/thousandeyes/assign-tests-to-an-agent/>
- <https://developer.cisco.com/docs/thousandeyes/delete-enterprise-agent/>
- <https://developer.cisco.com/docs/thousandeyes/delete-http-server-test/>
- <https://developer.cisco.com/docs/thousandeyes/create-http-server-test/>
- <https://developer.cisco.com/docs/thousandeyes/list-tags/>
- <https://developer.cisco.com/docs/thousandeyes/create-tag/>
- <https://developer.cisco.com/docs/thousandeyes/list-alert-rules/>
- <https://developer.cisco.com/docs/thousandeyes/create-alert-rule/>
- <https://developer.cisco.com/docs/thousandeyes/pagination/>
- <https://docs.thousandeyes.com/product-documentation/enterprise-agents/resetting-an-enterprise-agent>
