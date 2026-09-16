"""Small provider-neutral spatial index for observed UI geometry.

The index is intentionally independent of Playwright and browser coordinates.
It is used only to rank already-grounded candidates (never to invent a click
coordinate), which keeps duplicate responsive controls deterministic while
remaining useful for camera and cursor planning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import hypot
from typing import Generic, TypeVar

from app.contracts.models import Rect

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SpatialEntry(Generic[T]):
    value: T
    rect: Rect

    @property
    def center(self) -> tuple[float, float]:
        return (self.rect.x + self.rect.width / 2, self.rect.y + self.rect.height / 2)


class SpatialIndex(Generic[T]):
    """Uniform-grid index with deterministic nearest-candidate ranking.

    A uniform grid is preferable here to a heavyweight R-tree: candidate sets
    are small, the index is rebuilt from fresh DOM evidence for each action,
    and predictable ordering matters more than asymptotic tree maintenance.
    """

    def __init__(
        self, entries: Iterable[SpatialEntry[T]] = (), *, cell_size: float = 320.0
    ) -> None:
        if cell_size <= 0:
            raise ValueError("cell_size must be positive")
        self.cell_size = float(cell_size)
        self._buckets: dict[tuple[int, int], list[SpatialEntry[T]]] = {}
        self._entries: list[SpatialEntry[T]] = []
        for entry in entries:
            self.add(entry.value, entry.rect)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return (int(x // self.cell_size), int(y // self.cell_size))

    def add(self, value: T, rect: Rect) -> SpatialEntry[T]:
        entry = SpatialEntry(value=value, rect=rect)
        self._entries.append(entry)
        left, top = self._cell(rect.x, rect.y)
        right, bottom = self._cell(rect.x + max(0.0, rect.width), rect.y + max(0.0, rect.height))
        for row in range(top, bottom + 1):
            for column in range(left, right + 1):
                self._buckets.setdefault((column, row), []).append(entry)
        return entry

    def query(self, rect: Rect) -> list[SpatialEntry[T]]:
        """Return entries intersecting ``rect`` in insertion order."""
        left, top = self._cell(rect.x, rect.y)
        right, bottom = self._cell(rect.x + max(0.0, rect.width), rect.y + max(0.0, rect.height))
        seen: set[int] = set()
        result: list[SpatialEntry[T]] = []
        for row in range(top, bottom + 1):
            for column in range(left, right + 1):
                for entry in self._buckets.get((column, row), ()):
                    marker = id(entry)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    if _intersects(entry.rect, rect):
                        result.append(entry)
        return result

    def nearest(
        self,
        point: tuple[float, float],
        *,
        candidates: Iterable[SpatialEntry[T]] | None = None,
        tie_break: Callable[[SpatialEntry[T]], float] | None = None,
    ) -> SpatialEntry[T] | None:
        """Return the closest observed rectangle to a point.

        ``tie_break`` may return a deterministic secondary key (for example,
        the smallest y position for duplicated header/footer navigation).
        """
        values = list(candidates) if candidates is not None else self._entries
        if not values:
            return None
        px, py = point
        return min(
            values,
            key=lambda entry: (
                hypot(entry.center[0] - px, entry.center[1] - py),
                tie_break(entry) if tie_break else 0,
            ),
        )

    def __len__(self) -> int:
        return len(self._entries)


def _intersects(a: Rect, b: Rect) -> bool:
    return not (
        a.x + a.width < b.x or b.x + b.width < a.x or a.y + a.height < b.y or b.y + b.height < a.y
    )
