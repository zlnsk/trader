"""Canary deployment: run a proposed config on a slot subset, compare."""
from .runner import (
    start_canary, evaluate_canary, slots_for_canary,
    CanaryVerdict, CanaryConfig,
    PASS, FAIL, RUNNING, ABORTED,
)
__all__ = ["start_canary", "evaluate_canary", "slots_for_canary",
            "CanaryVerdict", "CanaryConfig",
            "PASS", "FAIL", "RUNNING", "ABORTED"]
