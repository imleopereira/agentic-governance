"""Scope enforcement exceptions.

Errors raised to callers never include the full policy object — policies may
reference internal tool names that shouldn't appear in agent-visible error
messages.
"""
from __future__ import annotations


class ScopeError(Exception):
    """Base class for all scope module errors."""


class ScopeViolation(ScopeError):
    """Raised when an agent attempts an action outside its registered scope."""


class PolicyNotRegistered(ScopeError):
    """Raised when checking scope for an agent with no registered policy."""
