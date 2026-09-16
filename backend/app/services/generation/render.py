from __future__ import annotations

from pathlib import Path

from app.services.render_stage import render_run


# Defined here (not in service.py) so stage mixins can import it without cycles.
class GenerationPreconditionError(RuntimeError):
    pass


class RenderMixin:
    def render_stage(self, *, run_id: str, artifact_root: Path) -> Path:
        return render_run(run_id=run_id, artifact_root=artifact_root)
