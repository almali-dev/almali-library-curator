"""Protection engine (S4): protected library, high rating, kept forever."""

from almali_library_curator.protection.engine import ProtectionEngine
from almali_library_curator.protection.models import (
    ProtectedCandidate,
    Protection,
    ProtectionKind,
    ProtectionOutcome,
)

__all__ = [
    "ProtectedCandidate",
    "Protection",
    "ProtectionEngine",
    "ProtectionKind",
    "ProtectionOutcome",
]
