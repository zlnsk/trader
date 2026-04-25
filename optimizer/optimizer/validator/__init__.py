"""The adversary.

Tries to falsify every proposal. Only proposals that survive every
gate earn status='validated'. Any gate failing yields status='rejected'
with a machine-readable reason.
"""
from .adversary import (
    Verdict, Gate, validate_proposal, REJECT, PASS, MARGINAL,
)

__all__ = ["Verdict", "Gate", "validate_proposal", "REJECT", "PASS", "MARGINAL"]
