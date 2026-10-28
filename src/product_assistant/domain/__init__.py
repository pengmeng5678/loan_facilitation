"""领域层对外出口。"""

from product_assistant.domain.matching import MatchingEngine, MatchingWeights
from product_assistant.domain.models import (
    Citation,
    CustomerProfile,
    MatchCandidate,
    MatchResult,
    Product,
    Proposal,
    RetrievedChunk,
)

__all__ = [
    "MatchingEngine",
    "MatchingWeights",
    "Citation",
    "CustomerProfile",
    "MatchCandidate",
    "MatchResult",
    "Product",
    "Proposal",
    "RetrievedChunk",
]
