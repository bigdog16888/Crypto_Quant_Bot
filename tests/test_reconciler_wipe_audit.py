"""
Regression test for audit_bot_wipes signature fix.

Verifies that the public audit_bot_wipes() wrapper executes without TypeError
and returns a valid WipeAuditResult.
"""
import pytest
from engine.reconciler_wipe_audit import audit_bot_wipes, WipeAuditResult


class TestAuditBotWipesSignature:
    """Test that public audit_bot_wipes() wrapper works correctly."""

    def test_public_audit_bot_wipes_executes_without_type_error(self):
        """
        Call public audit_bot_wipes() with real bot_id and symbol.
        Should not raise TypeError (was: 3 args passed to 4-arg function).
        Should return a WipeAuditResult instance.
        """
        # Bot 10018 = SUI long (exists in DB, inactive but present)
        # SUI/USDC:USDC = normalized pair format
        result = audit_bot_wipes(
            bot_id=10018,
            symbol="SUI/USDC:USDC",
            exchange_gap=0.0
        )

        # Basic contract: returns WipeAuditResult with expected fields
        assert isinstance(result, WipeAuditResult)
        assert result.bot_id == 10018
        assert result.symbol == "SUI/USDC:USDC"
        assert isinstance(result.suspect_rows, list)
        assert isinstance(result.total_suspect_qty, (int, float))
        assert isinstance(result.probable_cause_match, bool)

    def test_audit_bot_wipes_with_nonzero_gap(self):
        """Test with a non-zero exchange_gap to exercise probable_cause logic."""
        result = audit_bot_wipes(
            bot_id=10018,
            symbol="SUI/USDC:USDC",
            exchange_gap=1.5
        )

        assert isinstance(result, WipeAuditResult)
        assert result.bot_id == 10018
        assert result.symbol == "SUI/USDC:USDC"
        # probable_cause_match should be False since no suspect rows exist
        assert result.probable_cause_match is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])