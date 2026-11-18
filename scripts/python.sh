#!/usr/bin/env bash
# Use a working Python, skipping Windows Store execution aliases.
set -euo pipefail
if [[ -n "${DATACART_PYTHON:-}" ]]; then
    exec "$DATACART_PYTHON" "$@"
fi
for python_command in python3 python; do
    if command -v "$python_command" >/dev/null 2>&1 &&
        "$python_command" -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
        exec "$python_command" "$@"
    fi
done
if command -v py >/dev/null 2>&1 && py -3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
    exec py -3 "$@"
fi
echo "Python 3.12+ is required. Install it with PATH enabled, reopen Git Bash, or set DATACART_PYTHON." >&2
exit 1
