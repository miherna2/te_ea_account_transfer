#!/usr/bin/env sh
set -eu

exec .venv/bin/python -m te_agent_migrate "$@"
