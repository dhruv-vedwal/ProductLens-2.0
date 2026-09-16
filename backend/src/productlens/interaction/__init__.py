"""Evidence-driven interaction kernel for generic browser workflows."""

from productlens.interaction.director import CandidateScore, InteractionDirector
from productlens.interaction.kernel import InteractionKernel, InteractionPolicy

__all__ = ["CandidateScore", "InteractionDirector", "InteractionKernel", "InteractionPolicy"]
