# Repository publication policy

This is the user's publication rule for this repository. Read it before staging,
committing, pushing, or changing packaging and ignore rules.

- GitHub contains the runtime application and end-user documentation only.
- Permitted documentation is the root README, the minimal distribution's README,
  and `TE_AGENT_MIGRATE_MINIMAL_REQUIREMENTS/docs/OPERATIONS.md` (the user runbook).
  This file records the publication policy so agents can find it in a fresh clone.
- Architecture, design notes, issue/fix registers, development and testing
  configuration, test suites, sandbox helpers, lab setup, scenarios, inventories,
  and execution evidence stay local. Preserve these files on disk; do not stage,
  commit, push, or include them in release archives.
- In particular, keep `pyproject.local.toml`, `src/`, `tests/`, `uv.lock`, the
  minimal distribution's `dev/`, and every file in its `docs/` except the user
  runbook local. Its sandbox and qualification JSON examples are also local.
- Never publish credentials, `.env` files, customer inventories, `work/`, discovery
  snapshots, generated reports, downloaded libraries, or virtual environments.
- Public guides describe installation, usage, limitations, recovery, and expected
  user-visible behavior. Keep internal architecture, incident evidence, test
  results, and lab instructions out of those guides, commit messages, and PRs.
  Do not link public guides to local-only documents.
- Select permitted files explicitly. Review the entire staged file list and diff
  before committing, and check the source-distribution allowlist in
  `pyproject.toml`. Do not use broad staging or force-add excluded files.
- `.gitignore` does not remove previously tracked files. If excluded material is
  already tracked, remove it from the index while preserving the local copy.
  Do not rewrite remote history without explicit authorization.

The shared `.gitignore` files and package allowlist enforce this boundary in new
clones. Changes to them must preserve the rule above.
