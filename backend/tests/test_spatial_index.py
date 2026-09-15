from productlens.contracts.models import Rect
from productlens.execution.spatial_index import SpatialIndex


def test_spatial_index_queries_intersections_without_duplicate_entries():
    index = SpatialIndex[str](cell_size=50)
    index.add("left", Rect(x=0, y=0, width=30, height=30))
    index.add("right", Rect(x=100, y=0, width=30, height=30))
    assert [entry.value for entry in index.query(Rect(x=10, y=10, width=60, height=60))] == ["left"]


def test_spatial_index_nearest_uses_stable_secondary_order():
    index = SpatialIndex[int](cell_size=100)
    index.add(2, Rect(x=0, y=0, width=20, height=20))
    index.add(1, Rect(x=0, y=0, width=20, height=20))
    chosen = index.nearest((10, 10), tie_break=lambda entry: entry.value)
    assert chosen is not None and chosen.value == 1
