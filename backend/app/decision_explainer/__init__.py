"""
REVIVE Decision Explainer

Provides grounded, human-readable explanations
for decisions produced by the existing Revive pipeline.
"""

from .explainer import (
    DecisionExplainer,
    build_case_evidence,
)

__all__ = [
    "DecisionExplainer",
    "build_case_evidence",
]