from __future__ import annotations

from dataclasses import dataclass

from productlens.contracts.models import FailureCode


@dataclass
class RecoveryBudget:
    max_regrounds: int = 1
    used_regrounds: int = 0

    def allow_reground(self, code: FailureCode) -> bool:
        if (
            code is not FailureCode.TARGET_RESOLUTION_FAILURE
            or self.used_regrounds >= self.max_regrounds
        ):
            return False
        self.used_regrounds += 1
        return True
