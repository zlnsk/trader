"""Apply + rollback lifecycle."""
from .apply import apply_canary_globally
from .rollback import check_and_maybe_rollback, rollback_global
__all__ = ["apply_canary_globally", "check_and_maybe_rollback", "rollback_global"]
