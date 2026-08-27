#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/Library/Caches/te-ea-account-transfer/.venv}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/Library/Caches/uv}"

timestamp=$(date '+%H:%M:%S')
printf '%s INFO [launcher] using local environment %s\n' "$timestamp" "$UV_PROJECT_ENVIRONMENT"
printf '%s INFO [launcher] starting te-agent-migrate\n' "$timestamp"

exec uv run --project "$project_dir" te-agent-migrate "$@"
