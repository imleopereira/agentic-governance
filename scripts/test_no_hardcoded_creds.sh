#!/usr/bin/env bash
# Thin wrapper around scripts/test_no_hardcoded_creds.py for pre-push hooks.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${DIR}/test_no_hardcoded_creds.py" "$@"
