"""Versioned configuration store."""
from .versions import (
    ManagedKey, propose_version, activate_version, deactivate_version,
    active_global_version, resolved_for_slot, rollback_to,
    list_active_canaries, trace_lineage,
)

__all__ = [
    "ManagedKey", "propose_version", "activate_version",
    "deactivate_version", "active_global_version", "resolved_for_slot",
    "rollback_to", "list_active_canaries", "trace_lineage",
]
