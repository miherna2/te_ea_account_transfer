#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir/TE_AGENT_MIGRATE_MINIMAL_REQUIREMENTS"

# Use the same implementation and Python environment as the documented module command.
# In particular, do not let the legacy uv.lock select the retired src/ package.
if command -v python3 >/dev/null 2>&1; then
    exec python3 -m te_agent_migrate "$@"
fi
if command -v python >/dev/null 2>&1; then
    exec python -m te_agent_migrate "$@"
fi

printf '%s\n' 'Python 3.6 or newer is required. Install Python, then run this launcher again.' >&2
exit 2
