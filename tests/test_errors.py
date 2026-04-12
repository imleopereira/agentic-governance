"""Tests for the GovernanceError base class and recovery_hint field."""
from __future__ import annotations

from codeatelier_governance.errors import GovernanceError


class TestGovernanceError:
    """Gap #8: GovernanceError base class with recovery_hint."""

    def test_message_stored(self) -> None:
        err = GovernanceError("something broke")
        assert str(err) == "something broke"

    def test_recovery_hint_default_empty(self) -> None:
        err = GovernanceError("oops")
        assert err.recovery_hint == ""

    def test_recovery_hint_accessible(self) -> None:
        err = GovernanceError(
            "budget exceeded",
            recovery_hint="Increase per_session_usd or start a new session.",
        )
        assert err.recovery_hint == "Increase per_session_usd or start a new session."

    def test_is_exception(self) -> None:
        err = GovernanceError("test")
        assert isinstance(err, Exception)

    def test_subclass_inherits_hint(self) -> None:
        """Subclasses (AuditError, CostError, etc.) inherit recovery_hint."""
        from codeatelier_governance.audit.errors import AuditError

        err = AuditError("chain broken", recovery_hint="Re-key the audit secret.")
        assert isinstance(err, GovernanceError)
        assert err.recovery_hint == "Re-key the audit secret."


# ---------------------------------------------------------------------------
# Error message leakage — no secrets in user-facing exceptions
# ---------------------------------------------------------------------------
class TestErrorLeakage:
    """Errors raised to callers must not contain DB URLs, passwords, or
    internal file paths. Required by cybersecurity test gap #6."""

    def test_budget_exceeded_no_db_url(self) -> None:
        """BudgetExceeded message must not contain a connection string."""
        from codeatelier_governance.cost.errors import BudgetExceeded

        err = BudgetExceeded(
            "Agent 'bot-1' exceeded per_session_usd cap: used $1.50, limit $1.00"
        )
        msg = str(err)
        assert "postgresql://" not in msg
        assert "password" not in msg.lower()

    def test_scope_violation_no_internal_paths(self) -> None:
        """ScopeViolation message must not contain internal file paths."""
        from codeatelier_governance.scope.errors import ScopeViolation

        err = ScopeViolation(
            "Agent 'bot-1' attempted tool 'delete_db' which is not whitelisted."
        )
        msg = str(err)
        assert "/Users/" not in msg
        assert "/src/" not in msg
        assert "/home/" not in msg

    def test_chain_integrity_error_no_db_url(self) -> None:
        """ChainIntegrityError must not leak connection strings."""
        from codeatelier_governance.audit.errors import ChainIntegrityError

        err = ChainIntegrityError(
            "audit chain integrity violation at event abc-123"
        )
        msg = str(err)
        assert "postgresql://" not in msg
        assert "password" not in msg.lower()
