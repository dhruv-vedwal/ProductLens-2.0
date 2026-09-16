"""LiveDiscovery service composed from capture, probe, and orchestration mixins."""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.contracts.models import (
    ActionCapability,
    CandidateDemoFlow,
    DiscoveryBudget,
    FeatureKnowledge,
    FormField,
    FormSchema,
    ObjectiveRelationship,
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductContext,
    ProductRelationship,
    Target,
)
from app.planning.capability_resolution import resolve_capabilities

from app.discovery.live.helpers import *  # noqa: F403
from app.discovery.live.page_capture import PageCaptureMixin
from app.discovery.live.probes import CapabilityProbeMixin
from app.discovery.live.orchestration import DiscoveryOrchestrationMixin

class LiveDiscovery(PageCaptureMixin, CapabilityProbeMixin, DiscoveryOrchestrationMixin):
    """Inspects only the current page and its visible navigation; it never crawls blindly."""
    pass

__all__ = [
    "LiveDiscovery",
]
