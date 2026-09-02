"""Reference-aware storage primitives for Clipper.

Phase 1 intentionally makes discovery and cleanup planning read-only.  The
only automatic deletion supported here is for a newly registered temporary
artifact after its validated successor and durable manifest are confirmed.
"""

from .models import CleanupClassification, LifecycleClass
from .registry import ArtifactRegistry

__all__ = ["ArtifactRegistry", "CleanupClassification", "LifecycleClass"]
