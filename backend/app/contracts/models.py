"""Backward-compatible re-export of all contract models.

Prefer importing from ``app.contracts`` directly. This module exists so that
``from app.contracts.models import X`` continues to work everywhere.
"""

from app.contracts import *  # noqa: F403
from app.contracts import __all__ as __all__  # noqa: F401
