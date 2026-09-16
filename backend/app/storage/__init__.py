"""Artifact storage boundary.

Production capture remains local while browser/Remotion processes are active.
After a stage completes, this boundary can publish immutable artifacts to an
object store without changing the evidence layout consumed by retries.
"""

from app.storage.local import LocalArtifactStorage
from app.storage.s3 import S3ArtifactStorage

__all__ = ["LocalArtifactStorage", "S3ArtifactStorage"]
