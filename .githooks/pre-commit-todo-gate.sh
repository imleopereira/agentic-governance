#!/usr/bin/env bash
# F7 sub-item #3: pre-commit TODO gate.
#
# Blocks commits that introduce partial-state IOUs (TODO(later),
# TODO(v0.6.1), XXX, FIXME) without a version-tagged deferral.
#
# Allowed  : TODO(v0.6.1): wire up after F11 lands
# Allowed  : TODO(v1.0.0): replace with real impl
# BLOCKED  : TODO(later): fix this
# BLOCKED  : XXX broken
# BLOCKED  : FIXME
# BLOCKED  : TODO: no owner
#
# Usage as a git hook:
#   ln -s ../../.githooks/pre-commit-todo-gate.sh .git/hooks/pre-commit
# or simpler (preferred — see Makefile `install-hooks`):
#   git config core.hooksPath .githooks
#
# Usage ad-hoc (for testing):
#   bash .githooks/pre-commit-todo-gate.sh <file> [<file>...]
#
# Exit codes:
#   0  no forbidden TODOs found
#   1  forbidden TODOs found (commit blocked)

set -euo pipefail

# Patterns considered partial-state IOUs.
# NB: TODO(vN.M.P): is explicitly allowed because version-tagged TODOs
# have an explicit owner and a known landing window.
FORBIDDEN_REGEX='(TODO\(later\)|TODO\(v[0-9]+\.[0-9]+\.[0-9]+[^)]*\)(?!:)|XXX[^A-Za-z0-9]|FIXME|TODO:[^(])'
ALLOWED_REGEX='TODO\(v[0-9]+\.[0-9]+\.[0-9]+\):'

# Collect files to scan. If args were passed (ad-hoc test mode), scan
# those. Otherwise scan the staged diff.
files=()
if [[ $# -gt 0 ]]; then
  files=("$@")
else
  # Only scan staged, added/modified text files.
  while IFS= read -r f; do
    [[ -n "$f" ]] && files+=("$f")
  done < <(git diff --cached --name-only --diff-filter=AM 2>/dev/null || true)
fi

if [[ ${#files[@]} -eq 0 ]]; then
  exit 0
fi

violations=0
for f in "${files[@]}"; do
  [[ -f "$f" ]] || continue
  # Skip binary files.
  if file --mime "$f" 2>/dev/null | grep -q 'charset=binary'; then
    continue
  fi
  # Skip this hook file itself (documents the patterns).
  case "$f" in
    *.githooks/pre-commit-todo-gate.sh) continue ;;
  esac

  # grep -n for line numbers. -E extended regex. -w where reasonable.
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    # Allow version-tagged TODOs: if the line matches ALLOWED_REGEX
    # and does NOT contain any other forbidden token, skip it.
    if echo "$line" | grep -Eq "$ALLOWED_REGEX"; then
      # Strip the allowed token and re-check for other violations.
      stripped=$(echo "$line" | sed -E "s/TODO\(v[0-9]+\.[0-9]+\.[0-9]+\):[^\"']*//g")
      if ! echo "$stripped" | grep -Eq '(TODO\(later\)|XXX[^A-Za-z0-9]|FIXME|TODO:[^(])'; then
        continue
      fi
    fi
    violations=$((violations + 1))
    echo "  $f:$line" >&2
  done < <(grep -nE '(TODO\(later\)|TODO\([^v)][^)]*\)|XXX[^A-Za-z0-9]|FIXME|TODO:[^(])' "$f" 2>/dev/null || true)
done

if [[ $violations -gt 0 ]]; then
  echo "" >&2
  echo "pre-commit-todo-gate: $violations partial-state TODO(s) found." >&2
  echo "" >&2
  echo "Allowed form : TODO(v0.6.1): description" >&2
  echo "Forbidden    : TODO(later), XXX, FIXME, bare TODO:" >&2
  echo "" >&2
  echo "Version-tagged TODOs have explicit owners and landing windows." >&2
  echo "Everything else rots." >&2
  exit 1
fi

exit 0
