# Code Atelier Governance SDK — developer Makefile.
#
# This Makefile is intentionally thin. The canonical build tools are
# pytest / ruff / mypy / pip — targets here are shortcuts that encode
# conventions the contributing docs would otherwise ask you to memorise.

.PHONY: install-hooks

# install-hooks
# -------------
# DX Consultant mandate (F7): git hooks are worthless unless the
# developer actually runs them. Symlinking hooks into .git/hooks/ per
# file is fragile — core.hooksPath lets a single pointer cover the
# whole .githooks/ directory at once, and survives worktree switches.
#
# Run this once after cloning the repo:
#
#     make install-hooks
#
# That configures git to source hooks from .githooks/ (currently
# pre-commit-todo-gate.sh — blocks partial-state IOUs). Hook files are
# plain bash, executable, and documented inline.
install-hooks:
	git config core.hooksPath .githooks
	@echo "OK: git hooks installed from .githooks/"
