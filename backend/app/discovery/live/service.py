"""LiveDiscovery service composed from capture, probe, and orchestration mixins."""

from __future__ import annotations

from app.discovery.live.helpers import *
from app.discovery.live.orchestration import DiscoveryOrchestrationMixin
from app.discovery.live.page_capture import PageCaptureMixin
from app.discovery.live.probes import CapabilityProbeMixin


class LiveDiscovery(PageCaptureMixin, CapabilityProbeMixin, DiscoveryOrchestrationMixin):
    """Inspects only the current page and its visible navigation; it never crawls blindly."""

__all__ = [
    "LiveDiscovery",
]
