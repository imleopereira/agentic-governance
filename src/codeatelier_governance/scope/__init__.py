"""Action scope enforcement.

Public API:
    ScopePolicy           — declarative policy: allowed_tools + allowed_apis
    ScopeModule           — exposed via sdk.scope
    ScopeViolation        — raised when an action is outside the scope
    PolicyNotRegistered   — raised when no policy exists for an agent
"""
from .errors import PolicyNotRegistered, ScopeError, ScopeViolation
from .models import ScopePolicy
from .module import ScopeModule

__all__ = [
    "PolicyNotRegistered",
    "ScopeError",
    "ScopeModule",
    "ScopePolicy",
    "ScopeViolation",
]
