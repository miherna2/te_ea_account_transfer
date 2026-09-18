# ThousandEyes Enterprise Agent Migration - Minimal Requirements Edition

This folder is a self-contained compatibility edition of the account-group migration tool. It preserves the migration workflow and safety controls while removing the runtime dependencies on `uv`, Click, HTTPX, Rich, OpenPyXL, and Python dataclasses.

## Compatibility target

- Python 3.6 and Python 3.9 syntax and standard-library compatibility
- macOS, Linux, and Windows
- Standard `venv` and `pip`; `uv` is not required or used
- Only the three packages directly required by this application are listed in `requirements.txt`
- The application directly imports `requests`, `python-dotenv`, and `urllib3` from the approved list

Python 3.6 is end-of-life and should only be used when the restricted execution environment requires it. This edition avoids syntax and standard-library features introduced after Python 3.6, but the actual Python 3.6 interpreter, operating-system TLS stack, and package artifacts must still be available in the target environment.

Python-language compatibility and TLS compatibility are separate requirements. The lab Enterprise Agents currently require TLS 1.3 on their HTTPS/443 UI. Therefore, the selected Python interpreter must be linked to OpenSSL 1.1.1 or newer (or another SSL library with working TLS 1.3 support). Apple's `/usr/bin/python3` linked to LibreSSL 2.8.3 cannot connect to these agents even though the application code runs on Python 3.9. Use a Python.org or Homebrew interpreter with TLS 1.3 support and create the `venv` from that interpreter. The setup scripts display the selected Python and SSL runtimes and print a warning when Python does not report TLS 1.3 support.

`pip` will also install the transitive dependencies declared by Requests. Those are package-manager dependencies rather than additional application imports.

## What the tool does

The tool migrates ThousandEyes Enterprise Agents between account groups using API v7 plus each appliance's HTTPS UI on TCP 443.

- **Strategy A:** migrate agents only.
- **Strategy B:** migrate agents, share their tests with the target account group, and assign the migrated agents.
- **Strategy C:** migrate agents and recreate supported tests in the target account group with their exact names, enabled state, and agent associations.
- `inventory.csv` is the strict migration boundary. Strategy C recreates only tests assigned to the selected Enterprise Agents, even when the inventory happens to contain every Enterprise Agent in the source account group.
- Unrelated tests, unassigned tests, monitor-only tests, and agents absent from `inventory.csv` remain untouched.
- Tags and alert rules are considered only when referenced by an in-scope test and only when their corresponding opt-in creation flags are enabled.
- `--stale keep|remove` applies only to Strategy C source tests.
- With `--stale remove`, a recreated source test is retained when it still has associations to Enterprise Agents outside the inventory or to agents that did not complete. This prevents disruption to non-migrated agents.
- Source agent identities are deleted in every apply strategy after the destination identity is online and validated.
- `--create-missing-tags` and `--create-missing-alerts` opt in to Strategy C reconciliation of missing test tags and alert rules.
- `--parallel 1..5` controls the maximum number of agent UI connections in each batch.
- The operator must explicitly type every confirmation or failure action. Pressing Enter alone is never accepted as a default answer.

Dry run is the default. Destructive work requires `--apply` and a separate explicit confirmation.

## Agent UI authentication and CSRF handling

Every appliance receives its own isolated `requests.Session`. The tool follows the proven sequence used by the read-only Ansible implementation:

1. `GET https://AGENT/login` establishes the UI session and cookie jar.
2. `GET https://AGENT/api/session` obtains the initial CSRF token.
3. `POST https://AGENT/api/login` sends the UI credentials, the same cookies, the CSRF token, and the HTTPS Referer.
4. The authenticated cookie jar and CSRF token are retained for all later calls to that appliance.
5. Response headers `x-newcsrftoken` and `x-csrftoken` rotate the retained token when present.
6. Before a modifying call, `/api/app-config` refreshes the CSRF token; `/api/session` is the fallback.
7. HTTP 403 invalidates the local session state and requires a fresh authentication.

The UI connection always uses HTTPS/443, bypasses environment proxies, does not follow redirects, and disables certificate verification only for the appliance UI because lab agents commonly use self-signed certificates. ThousandEyes API v7 certificate verification remains enabled.

## Create the environment

### macOS or Linux

From this folder:

```bash
PYTHON_BIN=/path/to/tls13-capable-python3.9 ./setup_venv.sh
. .venv/bin/activate
```

For Python 3.6, use an installed Python 3.6 interpreter:

```bash
PYTHON_BIN=python3.6 ./setup_venv.sh
. .venv/bin/activate
```

### Windows PowerShell

```powershell
.\setup_venv.ps1 -PythonBin py
.\.venv\Scripts\Activate.ps1
```

To select Python 3.9 with the Windows launcher:

```powershell
py -3.9 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The setup scripts intentionally do not upgrade `pip`, because a current pip release may no longer run on Python 3.6.

Verify the runtime selected inside the environment:

```bash
python -c "import ssl, sys; print(sys.version); print(ssl.OPENSSL_VERSION); print(getattr(ssl, 'HAS_TLSv1_3', False))"
```

For the current lab agents, the last value must be `True`. If it is `False`, changing Requests settings or disabling certificate verification cannot solve the handshake because the server rejects the protocol before HTTP begins.

## Configuration

Copy the examples, then enter real values locally:

```bash
cp inventory.csv.example inventory.csv
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item inventory.csv.example inventory.csv
Copy-Item .env.example .env
```

Supported environment variables:

| Variable | Purpose |
| --- | --- |
| `TE_OAUTH_TOKEN` | ThousandEyes API v7 OAuth bearer token |
| `THOUSANDEYES_OAUTH_TOKEN` | Alternate OAuth variable name |
| `TE_AGENT_UI_USERNAME` | Enterprise Agent UI username |
| `TE_AGENT_UI_PASSWORD` | Enterprise Agent UI password |
| `TE_SOURCE_AID` | Optional non-interactive source account-group ID |
| `TE_TARGET_AID` | Optional non-interactive target account-group ID |
| `TE_TARGET_ACCOUNT_TOKEN` | Target account-group registration token |
| `TE_TARGET_ACCOUNT_TOKEN_AID` | Required AID binding when the registration token comes from the environment |

If the target registration token is loaded from the environment, its AID binding must exactly match the selected target. A mismatch stops the run before any agent reset.

When the target account-group token is entered at the hidden prompt or loaded from the environment, the CLI removes whitespace and invisible Unicode format characters commonly introduced by copy/paste before validating the required 32-character alphanumeric value. It never prints or stores the token in reports.

## Run

Dry-run Strategy C:

```bash
python -m te_agent_migrate \
  --inventory inventory.csv \
  --strategy C \
  --stale remove \
  --parallel 3 \
  --create-missing-tags \
  --create-missing-alerts
```

Apply Strategy C:

```bash
python -m te_agent_migrate \
  --apply \
  --inventory inventory.csv \
  --strategy C \
  --stale remove \
  --parallel 3 \
  --timeout 180 \
  --create-missing-tags \
  --create-missing-alerts
```

PowerShell uses the same Python module and flags:

```powershell
python -m te_agent_migrate `
  --apply `
  --inventory inventory.csv `
  --strategy C `
  --stale remove `
  --parallel 3 `
  --timeout 180 `
  --create-missing-tags `
  --create-missing-alerts
```

## Reports

The default `reports` directory receives:

- One redacted JSON audit report, written incrementally and finalized at exit.
- One CSV directory containing summary, agent, test, unsupported-test, stale-entry, diff, decision, error, and step reports.

Excel output is intentionally replaced with CSV because OpenPyXL is not in the approved dependency list.

## Tests

Tests use only the standard-library `unittest` runner:

```bash
python -m unittest discover -s tests -v
```

No test contacts ThousandEyes or an Enterprise Agent unless a separate lab integration run is intentionally executed.
