#!/usr/bin/env sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

python - <<'PY'
import ssl
import sys

print("Python runtime: %s" % sys.version.split()[0])
print("SSL runtime: %s" % getattr(ssl, "OPENSSL_VERSION", "unknown"))
if not getattr(ssl, "HAS_TLSv1_3", False):
    print(
        "WARNING: This Python SSL runtime does not report TLS 1.3 support. "
        "Agents that require TLS 1.3 will reject UI connections. Recreate "
        "the environment with a TLS 1.3-capable Python interpreter."
    )
PY

printf '\nEnvironment ready. Activate it with:\n  . .venv/bin/activate\n'
